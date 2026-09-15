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

import gzip
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from observed_routes import ObservedRoutes

# Railway (y otros hosts similares) asignan el puerto via la variable de
# entorno PORT; en local usamos 8787 si no esta definida.
PORT = int(os.environ.get("PORT", 8787))
API_BASE = "https://www.jaha.com.py/rest_backend"
MAS_API_BASE = "https://geomastarjeta.z1.mastarjeta.net"
MAS_AUTH_TOKEN = "4d30d2b7cf01ac8cd46fec1da00686f112cb63a5"
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR / "data")
SUBS_FILE = DATA_DIR / "notify_subscriptions.json"
VAPID_FILE = DATA_DIR / "vapid_private.pem"
TRACKS_DB_FILE = DATA_DIR / "observed_bus_tracks.sqlite3"
observed_routes = ObservedRoutes(
    TRACKS_DB_FILE,
    osrm_url=os.environ.get("OSRM_MATCH_URL", "https://router.project-osrm.org"),
    poll_seconds=int(os.environ.get("OBSERVED_POLL_SECONDS", "30")),
)
_line_fetch_locks = {}
_line_fetch_locks_guard = threading.Lock()

POSITIONS_CACHE_TTL = 8  # segundos - compartido entre el proxy de la UI y el chequeo de proximidad
NOTIFY_CHECK_INTERVAL = 20  # segundos entre chequeos de proximidad
VAPID_CLAIMS_SUB = "mailto:notificaciones@example.com"
ACTIVE_TAIL_WINDOW_SECONDS = 5 * 60
# Las muestras GPS no contienen la geometría de las calles entre dos puntos.
# Si la separación es demasiado grande, unirlas dibuja una diagonal ficticia
# que puede atravesar manzanas o edificios.
MAX_OBSERVED_TRACK_GAP_SECONDS = 75
MAX_OBSERVED_TRACK_STEP_KM = 0.35

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

MAS_HEADERS = {
    "User-Agent": "Dart/3.0 (dart:io)",
    "Authorization": f"Token {MAS_AUTH_TOKEN}",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
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


def call_mas_api(path: str, timeout: int = 12) -> tuple[int, any]:
    url = f"{MAS_API_BASE}{path}" if path.startswith("/") else f"{MAS_API_BASE}/{path}"
    req = urllib.request.Request(url, headers=MAS_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return resp.status, json.loads(data.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()
            if err_body[:2] == b"\x1f\x8b":
                err_body = gzip.decompress(err_body)
            return e.code, json.loads(err_body.decode("utf-8", errors="replace"))
        except Exception:
            return e.code, None
    except Exception as e:
        return 502, None


# ---------------------------------------------------------------------------
# Caché de empresas y líneas de Más Tarjeta (TTL de 30 minutos)
# ---------------------------------------------------------------------------
_mas_lines_cache: list[dict] = []
_mas_lines_cache_time: float = 0
_mas_lines_lock = threading.Lock()


def get_mas_lines() -> list[dict]:
    global _mas_lines_cache, _mas_lines_cache_time
    now = time.time()
    with _mas_lines_lock:
        if _mas_lines_cache and (now - _mas_lines_cache_time < 1800):
            return _mas_lines_cache

    status, empresas = call_mas_api("/api/empresas/v2", timeout=12)
    if status != 200 or not isinstance(empresas, list):
        return _mas_lines_cache

    lines = []
    seen_ids = set()
    for emp in empresas:
        linea_obj = emp.get("linea") or {}
        lid = linea_obj.get("id")
        if lid is None or lid in seen_ids:
            continue
        seen_ids.add(lid)

        nro_str = str(linea_obj.get("nro_linea") or emp.get("nro_linea") or "").strip()
        nombre_std = (emp.get("nombre_std") or emp.get("nombre") or "").strip()
        nombre_alias = (emp.get("nombre") or emp.get("nombre_std") or "").strip()

        if nro_str.upper().startswith("LINEA"):
            line_name = nro_str.upper()
        elif nro_str and nro_str != "0":
            line_name = f"LINEA {nro_str}"
        else:
            line_name = nombre_std or nombre_alias or f"LÍNEA {lid}"

        rgb = emp.get("color")
        color_hex = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" if (isinstance(rgb, list) and len(rgb) >= 3) else "#10b981"

        lines.append({
            "id": f"mas_{lid}",
            "name": line_name,
            "company_id": emp.get("id"),
            "company": {
                "id": emp.get("id"),
                "name": nombre_std,
                "alias": nombre_alias,
            },
            "provider": "mas",
            "color": color_hex,
            "marcador": emp.get("marcador")
        })

    with _mas_lines_lock:
        _mas_lines_cache = lines
        _mas_lines_cache_time = now
    return lines


def catalog_identity(value) -> str:
    """Normaliza nombres del catálogo para comparar JAHA con Más."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def mas_line_mirrors_jaha(line: dict, jaha_names: set[str]) -> bool:
    """Detecta las fichas de Más que solamente replican una línea de JAHA.

    En esas fichas, Más usa como nombre o alias de empresa el nombre oficial
    de la línea JAHA (por ejemplo, ``LINEA 30`` o ``LINEA 5 CH``). Se exige esa
    coincidencia de empresa para no eliminar por accidente dos líneas propias
    de operadores diferentes que compartan el mismo número.
    """
    company = line.get("company") or {}
    company_labels = (company.get("name"), company.get("alias"))
    return any(
        identity and identity in jaha_names
        for identity in (catalog_identity(label) for label in company_labels)
    )


def get_all_lines_combined() -> list[dict]:
    jaha_lines = []
    status, body = call_api("/bus/lines")
    if status == 200:
        try:
            parsed = json.loads(body)
            if parsed.get("success"):
                for item in parsed.get("data", []):
                    item["provider"] = "jaha"
                    jaha_lines.append(item)
        except Exception:
            pass

    mas_lines = get_mas_lines()

    # Más también publica copias de varias líneas metropolitanas de JAHA.
    # Cuando JAHA está disponible conservamos su ficha, que contiene más
    # información, y dejamos en Más solamente sus servicios propios. Si JAHA
    # falla, no filtramos nada y las copias de Más sirven como respaldo.
    if jaha_lines:
        jaha_names = {
            catalog_identity(line.get("name"))
            for line in jaha_lines
            if catalog_identity(line.get("name"))
        }
        mas_lines = [
            line for line in mas_lines
            if not mas_line_mirrors_jaha(line, jaha_names)
        ]

    return jaha_lines + mas_lines


# ---------------------------------------------------------------------------
# Caché de trazados (rutas/ramales) de Más Tarjeta (TTL de 15 minutos)
# ---------------------------------------------------------------------------
_mas_rutas_cache: dict[str, tuple[float, list[dict]]] = {}
_mas_rutas_lock = threading.Lock()


def get_mas_routes_for_line(mas_line_id: str) -> list[dict]:
    now = time.time()
    with _mas_rutas_lock:
        cached = _mas_rutas_cache.get(mas_line_id)
        if cached and (now - cached[0] < 900):
            return cached[1]

    status, rutas = call_mas_api(f"/api/rutas/por_linea/?linea_id={mas_line_id}", timeout=10)
    if status == 200 and isinstance(rutas, list):
        with _mas_rutas_lock:
            _mas_rutas_cache[mas_line_id] = (now, rutas)
        return rutas
    return []


def get_mas_path(line_str: str) -> tuple[int, dict]:
    mas_line_id = line_str.replace("mas_", "")
    rutas = get_mas_routes_for_line(mas_line_id)
    if not rutas:
        return 404, {"success": False, "error": "No se encontraron recorridos para esta línea de Más Tarjeta"}

    services = []
    palette = ["#10b981", "#06b6d4", "#f59e0b", "#ec4899", "#8b5cf6", "#3b82f6"]
    for idx, r in enumerate(rutas):
        camino = r.get("camino") or {}
        coords = camino.get("coordinates") or []
        if not coords:
            continue
        traces = [
            {"order": i + 1, "latitud": pt[1], "longitud": pt[0]}
            for i, pt in enumerate(coords)
        ]
        color = r.get("color") or palette[idx % len(palette)]
        services.append({
            "service_id": r.get("id_ruta"),
            "name": r.get("nombre") or f"Ramal {idx + 1}",
            "routes": [
                {
                    "route_id": r.get("id_ruta"),
                    "name": r.get("nombre") or f"Ramal {idx + 1}",
                    "color": color,
                    "sections": [
                        {
                            "order": 1,
                            "traces": traces
                        }
                    ]
                }
            ]
        })

    return 200, {"success": True, "data": {"services": services}}


def get_mas_positions_cached(line_str: str) -> tuple[int, bytes]:
    with _positions_cache_lock:
        now = time.time()
        cached = _positions_cache.get(line_str)
        if cached and now - cached[0] < POSITIONS_CACHE_TTL:
            return cached[1], cached[2]

    mas_line_id = line_str.replace("mas_", "")
    rutas = get_mas_routes_for_line(mas_line_id)
    if not rutas:
        body = json.dumps({"success": True, "data": []}).encode("utf-8")
        with _positions_cache_lock:
            _positions_cache[line_str] = (time.time(), 200, body)
        return 200, body

    route_ids = [r.get("id_ruta") for r in rutas if r.get("id_ruta") is not None]
    all_raw_buses = []

    any_success = False
    with ThreadPoolExecutor(max_workers=min(8, len(route_ids) or 1)) as executor:
        futures = {
            executor.submit(call_mas_api, f"/api/buses/posiciones_bus_v2/?id_ruta={rid}", 5): rid
            for rid in route_ids
        }
        for future in as_completed(futures):
            try:
                status, b_list = future.result()
                if status == 200:
                    any_success = True
                    if isinstance(b_list, list):
                        all_raw_buses.extend(b_list)
            except Exception:
                pass

    if not any_success:
        with _positions_cache_lock:
            cached = _positions_cache.get(line_str)
            if cached:
                return cached[1], cached[2]

    seen_units = set()
    normalized_units = []
    
    current_time_str = datetime.now().isoformat()
    
    for u in all_raw_buses:
        unit_num = str(u.get("nro_coche") or u.get("id") or "")
        if not unit_num or unit_num in seen_units:
            continue
        seen_units.add(unit_num)

        ubicacion = u.get("ubicacion") or {}
        coords = ubicacion.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]

        speed = u.get("velocidad") or 0
        itin = u.get("itinerario") or {}
        route_name = itin.get("nombre") or ""
        modified = u.get("modified") or ""
        
        # Considerar en movimiento si tiene velocidad o si se actualizó muy recientemente
        is_moving = speed > 0
        if not is_moving and modified:
            try:
                # modified es algo como "2023-10-25T14:30:00Z"
                mod_time = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
                if time.time() - mod_time < 120:
                    is_moving = True
            except:
                is_moving = True # Ante la duda, mostrar en movimiento para que no queden todos rojos
        elif not is_moving:
            is_moving = True

        time_str = modified[11:19] if len(modified) >= 19 else ""
        bearing = u.get("sentido") if u.get("sentido") is not None else u.get("rotacion_marker")

        normalized_units.append({
            "unit": unit_num,
            "lat": float(lat),
            "lon": float(lon),
            "status": "EN MOVIMIENTO" if is_moving else "DETENIDO",
            "route": route_name,
            "observed_at": modified,
            "time": time_str,
            "air": True,
            "speed": speed,
            "bearing": bearing,
            "provider": "mas"
        })

    body = json.dumps({"success": True, "data": normalized_units}).encode("utf-8")
    record_bus_observations(line_str, body)

    with _positions_cache_lock:
        _positions_cache[line_str] = (time.time(), 200, body)
    return 200, body


# ---------------------------------------------------------------------------
# Cache corta de posiciones por linea. La usan tanto /api/positions (para la
# UI) como el chequeo de proximidad de fondo, asi si hay varios usuarios
# mirando o vigilando la misma linea no se golpea la API de JAHA una vez por
# usuario sino una vez cada POSITIONS_CACHE_TTL segundos.
# ---------------------------------------------------------------------------
_positions_cache: dict[str, tuple[float, int, bytes]] = {}
_positions_cache_lock = threading.Lock()
_tracks_lock = threading.Lock()


def ensure_sqlite_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    """Agrega una columna sin invalidar las bases creadas por versiones anteriores."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_tracks_db():
    """Crea un historial local, compartido por todos los usuarios del sitio.

    Solo se guardan posiciones de unidades de transporte publicas, nunca la
    ubicacion de quien consulta el mapa. El historial se usa para detectar
    cambios de recorrido y se depura automaticamente.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(TRACKS_DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bus_observations (
                line_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bus_observations_line_time
            ON bus_observations(line_id, observed_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_deviations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_id TEXT NOT NULL,
                points_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapped_deviations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_id TEXT NOT NULL,
                points_json TEXT NOT NULL,
                last_seen INTEGER NOT NULL
            )
        """)
        ensure_sqlite_column(conn, "bus_observations", "route_name", "TEXT NOT NULL DEFAULT ''")
        ensure_sqlite_column(conn, "raw_deviations", "route_name", "TEXT NOT NULL DEFAULT ''")
        ensure_sqlite_column(conn, "snapped_deviations", "route_name", "TEXT NOT NULL DEFAULT ''")


def record_bus_observations(line: str, body: bytes):
    """Entrega una respuesta normalizada al registro central compartido."""
    try:
        response = json.loads(body)
        if not response.get("success"):
            return
        units = response.get("data") or []
    except (ValueError, TypeError):
        return

    observed_routes.observe(str(line), units)


def observed_tracks(line: str, days: int) -> list[list[list[float]]]:
    """Devuelve trazas separadas por viaje para que el navegador filtre los
    tramos fuera de la ruta oficial que ya tiene cargada."""
    cutoff = int(time.time() - days * 86400)
    with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
        rows = conn.execute(
            """SELECT unit_id, observed_at, lat, lon, route_name FROM bus_observations
               WHERE line_id = ? AND observed_at >= ?
               ORDER BY unit_id, observed_at""",
            (str(line), cutoff),
        ).fetchall()

    tracks: list[list[list[float]]] = []
    current: list[list[float]] = []
    previous_unit = previous_at = previous_lat = previous_lon = None
    for unit_id, observed_at, lat, lon, route_name in rows:
        split = (
            previous_unit != unit_id
            or previous_at is None
            or observed_at - previous_at > MAX_OBSERVED_TRACK_GAP_SECONDS
            or haversine_km(previous_lat, previous_lon, lat, lon) > MAX_OBSERVED_TRACK_STEP_KM
        )
        if split:
            if len(current) >= 3:
                tracks.append(current)
            current = []
        current.append([lat, lon, route_name or ""])
        previous_unit, previous_at, previous_lat, previous_lon = unit_id, observed_at, lat, lon
    if len(current) >= 3:
        tracks.append(current)
    return tracks

def active_tails(line: str) -> dict[str, list[list[float]]]:
    """Devuelve las últimas muestras de unidades que siguen activas.

    La clasificación de "fuera de ruta" depende del trazado oficial que el
    navegador ya descargó. Por eso aquí conservamos el tramo reciente completo
    (hasta 40 muestras); el cliente se queda únicamente con el último segmento
    continuo que está fuera de la ruta. La base SQLite hace que sobreviva a un
    reinicio del proceso, siempre que ``data/`` esté en almacenamiento persistente.
    """
    cutoff = int(time.time() - ACTIVE_TAIL_WINDOW_SECONDS)
    with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
        rows = conn.execute(
            """SELECT unit_id, observed_at, lat, lon, route_name FROM bus_observations
               WHERE line_id = ?
                 AND unit_id IN (
                    SELECT unit_id FROM bus_observations
                    WHERE line_id = ? AND observed_at >= ?
                 )
               ORDER BY unit_id, observed_at DESC""",
            (str(line), str(line), cutoff),
        ).fetchall()

    tails: dict[str, list[list[float]]] = {}
    for unit_id, _observed_at, lat, lon, route_name in rows:
        # La consulta viene en orden descendente: no necesitamos traer una
        # hora completa para luego recortarla en memoria.
        if len(tails.setdefault(unit_id, [])) < 40:
            tails[unit_id].append([lat, lon, route_name or ""])

    # Leaflet espera los puntos en orden cronológico.
    return {unit_id: list(reversed(points)) for unit_id, points in tails.items()}

def get_positions_cached(line: str) -> tuple[int, bytes]:
    # One upstream request per line even if the collector and many clients
    # hit an expired cache simultaneously.
    with _line_fetch_locks_guard:
        lock = _line_fetch_locks.setdefault(str(line), threading.Lock())
    with lock:
        return _get_positions_cached(str(line))


def _get_positions_cached(line: str) -> tuple[int, bytes]:
    if str(line).startswith("mas_"):
        return get_mas_positions_cached(line)

    with _positions_cache_lock:
        now = time.time()
        cached = _positions_cache.get(line)
        if cached and now - cached[0] < POSITIONS_CACHE_TTL:
            return cached[1], cached[2]

    # No mantenemos el candado mientras esperamos la red: el modo "todas"
    # puede consultar varias líneas en paralelo sin bloquear a quienes miran
    # una línea puntual.
    status, body = call_api("/bus/positions", method="POST", body={"line": line})
    if status == 200:
        record_bus_observations(line, body)
    with _positions_cache_lock:
        _positions_cache[line] = (time.time(), status, body)
    return status, body


def get_positions_batch(lines: list[str]) -> dict[str, list[dict]]:
    """Consulta varias líneas con un máximo pequeño de solicitudes simultáneas.

    Mantiene una respuesta por línea, por lo que los identificadores de unidad
    nunca se mezclan entre recorridos. La caché corta existente se reutiliza.
    """
    unique_lines = list(dict.fromkeys(str(line) for line in lines if str(line)))[:150]
    results: dict[str, list[dict]] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(get_positions_cached, line): line for line in unique_lines}
        for future in as_completed(futures):
            line = futures[future]
            try:
                status, body = future.result()
                response = json.loads(body)
                if status == 200 and response.get("success"):
                    results[line] = response.get("data") or []
                else:
                    results[line] = []
            except (ValueError, TypeError, OSError):
                results[line] = []
    return results


# ---------------------------------------------------------------------------
# Caché completa de trazados de líneas para el Planificador de Viajes.
# Se llena asíncronamente para no bloquear el inicio.
# ---------------------------------------------------------------------------
_all_paths_cache = {}
_all_paths_lock = threading.Lock()
_all_lines_info = []

def path_fetcher_loop():
    global _all_lines_info
    time.sleep(2)  # Dar tiempo al server de iniciar
    status, body = call_api("/bus/lines")
    if status == 200:
        try:
            data = json.loads(body)
            if data.get("success"):
                _all_lines_info = data.get("data", [])
        except:
            pass

    for line in _all_lines_info:
        line_id = str(line.get("id"))
        status, body = call_api(f"/bus/lineServices/{line_id}")
        if status == 200:
            try:
                data = json.loads(body)
                if data.get("success"):
                    routes_list = []
                    for service in data.get("data", {}).get("services", []):
                        for route in service.get("routes", []):
                            for section in route.get("sections", []):
                                points = []
                                for tr in section.get("traces", []):
                                    try:
                                        points.append((float(tr["latitud"]), float(tr["longitud"])))
                                    except:
                                        pass
                                if points:
                                    routes_list.append(points)
                    with _all_paths_lock:
                        _all_paths_cache[line_id] = routes_list
            except:
                pass
        time.sleep(1) # rate limiting simple


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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # Cliente cerro conexion antes de terminar

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
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # Cliente cerro conexion antes de terminar

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        print(f"GET {parsed.path} {qs}")

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("index.html")
            return

        if parsed.path == "/api/lines":
            all_lines = get_all_lines_combined()
            self._send_json_obj(200, {"success": True, "data": all_lines})
            return

        if parsed.path == "/api/observed-routes":
            line = qs.get("line", [""])[0]
            if not line or len(line) > 80:
                self._send_json_obj(400, {"success": False, "error": "falta line"})
                return
            self._send_json_obj(200, observed_routes.snapshot(line, qs.get("history") == ["1"]))
            return

        if parsed.path == "/api/observed-health":
            self._send_json_obj(200, {
                "success": True,
                "collector_enabled": os.environ.get("OBSERVED_COLLECTOR", "1") != "0",
                "last_cycle": observed_routes.last_cycle,
                "matching_enabled": bool(observed_routes.osrm_url),
                "railway_volume_configured": bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")),
            })
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
            if str(line).startswith("mas_"):
                status, data = get_mas_path(line)
                self._send_json_obj(status, data)
                return
            status, body = call_api(f"/bus/lineServices/{line}")
            self._send_json(status, body)
            return

        if parsed.path == "/api/observed-tracks":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json_obj(400, {"success": False, "error": "falta line"})
                return
            try:
                days = max(1, min(21, int(qs.get("days", [14])[0])))
            except ValueError:
                days = 14
            self._send_json_obj(200, {"success": True, "days": days, "tracks": observed_tracks(line, days)})
            return

        if parsed.path == "/api/active-tails":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json_obj(400, {"success": False, "error": "falta line"})
                return
            self._send_json_obj(200, {"success": True, "tails": active_tails(line)})
            return

        if parsed.path == "/api/deviations":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json_obj(400, {"success": False, "error": "falta line"})
                return
            with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
                rows = conn.execute(
                    "SELECT points_json, route_name FROM snapped_deviations WHERE line_id = ?",
                    (str(line),),
                ).fetchall()
            deviations = []
            for points_json, route_name in rows:
                try:
                    deviations.append({"points": json.loads(points_json), "route": route_name or ""})
                except:
                    pass
            self._send_json_obj(200, {"success": True, "deviations": deviations})
            return

        if parsed.path == "/api/planificar":
            try:
                olat = float(qs.get("olat", [0])[0])
                olon = float(qs.get("olon", [0])[0])
                dlat = float(qs.get("dlat", [0])[0])
                dlon = float(qs.get("dlon", [0])[0])
            except ValueError:
                self._send_json(400, b'{"success": false, "error": "coordenadas invalidas"}')
                return

            best_options = []

            with _all_paths_lock:
                for line_id, routes in _all_paths_cache.items():
                    if not routes: continue
                    
                    best_walk_for_line = float('inf')
                    
                    for points in routes:
                        min_dist_o, best_idx_o = float('inf'), -1
                        min_dist_d, best_idx_d = float('inf'), -1
                        
                        for i, p in enumerate(points):
                            d_o = haversine_km(olat, olon, p[0], p[1])
                            if d_o < min_dist_o:
                                min_dist_o = d_o
                                best_idx_o = i
                            
                            d_d = haversine_km(dlat, dlon, p[0], p[1])
                            if d_d < min_dist_d:
                                min_dist_d = d_d
                                best_idx_d = i
                        
                        # Umbral de 1.5km, y debe ir en sentido correcto
                        if min_dist_o < 1.5 and min_dist_d < 1.5 and best_idx_d > best_idx_o:
                            total_walk = min_dist_o + min_dist_d
                            if total_walk < best_walk_for_line:
                                best_walk_for_line = total_walk
                    
                    if best_walk_for_line < float('inf'):
                        line_name = next((l["name"] for l in _all_lines_info if str(l["id"]) == line_id), f"Linea {line_id}")
                        best_options.append({
                            "id": line_id,
                            "name": line_name,
                            "walkingDist": int(best_walk_for_line * 1000)
                        })

            # Ordenar opciones por menor distancia a pie
            best_options.sort(key=lambda x: x["walkingDist"])
            top_3 = best_options[:3]

            res = {"success": True, "data": top_3}
                
            self._send_json_obj(200, res)
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

        if parsed.path == "/api/positions/batch":
            lines = body.get("lines") if isinstance(body, dict) else None
            if not isinstance(lines, list):
                self._send_json_obj(400, {"success": False, "error": "falta lines"})
                return
            self._send_json_obj(200, {"success": True, "lines": get_positions_batch(lines)})
            return

        if parsed.path == "/api/observed-routes/batch":
            lines = body.get("lines") if isinstance(body, dict) else None
            if not isinstance(lines, list) or len(lines) > 25 or any(not isinstance(x, str) or len(x)>80 for x in lines):
                self._send_json_obj(400, {"success": False, "error": "se requieren hasta 25 lineas"})
                return
            self._send_json_obj(200, {"success": True, "lines": {
                line: observed_routes.snapshot(line, body.get("history") is True) for line in set(lines)
            }})
            return

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

        if parsed.path == "/api/report-deviation":
            # Legacy browsers cannot create evidence or alter the shared counts.
            self._send_json_obj(410, {"success": False, "error": "Las observaciones se registran en el servidor"})
            return

        self._send_json_obj(404, {"success": False, "error": "not found"})

def osrm_processor_loop():
    while True:
        try:
            with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
                now = int(time.time())
                conn.execute("DELETE FROM snapped_deviations WHERE last_seen < ?", (now - 7 * 86400,))
                rows = conn.execute("SELECT id, line_id, points_json, route_name FROM raw_deviations").fetchall()
            
            for row in rows:
                raw_id, line_id, points_json, route_name = row
                try:
                    points = json.loads(points_json)
                    if len(points) > 99:
                        step = len(points) / 99
                        points = [points[int(i * step)] for i in range(99)]
                    
                    coords_str = ";".join(f"{p[1]},{p[0]}" for p in points)
                    url = f"http://router.project-osrm.org/match/v1/driving/{coords_str}?geometries=geojson&overview=full"
                    
                    req = urllib.request.Request(url, headers={"User-Agent": "BusTrackerBot/1.0"}, method="GET")
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        res = json.loads(resp.read())
                        if res.get("code") == "Ok" and res.get("matchings"):
                            geometry = res["matchings"][0].get("geometry", {})
                            if geometry.get("type") == "LineString":
                                coords = geometry.get("coordinates", [])
                                snapped_points = [[c[1], c[0]] for c in coords]
                                snapped_json = json.dumps(snapped_points)
                                
                                with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
                                    existing = conn.execute(
                                        "SELECT id, points_json FROM snapped_deviations WHERE line_id = ? AND route_name = ?",
                                        (line_id, route_name or ""),
                                    ).fetchall()
                                    is_dup = False
                                    for ex_id, ex_json in existing:
                                        try:
                                            ex_pts = json.loads(ex_json)
                                            if len(ex_pts) > 0 and len(snapped_points) > 0:
                                                d_start = haversine_km(ex_pts[0][0], ex_pts[0][1], snapped_points[0][0], snapped_points[0][1])
                                                d_end = haversine_km(ex_pts[-1][0], ex_pts[-1][1], snapped_points[-1][0], snapped_points[-1][1])
                                                if d_start < 0.2 and d_end < 0.2:
                                                    conn.execute("UPDATE snapped_deviations SET last_seen = ? WHERE id = ?", (int(time.time()), ex_id))
                                                    is_dup = True
                                                    break
                                        except:
                                            pass
                                    if not is_dup:
                                        conn.execute(
                                            "INSERT INTO snapped_deviations (line_id, points_json, last_seen, route_name) VALUES (?, ?, ?, ?)",
                                            (line_id, snapped_json, int(time.time()), route_name or ""),
                                        )
                except Exception as e:
                    print(f"Error matching OSRM for raw_id {raw_id}: {e}")
                
                with _tracks_lock, sqlite3.connect(TRACKS_DB_FILE) as conn:
                    conn.execute("DELETE FROM raw_deviations WHERE id = ?", (raw_id,))
                    
        except Exception as e:
            print(f"Error in osrm_processor_loop: {e}")
            
        time.sleep(4 * 3600)  # every 4 hours

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_tracks_db()
    observed_routes.init()
    load_subs()
    
    # Iniciar fetcher en background para el planificador
    threading.Thread(target=path_fetcher_loop, daemon=True).start()
    # The old queue remains on disk as legacy evidence; it has no reliable
    # unit identities and must not be counted as confirmed bus passages.
    def official_path(line):
        if line.startswith("mas_"):
            # Empty successful Más catalogs are distinct from network failures.
            status, raw = call_mas_api(f"/api/rutas/por_linea/?linea_id={line[4:]}", timeout=10)
            if status != 200 or not isinstance(raw, list): return None
            if not raw: return {"services": []}
            with _mas_rutas_lock:
                _mas_rutas_cache[line[4:]] = (time.time(), raw)
            status, data = get_mas_path(line)
        else:
            status, body = call_api(f"/bus/lineServices/{line}")
            data = json.loads(body)
        return data.get("data") if status == 200 and data.get("success") else None

    if os.environ.get("OBSERVED_COLLECTOR", "1") != "0":
        threading.Thread(target=observed_routes.collector,
                         args=(get_all_lines_combined, get_positions_cached, official_path), daemon=True).start()
    threading.Thread(target=observed_routes.matcher_loop, daemon=True).start()
    if os.environ.get("RAILWAY_ENVIRONMENT_ID") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        print("ATENCION: Railway no tiene un volumen montado; el historial no sobrevivira a un despliegue.")
    if not observed_routes.osrm_url:
        print("OSRM_MATCH_URL no configurado: las estelas se conservan como puntos GPS pendientes de ajuste.")

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
        observed_routes.stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
