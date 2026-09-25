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
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, quote
from email import policy
from email.parser import BytesParser
from feedback import FeedbackStore, SESSION_SECONDS, validate_feedback, validate_news
from observed_routes import ObservedRoutes
from shared_trips import SharedTripStore
from stop_reports import StopReportStore, in_paraguay, haversine_meters

# Railway (y otros hosts similares) asignan el puerto via la variable de
# entorno PORT; en local usamos 8787 si no esta definida.
PORT = int(os.environ.get("PORT", 8787))
API_BASE = "https://www.jaha.com.py/rest_backend"
MAS_API_BASE = "https://geomastarjeta.z1.mastarjeta.net"
MAS_AUTH_TOKEN = os.environ.get("MAS_AUTH_TOKEN", "").strip()
BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR / "data")
SUBS_FILE = DATA_DIR / "notify_subscriptions.json"
VAPID_FILE = DATA_DIR / "vapid_private.pem"
TRACKS_DB_FILE = DATA_DIR / "observed_bus_tracks.sqlite3"
FEEDBACK_DB_FILE = DATA_DIR / "feedback.sqlite3"
SHARED_TRIPS_DB_FILE = DATA_DIR / "shared_trips.sqlite3"
COMMUNITY_STOPS_FILE = DATA_DIR / "community_stops.json"
STOP_PHOTOS_DIR = DATA_DIR / "stop_photos"
STOP_REPORTS_DB_FILE = DATA_DIR / "stop_reports.sqlite3"
PLANNER_ROUTES_CACHE_FILE = DATA_DIR / "planner_routes_cache.json"

feedback_store = FeedbackStore(FEEDBACK_DB_FILE)
shared_trip_store = SharedTripStore(SHARED_TRIPS_DB_FILE)
stop_report_store = StopReportStore(STOP_REPORTS_DB_FILE, STOP_PHOTOS_DIR, COMMUNITY_STOPS_FILE)
stop_report_store.init()

def _load_municipal_json(filename: str) -> list[dict]:
    for directory in (DATA_DIR, BASE_DIR / "data"):
        path = directory / filename
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error cargando {path}: {e}")
    return []

_asuncion_stops: list[dict] = _load_municipal_json("asuncion_stops.json")
_asuncion_traffic_lights: list[dict] = _load_municipal_json("asuncion_traffic_lights.json")
_asuncion_pois: list[dict] = _load_municipal_json("asuncion_pois.json")


def get_all_stops_combined() -> list[dict]:
    """Combina paradas oficiales municipales con paradas comunitarias aprobadas."""
    official = _asuncion_stops or []
    community = []
    if COMMUNITY_STOPS_FILE.exists():
        try:
            with open(COMMUNITY_STOPS_FILE, "r", encoding="utf-8") as f:
                community = json.load(f)
        except Exception:
            pass
    seen = set()
    res = []
    for s in official:
        sid = s.get("id")
        if sid:
            seen.add(sid)
        res.append(s)
    for s in community:
        sid = s.get("id")
        if sid and sid not in seen:
            seen.add(sid)
            res.append(s)
    return res


def _feedback_admin_password() -> str:
    return os.environ.get("FEEDBACK_ADMIN_PASSWORD", "")


def feedback_enabled() -> bool:
    return len(_feedback_admin_password()) >= 16


_tomtom_api_key = os.environ.get("TOMTOM_API_KEY", "").strip()
_match_provider = os.environ.get(
    "MATCH_PROVIDER", "tomtom" if _tomtom_api_key else "osrm"
).strip().lower()
_match_shadow_default = "1" if _match_provider == "tomtom" else "0"
observed_routes = ObservedRoutes(
    TRACKS_DB_FILE,
    osrm_url=os.environ.get(
        "OSRM_MATCH_URL", "https://router.project-osrm.org" if _match_provider == "osrm" else ""
    ),
    poll_seconds=int(os.environ.get("OBSERVED_POLL_SECONDS", "30")),
    match_provider=_match_provider,
    tomtom_api_key=_tomtom_api_key,
    shadow_mode=os.environ.get("MATCH_SHADOW_MODE", _match_shadow_default) != "0",
    raw_retention_days=int(os.environ.get("OBSERVED_RAW_RETENTION_DAYS", "7")),
    max_pending_jobs=int(os.environ.get("MATCH_MAX_PENDING_JOBS", "5000")),
    max_job_attempts=int(os.environ.get("MATCH_MAX_ATTEMPTS", "8")),
    monthly_match_limit=int(os.environ.get("TOMTOM_MONTHLY_LIMIT", "2200")),
    weekly_match_limit=int(os.environ.get("TOMTOM_WEEKLY_LIMIT", "400")),
    match_weekday=int(os.environ.get("TOMTOM_WEEKDAY", "6")),
    match_hour=int(os.environ.get("TOMTOM_HOUR", "3")),
    match_run_on_start=os.environ.get("MATCH_RUN_ON_START", "0") == "1",
    min_confirmed_buses=int(os.environ.get("OBSERVED_MIN_CONFIRMED_BUSES", "2")),
    min_confirmed_passes=int(os.environ.get("OBSERVED_MIN_CONFIRMED_PASSES", "2")),
    tomtom_min_interval=float(os.environ.get("TOMTOM_MIN_INTERVAL_SECONDS", "1")),
)
# Un conjunto fijo de candados evita que identificadores arbitrarios enviados
# por Internet hagan crecer un diccionario para siempre.
_line_fetch_locks = [threading.Lock() for _ in range(64)]
_batch_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="positions")

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_BATCH_LINES = 120
LINE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CATALOG_CACHE_TTL = 300

POSITIONS_CACHE_TTL = 8  # segundos - compartido entre el proxy de la UI y el chequeo de proximidad
NOTIFY_CHECK_INTERVAL = 20  # segundos entre chequeos de proximidad
SUBSCRIPTION_TTL_SECONDS = 30 * 86400
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
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}
if MAS_AUTH_TOKEN:
    MAS_HEADERS["Authorization"] = f"Token {MAS_AUTH_TOKEN}"

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


_combined_lines_cache: list[dict] = []
_combined_lines_cache_time = 0.0
_combined_lines_cache_lock = threading.Lock()
_combined_lines_refresh_lock = threading.Lock()


def get_all_lines_combined() -> list[dict]:
    """Devuelve el catálogo unificado sin consultar proveedores por usuario.

    El catálogo cambia poco. Una caché compartida reduce demoras y evita que
    una ráfaga de visitantes se convierta en la misma ráfaga contra JAHA.
    Ante una caída temporal conservamos la última copia completa conocida.
    """
    global _combined_lines_cache, _combined_lines_cache_time
    now = time.time()
    with _combined_lines_cache_lock:
        if _combined_lines_cache and now - _combined_lines_cache_time < CATALOG_CACHE_TTL:
            return list(_combined_lines_cache)

    with _combined_lines_refresh_lock:
        now = time.time()
        with _combined_lines_cache_lock:
            if _combined_lines_cache and now - _combined_lines_cache_time < CATALOG_CACHE_TTL:
                return list(_combined_lines_cache)
            previous = list(_combined_lines_cache)

        combined, jaha_available = _fetch_all_lines_combined()
        # Si JAHA falla, no reemplazamos un catálogo completo por uno parcial.
        if previous and not jaha_available:
            return previous
        if combined:
            with _combined_lines_cache_lock:
                _combined_lines_cache = list(combined)
                _combined_lines_cache_time = now
        return combined or previous


def _fetch_all_lines_combined() -> tuple[list[dict], bool]:
    jaha_lines = []
    jaha_available = False
    status, body = call_api("/bus/lines")
    if status == 200:
        try:
            parsed = json.loads(body)
            if parsed.get("success"):
                jaha_available = True
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

    return jaha_lines + mas_lines, jaha_available


def known_line_ids() -> set[str]:
    return {str(line.get("id")) for line in get_all_lines_combined() if line.get("id") is not None}


def valid_line_id(value) -> bool:
    return isinstance(value, str) and bool(LINE_ID_RE.fullmatch(value))


def valid_coordinates(lat, lon) -> bool:
    try:
        lat_value, lon_value = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat_value) and math.isfinite(lon_value) and -90 <= lat_value <= 90 and -180 <= lon_value <= 180


def valid_client_id(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def official_stop(stop_id) -> dict | None:
    if not isinstance(stop_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", stop_id):
        return None
    return next((stop for stop in get_all_stops_combined() if stop.get("id") == stop_id), None)


def public_stop(stop_id) -> dict | None:
    stop = official_stop(stop_id)
    if not stop:
        return None
    return {key: stop.get(key) for key in ("id", "name", "type", "lat", "lon")}


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


def current_bus(line_id: str, unit_id: str) -> dict | None:
    """Return one public vehicle position; passenger data never enters this response."""
    if not valid_line_id(line_id) or not isinstance(unit_id, str) or not 1 <= len(unit_id) <= 80:
        return None
    status, payload = get_positions_cached(line_id)
    if status != 200:
        return None
    try:
        units = json.loads(payload).get("data") or []
    except (TypeError, ValueError):
        return None
    return next((unit for unit in units if str(unit.get("unit")) == unit_id), None)


def shared_trip_payload(public_token: str) -> dict | None:
    trip = shared_trip_store.get(public_token)
    if not trip:
        return None
    result = {
        "status": trip["status"],
        "intent": trip["intent"],
        "lineId": trip.get("line_id"),
        "unitId": trip.get("unit_id"),
        "startedAt": trip["started_at"],
        "expiresAt": trip["expires_at"],
        "maxExpiresAt": trip["max_expires_at"],
        "verification": trip.get("verification"),
        "lastVerifiedAt": trip.get("last_verified_at"),
        "endReason": trip.get("end_reason") or None,
        "extended": bool(trip.get("extended")),
        "stop": public_stop(trip.get("stop_id")),
        "destination": public_stop(trip.get("destination_stop_id")),
        "bus": None,
    }
    if trip.get("line_id") and trip.get("unit_id") and trip["status"] != "ended":
        bus = current_bus(trip["line_id"], trip["unit_id"])
        if bus:
            result["bus"] = {key: bus.get(key) for key in (
                "unit", "lat", "lon", "status", "route", "time", "speed", "bearing", "provider"
            )}
            destination = result["destination"]
            if destination and valid_coordinates(bus.get("lat"), bus.get("lon")):
                result["destinationDistanceMeters"] = round(haversine_km(
                    float(bus["lat"]), float(bus["lon"]),
                    float(destination["lat"]), float(destination["lon"]),
                ) * 1000)
    return result


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
    lock = _line_fetch_locks[hash(str(line)) % len(_line_fetch_locks)]
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
    unique_lines = list(dict.fromkeys(str(line) for line in lines if str(line)))[:MAX_BATCH_LINES]
    results: dict[str, list[dict]] = {}

    futures = {_batch_executor.submit(get_positions_cached, line): line for line in unique_lines}
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
# Catálogo geográfico del planificador.
#
# Se prepara en segundo plano y se comparte entre todos los visitantes. Cada
# entrada representa un recorrido completo (no una sección suelta), con su
# ramal, sentido y longitud acumulada. De esa manera una consulta del usuario
# solo hace cálculos locales y no provoca una ráfaga contra JAHA o Más.
# ---------------------------------------------------------------------------
PLANNER_REFRESH_SECONDS = max(300, int(os.environ.get("PLANNER_REFRESH_SECONDS", "1800")))
PLANNER_MAX_WALK_KM = 1.5
PLANNER_RESULT_LIMIT = 5
PLANNER_WALKING_SPEED_M_PER_MIN = 75.0
PLANNER_BUS_SPEED_M_PER_MIN = 330.0

# Optional self-hosted or contracted Nominatim-compatible search endpoint.
# Do not silently rely on the restricted public OSM geocoder.
GEOCODER_SEARCH_URL = os.environ.get("GEOCODER_SEARCH_URL", "").strip()
_geocode_lock = threading.Lock()
_geocode_cache = {}
_geocode_last_request = 0.0
_volume_stats_lock = threading.Lock()
_volume_stats_cache = (0.0, {})


def volume_stats():
    """A cached estimate of app data against the Railway volume allowance."""
    global _volume_stats_cache
    with _volume_stats_lock:
        if time.monotonic() - _volume_stats_cache[0] < 60:
            return _volume_stats_cache[1]
        used = 0
        sqlite_files = []
        for directory, _, names in os.walk(DATA_DIR):
            for name in names:
                try:
                    path = Path(directory) / name
                    used += path.stat().st_size
                    if name.endswith('.sqlite3'):
                        sqlite_files.append(path)
                except OSError:
                    pass
        # Free SQLite pages still occupy the volume. Report their size so an
        # operator can plan a backed-up compaction, never run one in a request.
        reclaimable = 0
        for path in sqlite_files:
            try:
                with sqlite3.connect(path, timeout=2) as db:
                    free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
                    page_size = db.execute('PRAGMA page_size').fetchone()[0]
                    reclaimable += free_pages * page_size
            except (OSError, sqlite3.Error):
                pass
        try:
            budget = max(1, int(os.environ.get("VOLUME_BUDGET_BYTES", "5000000000")))
        except ValueError:
            budget = 5_000_000_000
        stats = {"used_bytes": used, "budget_bytes": budget,
                 "used_percent": round(100 * used / budget, 1),
                 "remaining_estimate_bytes": max(0, budget - used),
                 "sqlite_reclaimable_estimate_bytes": reclaimable}
        _volume_stats_cache = (time.monotonic(), stats)
        return stats


def search_address(query):
    global _geocode_last_request
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 200:
        return 400, {"success": False, "error": "Escribí entre 3 y 200 caracteres."}
    use_tomtom = not GEOCODER_SEARCH_URL and bool(_tomtom_api_key)
    endpoint = urlparse(GEOCODER_SEARCH_URL)
    if not use_tomtom and (endpoint.scheme != "https" or not endpoint.hostname):
        return 503, {"success": False, "error": "La búsqueda de direcciones no está configurada. Usá tu ubicación o elegí el punto en el mapa."}
    query = query.strip()
    with _geocode_lock:
        cached = _geocode_cache.get(query.casefold())
        if cached and time.monotonic() - cached[0] < 86400:
            return 200, {"success": True, "data": cached[1]}
        now = time.monotonic()
        if now - _geocode_last_request < 1.1:
            return 429, {"success": False, "error": "Esperá un momento antes de buscar de nuevo."}
        _geocode_last_request = now
        if use_tomtom:
            url = ("https://api.tomtom.com/search/2/geocode/" + quote(query, safe="") +
                   ".json?" + urlencode({"key": _tomtom_api_key, "countrySet": "PY", "limit": 5}))
        else:
            separator = "&" if endpoint.query else "?"
            url = GEOCODER_SEARCH_URL + separator + urlencode({"format": "json", "countrycodes": "py", "limit": 5, "q": query})
        request = urllib.request.Request(url, headers={"User-Agent": "ColectivosRoutePlanner/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("oversized geocoder response")
            items = json.loads(raw)
            if use_tomtom:
                items = items.get("results") if isinstance(items, dict) else None
            if not isinstance(items, list):
                raise ValueError("invalid geocoder response")
            results = []
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                try:
                    position = (item.get("position") or {}) if use_tomtom else item
                    lat, lon = float(position["lat"]), float(position["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if valid_coordinates(lat, lon) and (not use_tomtom or
                    (item.get("address") or {}).get("countryCode", "PY") == "PY"):
                    label = ((item.get("address") or {}).get("freeformAddress") if use_tomtom
                             else item.get("display_name"))
                    results.append({"lat": lat, "lon": lon, "display_name": str(label or "Punto del mapa")[:500]})
        except (OSError, ValueError):
            return 502, {"success": False, "error": "No se pudo buscar la dirección. Podés elegir el punto en el mapa."}
        if len(_geocode_cache) >= 256:
            _geocode_cache.pop(next(iter(_geocode_cache)))
        _geocode_cache[query.casefold()] = (time.monotonic(), results)
        return 200, {"success": True, "data": results}

_all_paths_cache: dict[str, list[dict]] = {}
_all_paths_lock = threading.Lock()
_all_lines_info: list[dict] = []
_planner_last_refresh = 0.0


def _load_planner_routes_from_disk():
    global _all_paths_cache
    for path in (PLANNER_ROUTES_CACHE_FILE, BASE_DIR / "data" / "planner_routes_cache.json"):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded = {}
                for k, rlist in data.items():
                    loaded[k] = []
                    for r in rlist:
                        rc = dict(r)
                        rc["points"] = [tuple(p) for p in rc.get("points", [])]
                        rc["bbox"] = tuple(rc.get("bbox", (-90, -180, 90, 180)))
                        rc["gaps"] = set(rc.get("gaps", ()))
                        loaded[k].append(rc)
                with _all_paths_lock:
                    _all_paths_cache.clear()
                    _all_paths_cache.update(loaded)
                print(f"Rutas de colectivos cargadas desde disco: {len(loaded)} líneas")
                return
            except Exception as e:
                print(f"Error cargando planner_routes_cache: {e}")


def _save_planner_routes_to_disk():
    try:
        data = {}
        with _all_paths_lock:
            for k, rlist in _all_paths_cache.items():
                data[k] = []
                for r in rlist:
                    rc = dict(r)
                    rc["gaps"] = list(rc.get("gaps", ()))
                    data[k].append(rc)
        with open(PLANNER_ROUTES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Error guardando planner_routes_cache: {e}")


_load_planner_routes_from_disk()


def planner_route_direction(value) -> str | None:
    text = str(value or "").lower()
    vuelta = bool(re.search(r"\((?:v|vuelta)\)|(?:^|[\s_-])vuelta(?:$|[\s_-])", text))
    ida = bool(re.search(r"\((?:i|ida)\)|(?:^|[\s_-])ida(?:$|[\s_-])", text))
    return None if ida == vuelta else ("Ida" if ida else "Vuelta")


def _planner_cumulative(points: list[tuple[float, float]]) -> list[float]:
    cumulative = [0.0]
    for previous, current in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + haversine_km(*previous, *current) * 1000)
    return cumulative


def planner_order(item):
    try:
        value = float(item.get('order') or 0)
        return value if math.isfinite(value) else 0
    except (TypeError, ValueError):
        return 0


def extract_planner_routes(line: dict, data: dict | None) -> list[dict]:
    """Normaliza la respuesta de cualquiera de los proveedores.

    Las secciones ordenadas de una ruta se unen. Antes se almacenaban como
    rutas independientes y un origen en la primera sección no podía alcanzar
    un destino que estuviera en la segunda.
    """
    routes = []
    if not isinstance(data, dict):
        return routes

    for service in data.get("services") or []:
        for route in service.get("routes") or []:
            points: list[tuple[float, float]] = []
            gaps = set()
            sections = sorted(route.get("sections") or [], key=planner_order)
            for section in sections:
                section_start = True
                invalid_previous = False
                traces = sorted(section.get("traces") or [], key=planner_order)
                for trace in traces:
                    try:
                        point = (float(trace["latitud"]), float(trace["longitud"]))
                    except (KeyError, TypeError, ValueError):
                        invalid_previous = True
                        continue
                    if not valid_coordinates(*point):
                        invalid_previous = True
                        continue
                    # Los proveedores suelen repetir el extremo entre dos
                    # secciones. Evitarlo conserva limpio el largo acumulado.
                    if not points or point != points[-1]:
                        if points:
                            step = haversine_km(*points[-1], *point) * 1000
                            if invalid_previous or step > 2000 or (section_start and step > 75):
                                gaps.add(len(points) - 1)
                        points.append(point)
                    section_start = False
                    invalid_previous = False

            if len(points) < 2:
                continue
            route_name = str(route.get("name") or service.get("name") or "Principal").strip()
            cumulative = _planner_cumulative(points)
            routes.append({
                "line_id": str(line.get("id")),
                "line_name": str(line.get("name") or f"Línea {line.get('id')}"),
                "provider": str(line.get("provider") or "jaha"),
                "service_id": service.get("service_id"),
                "route_id": route.get("route_id"),
                "route_name": route_name,
                "direction": planner_route_direction(route_name),
                "points": points,
                "cumulative": cumulative,
                "length_m": cumulative[-1],
                # Nearby terminals do not prove that passengers can stay on
                # board for another circuit. Never infer that connection.
                "closed": False,
                "gaps": gaps,
                "bbox": (
                    min(point[0] for point in points), min(point[1] for point in points),
                    max(point[0] for point in points), max(point[1] for point in points),
                ),
            })
    return routes


def fetch_planner_path(line: dict) -> dict | None:
    line_id = str(line.get("id"))
    if line_id.startswith("mas_"):
        status, response = get_mas_path(line_id)
    else:
        status, raw = call_api(f"/bus/lineServices/{line_id}")
        try:
            response = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if status != 200 or not isinstance(response, dict) or not response.get("success"):
        return None
    return response.get("data")


def refresh_planner_paths(lines: list[dict] | None = None, max_workers: int = 4) -> int:
    """Actualiza el catálogo sin descartar la última copia si una API falla."""
    global _all_lines_info, _planner_last_refresh
    lines = list(lines if lines is not None else get_all_lines_combined())
    with _all_paths_lock:
        previous = dict(_all_paths_cache)
    if not lines:
        return sum(bool(routes) for routes in previous.values())

    refreshed: dict[str, list[dict]] = {}

    def load(line):
        data = fetch_planner_path(line)
        return data, extract_planner_routes(line, data) if data is not None else None

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 6))) as executor:
        futures = {executor.submit(load, line): line for line in lines}
        for future in as_completed(futures):
            line = futures[future]
            line_id = str(line.get("id"))
            try:
                data, routes = future.result()
            except Exception:
                data, routes = None, None
            if routes is not None:
                # Una respuesta válida pero vacía significa que el proveedor
                # actualmente no posee geometría para esa línea.
                refreshed[line_id] = routes
            elif line_id in previous:
                refreshed[line_id] = previous[line_id]

    with _all_paths_lock:
        _all_paths_cache.clear()
        _all_paths_cache.update(refreshed)
        _all_lines_info = lines
        _planner_last_refresh = time.time()
    _save_planner_routes_to_disk()
    return sum(bool(routes) for routes in refreshed.values())


def path_fetcher_loop():
    # Dar tiempo a que el servidor empiece a responder el HTML. Luego el
    # catálogo se renueva periódicamente y sobrevive a fallas transitorias.
    if observed_routes.stop.wait(2):
        return
    while not observed_routes.stop.is_set():
        try:
            refresh_planner_paths()
        except Exception as exc:
            print(f"Error actualizando el planificador: {exc}")
        observed_routes.stop.wait(PLANNER_REFRESH_SECONDS)


def project_point_to_planner_route(lat: float, lon: float, route: dict) -> dict | None:
    """Proyecta un punto sobre segmentos, no solamente sobre vértices."""
    points = route.get("points") or []
    cumulative = route.get("cumulative") or []
    if len(points) < 2 or len(cumulative) != len(points):
        return None

    earth_radius = 6371000.0
    ref_cos = math.cos(math.radians(lat))
    best = None
    for index, (start, end) in enumerate(zip(points, points[1:])):
        if index in route.get("gaps", ()):
            continue
        ax = earth_radius * math.radians(start[1] - lon) * ref_cos
        ay = earth_radius * math.radians(start[0] - lat)
        bx = earth_radius * math.radians(end[1] - lon) * ref_cos
        by = earth_radius * math.radians(end[0] - lat)
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy
        fraction = -(ax * dx + ay * dy) / length_squared if length_squared else 0.0
        fraction = max(0.0, min(1.0, fraction))
        cx, cy = ax + fraction * dx, ay + fraction * dy
        distance_m = math.hypot(cx, cy)
        if best is None or distance_m < best["distance_m"]:
            segment_length = cumulative[index + 1] - cumulative[index]
            best = {
                "distance_m": distance_m,
                "arc_m": cumulative[index] + fraction * segment_length,
                "lat": start[0] + fraction * (end[0] - start[0]),
                "lon": start[1] + fraction * (end[1] - start[1]),
                "segment": index,
            }
    return best


MAX_STOP_TO_ROUTE_DISTANCE_METERS = 150.0


def find_nearby_bus_routes(lat: float, lon: float, max_meters: float = MAX_STOP_TO_ROUTE_DISTANCE_METERS) -> list[dict]:
    """
    Encuentra todos los ramales y líneas de colectivos a menos de max_meters de las coordenadas dadas.
    Retorna lista ordenada por distancia en metros.
    """
    with _all_paths_lock:
        all_routes = [r for rlist in _all_paths_cache.values() for r in rlist]

    if not all_routes:
        _load_planner_routes_from_disk()
        with _all_paths_lock:
            all_routes = [r for rlist in _all_paths_cache.values() for r in rlist]

    lat_margin = max_meters / 111200.0
    lon_margin = max_meters / max(20.0, 111200.0 * math.cos(math.radians(lat)))
    results = []

    for route in all_routes:
        bbox = route.get("bbox")
        if not bbox:
            continue
        if not (bbox[0] - lat_margin <= lat <= bbox[2] + lat_margin and
                bbox[1] - lon_margin <= lon <= bbox[3] + lon_margin):
            continue
        proj = project_point_to_planner_route(lat, lon, route)
        if proj and proj["distance_m"] <= max_meters:
            results.append({
                "line_id": str(route.get("line_id")),
                "line_name": route.get("line_name") or f"Línea {route.get('line_id')}",
                "route_name": route.get("route_name") or "Recorrido",
                "direction": route.get("direction"),
                "distance_m": round(proj["distance_m"], 1),
            })

    results.sort(key=lambda x: x["distance_m"])
    return results


def get_unique_nearby_lines(nearby_routes: list[dict]) -> list[dict]:
    """Agrupa los recorridos cercanos por línea conservando la distancia mínima y sentidos."""
    lines_map = {}
    for r in nearby_routes:
        lid = str(r["line_id"])
        if lid not in lines_map:
            lines_map[lid] = {
                "line_id": lid,
                "line_name": r["line_name"],
                "distance_m": r["distance_m"],
                "directions": set(),
                "routes": []
            }
        else:
            lines_map[lid]["distance_m"] = min(lines_map[lid]["distance_m"], r["distance_m"])
        if r.get("direction"):
            lines_map[lid]["directions"].add(r["direction"])
        if r.get("route_name") and r["route_name"] not in lines_map[lid]["routes"]:
            lines_map[lid]["routes"].append(r["route_name"])

    res = list(lines_map.values())
    for item in res:
        item["directions"] = sorted(list(item["directions"]))
    res.sort(key=lambda x: x["distance_m"])
    return res


def plan_trip(olat: float, olon: float, dlat: float, dlon: float) -> list[dict]:
    """Devuelve recorridos directos ordenados por tiempo aproximado total."""
    with _all_paths_lock:
        snapshot = [route for routes in _all_paths_cache.values() for route in routes]

    options = []
    for route in snapshot:
        min_lat, min_lon, max_lat, max_lon = route.get("bbox", (-90, -180, 90, 180))
        lat_margin = PLANNER_MAX_WALK_KM / 111.2
        origin_lon_margin = PLANNER_MAX_WALK_KM / max(20.0, 111.2 * math.cos(math.radians(olat)))
        destination_lon_margin = PLANNER_MAX_WALK_KM / max(20.0, 111.2 * math.cos(math.radians(dlat)))
        origin_near = (
            min_lat - lat_margin <= olat <= max_lat + lat_margin
            and min_lon - origin_lon_margin <= olon <= max_lon + origin_lon_margin
        )
        destination_near = (
            min_lat - lat_margin <= dlat <= max_lat + lat_margin
            and min_lon - destination_lon_margin <= dlon <= max_lon + destination_lon_margin
        )
        if not origin_near or not destination_near:
            continue
        origin = project_point_to_planner_route(olat, olon, route)
        destination = project_point_to_planner_route(dlat, dlon, route)
        if origin is None or destination is None:
            continue
        if origin["distance_m"] >= PLANNER_MAX_WALK_KM * 1000 or destination["distance_m"] >= PLANNER_MAX_WALK_KM * 1000:
            continue

        ride_distance = destination["arc_m"] - origin["arc_m"]
        wraps = False
        if ride_distance < -20 and route.get("closed"):
            ride_distance = route["length_m"] - origin["arc_m"] + destination["arc_m"]
            wraps = True
        if ride_distance <= 20:
            continue
        if any(origin['segment'] <= gap <= destination['segment'] for gap in route.get('gaps', ())):
            continue

        origin_walk = int(round(origin["distance_m"]))
        destination_walk = int(round(destination["distance_m"]))
        total_walk = origin_walk + destination_walk
        walk_minutes = total_walk / PLANNER_WALKING_SPEED_M_PER_MIN
        bus_minutes = ride_distance / PLANNER_BUS_SPEED_M_PER_MIN
        estimated_minutes = max(1, int(round(walk_minutes + bus_minutes)))
        board_stop = find_nearest_official_stop(origin["lat"], origin["lon"], 150.0)
        alight_stop = find_nearest_official_stop(destination["lat"], destination["lon"], 150.0)
        options.append({
            "id": route["line_id"],
            "name": route["line_name"],
            "provider": route["provider"],
            "serviceId": route["service_id"],
            "routeId": route["route_id"],
            "routeName": route["route_name"],
            "direction": route["direction"],
            "walkingDist": total_walk,
            "originWalkMeters": origin_walk,
            "destinationWalkMeters": destination_walk,
            "rideDistanceMeters": int(round(ride_distance)),
            "estimatedMinutes": estimated_minutes,
            "board": {"lat": origin["lat"], "lon": origin["lon"]},
            "alight": {"lat": destination["lat"], "lon": destination["lon"]},
            "boardStop": board_stop,
            "alightStop": alight_stop,
            "wraps": wraps,
        })

    # Se conserva una alternativa por recorrido/sentido; algunos proveedores
    # repiten la misma ficha bajo varios servicios internos.
    options.sort(key=lambda item: (item["estimatedMinutes"], item["walkingDist"], item["rideDistanceMeters"]))
    unique = []
    seen = set()
    for option in options:
        identity = (
            option["id"], catalog_identity(option["routeName"]),
            option["direction"] or "",
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(option)
        if len(unique) >= PLANNER_RESULT_LIMIT:
            break
    return unique


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
_subs_file_lock = threading.Lock()
_subs: dict[str, dict] = {}


def load_subs():
    global _subs
    if SUBS_FILE.is_file():
        try:
            loaded = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            cutoff = time.time() - SUBSCRIPTION_TTL_SECONDS
            retained = {}
            for client_id, sub in loaded.items() if isinstance(loaded, dict) else []:
                try:
                    if isinstance(sub, dict) and float(sub.get("updatedAt") or 0) >= cutoff:
                        retained[client_id] = sub
                except (TypeError, ValueError):
                    continue
            _subs = retained
        except Exception:
            _subs = {}


def save_subs():
    with _subs_file_lock:
        DATA_DIR.mkdir(exist_ok=True)
        with _subs_lock:
            snapshot = json.dumps(_subs)
        temporary = SUBS_FILE.with_suffix(".tmp")
        temporary.write_text(snapshot, encoding="utf-8")
        os.replace(temporary, SUBS_FILE)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def find_nearest_official_stop(lat: float, lon: float, max_meters: float = 120.0) -> dict | None:
    best = None
    best_dist = float("inf")
    for stop in get_all_stops_combined():
        dist_m = haversine_km(lat, lon, stop["lat"], stop["lon"]) * 1000.0
        if dist_m <= max_meters and dist_m < best_dist:
            best_dist = dist_m
            best = {
                "id": stop["id"],
                "name": stop["name"],
                "type": stop.get("type", "refugio"),
                "distanceMeters": int(round(dist_m)),
                "lat": stop["lat"],
                "lon": stop["lon"],
            }
    return best


def search_pois(query: str, limit: int = 15) -> list[dict]:
    if not isinstance(query, str) or len(query.strip()) < 2:
        return []
    normalized_q = unicodedata.normalize("NFKD", query.strip().lower())
    normalized_q = "".join(c for c in normalized_q if not unicodedata.combining(c))
    tokens = [t for t in normalized_q.split() if t]
    if not tokens:
        return []

    results = []
    for poi in _asuncion_pois:
        name = poi.get("name", "")
        norm_name = unicodedata.normalize("NFKD", name.lower())
        norm_name = "".join(c for c in norm_name if not unicodedata.combining(c))
        if all(token in norm_name for token in tokens):
            results.append({
                "id": poi.get("id"),
                "name": name,
                "category": poi.get("category", "Lugar de Interés"),
                "lat": poi["lat"],
                "lon": poi["lon"],
                "display_name": f"{name} ({poi.get('category', 'Asunción')})"
            })
            if len(results) >= limit:
                break
    return results


def send_push(push_subscription: dict, title: str, body_text: str,
              line_id: str | None = None) -> str:
    """Distingue entrega aceptada, fallo temporal y suscripción vencida."""
    try:
        webpush(
            subscription_info=push_subscription,
            data=json.dumps({"title": title, "body": body_text, "line": line_id}),
            vapid_private_key=_vapid,
            vapid_claims={"sub": VAPID_CLAIMS_SUB},
        )
        return "sent"
    except WebPushException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        print("push error:", exc)
        if status_code in (404, 410):
            return "expired"
        return "failed"
    except Exception as exc:
        print("push error inesperado:", exc)
        return "failed"


def proximity_transition(sub: dict, min_dist: float | None, now: float) -> tuple[str | None, bool]:
    """Decide el cambio de estado; ninguna muestra válida no equivale a alejamiento."""
    was_inside = bool(sub.get("insideRadius"))
    if min_dist is None:
        last_seen = float(sub.get("lastObservedAt") or now)
        return None, was_inside and now - last_seen < 300
    radius = float(sub.get("radiusKm", 5))
    if was_inside:
        # Un GPS que oscila sobre el borde no genera múltiples avisos.
        return None, min_dist < radius + max(0.15, radius * 0.1)
    if min_dist <= radius:
        if now < float(sub.get("pushRetryAfter") or 0):
            return None, False
        return "notify", False
    return None, False


def same_notification_watch(previous: dict | None, line: str, radius: float,
                            lat: float, lon: float) -> bool:
    return bool(previous and previous.get("line") == line
                and previous.get("radiusKm") == radius
                and valid_coordinates(previous.get("lat"), previous.get("lon"))
                and haversine_km(float(previous["lat"]), float(previous["lon"]), lat, lon) < 0.15)


def check_notifications_once():
    """Evalúa un ciclo con estado persistido por suscriptor."""
    with _subs_lock:
        snapshot = list(_subs.items())

    by_line: dict[str, list[tuple[str, dict]]] = {}
    stale_ids = []
    cutoff = time.time() - SUBSCRIPTION_TTL_SECONDS
    for client_id, sub in snapshot:
        if float(sub.get("updatedAt") or 0) < cutoff:
            stale_ids.append(client_id)
            continue
        if not sub.get("line") or sub.get("lat") is None or sub.get("lon") is None:
            continue
        by_line.setdefault(sub["line"], []).append((client_id, sub))

    expired_ids = list(stale_ids)
    changed = False

    for line, entries in by_line.items():
        try:
            _status, body = get_positions_cached(line)
            data = json.loads(body)
        except Exception as exc:
            print(f"Error consultando posiciones para avisos de {line}: {exc}")
            continue
        if _status != 200 or not isinstance(data, dict) or not data.get("success"):
            continue
        units = data.get("data") or []

        for client_id, sub in entries:
            min_dist = None
            for u in units:
                try:
                    ulat, ulon = float(u["lat"]), float(u["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not valid_coordinates(ulat, ulon):
                    continue
                d = haversine_km(sub["lat"], sub["lon"], ulat, ulon)
                if min_dist is None or d < min_dist:
                    min_dist = d

            now = time.time()
            action, inside = proximity_transition(sub, min_dist, now)
            if action == "notify":
                line_name = sub.get("lineName") or f"Linea {line}"
                result = send_push(
                    sub["pushSubscription"],
                    "\U0001f68c Bus cerca",
                    f"{line_name} está a unos {min_dist:.1f} km del último punto guardado.",
                    line,
                )
                if result == "expired":
                    expired_ids.append(client_id)
                    continue
                if result == "sent":
                    inside = True

            with _subs_lock:
                if _subs.get(client_id) is not sub:
                    continue
                if action == "notify" and result == "failed":
                    # Reintentar sin insistir cada 20 segundos.
                    sub["pushRetryAfter"] = now + 120
                    changed = True
                if min_dist is not None and now - float(sub.get("lastObservedAt") or 0) >= 60:
                    sub["lastObservedAt"] = now
                    changed = True
                if sub.get("insideRadius") != inside:
                    sub["insideRadius"] = inside
                    changed = True

    if expired_ids:
        with _subs_lock:
            for cid in expired_ids:
                _subs.pop(cid, None)
        changed = True

    if changed:
        save_subs()


def notifier_loop():
    """Cada NOTIFY_CHECK_INTERVAL segundos comprueba las líneas vigiladas."""
    while True:
        time.sleep(NOTIFY_CHECK_INTERVAL)
        if PUSH_AVAILABLE:
            try:
                check_notifications_once()
            except Exception as exc:
                print(f"Error revisando avisos: {exc}")


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class FixedWindowRateLimiter:
    """Límite sencillo en memoria para las rutas públicas más costosas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buckets: dict[str, tuple[float, int, float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            started, count, _last_seen = self.buckets.get(key, (now, 0, now))
            if now - started >= window_seconds:
                started, count = now, 0
            count += 1
            self.buckets[key] = (started, count, now)

            if len(self.buckets) > 2048:
                cutoff = now - 2 * window_seconds
                stale = [bucket_key for bucket_key, bucket in self.buckets.items() if bucket[2] < cutoff]
                for bucket_key in stale:
                    self.buckets.pop(bucket_key, None)
                if len(self.buckets) > 2048:
                    oldest = sorted(self.buckets, key=lambda bucket_key: self.buckets[bucket_key][2])
                    for bucket_key in oldest[:len(self.buckets) - 2048]:
                        self.buckets.pop(bucket_key, None)

            retry_after = max(1, math.ceil(window_seconds - (now - started)))
            return count <= limit, retry_after


_rate_limiter = FixedWindowRateLimiter()


def resolve_static_file(rel_path: str) -> Path | None:
    """Resuelve un archivo únicamente si permanece dentro de static/."""
    try:
        candidate = (STATIC_DIR / rel_path).resolve()
        candidate.relative_to(STATIC_DIR)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def static_content_type(path: Path) -> str:
    explicit = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/manifest+json; charset=utf-8" if path.name == "manifest.json" else "application/json; charset=utf-8",
        ".webmanifest": "application/manifest+json; charset=utf-8",
        ".svg": "image/svg+xml",
    }
    return explicit.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia el log por defecto, ya lo hacemos mas abajo

    def end_headers(self):
        # Cabeceras compatibles con la PWA actual. La política permite los
        # proveedores de mapa/fuentes existentes, pero bloquea marcos, cámara,
        # micrófono y contenido inesperado.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "geolocation=(self), camera=(self), microphone=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https://tiles.openfreemap.org https://unpkg.com; "
            "connect-src 'self' https://tiles.openfreemap.org https://router.project-osrm.org; "
            "worker-src 'self' blob:; manifest-src 'self'",
        )
        super().end_headers()

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        candidate = forwarded or self.client_address[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return str(self.client_address[0])[:64]

    def _allow_request(self, group: str, limit: int, window_seconds: int = 60) -> bool:
        allowed, retry_after = _rate_limiter.allow(
            f"{group}:{self._client_ip()}", limit, window_seconds
        )
        if allowed:
            return True
        self._send_json_obj(
            429,
            {"success": False, "error": "demasiadas solicitudes; intenta nuevamente en unos segundos"},
            {"Retry-After": str(retry_after)},
        )
        return False

    def _send_json(self, status: int, payload: bytes, extra_headers: dict[str, str] | None = None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # Cliente cerro conexion antes de terminar

    def _send_json_obj(self, status: int, obj: dict, extra_headers: dict[str, str] | None = None):
        self._send_json(status, json.dumps(obj).encode("utf-8"), extra_headers)

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise RequestBodyError(400, "Content-Length invalido")
        if length <= 0:
            return None
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyError(413, "la solicitud supera el limite de 64 KB")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestBodyError(400, "JSON invalido")

    def _read_multipart_body(self, max_bytes: int = 5 * 1024 * 1024) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise RequestBodyError(400, "Content-Length invalido")
        if length <= 0:
            raise RequestBodyError(400, "Falta contenido en la solicitud")
        if length > max_bytes:
            raise RequestBodyError(413, "El archivo supera el limite de 5 MB")
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise RequestBodyError(400, "Se requiere Content-Type multipart/form-data")

        msg_bytes = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
        msg = BytesParser(policy=policy.default).parsebytes(msg_bytes)
        fields = {}
        files = {}
        for part in msg.iter_parts():
            disp = part.get("Content-Disposition", "")
            if "form-data" not in disp:
                continue
            name = part.get_param("name", header="content-disposition")
            filename = part.get_param("filename", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = (filename, payload)
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def _feedback_admin_token(self) -> str:
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["feedback_admin"].value if "feedback_admin" in cookie else ""
        except (CookieError, ValueError):
            return ""

    def _feedback_cookie(self, token: str = "") -> str:
        # Railway termina HTTPS antes de llegar a este servidor HTTP.
        secure = bool(os.environ.get("RAILWAY_ENVIRONMENT_ID")) or self.headers.get("X-Forwarded-Proto") == "https"
        cookie = f"feedback_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS if token else 0}"
        return cookie + ("; Secure" if secure else "")

    def _require_feedback_admin(self, csrf=False) -> bool:
        session_csrf = feedback_store.session(
            self._feedback_admin_token(), _feedback_admin_password()
        ) if feedback_enabled() else None
        if not session_csrf:
            self._send_json_obj(401, {"success": False, "error": "iniciá sesión"})
            return False
        if csrf and not secrets.compare_digest(self.headers.get("X-Feedback-CSRF", ""), session_csrf):
            self._send_json_obj(403, {"success": False, "error": "solicitud no autorizada"})
            return False
        return True

    def _send_file(self, rel_path: str):
        file_path = resolve_static_file(rel_path)
        if file_path is None:
            self.send_error(404, "Not found")
            return
        data = file_path.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", static_content_type(file_path))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # Cliente cerro conexion antes de terminar

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # No registrar parámetros: el planificador contiene coordenadas exactas.
        if not parsed.path.startswith("/api/admin/tomtom-tile/"):
            print(f"GET {parsed.path}")

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("index.html")
            return

        if parsed.path == "/admin/feedback":
            self._send_file("admin-feedback.html")
            return

        if parsed.path == "/api/feedback/config":
            self._send_json_obj(200, {"success": True, "enabled": feedback_enabled()})
            return

        if parsed.path == "/api/news":
            client_id = qs.get("clientId", [""])[0]
            if client_id and not valid_client_id(client_id):
                self._send_json_obj(400, {"success": False, "error": "clientId invalido"})
                return
            items = feedback_store.list_news()
            read_id = feedback_store.get_news_read_id(client_id) if client_id else 0
            self._send_json_obj(200, {
                "success": True,
                "items": items,
                "lastSeenNewsId": read_id,
            })
            return

        if parsed.path == "/api/admin/feedback/session":
            csrf = feedback_store.session(
                self._feedback_admin_token(), _feedback_admin_password()
            ) if feedback_enabled() else None
            self._send_json_obj(200, {"success": True, "active": bool(csrf), "csrf": csrf})
            return

        if parsed.path == "/api/admin/feedback":
            if not self._require_feedback_admin():
                return
            status = qs.get("status", ["todos"])[0]
            if status not in {"todos", "nuevo", "revisado"}:
                self._send_json_obj(400, {"success": False, "error": "filtro inválido"})
                return
            try:
                before = int(qs["before"][0]) if "before" in qs else None
                limit = int(qs.get("limit", ["50"])[0])
            except (ValueError, IndexError):
                self._send_json_obj(400, {"success": False, "error": "paginación inválida"})
                return
            if (before is not None and not 0 < before <= 2**63 - 1) or not 1 <= limit <= 100:
                self._send_json_obj(400, {"success": False, "error": "paginación inválida"})
                return
            items, next_before = feedback_store.list(status, before, limit)
            self._send_json_obj(200, {"success": True, "items": items, "nextBefore": next_before})
            return

        if parsed.path == "/api/admin/stop-reports":
            if not self._require_feedback_admin():
                return
            status = qs.get("status", [None])[0]
            if status == "todos" or not status:
                status = None
            elif status not in {"pendiente", "aprobada", "rechazada"}:
                self._send_json_obj(400, {"success": False, "error": "status invalido"})
                return
            try:
                before = int(qs["before"][0]) if "before" in qs else None
                limit = int(qs.get("limit", ["50"])[0])
            except (ValueError, IndexError):
                self._send_json_obj(400, {"success": False, "error": "paginacion invalida"})
                return
            items, next_before = stop_report_store.list_reports(status, before, limit)
            usage = stop_report_store.photos_disk_usage()
            self._send_json_obj(200, {
                "success": True,
                "items": items,
                "nextBefore": next_before,
                "storage": usage
            })
            return

        if parsed.path == "/api/admin/stop-reports/photo":
            if not self._require_feedback_admin():
                return
            try:
                report_id = int(qs.get("id", ["0"])[0])
            except ValueError:
                self._send_json_obj(400, {"success": False, "error": "id invalido"})
                return
            photo_path = stop_report_store.get_photo_path(report_id)
            if not photo_path or not photo_path.exists():
                self._send_json_obj(404, {"success": False, "error": "foto no disponible (ya fue procesada o eliminada)"})
                return
            data = photo_path.read_bytes()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError):
                pass
            return

        if parsed.path == "/api/stop-reports/my-status":
            if not self._allow_request("stop-report-status", 300, 3600):
                return
            client_id = qs.get("clientId", [""])[0]
            if not client_id or not valid_client_id(client_id):
                self._send_json_obj(400, {"success": False, "error": "clientId invalido"})
                return
            ids_raw = qs.get("ids", [""])[0]
            ids = []
            if ids_raw:
                parts = [part.strip() for part in ids_raw.split(",")]
                if len(parts) > 30 or any(not part.isdigit() or int(part) <= 0 for part in parts):
                    self._send_json_obj(400, {"success": False, "error": "ids invalidos"})
                    return
                ids = list(dict.fromkeys(int(part) for part in parts))
            reports = stop_report_store.get_user_reports_status(client_id, ids or None)
            self._send_json_obj(200, {"success": True, "reports": reports})
            return

        if parsed.path == "/api/stop-reports/check-location":
            if not self._allow_request("stop-report-location", 60, 3600):
                return
            try:
                lat = float(qs.get("lat", [""])[0])
                lon = float(qs.get("lon", [""])[0])
                accuracy = float(qs.get("accuracy", ["0"])[0]) if qs.get("accuracy") else 0.0
            except (ValueError, IndexError):
                self._send_json_obj(400, {"success": False, "error": "Coordenadas o precision GPS invalidas"})
                return

            if not math.isfinite(accuracy) or accuracy < 0:
                self._send_json_obj(400, {"success": False, "error": "Precision GPS invalida"})
                return

            if not in_paraguay(lat, lon):
                self._send_json_obj(200, {
                    "success": True,
                    "valid": False,
                    "reason": "out_of_bounds",
                    "error": "La ubicación debe estar dentro del territorio paraguayo."
                })
                return

            if accuracy > 100:
                self._send_json_obj(200, {
                    "success": True,
                    "valid": False,
                    "reason": "poor_accuracy",
                    "error": f"La señal GPS es imprecisa (+-{int(round(accuracy))}m). Por favor acercate a la parada o salí al exterior."
                })
                return

            nearby_stop = find_nearest_official_stop(lat, lon, max_meters=50.0)
            if nearby_stop:
                self._send_json_obj(200, {
                    "success": True,
                    "valid": False,
                    "reason": "duplicate_stop",
                    "error": f"Ya existe una parada registrada a {nearby_stop['distanceMeters']}m: {nearby_stop['name']}",
                    "nearbyStop": nearby_stop
                })
                return

            nearby_pending = stop_report_store.find_nearby_pending(lat, lon, max_meters=50.0)
            if nearby_pending:
                self._send_json_obj(200, {
                    "success": True,
                    "valid": False,
                    "reason": "duplicate_pending",
                    "error": f"Ya existe un reporte en revisión para esta misma ubicación (a {nearby_pending['distanceMeters']}m).",
                    "nearbyPending": nearby_pending
                })
                return

            nearby_routes = find_nearby_bus_routes(lat, lon, max_meters=MAX_STOP_TO_ROUTE_DISTANCE_METERS)
            if not nearby_routes:
                self._send_json_obj(200, {
                    "success": True,
                    "valid": False,
                    "reason": "no_bus_routes",
                    "error": f"No se detecta ningún recorrido de colectivos cerca de este punto (a menos de {int(MAX_STOP_TO_ROUTE_DISTANCE_METERS)}m). Las paradas deben estar sobre calles donde circulen buses."
                })
                return

            unique_lines = get_unique_nearby_lines(nearby_routes)
            self._send_json_obj(200, {
                "success": True,
                "valid": True,
                "distance_to_route_m": nearby_routes[0]["distance_m"],
                "lines": unique_lines,
                "routes": nearby_routes[:6]
            })
            return

        if parsed.path == "/api/admin/observed-review":
            if not self._require_feedback_admin():
                return
            status = qs.get("status", ["all"])[0]
            if status not in {"all", "accepted", "rejected", "pending", "awaiting_review", "approved", "discarded"}:
                self._send_json_obj(400, {"success": False, "error": "filtro inválido"})
                return
            line = qs.get("line", [""])[0].strip()
            if line and not valid_line_id(line):
                self._send_json_obj(400, {"success": False, "error": "línea inválida"})
                return
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except (ValueError, IndexError):
                self._send_json_obj(400, {"success": False, "error": "límite inválido"})
                return
            if not 1 <= limit <= 100:
                self._send_json_obj(400, {"success": False, "error": "límite inválido"})
                return
            self._send_json_obj(200, observed_routes.review(status, line, limit))
            return

        if parsed.path.startswith("/api/admin/tomtom-tile/"):
            if not self._require_feedback_admin():
                return
            if not self._allow_request("tomtom-map-tiles", 120):
                return
            match = re.fullmatch(r"/api/admin/tomtom-tile/(\d{1,2})/(\d{1,8})/(\d{1,8})\.png", parsed.path)
            if not _tomtom_api_key or not match:
                self.send_error(404)
                return
            z, x, y = map(int, match.groups())
            if z > 22 or x >= 2**z or y >= 2**z:
                self.send_error(404)
                return
            url = f"https://api.tomtom.com/map/1/tile/basic/main/{z}/{x}/{y}.png?" + urlencode({"key": _tomtom_api_key})
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "image/png"}), timeout=10) as response:
                    tile = response.read(256 * 1024 + 1)
                if len(tile) > 256 * 1024 or not tile.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("respuesta de mapa inválida")
            except (OSError, ValueError):
                self.send_error(502)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(tile)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(tile)
            except (BrokenPipeError, ConnectionAbortedError):
                pass
            return

        if parsed.path == "/api/lines":
            all_lines = get_all_lines_combined()
            self._send_json_obj(200, {"success": True, "data": all_lines})
            return

        if parsed.path == "/api/stops":
            all_stops = get_all_stops_combined()
            self._send_json_obj(200, {"success": True, "count": len(all_stops), "data": all_stops})
            return

        if parsed.path == "/api/traffic-lights":
            self._send_json_obj(200, {"success": True, "count": len(_asuncion_traffic_lights), "data": _asuncion_traffic_lights})
            return

        if parsed.path == "/api/pois":
            q = qs.get("q", [""])[0]
            items = search_pois(q, limit=20)
            self._send_json_obj(200, {"success": True, "count": len(items), "data": items})
            return

        if parsed.path == "/api/observed-routes":
            line = qs.get("line", [""])[0]
            if not line or len(line) > 80:
                self._send_json_obj(400, {"success": False, "error": "falta line"})
                return
            self._send_json_obj(200, observed_routes.snapshot(line, qs.get("history") == ["1"]))
            return

        if parsed.path == "/api/observed-health":
            observed_health = observed_routes.health()
            storage = volume_stats()
            if storage["used_percent"] >= 80:
                observed_health["warnings"].append("El volumen de datos supera el 80% del presupuesto configurado")
            self._send_json_obj(200, {
                "success": True,
                "collector_enabled": os.environ.get("OBSERVED_COLLECTOR", "1") != "0",
                "last_cycle": observed_routes.last_cycle,
                "railway_volume_configured": bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")),
                "storage": storage,
                **observed_health,
            })
            return

        if parsed.path == "/api/positions":
            line = qs.get("line", [None])[0]
            if not valid_line_id(line):
                self._send_json(400, b'{"success": false, "error": "line invalida"}')
                return
            if line not in known_line_ids():
                self._send_json_obj(404, {"success": False, "error": "linea inexistente"})
                return
            if not self._allow_request("positions", 120):
                return
            status, body = get_positions_cached(line)
            self._send_json(status, body)
            return

        if parsed.path == "/api/path":
            line = qs.get("line", [None])[0]
            if not valid_line_id(line):
                self._send_json(400, b'{"success": false, "error": "line invalida"}')
                return
            if line not in known_line_ids():
                self._send_json_obj(404, {"success": False, "error": "linea inexistente"})
                return
            if not self._allow_request("path", 60):
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
            if not self._allow_request("planificar", 60):
                return
            try:
                olat = float(qs.get("olat", [None])[0])
                olon = float(qs.get("olon", [None])[0])
                dlat = float(qs.get("dlat", [None])[0])
                dlon = float(qs.get("dlon", [None])[0])
            except (TypeError, ValueError):
                self._send_json(400, b'{"success": false, "error": "coordenadas invalidas"}')
                return
            if not valid_coordinates(olat, olon) or not valid_coordinates(dlat, dlon):
                self._send_json(400, b'{"success": false, "error": "coordenadas invalidas"}')
                return
            with _all_paths_lock:
                loaded_lines = sum(bool(routes) for routes in _all_paths_cache.values())
                ready = loaded_lines > 0
            self._send_json_obj(200, {
                "success": True,
                "ready": ready,
                "loadedLines": loaded_lines,
                "data": plan_trip(olat, olon, dlat, dlon),
            })
            return

        if parsed.path == "/api/notify/vapid-public-key":
            self._send_json_obj(200, {"available": PUSH_AVAILABLE, "publicKey": _vapid_public_b64})
            return

        if parsed.path == "/api/notify/status":
            client_id = qs.get("clientId", [""])[0]
            if not valid_client_id(client_id):
                self._send_json_obj(400, {"success": False, "error": "clientId invalido"})
                return
            if not self._allow_request("notify-status", 60):
                return
            with _subs_lock:
                sub = _subs.get(client_id)
                state = None if not sub else {
                    "line": sub.get("line"), "lineName": sub.get("lineName"),
                    "radiusKm": sub.get("radiusKm"),
                    "locationUpdatedAt": sub.get("locationUpdatedAt", sub.get("updatedAt")),
                }
            self._send_json_obj(200, {"success": True, "active": state is not None, "data": state})
            return

        # cualquier otro archivo estatico (por si se agregan mas, ej. sw.js)
        self._send_file(parsed.path.lstrip("/"))

    def do_POST(self):
        parsed = urlparse(self.path)
        print(f"POST {parsed.path}")

        if parsed.path == "/api/stop-report":
            if not self._allow_request("stop-report", 5, 3600):
                return
            try:
                fields, files = self._read_multipart_body()
            except RequestBodyError as exc:
                self._send_json_obj(exc.status, {"success": False, "error": exc.message})
                return

            client_id = fields.get("clientId", "").strip()
            if not valid_client_id(client_id):
                self._send_json_obj(400, {"success": False, "error": "Identificador de dispositivo (clientId) invalido"})
                return

            if stop_report_store.count_recent_by_client(client_id, 24) >= 3:
                self._send_json_obj(429, {
                    "success": False,
                    "error": "Alcanzaste el limite de 3 reportes en 24 horas desde este dispositivo. ¡Muchas gracias por colaborar!"
                })
                return

            try:
                lat = float(fields.get("lat", ""))
                lon = float(fields.get("lon", ""))
                accuracy = float(fields.get("accuracy", "0"))
            except ValueError:
                self._send_json_obj(400, {"success": False, "error": "Coordenadas o precision GPS invalidas"})
                return

            if not math.isfinite(accuracy) or accuracy < 0:
                self._send_json_obj(400, {"success": False, "error": "Precision GPS invalida"})
                return

            if not in_paraguay(lat, lon):
                self._send_json_obj(400, {"success": False, "error": "La ubicacion debe estar dentro del territorio paraguayo"})
                return

            if accuracy > 100:
                self._send_json_obj(400, {
                    "success": False,
                    "error": f"La senial GPS es muy imprecisa (+-{int(round(accuracy))}m). Por favor acercate a la parada o sali al exterior."
                })
                return

            nearby_stop = find_nearest_official_stop(lat, lon, max_meters=50.0)
            if nearby_stop:
                self._send_json_obj(409, {
                    "success": False,
                    "error": f"Ya existe una parada registrada a {nearby_stop['distanceMeters']}m: {nearby_stop['name']}",
                    "nearbyStop": nearby_stop
                })
                return

            nearby_pending = stop_report_store.find_nearby_pending(lat, lon, max_meters=50.0)
            if nearby_pending:
                self._send_json_obj(409, {
                    "success": False,
                    "error": f"Ya existe un reporte en revision para esta misma ubicacion (a {nearby_pending['distanceMeters']}m).",
                    "nearbyPending": nearby_pending
                })
                return

            nearby_routes = find_nearby_bus_routes(lat, lon, max_meters=MAX_STOP_TO_ROUTE_DISTANCE_METERS)
            if not nearby_routes:
                self._send_json_obj(400, {
                    "success": False,
                    "error": f"No se detectó ningún recorrido de colectivos cerca de esta ubicación (a menos de {int(MAX_STOP_TO_ROUTE_DISTANCE_METERS)}m). Las paradas deben ubicarse sobre una calle por donde circule alguna línea de transporte público."
                })
                return

            unique_lines = get_unique_nearby_lines(nearby_routes)

            if "photo" not in files:
                self._send_json_obj(400, {"success": False, "error": "Es obligatorio adjuntar una foto de la parada"})
                return

            photo_filename, photo_bytes = files["photo"]
            if len(photo_bytes) < 100:
                self._send_json_obj(400, {"success": False, "error": "El archivo de imagen esta vacio o corrupto"})
                return

            description = fields.get("description", "")
            reporter_name = fields.get("name", "")

            push_sub = None
            push_raw = fields.get("pushSubscription")
            if push_raw:
                try:
                    push_sub = json.loads(push_raw)
                    if not isinstance(push_sub, dict) or not push_sub.get("endpoint"):
                        push_sub = None
                except Exception:
                    push_sub = None

            try:
                res = stop_report_store.create(
                    lat=lat, lon=lon, accuracy=accuracy, raw_photo_bytes=photo_bytes,
                    client_id=client_id, description=description,
                    reporter_name=reporter_name, push_subscription=push_sub,
                    nearby_lines=unique_lines
                )
            except Exception as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return

            self._send_json_obj(201, {
                "success": True,
                "id": res["id"],
                "lines": unique_lines,
                "message": "¡Reporte enviado exitosamente! Sera revisado por un administrador."
            })
            return

        try:
            body = self._read_json_body()
        except RequestBodyError as exc:
            self._send_json_obj(exc.status, {"success": False, "error": exc.message})
            return

        if parsed.path == "/api/news/read":
            if not self._allow_request("news-read", 60, 3600):
                return
            if not isinstance(body, dict):
                self._send_json_obj(400, {"success": False, "error": "datos invalidos"})
                return
            client_id = body.get("clientId")
            news_id = body.get("newsId")
            if not valid_client_id(client_id) or type(news_id) is not int or news_id < 0:
                self._send_json_obj(400, {"success": False, "error": "clientId o newsId invalidos"})
                return
            read_id = feedback_store.mark_news_read(client_id, news_id)
            self._send_json_obj(200, {"success": True, "lastSeenNewsId": read_id})
            return

        if parsed.path == "/api/feedback":
            if not feedback_enabled():
                self._send_json_obj(503, {"success": False, "error": "comentarios aún no disponibles"})
                return
            if not self._allow_request("feedback", 3, 3600):
                return
            try:
                record = validate_feedback(body)
            except ValueError as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return
            feedback_id, delete_token = feedback_store.create(record)
            self._send_json_obj(201, {"success": True, "code": f"C-{feedback_id:06d}-{delete_token}"})
            return

        if parsed.path == "/api/feedback/delete":
            if not feedback_enabled():
                self._send_json_obj(503, {"success": False, "error": "comentarios aún no disponibles"})
                return
            if not self._allow_request("feedback-delete", 10, 3600):
                return
            code = body.get("code") if isinstance(body, dict) else None
            deleted = feedback_store.delete_with_code(code)
            self._send_json_obj(200 if deleted else 400, {
                "success": deleted,
                "error": None if deleted else "código inválido o comentario ya eliminado",
            })
            return

        if parsed.path == "/api/admin/feedback/login":
            if not self._allow_request("feedback-admin-login", 5, 900):
                return
            configured_password = _feedback_admin_password()
            if len(configured_password) < 16:
                self._send_json_obj(503, {"success": False, "error": "panel no configurado"})
                return
            token = feedback_store.login(body.get("password") if isinstance(body, dict) else None,
                                         configured_password)
            if not token:
                self._send_json_obj(401, {"success": False, "error": "contraseña incorrecta"})
                return
            self._send_json_obj(200, {"success": True}, {"Set-Cookie": self._feedback_cookie(token)})
            return

        if parsed.path in {"/api/admin/feedback/status", "/api/admin/feedback/delete", "/api/admin/feedback/logout"}:
            if not self._require_feedback_admin(csrf=True):
                return
            if parsed.path == "/api/admin/feedback/logout":
                feedback_store.logout(self._feedback_admin_token(), _feedback_admin_password())
                self._send_json_obj(200, {"success": True}, {"Set-Cookie": self._feedback_cookie()})
                return
            feedback_id = body.get("id") if isinstance(body, dict) else None
            if type(feedback_id) is not int or not 0 < feedback_id <= 2**63 - 1:
                self._send_json_obj(400, {"success": False, "error": "comentario inválido"})
                return
            if parsed.path == "/api/admin/feedback/status":
                status = body.get("status")
                if status not in {"nuevo", "revisado"}:
                    self._send_json_obj(400, {"success": False, "error": "estado inválido"})
                    return
                found = feedback_store.set_status(feedback_id, status)
            else:
                found = feedback_store.delete(feedback_id)
            self._send_json_obj(200 if found else 404, {"success": found})
            return

        if parsed.path == "/api/admin/news":
            if not self._require_feedback_admin(csrf=True):
                return
            try:
                record = validate_news(body)
            except ValueError as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return
            news_id = feedback_store.create_news(record)
            self._send_json_obj(201, {"success": True, "id": news_id})
            return

        if parsed.path == "/api/admin/observed-match/run-one":
            if not self._require_feedback_admin(csrf=True):
                return
            if observed_routes.match_provider != "tomtom" or not observed_routes.matching_enabled():
                self._send_json_obj(409, {"success": False, "error": "TomTom no está configurado"})
                return
            result = observed_routes.run_match_batch(limit=1)
            self._send_json_obj(200, {
                "success": True,
                **result,
                "health": observed_routes.health(),
            })
            return

        if parsed.path == "/api/admin/observed-review/decide":
            if not self._require_feedback_admin(csrf=True):
                return
            try:
                result = observed_routes.decide_review(
                    body.get("id") if isinstance(body, dict) else None,
                    body.get("action") if isinstance(body, dict) else None,
                    body.get("note", "") if isinstance(body, dict) else "",
                    variant=body.get("variant", "original") if isinstance(body, dict) else "original")
            except ValueError as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return
            self._send_json_obj(200, result)
            return

        if parsed.path == "/api/admin/observed-review/refine":
            if not self._require_feedback_admin(csrf=True):
                return
            try:
                result = observed_routes.refine_review(
                    body.get("id") if isinstance(body, dict) else None)
            except ValueError as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return
            self._send_json_obj(200, result)
            return

        if parsed.path == "/api/admin/news/delete":
            if not self._require_feedback_admin(csrf=True):
                return
            news_id = body.get("id") if isinstance(body, dict) else None
            if type(news_id) is not int or not 0 < news_id <= 2**63 - 1:
                self._send_json_obj(400, {"success": False, "error": "identificador de novedad inválido"})
                return
            deleted = feedback_store.delete_news(news_id)
            self._send_json_obj(200 if deleted else 404, {"success": deleted})
            return

        if parsed.path == "/api/admin/stop-reports/review":
            if not self._require_feedback_admin(csrf=True):
                return
            report_id = body.get("id") if isinstance(body, dict) else None
            action = body.get("action") if isinstance(body, dict) else None
            if type(report_id) is not int or not action:
                self._send_json_obj(400, {"success": False, "error": "Datos de revision incompletos"})
                return
            try:
                res = stop_report_store.review(
                    report_id=report_id,
                    action=action,
                    admin_notes=body.get("admin_notes", ""),
                    street_name=body.get("street_name", ""),
                    street_side=body.get("street_side", ""),
                    stop_name=body.get("stop_name", ""),
                    stop_type=body.get("stop_type", "refugio"),
                    rejection_reason=body.get("rejection_reason", "")
                )
            except Exception as exc:
                self._send_json_obj(400, {"success": False, "error": str(exc)})
                return

            notify_user = body.get("notify_user", True)
            push_sub = res.get("push_subscription")
            if notify_user and push_sub:
                try:
                    if action == "aprobar":
                        sname = body.get("stop_name") or body.get("street_name") or "tu parada"
                        send_push(
                            push_sub,
                            "\U0001f389 ¡Parada agregada al mapa!",
                            f"Tu reporte de parada en {sname} fue aprobado y ya esta en JAHA."
                        )
                        stop_report_store.mark_notified(report_id)
                    elif action in ("rechazar", "duplicada"):
                        reason = body.get("rejection_reason") or "No cumple con las pautas de paradas"
                        send_push(
                            push_sub,
                            "\u274c Reporte de parada no aprobado",
                            f"Tu carga de parada no fue aprobada: {reason}"
                        )
                        stop_report_store.mark_notified(report_id)
                except Exception as p_err:
                    print(f"Error enviando push de parada: {p_err}")

            self._send_json_obj(200, {"success": True, "data": res})
            return

        if parsed.path == "/api/shared-trip/start":
            if not self._allow_request("shared-trip-start", 10, 3600):
                return
            if not isinstance(body, dict) or not valid_client_id(body.get("clientId")):
                self._send_json_obj(400, {"success": False, "error": "dispositivo invalido"})
                return
            kind = body.get("kind")
            intent = body.get("intent")
            if (kind, intent) not in {
                ("stop", "waiting"), ("stop", "going"), ("bus", "trip"),
            }:
                self._send_json_obj(400, {"success": False, "error": "tipo de viaje invalido"})
                return
            stop_id = body.get("stopId")
            destination_id = body.get("destinationStopId")
            if stop_id is not None and not official_stop(stop_id):
                self._send_json_obj(400, {"success": False, "error": "parada inexistente"})
                return
            if destination_id is not None and not official_stop(destination_id):
                self._send_json_obj(400, {"success": False, "error": "destino inexistente"})
                return
            line_id = unit_id = None
            if kind == "stop":
                if not stop_id:
                    self._send_json_obj(400, {"success": False, "error": "falta la parada"})
                    return
                if intent == "waiting":
                    stop = official_stop(stop_id)
                    try:
                        accuracy = float(body.get("accuracy"))
                        nearby = valid_coordinates(body.get("lat"), body.get("lon")) and 0 <= accuracy <= 250 and haversine_km(
                            float(body["lat"]), float(body["lon"]), float(stop["lat"]), float(stop["lon"])
                        ) * 1000 <= 500
                    except (TypeError, ValueError):
                        nearby = False
                    if not nearby:
                        self._send_json_obj(409, {"success": False, "error": "no pudimos confirmar que estes cerca de esa parada"})
                        return
            else:
                line_id, unit_id = body.get("lineId"), str(body.get("unitId") or "")
                bus = current_bus(line_id, unit_id)
                try:
                    accuracy = float(body.get("accuracy"))
                    nearby = bus and 0 <= accuracy <= 250 and valid_coordinates(body.get("lat"), body.get("lon")) and haversine_km(
                        float(body["lat"]), float(body["lon"]), float(bus["lat"]), float(bus["lon"])
                    ) * 1000 <= 700
                except (TypeError, ValueError):
                    nearby = False
                if not nearby:
                    self._send_json_obj(409, {"success": False, "error": "no pudimos confirmar que estes cerca de ese bus"})
                    return
            created = shared_trip_store.create(
                body["clientId"], intent=intent, line_id=line_id, unit_id=unit_id,
                stop_id=stop_id, destination_stop_id=destination_id,
            )
            self._send_json_obj(201, {"success": True, **created})
            return

        if parsed.path == "/api/shared-trip/view":
            if not self._allow_request("shared-trip-view", 90):
                return
            token = body.get("token") if isinstance(body, dict) else None
            trip = shared_trip_payload(token)
            self._send_json_obj(200 if trip else 404, {
                "success": bool(trip), "trip": trip,
                "error": None if trip else "enlace inexistente o eliminado",
            })
            return

        if parsed.path == "/api/shared-trip/update":
            if not self._allow_request("shared-trip-update", 90):
                return
            if not isinstance(body, dict):
                self._send_json_obj(400, {"success": False, "error": "datos invalidos"})
                return
            token, owner_token = body.get("token"), body.get("ownerToken")
            action = body.get("action")
            updated = False
            if action == "heartbeat":
                existing = shared_trip_store.get_owned(token, owner_token)
                bus = current_bus(existing.get("line_id"), existing.get("unit_id")) if existing else None
                try:
                    accuracy = float(body.get("accuracy"))
                    reliable = bool(bus and 0 <= accuracy <= 150 and valid_coordinates(body.get("lat"), body.get("lon")))
                    distance = haversine_km(float(body["lat"]), float(body["lon"]),
                                            float(bus["lat"]), float(bus["lon"])) * 1000 if reliable else None
                except (TypeError, ValueError):
                    reliable, distance = False, None
                updated = shared_trip_store.heartbeat(
                    token, owner_token, reliable=reliable, distance_m=distance,
                ) is not None
            elif action == "destination":
                stop_id = body.get("stopId")
                updated = bool(official_stop(stop_id)) and shared_trip_store.set_destination(token, owner_token, stop_id)
            elif action == "board":
                line_id, unit_id = body.get("lineId"), str(body.get("unitId") or "")
                destination_id = body.get("destinationStopId")
                existing = shared_trip_store.get_owned(token, owner_token)
                destination_valid = destination_id is None or bool(official_stop(destination_id))
                bus = current_bus(line_id, unit_id) if existing and destination_valid else None
                try:
                    accuracy = float(body.get("accuracy"))
                    nearby = bus and 0 <= accuracy <= 250 and valid_coordinates(body.get("lat"), body.get("lon")) and haversine_km(
                        float(body["lat"]), float(body["lon"]), float(bus["lat"]), float(bus["lon"])
                    ) * 1000 <= 700
                except (TypeError, ValueError):
                    nearby = False
                if nearby:
                    updated = shared_trip_store.board(token, owner_token, line_id, unit_id, destination_id)
            elif action == "extend":
                updated = shared_trip_store.extend(token, owner_token)
            elif action == "end":
                updated = shared_trip_store.end(token, owner_token)
            else:
                self._send_json_obj(400, {"success": False, "error": "accion invalida"})
                return
            trip = shared_trip_payload(token) if updated else None
            self._send_json_obj(200 if updated else 403, {
                "success": bool(updated), "trip": trip,
                "error": None if updated else "no autorizado o viaje finalizado",
            })
            return

        if parsed.path == "/api/geocode":
            if not self._allow_request("geocode", 15):
                return
            status, result = search_address(body.get("query") if isinstance(body, dict) else None)
            self._send_json_obj(status, result)
            return

        if parsed.path == "/api/planificar":
            if not self._allow_request("planificar", 60):
                return
            try:
                olat = float(body.get("olat"))
                olon = float(body.get("olon"))
                dlat = float(body.get("dlat"))
                dlon = float(body.get("dlon"))
            except (AttributeError, TypeError, ValueError):
                self._send_json_obj(400, {"success": False, "error": "coordenadas invalidas"})
                return
            if not valid_coordinates(olat, olon) or not valid_coordinates(dlat, dlon):
                self._send_json_obj(400, {"success": False, "error": "coordenadas invalidas"})
                return
            with _all_paths_lock:
                loaded_lines = sum(bool(routes) for routes in _all_paths_cache.values())
                ready = loaded_lines > 0
            self._send_json_obj(200, {
                "success": True,
                "ready": ready,
                "loadedLines": loaded_lines,
                "data": plan_trip(olat, olon, dlat, dlon),
            })
            return

        if parsed.path == "/api/positions/batch":
            lines = body.get("lines") if isinstance(body, dict) else None
            if (
                not isinstance(lines, list)
                or not lines
                or len(lines) > MAX_BATCH_LINES
                or any(not valid_line_id(line) for line in lines)
            ):
                self._send_json_obj(400, {
                    "success": False,
                    "error": f"se requieren entre 1 y {MAX_BATCH_LINES} lineas validas",
                })
                return
            unique_lines = list(dict.fromkeys(lines))
            unknown = [line for line in unique_lines if line not in known_line_ids()]
            if unknown:
                self._send_json_obj(400, {"success": False, "error": "una o mas lineas no existen"})
                return
            if not self._allow_request("positions-batch", 12):
                return
            self._send_json_obj(200, {"success": True, "lines": get_positions_batch(unique_lines)})
            return

        if parsed.path == "/api/observed-routes/batch":
            lines = body.get("lines") if isinstance(body, dict) else None
            if not isinstance(lines, list) or len(lines) > 25 or any(not isinstance(x, str) or len(x)>80 for x in lines):
                self._send_json_obj(400, {"success": False, "error": "se requieren hasta 25 lineas"})
                return
            if not self._allow_request("observed-routes-batch", 30):
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
            client_id = body["clientId"]
            line = body["line"]
            try:
                radius = float(body.get("radiusKm", 5))
            except (TypeError, ValueError):
                radius = -1
            if (
                not valid_client_id(client_id)
                or not valid_line_id(line)
                or line not in known_line_ids()
                or not isinstance(body.get("pushSubscription"), dict)
                or not valid_coordinates(body.get("lat"), body.get("lon"))
                or not math.isfinite(radius)
                or not 0.1 <= radius <= 50
            ):
                self._send_json_obj(400, {"success": False, "error": "datos de aviso invalidos"})
                return
            if not self._allow_request("notify", 30):
                return
            with _subs_lock:
                previous = _subs.get(client_id)
                same_point = same_notification_watch(previous, line, radius,
                                                     float(body["lat"]), float(body["lon"]))
                _subs[client_id] = {
                    "line": line,
                    "lineName": str(body.get("lineName") or "")[:120],
                    "radiusKm": radius,
                    "pushSubscription": body["pushSubscription"],
                    # Un aviso de 100 m o más no necesita conservar la
                    # precisión completa del GPS del usuario (~11 m alcanza).
                    "lat": round(float(body["lat"]), 4),
                    "lon": round(float(body["lon"]), 4),
                    "insideRadius": bool(previous.get("insideRadius")) if same_point else False,
                    "pushRetryAfter": previous.get("pushRetryAfter", 0) if same_point else 0,
                    "lastObservedAt": previous.get("lastObservedAt", 0) if same_point else 0,
                    "updatedAt": time.time(),
                    "locationUpdatedAt": time.time(),
                }
            save_subs()
            self._send_json_obj(200, {"success": True})
            return

        if parsed.path == "/api/notify/location":
            if not body or not body.get("clientId"):
                self._send_json_obj(400, {"success": False, "error": "falta clientId"})
                return
            client_id = body["clientId"]
            if not valid_client_id(client_id) or not valid_coordinates(body.get("lat"), body.get("lon")):
                self._send_json_obj(400, {"success": False, "error": "ubicacion invalida"})
                return
            if not self._allow_request("notify-location", 60):
                return
            with _subs_lock:
                if client_id in _subs:
                    _subs[client_id]["lat"] = round(float(body["lat"]), 4)
                    _subs[client_id]["lon"] = round(float(body["lon"]), 4)
                    _subs[client_id]["updatedAt"] = time.time()
                    _subs[client_id]["locationUpdatedAt"] = time.time()
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
            client_id = body["clientId"]
            if not valid_client_id(client_id):
                self._send_json_obj(400, {"success": False, "error": "clientId invalido"})
                return
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

def feedback_prune_loop():
    while not observed_routes.stop.is_set():
        for name, prune in (("comentarios", feedback_store.prune),
                            ("viajes compartidos", shared_trip_store.prune),
                            ("reportes de paradas", stop_report_store.prune)):
            try:
                prune()
            except Exception as exc:
                print(f"Error al depurar {name}: {exc}")
        observed_routes.stop.wait(3600)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_tracks_db()
    feedback_store.init()
    feedback_store.publish_release_news("share-bus-links-2026-09-21", validate_news({
        "title": "Compartí un bus y tu punto de bajada",
        "content": (
            "Ahora podés compartir una unidad para que otra persona vea solamente ese bus. De forma "
            "opcional, podés indicar dónde querés bajar eligiendo una parada o un punto del mapa; el "
            "enlace muestra la distancia y el tiempo estimado. También podés compartir paradas, "
            "guardarlas como favoritas y ponerles apodos privados como ‘Casa de mamá’."
        ),
        "tag": "mejora",
    }))
    shared_trip_store.init()
    observed_routes.init()
    load_subs()
    threading.Thread(target=feedback_prune_loop, daemon=True).start()
    
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
    if not observed_routes.matching_enabled():
        print("Map matching no configurado: las estelas se conservan como puntos GPS pendientes de ajuste.")
    elif observed_routes.match_provider == "tomtom":
        print(f"TomTom Snap to Roads activo con aprobación humana; lote semanal limitado a "
              f"{observed_routes.weekly_match_limit} solicitudes.")

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
