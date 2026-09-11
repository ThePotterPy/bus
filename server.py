"""
Servidor local para el tracker de colectivos JAHA.

Sirve la pagina web (index.html) y actua de proxy hacia la API real de
JAHA (https://www.jaha.com.py/rest_backend), que no permite CORS, por lo
que el navegador no puede llamarla directamente desde una pagina web.

Ademas maneja las notificaciones Web Push de proximidad: guarda una
suscripcion por dispositivo (linea + radio + ultima ubicacion) y un hilo
de fondo revisa periodicamente si algun bus de esa linea entro en el
radio configurado.

Uso:
    python server.py
    (abre http://localhost:8787 en el navegador)

Las notificaciones de proximidad requieren la libreria pywebpush:
    pip install pywebpush
Si no esta instalada, el resto de la app (mapa, buscador, favoritos)
funciona igual; solo se desactiva la funcion de "avisarme".
"""

import json
import math
import os
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Railway (y otros hosts similares) asignan el puerto via la variable de
# entorno PORT; en local usamos 8787 si no esta definida.
PORT = int(os.environ.get("PORT", 8787))
API_BASE = "https://www.jaha.com.py/rest_backend"
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SUBS_FILE = DATA_DIR / "notify_subscriptions.json"
VAPID_FILE = DATA_DIR / "vapid_private.pem"

POSITIONS_CACHE_TTL = 8  # segundos - compartido entre el proxy de la UI y el chequeo de proximidad
NOTIFY_CHECK_INTERVAL = 20  # segundos entre chequeos de proximidad
VAPID_CLAIMS_SUB = "mailto:notificaciones@example.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

try:
    from py_vapid import Vapid
    from py_vapid.utils import b64urlencode
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from pywebpush import webpush, WebPushException

    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False


def call_api(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes]:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 502, json.dumps({"success": False, "error": str(e.reason)}).encode()


# ---------------------------------------------------------------------------
# Cache corta de posiciones por linea. La usan tanto /api/positions (para la
# UI) como el chequeo de proximidad de fondo, asi si hay varios usuarios
# mirando o vigilando la misma linea no se golpea la API de JAHA una vez por
# usuario sino una vez cada POSITIONS_CACHE_TTL segundos.
# ---------------------------------------------------------------------------
_positions_cache: dict[str, tuple[float, int, bytes]] = {}
_positions_cache_lock = threading.Lock()


def get_positions_cached(line: str) -> tuple[int, bytes]:
    now = time.time()
    with _positions_cache_lock:
        cached = _positions_cache.get(line)
        if cached and now - cached[0] < POSITIONS_CACHE_TTL:
            return cached[1], cached[2]
    status, body = call_api("/bus/positions", method="POST", body={"line": line})
    with _positions_cache_lock:
        _positions_cache[line] = (now, status, body)
    return status, body


# ---------------------------------------------------------------------------
# VAPID (identidad del servidor para Web Push) - se genera una sola vez y se
# guarda en disco para que las suscripciones existentes sigan siendo validas
# entre reinicios del servidor.
# ---------------------------------------------------------------------------
_vapid = None
_vapid_public_b64 = None
if PUSH_AVAILABLE:
    DATA_DIR.mkdir(exist_ok=True)
    _vapid_env_pem = os.environ.get("VAPID_PRIVATE_KEY_PEM")
    if _vapid_env_pem:
        # En hosts con filesystem efimero (Railway, etc.) cada redeploy
        # borraria el .pem local y generaria una clave nueva, invalidando
        # todas las suscripciones existentes. Si esta variable esta seteada,
        # se usa esa clave fija en vez de la del archivo.
        _vapid_env_pem = _vapid_env_pem.replace("\\n", "\n")
        if "BEGIN PRIVATE KEY" not in _vapid_env_pem:
            _vapid_env_pem = f"-----BEGIN PRIVATE KEY-----\n{_vapid_env_pem.strip()}\n-----END PRIVATE KEY-----"
        _vapid = Vapid.from_pem(_vapid_env_pem.encode("utf-8"))
    else:
        _vapid = Vapid.from_file(str(VAPID_FILE))
    _raw_pub = _vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    _vapid_public_b64 = b64urlencode(_raw_pub)


# ---------------------------------------------------------------------------
# Suscripciones de notificacion por dispositivo (clientId generado en el
# navegador). Persistidas en un JSON simple: no hace falta una base de datos
# para este volumen de datos.
# ---------------------------------------------------------------------------
_subs_lock = threading.Lock()
_subs: dict[str, dict] = {}


def load_subs():
    global _subs
    if SUBS_FILE.is_file():
        try:
            _subs = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _subs = {}


def save_subs():
    DATA_DIR.mkdir(exist_ok=True)
    with _subs_lock:
        snapshot = json.dumps(_subs)
    SUBS_FILE.write_text(snapshot, encoding="utf-8")


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def send_push(push_subscription: dict, title: str, body_text: str) -> str | None:
    """Devuelve 'expired' si la suscripcion ya no es valida y hay que borrarla."""
    try:
        webpush(
            subscription_info=push_subscription,
            data=json.dumps({"title": title, "body": body_text}),
            vapid_private_key=_vapid,
            vapid_claims={"sub": VAPID_CLAIMS_SUB},
        )
    except WebPushException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        print("push error:", exc)
        if status_code in (404, 410):
            return "expired"
    except Exception as exc:
        print("push error inesperado:", exc)
    return None


def notifier_loop():
    """Cada NOTIFY_CHECK_INTERVAL segundos revisa, por linea vigilada, si
    algun bus entro en el radio configurado por cada suscriptor. Aplica
    histeresis (solo avisa en la transicion fuera->dentro) para no mandar
    una notificacion por cada actualizacion de posicion."""
    while True:
        time.sleep(NOTIFY_CHECK_INTERVAL)
        if not PUSH_AVAILABLE:
            continue

        with _subs_lock:
            snapshot = list(_subs.items())

        by_line: dict[str, list[tuple[str, dict]]] = {}
        for client_id, sub in snapshot:
            if not sub.get("line") or sub.get("lat") is None or sub.get("lon") is None:
                continue
            by_line.setdefault(sub["line"], []).append((client_id, sub))

        expired_ids = []
        changed = False

        for line, entries in by_line.items():
            _status, body = get_positions_cached(line)
            try:
                data = json.loads(body)
            except Exception:
                continue
            if not data.get("success"):
                continue
            units = data.get("data") or []

            for client_id, sub in entries:
                min_dist = None
                for u in units:
                    try:
                        ulat, ulon = float(u["lat"]), float(u["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    d = haversine_km(sub["lat"], sub["lon"], ulat, ulon)
                    if min_dist is None or d < min_dist:
                        min_dist = d

                radius = sub.get("radiusKm", 5)
                inside = min_dist is not None and min_dist <= radius
                was_inside = sub.get("insideRadius", False)

                if inside and not was_inside:
                    line_name = sub.get("lineName") or f"Linea {line}"
                    result = send_push(
                        sub["pushSubscription"],
                        "\U0001f68c Bus cerca",
                        f"{line_name} esta a unos {min_dist:.1f} km de tu ubicacion.",
                    )
                    if result == "expired":
                        expired_ids.append(client_id)
                        continue

                with _subs_lock:
                    if client_id in _subs and _subs[client_id].get("insideRadius") != inside:
                        _subs[client_id]["insideRadius"] = inside
                        changed = True

        if expired_ids:
            with _subs_lock:
                for cid in expired_ids:
                    _subs.pop(cid, None)
            changed = True

        if changed:
            save_subs()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia el log por defecto, ya lo hacemos mas abajo

    def _send_json(self, status: int, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json_obj(self, status: int, obj: dict):
        self._send_json(status, json.dumps(obj).encode("utf-8"))

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _send_file(self, rel_path: str):
        file_path = STATIC_DIR / rel_path
        if not file_path.is_file():
            self.send_error(404, "Not found")
            return
        content_type = "text/html; charset=utf-8"
        if rel_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif rel_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        print(f"GET {parsed.path} {qs}")

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("index.html")
            return

        if parsed.path == "/api/lines":
            status, body = call_api("/bus/lines")
            self._send_json(status, body)
            return

        if parsed.path == "/api/positions":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json(400, b'{"success": false, "error": "falta line"}')
                return
            status, body = get_positions_cached(line)
            self._send_json(status, body)
            return

        if parsed.path == "/api/path":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json(400, b'{"success": false, "error": "falta line"}')
                return
            status, body = call_api(f"/bus/lineServices/{line}")
            self._send_json(status, body)
            return

        if parsed.path == "/api/notify/vapid-public-key":
            self._send_json_obj(200, {"available": PUSH_AVAILABLE, "publicKey": _vapid_public_b64})
            return

        # cualquier otro archivo estatico (por si se agregan mas, ej. sw.js)
        self._send_file(parsed.path.lstrip("/"))

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self._read_json_body()
        print(f"POST {parsed.path}")

        if parsed.path == "/api/notify/subscribe":
            if not PUSH_AVAILABLE:
                self._send_json_obj(503, {"success": False, "error": "pywebpush no esta instalado en el servidor"})
                return
            if not body or not body.get("clientId") or not body.get("pushSubscription") or not body.get("line"):
                self._send_json_obj(400, {"success": False, "error": "faltan datos (clientId, pushSubscription, line)"})
                return
            client_id = str(body["clientId"])
            with _subs_lock:
                _subs[client_id] = {
                    "line": str(body["line"]),
                    "lineName": body.get("lineName"),
                    "radiusKm": float(body.get("radiusKm", 5)),
                    "pushSubscription": body["pushSubscription"],
                    "lat": body.get("lat"),
                    "lon": body.get("lon"),
                    "insideRadius": False,
                    "updatedAt": time.time(),
                }
            save_subs()
            self._send_json_obj(200, {"success": True})
            return

        if parsed.path == "/api/notify/location":
            if not body or not body.get("clientId"):
                self._send_json_obj(400, {"success": False, "error": "falta clientId"})
                return
            client_id = str(body["clientId"])
            with _subs_lock:
                if client_id in _subs:
                    _subs[client_id]["lat"] = body.get("lat")
                    _subs[client_id]["lon"] = body.get("lon")
                    _subs[client_id]["updatedAt"] = time.time()
                    found = True
                else:
                    found = False
            if found:
                save_subs()
            self._send_json_obj(200 if found else 404, {"success": found})
            return

        if parsed.path == "/api/notify/unsubscribe":
            if not body or not body.get("clientId"):
                self._send_json_obj(400, {"success": False, "error": "falta clientId"})
                return
            client_id = str(body["clientId"])
            with _subs_lock:
                _subs.pop(client_id, None)
            save_subs()
            self._send_json_obj(200, {"success": True})
            return

        self._send_json_obj(404, {"success": False, "error": "not found"})


def main():
    load_subs()
    if PUSH_AVAILABLE:
        threading.Thread(target=notifier_loop, daemon=True).start()
    else:
        print("Aviso: pywebpush no esta instalado - las notificaciones de proximidad quedan desactivadas.")
        print("Para activarlas: pip install pywebpush")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Servidor corriendo en http://localhost:{PORT}")
    print("Presiona Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo servidor...")
        server.shutdown()


if __name__ == "__main__":
    main()
