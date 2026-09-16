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
from urllib.parse import urlparse, parse_qs, urlencode
from observed_routes import ObservedRoutes

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
observed_routes = ObservedRoutes(
    TRACKS_DB_FILE,
    osrm_url=os.environ.get("OSRM_MATCH_URL", "https://router.project-osrm.org"),
    poll_seconds=int(os.environ.get("OBSERVED_POLL_SECONDS", "30")),
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


def search_address(query):
    global _geocode_last_request
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 200:
        return 400, {"success": False, "error": "Escribí entre 3 y 200 caracteres."}
    endpoint = urlparse(GEOCODER_SEARCH_URL)
    if endpoint.scheme != "https" or not endpoint.hostname:
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
        separator = "&" if endpoint.query else "?"
        url = GEOCODER_SEARCH_URL + separator + urlencode({"format": "json", "countrycodes": "py", "limit": 5, "q": query})
        request = urllib.request.Request(url, headers={"User-Agent": "ColectivosRoutePlanner/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("oversized geocoder response")
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("invalid geocoder response")
            results = []
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                try:
                    lat, lon = float(item["lat"]), float(item["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if valid_coordinates(lat, lon):
                    results.append({"lat": lat, "lon": lon, "display_name": str(item.get("display_name") or "Punto del mapa")[:500]})
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
        self.send_header("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
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
        print(f"GET {parsed.path}")

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
        try:
            body = self._read_json_body()
        except RequestBodyError as exc:
            self._send_json_obj(exc.status, {"success": False, "error": exc.message})
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
