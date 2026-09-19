"""Shared observed routes. Original provider paths are read-only evidence.

All durable writes happen on the server. Raw GPS never becomes an invented
street line: only validated map matches contribute to coloured street edges.
"""
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


WINDOW = 14 * 86400
GAP = 90
ASUNCION = timezone(timedelta(hours=-3))
MATCH_TIMEZONE = ZoneInfo('America/Asuncion')


class MatchBudgetExhausted(RuntimeError):
    """The configured external map-matching allowance has been consumed."""


def gps_spike(a, b, c, jump_m=80, return_m=35):
    """Detect a single GPS point that jumps away and immediately comes back.

    The time checks intentionally make this conservative: a real detour or a
    long stop must not be removed merely because its endpoints are nearby.
    """
    if not a or not b or not c or len(a) < 3 or len(b) < 3 or len(c) < 3:
        return False
    first_gap, second_gap = b[2] - a[2], c[2] - b[2]
    if not (0 < first_gap <= GAP and 0 < second_gap <= GAP):
        return False
    return distance(a, b) >= jump_m and distance(b, c) >= jump_m and distance(a, c) <= return_m


class TomTomMatcher:
    """Small server-side adapter for TomTom Snap to Roads.

    Only the geometry and validation signals needed by this project are
    requested. The API key never leaves the backend.
    """

    ENDPOINT = 'https://api.tomtom.com/snapToRoads/1'

    def __init__(self, api_key, timeout=25, offroad_margin=40):
        self.api_key = str(api_key or '').strip()
        self.timeout = timeout
        self.offroad_margin = max(20, min(100, int(offroad_margin)))
        self.last_quality = {}

    @staticmethod
    def _heading(a, b):
        lat1, lat2 = math.radians(a[0]), math.radians(b[0])
        delta_lon = math.radians(b[1] - a[1])
        y = math.sin(delta_lon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
        return round((math.degrees(math.atan2(y, x)) + 360) % 360, 1)

    def __call__(self, points):
        self.last_quality = {'input_points': len(points)}
        if not self.api_key:
            raise ValueError('TOMTOM_API_KEY no configurada')
        if len(points) < 3:
            raise ValueError('TomTom requiere al menos tres puntos confiables')

        features = []
        for index, point in enumerate(points):
            other = points[index + 1] if index + 1 < len(points) else points[index - 1]
            start, end = (point, other) if index + 1 < len(points) else (other, point)
            properties = {
                'heading': self._heading(start, end),
                'timestamp': datetime.fromtimestamp(point[2], timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [point[1], point[0]]},
                'properties': properties,
            })

        fields = ('{projectedPoints{geometry{coordinates},properties{routeIndex,snapResult}},'
                  'route{geometry{coordinates},properties{id,confidence}},'
                  'distances{total,road,offRoad,unit}}')
        query = urlencode({
            'key': self.api_key,
            'fields': fields,
            'vehicleType': 'Bus',
            'measurementSystem': 'metric',
            'offroadMargin': self.offroad_margin,
        })
        request = urllib.request.Request(
            f'{self.ENDPOINT}?{query}',
            data=json.dumps({'points': features}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError('Respuesta TomTom demasiado grande')
        data = json.loads(raw)

        projected = data.get('projectedPoints') or []
        matched = [item for item in projected
                   if (item.get('properties') or {}).get('snapResult') == 'Matched']
        self.last_quality['matched_points'] = len(matched)
        self.last_quality['matched_ratio'] = round(len(matched) / len(points), 4)
        if len(matched) < math.ceil(len(points) * .9):
            raise ValueError('TomTom no pudo ajustar al menos 90% de los puntos')

        projected_by_input = []
        for source, item in zip(points, projected):
            props = item.get('properties') or {}
            coords = (item.get('geometry') or {}).get('coordinates')
            if props.get('snapResult') != 'Matched' or not isinstance(coords, list) or len(coords) < 2:
                continue
            snapped = [float(coords[1]), float(coords[0])]
            projected_by_input.append((source, snapped))
        if projected_by_input and sum(distance(source, snapped) <= self.offroad_margin
                                      for source, snapped in projected_by_input) < math.ceil(len(points) * .9):
            raise ValueError('Demasiados puntos quedaron lejos de la calle ajustada')

        distances = data.get('distances') or {}
        total_distance = float(distances.get('total') or 0)
        offroad_distance = float(distances.get('offRoad') or 0)
        self.last_quality['offroad_ratio'] = round(
            offroad_distance / total_distance, 4) if total_distance > 0 else 0
        if total_distance > 0 and offroad_distance / total_distance > .05:
            raise ValueError('TomTom detecto mas de 5% del trayecto fuera de calle')

        parts, confidences = [], []
        for element in data.get('route') or []:
            coords = (element.get('geometry') or {}).get('coordinates') or []
            try:
                path = [[float(point[1]), float(point[0])] for point in coords]
                confidence = float((element.get('properties') or {})['confidence'])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if len(path) >= 2 and all(all(math.isfinite(value) for value in point) for point in path):
                parts.append(path)
                confidences.append(confidence)
        if confidences:
            self.last_quality['minimum_confidence'] = round(min(confidences), 4)
        if not parts or not confidences or min(confidences) < .8:
            raise ValueError('TomTom devolvio una geometria de baja confianza')

        source_length = length(points)
        matched_length = sum(length(path) for path in parts)
        self.last_quality.update(
            source_length_m=round(source_length, 1),
            matched_length_m=round(matched_length, 1),
            route_elements=len(parts),
        )
        if matched_length > max(250, source_length * 1.6) or matched_length < source_length * .45:
            raise ValueError('La geometria TomTom no conserva una longitud razonable')
        return parts


def branch_key(name):
    text = unicodedata.normalize('NFD', str(name or '')).lower()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'\((?:i|v|ida|vuelta)\)', ' ', text)
    text = re.sub(r'(?:^|[\s_-])(?:ida|vuelta)(?=$|[\s_-])', ' ', text)
    # Provider route IDs often appear as standalone suffixes ("Rojo 3").
    # They are metadata, not separate public branch names.
    text = re.sub(r'\b\d+\b', ' ', text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def xy(p):
    return (p[1] * 111320 * math.cos(math.radians(-25)), p[0] * 111320)


def distance(a, b):
    x, y = xy(a); u, v = xy(b)
    return math.hypot(x-u, y-v)


def point_distance(p, a, b):
    x, y = xy(p); ax, ay = xy(a); bx, by = xy(b)
    dx, dy = bx-ax, by-ay
    t = max(0, min(1, ((x-ax)*dx + (y-ay)*dy) / (dx*dx + dy*dy or 1)))
    return math.hypot(x-ax-t*dx, y-ay-t*dy)


def length(points):
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def sample_time(unit, now):
    """Return a trustworthy sample time when possible.

    JAHA reports ``online: 0`` for some entire lines even while their GPS
    coordinates keep changing.  Treat that field as advisory: actual movement
    is validated later using displacement, ordering, gap, teleport and speed
    checks.  A stationary/offline unit therefore cannot build a trail.
    """
    raw = str(unit.get('observed_at') or unit.get('modified') or unit.get('time') or '')
    try:
        if re.fullmatch(r'\d{2}:\d{2}:\d{2}', raw):
            local = datetime.fromtimestamp(now, ASUNCION)
            stamp = datetime.combine(local.date(), datetime.strptime(raw, '%H:%M:%S').time(), ASUNCION).timestamp()
            if stamp > now + 300:
                stamp -= 86400
        elif raw:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            stamp = dt.replace(tzinfo=ASUNCION).timestamp() if dt.tzinfo is None else dt.timestamp()
        else:
            stamp = now
        return int(stamp) if now - 180 <= stamp <= now + 30 else None
    except (ValueError, TypeError, OverflowError):
        return None


class OfficialIndex:
    def __init__(self, data):
        self.cells = {}
        self.names = set()
        for service in data.get('services', []):
            for route in service.get('routes', []):
                name = branch_key(route.get('name') or service.get('name'))
                self.names.add(name)
                for section in route.get('sections', []):
                    points = []
                    for p in sorted(section.get('traces', []), key=lambda p: p.get('order', 0)):
                        try:
                            pos = [float(p['latitud']), float(p['longitud'])]
                            if all(math.isfinite(v) for v in pos): points.append(pos)
                        except (ValueError, KeyError, TypeError):
                            continue
                    for a, b in zip(points, points[1:]):
                        ax, ay = xy(a); bx, by = xy(b)
                        # Index only nearby segments, never join separate sections.
                        steps = max(1, int(distance(a, b) / 100) + 1)
                        for i in range(steps + 1):
                            key = (int((ax+(bx-ax)*i/steps)//150), int((ay+(by-ay)*i/steps)//150))
                            self.cells.setdefault(key, []).append((name, a, b))

    def measure(self, point, route):
        key = branch_key(route)
        # Only exact branch matching is trusted. Unknown branches compare with
        # all official paths and retain their reported label separately.
        exact = key in self.names
        x, y = xy(point); cx, cy = int(x//150), int(y//150)
        best = math.inf
        for ix in range(cx-1, cx+2):
            for iy in range(cy-1, cy+2):
                for name, a, b in self.cells.get((ix, iy), []):
                    if not exact or name == key:
                        best = min(best, point_distance(point, a, b))
        return best


def street_edges(points):
    """Split matched geometry on a fixed 25m grid, keeping all street bends.

    Directed endpoints identify actual geometry, not merely nearby GPS cells.
    Partial edge overlaps are conservatively separate (never over-counted).
    """
    # OSRM can insert projected GPS points on a straight road at different
    # places per bus. Remove only collinear vertices before canonical splitting.
    simplified = []
    for point in points:
        while len(simplified) >= 2 and point_distance(simplified[-1], simplified[-2], point) < .15:
            simplified.pop()
        simplified.append(point)
    for a, b in zip(simplified, simplified[1:]):
        ax, ay = xy(a); bx, by = xy(b)
        fractions = {0., 1.}
        for start, end in ((ax, bx), (ay, by)):
            if abs(end-start) < .001: continue
            for grid in range(math.floor(min(start, end)/25)+1, math.ceil(max(start, end)/25)):
                t = (grid*25-start)/(end-start)
                if 0 < t < 1: fractions.add(t)
        cuts = sorted(fractions)
        for t, u in zip(cuts, cuts[1:]):
            first = [a[j]+(b[j]-a[j])*t for j in (0, 1)]
            last = [a[j]+(b[j]-a[j])*u for j in (0, 1)]
            if distance(first, last) < 2: continue
            key = ','.join(str(round(v, 6)) for p in (first, last) for v in p)
            yield key, [first, last]


class ObservedRoutes:
    def __init__(self, path, osrm_url='', poll_seconds=30, match_provider=None,
                 tomtom_api_key='', shadow_mode=False, raw_retention_days=7,
                 max_pending_jobs=5000, max_job_attempts=8,
                 monthly_match_limit=2200, weekly_match_limit=400,
                 match_weekday=6, match_hour=3, match_run_on_start=False,
                 min_confirmed_buses=1, min_confirmed_passes=1):
        self.path = str(path)
        self.osrm_url = osrm_url.rstrip('/')
        self.poll_seconds = max(15, poll_seconds)
        self.match_provider = (match_provider or ('osrm' if self.osrm_url else 'disabled')).strip().lower()
        if self.match_provider not in {'disabled', 'osrm', 'tomtom'}:
            raise ValueError('MATCH_PROVIDER debe ser disabled, osrm o tomtom')
        self.tomtom_matcher = TomTomMatcher(tomtom_api_key) if tomtom_api_key else None
        self.shadow_mode = bool(shadow_mode)
        self.raw_retention = max(1, int(raw_retention_days)) * 86400
        self.max_pending_jobs = max(100, int(max_pending_jobs))
        self.max_job_attempts = max(1, int(max_job_attempts))
        self.monthly_match_limit = max(0, int(monthly_match_limit))
        self.weekly_match_limit = max(1, int(weekly_match_limit))
        self.match_weekday = max(0, min(6, int(match_weekday)))
        self.match_hour = max(0, min(23, int(match_hour)))
        self.match_run_on_start = bool(match_run_on_start)
        self.min_confirmed_buses = max(1, int(min_confirmed_buses))
        self.min_confirmed_passes = max(1, int(min_confirmed_passes))
        self.lock = threading.RLock()
        self.indexes = {}
        self.stop = threading.Event()
        self.cache = {}
        self.last_error = ''
        self.last_cycle = 0
        self.path_retry = {}

    def matching_enabled(self):
        return ((self.match_provider == 'tomtom' and self.tomtom_matcher is not None) or
                (self.match_provider == 'osrm' and bool(self.osrm_url)))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS observed_official (
                    line TEXT PRIMARY KEY, data TEXT NOT NULL, checked REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_state (
                    line TEXT, unit TEXT, state TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(line, unit));
                CREATE TABLE IF NOT EXISTS observed_samples (
                    line TEXT, unit TEXT, stamp INTEGER, lat REAL, lon REAL, route TEXT,
                    PRIMARY KEY(line, unit, stamp));
                CREATE INDEX IF NOT EXISTS observed_samples_age ON observed_samples(stamp);
                CREATE TABLE IF NOT EXISTS observed_jobs (
                    id TEXT PRIMARY KEY, line TEXT, unit TEXT, route TEXT, passage TEXT,
                    kind TEXT, points TEXT, seen REAL, status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
                    error TEXT DEFAULT '', geometry TEXT);
                CREATE INDEX IF NOT EXISTS observed_jobs_queue ON observed_jobs(status, next_try);
                CREATE INDEX IF NOT EXISTS observed_jobs_line ON observed_jobs(line, seen);
                CREATE TABLE IF NOT EXISTS observed_edges (
                    id TEXT PRIMARY KEY, line TEXT, branch TEXT, route TEXT, kind TEXT,
                    points TEXT, last_seen REAL);
                CREATE INDEX IF NOT EXISTS observed_edges_line ON observed_edges(line, last_seen);
                CREATE TABLE IF NOT EXISTS observed_passages (
                    edge TEXT, unit TEXT, passage TEXT, seen REAL,
                    PRIMARY KEY(edge, unit, passage));
                CREATE INDEX IF NOT EXISTS observed_passages_seen ON observed_passages(edge, seen);
                CREATE TABLE IF NOT EXISTS observed_match_cache (
                    key TEXT PRIMARY KEY, geometry TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS observed_api_usage (
                    period TEXT, provider TEXT, requests INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL,
                    PRIMARY KEY(period, provider));
                CREATE TABLE IF NOT EXISTS observed_match_audit (
                    job TEXT PRIMARY KEY, provider TEXT NOT NULL, result TEXT NOT NULL,
                    metrics TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS observed_match_audit_created
                    ON observed_match_audit(created);
            ''')
            db.execute("UPDATE observed_jobs SET status='pending' WHERE status='processing' AND next_try<=?",
                       (time.time(),))
            if self.match_provider == 'tomtom':
                # Give recent jobs exhausted by the previous OSRM provider one
                # fresh chance. TomTom failures already have an audit row and
                # are deliberately not reset on every restart.
                db.execute('''UPDATE observed_jobs SET status='pending',attempts=0,next_try=0,error=''
                              WHERE status='failed' AND seen>=? AND NOT EXISTS (
                                SELECT 1 FROM observed_match_audit a WHERE a.job=observed_jobs.id)''',
                           (time.time()-self.raw_retention,))
                self._trim_pending(db)
            for row in db.execute('SELECT line, data FROM observed_official'):
                self.indexes[row['line']] = OfficialIndex(json.loads(row['data']))

    @staticmethod
    def _month_key(now):
        return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m')

    def match_usage(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            row = db.execute('SELECT requests FROM observed_api_usage WHERE period=? AND provider=?',
                             (self._month_key(now), self.match_provider)).fetchone()
        return int(row['requests']) if row else 0

    def remaining_match_budget(self, now=None):
        return max(0, self.monthly_match_limit - self.match_usage(now))

    def health(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            counts = {row['status']: row['total'] for row in db.execute(
                'SELECT status, COUNT(*) AS total FROM observed_jobs GROUP BY status')}
            pending_groups = db.execute('''SELECT COUNT(*) FROM (
                SELECT 1 FROM observed_jobs WHERE status IN ('pending','processing')
                GROUP BY line,unit,passage)''').fetchone()[0]
            audits = {row['result']: row['total'] for row in db.execute(
                'SELECT result, COUNT(*) AS total FROM observed_match_audit WHERE created>=? GROUP BY result',
                (now-30*86400,))}
            row = db.execute('SELECT requests FROM observed_api_usage WHERE period=? AND provider=?',
                             (self._month_key(now), self.match_provider)).fetchone()
        used = int(row['requests']) if row else 0
        validated = audits.get('accepted', 0)
        rejected = audits.get('rejected', 0)
        pending = counts.get('pending', 0) + counts.get('processing', 0)
        warnings = []
        if not self.matching_enabled():
            warnings.append('Map matching no está configurado')
        if pending >= self.max_pending_jobs * .8:
            warnings.append('La cola de recorridos supera el 80% de su capacidad')
        if self.match_provider == 'tomtom' and self.monthly_match_limit and used >= self.monthly_match_limit * .8:
            warnings.append('El consumo TomTom supera el 80% del límite mensual')
        if counts.get('failed', 0):
            warnings.append('Hay recorridos que agotaron sus reintentos')
        return {
            'matching_enabled': self.matching_enabled(),
            'matching_provider': self.match_provider,
            'shadow_mode': self.shadow_mode,
            'pending_jobs': pending,
            'pending_passages': pending_groups,
            'failed_jobs': counts.get('failed', 0),
            'validated_jobs': counts.get('done', 0),
            'accepted_last_30_days': validated,
            'rejected_last_30_days': rejected,
            'acceptance_rate': round(validated / (validated+rejected), 4) if validated+rejected else None,
            'monthly_requests': used,
            'monthly_limit': self.monthly_match_limit if self.match_provider == 'tomtom' else None,
            'queue_limit': self.max_pending_jobs,
            'minimum_confirmed_buses': self.min_confirmed_buses,
            'minimum_confirmed_passes': self.min_confirmed_passes,
            'warnings': warnings,
        }

    def review(self, status='all', line='', limit=50, now=None):
        """Return recent matching evidence for the authenticated admin panel."""
        now = time.time() if now is None else now
        clauses = ['j.seen>=?', "j.status!='merged'"]
        params = [now-30*86400]
        if status == 'accepted':
            clauses.append("a.result='accepted'")
        elif status == 'rejected':
            clauses.append("(a.result='rejected' OR j.status='failed')")
        elif status == 'pending':
            clauses.append("j.status IN ('pending','processing')")
        if line:
            clauses.append('j.line=?')
            params.append(line)
        params.append(max(1, min(100, int(limit))))
        query = f'''SELECT j.id,j.line,j.route,j.kind,j.seen,j.status,j.attempts,
                    j.error,j.points,j.geometry,a.provider,a.result AS audit_result,a.metrics
                    FROM observed_jobs j LEFT JOIN observed_match_audit a ON a.job=j.id
                    WHERE {' AND '.join(clauses)} ORDER BY j.seen DESC LIMIT ?'''
        items = []
        with self.lock, self.connect() as db:
            rows = db.execute(query, params).fetchall()
        for row in rows:
            try:
                raw_points = json.loads(row['points'] or '[]')
                geometry = json.loads(row['geometry'] or '[]')
                metrics = json.loads(row['metrics'] or '{}')
            except (TypeError, json.JSONDecodeError):
                raw_points, geometry, metrics = [], [], {}
            items.append({
                'id': row['id'], 'line': row['line'], 'route': row['route'],
                'kind': row['kind'], 'seen': row['seen'], 'status': row['status'],
                'attempts': row['attempts'], 'error': row['error'],
                'provider': row['provider'] or self.match_provider,
                'result': row['audit_result'], 'metrics': metrics,
                'points': [[point[0], point[1]] for point in raw_points if len(point) >= 2],
                'geometry': geometry,
            })
        return {'success': True, 'items': items, 'health': self.health(now),
                'raw_retention_days': self.raw_retention // 86400}

    def _reserve_match_request(self, now):
        if self.match_provider != 'tomtom':
            return True
        period = self._month_key(now)
        with self.lock, self.connect() as db:
            row = db.execute('SELECT requests FROM observed_api_usage WHERE period=? AND provider=?',
                             (period, 'tomtom')).fetchone()
            used = int(row['requests']) if row else 0
            if used >= self.monthly_match_limit:
                return False
            db.execute('''INSERT INTO observed_api_usage(period,provider,requests,updated)
                          VALUES(?,?,1,?)
                          ON CONFLICT(period,provider) DO UPDATE SET
                          requests=requests+1, updated=excluded.updated''',
                       (period, 'tomtom', now))
        return True

    def _trim_pending(self, db):
        count = db.execute("SELECT COUNT(*) FROM observed_jobs WHERE status='pending'").fetchone()[0]
        excess = count - self.max_pending_jobs
        if excess > 0:
            db.execute('''DELETE FROM observed_jobs WHERE id IN (
                SELECT id FROM observed_jobs WHERE status='pending'
                ORDER BY seen ASC LIMIT ?)''', (excess,))

    @staticmethod
    def _record_audit(db, job_id, provider, result, metrics, created):
        db.execute('''INSERT OR REPLACE INTO observed_match_audit
                      (job,provider,result,metrics,created) VALUES(?,?,?,?,?)''',
                   (job_id, provider, result, json.dumps(metrics or {}), created))

    def save_official(self, line, data):
        index = OfficialIndex(data)
        with self.lock, self.connect() as db:
            # An empty successful response must not erase a previously known path.
            if not index.cells and line in self.indexes and self.indexes[line].cells:
                db.execute('UPDATE observed_official SET checked=? WHERE line=?', (time.time(), line))
                return
            db.execute('INSERT OR REPLACE INTO observed_official VALUES(?,?,?)', (line, json.dumps(data), time.time()))
            self.indexes[line] = index

    def _queue(self, db, line, unit, state):
        points = state.get('tail', [])
        if len(points) < 3 or length(points) < 100: return
        passage = state['passage']
        key = hashlib.sha256(f'{line}|{unit}|{passage}|{points[-1][2]}'.encode()).hexdigest()
        inserted = db.execute('''INSERT OR IGNORE INTO observed_jobs
            (id,line,unit,route,passage,kind,points,seen) VALUES(?,?,?,?,?,?,?,?)''',
            (key, line, unit, state.get('route', ''), passage, state['kind'], json.dumps(points), points[-1][2]))
        if inserted.rowcount:
            self._trim_pending(db)

    def observe(self, line, units, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            index = self.indexes.get(line)
            for unit in units:
                try:
                    uid = str(unit['unit'])
                    p = [float(unit['lat']), float(unit['lon'])]
                    if not uid or not all(math.isfinite(v) for v in p) or not (-90<p[0]<90 and -180<p[1]<180): continue
                    stamp = sample_time(unit, now)
                    if stamp is None: continue
                    route = str(unit.get('route') or '')[:160]
                    row = db.execute('SELECT state FROM observed_state WHERE line=? AND unit=?', (line, uid)).fetchone()
                    state = json.loads(row['state']) if row else {}
                    prev = state.get('prev')
                    if prev and (stamp <= prev[2] or distance(p, prev) < 12): continue
                    cur = p+[stamp]
                    if gps_spike(state.get('prev_prev'), prev, cur):
                        # Replace the isolated jump B with the plausible A -> C
                        # continuation before it can become durable evidence.
                        spike_stamp = prev[2]
                        db.execute('DELETE FROM observed_samples WHERE line=? AND unit=? AND stamp=?',
                                   (line, uid, spike_stamp))
                        for field in ('candidate', 'tail'):
                            if state.get(field):
                                state[field] = [point for point in state[field] if point[2] != spike_stamp]
                        prev = state.get('prev_prev')
                        state['prev'] = prev
                        state.pop('prev_prev', None)
                        state['streak'] = max(0, state.get('streak', 0)-1)
                        state['inside'] = 0
                    if db.execute('INSERT OR IGNORE INTO observed_samples VALUES(?,?,?,?,?,?)',
                                  (line, uid, stamp, *p, route)).rowcount == 0: continue
                    step = distance(p, prev) if prev else 0
                    elapsed = stamp-prev[2] if prev else 0
                    same_route = branch_key(route) == branch_key(state.get('route'))
                    broken = prev and (elapsed > GAP or step > 350 or
                                        step/elapsed > 35 or not same_route)
                    suspected_origin = (prev if broken and elapsed <= GAP and same_route and
                                        (step > 350 or step/elapsed > 35) else None)
                    if broken:
                        self._queue(db, line, uid, state)
                        state = {}; prev = None
                    if index is None:
                        # A failed official download is not proof of a missing route.
                        state = {'prev': cur, 'prev_prev': prev or suspected_origin, 'route': route}
                    else:
                        missing = not index.cells
                        dist = index.measure(p, route) if not missing else math.inf
                        outside = missing or dist > 70
                        state['streak'] = state.get('streak', 0)+1 if outside else 0
                        if not state.get('off') and outside:
                            state.setdefault('candidate', []).append(cur)
                            if state['streak'] >= 3:
                                state.update(off=True, tail=state.pop('candidate'), passage=f'{uid}:{stamp}',
                                             kind='observed' if missing else 'alternative', route=route)
                        elif state.get('off'):
                            state.setdefault('tail', []).append(cur)
                            state['inside'] = state.get('inside', 0)+1 if dist < 40 else 0
                            if state['inside'] >= 2:
                                self._queue(db, line, uid, state)
                                state = {}
                            elif len(state['tail']) >= 24:
                                self._queue(db, line, uid, state)
                                state['tail'] = state['tail'][-3:]
                        else:
                            state.pop('candidate', None)
                        state.update(prev_prev=prev or suspected_origin, prev=cur, route=route)
                    db.execute('INSERT OR REPLACE INTO observed_state VALUES(?,?,?,?)',
                               (line, uid, json.dumps(state), stamp))
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue

    def flush_idle(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            for row in db.execute('SELECT * FROM observed_state WHERE updated<?', (now-GAP,)).fetchall():
                state = json.loads(row['state'])
                self._queue(db, row['line'], row['unit'], state)
                # Keep the last accepted timestamp across restarts and stale responses.
                state = {'prev': state.get('prev'), 'route': state.get('route', '')}
                db.execute('UPDATE observed_state SET state=? WHERE line=? AND unit=?',
                           (json.dumps(state), row['line'], row['unit']))

    def snapshot(self, line, history=False, now=None):
        now = time.time() if now is None else now
        cache_key = (line, history)
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and now-cached[0] < 15: return cached[1]
            with self.connect() as db:
                edges = db.execute('''SELECT e.*,
                    COUNT(DISTINCT CASE WHEN p.seen>=? THEN p.unit END) AS buses,
                    COUNT(CASE WHEN p.seen>=? THEN 1 END) AS passes,
                    COUNT(DISTINCT p.unit) AS historical_buses
                    FROM observed_edges e JOIN observed_passages p ON p.edge=e.id
                    WHERE e.line=? AND (? OR e.last_seen>=?) GROUP BY e.id
                    HAVING ? OR
                      COUNT(DISTINCT CASE WHEN p.seen>=? THEN p.unit END)>=? OR
                      COUNT(CASE WHEN p.seen>=? THEN 1 END)>=?
                    ORDER BY e.last_seen DESC LIMIT 12000''',
                    (now-WINDOW, now-WINDOW, line, int(history), now-WINDOW,
                     int(history), now-WINDOW, self.min_confirmed_buses,
                     now-WINDOW, self.min_confirmed_passes)).fetchall()
                alternatives = [dict(id=r['id'], points=json.loads(r['points']), route=r['route'],
                    kind=r['kind'], buses=r['buses'], passes=r['passes'], historical_buses=r['historical_buses'],
                    last_seen=r['last_seen'], archived=r['last_seen']<now-WINDOW) for r in edges]
                pending = [dict(id=r['id'], points=json.loads(r['points']), route=r['route'], unit=r['unit'],
                                kind=r['kind'], last_seen=r['seen']) for r in db.execute(
                    "SELECT * FROM observed_jobs WHERE line=? AND status='pending' AND seen>=? ORDER BY seen DESC LIMIT 120",
                    (line, now-WINDOW))]
                tails = []
                for row in db.execute('SELECT * FROM observed_state WHERE line=? AND updated>=?', (line, now-180)):
                    state = json.loads(row['state'])
                    if state.get('off') and state.get('tail'):
                        tails.append(dict(unit=row['unit'], points=state['tail'], route=state.get('route', '')))
            value = dict(success=True, line=line, alternatives=alternatives, pending=pending, tails=tails,
                         window_days=14, matching_enabled=self.matching_enabled(),
                         matching_provider=self.match_provider, shadow_mode=self.shadow_mode,
                         minimum_confirmed_buses=self.min_confirmed_buses,
                         minimum_confirmed_passes=self.min_confirmed_passes,
                         truncated=len(edges)>=12000)
            self.cache[cache_key] = (now, value)
            return value

    def match_geometry(self, points):
        coords = ';'.join(f'{p[1]:.6f},{p[0]:.6f}' for p in points)
        stamps = ';'.join(str(int(p[2])) for p in points)
        url = f'{self.osrm_url}/match/v1/driving/{coords}?geometries=geojson&overview=full&timestamps={stamps}&radiuses=' + ';'.join(['15']*len(points)) + '&gaps=split&tidy=true'
        req = urllib.request.Request(url, headers={'User-Agent': 'JahaObservedRoutes/1.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
        if data.get('code') != 'Ok': raise ValueError('Map matching: '+str(data.get('code')))
        parts = []
        covered = 0
        for index, match in enumerate(data.get('matchings', [])):
            if match.get('confidence', 0) < .8: continue
            source = [p for p, tr in zip(points, data.get('tracepoints', []))
                      if tr and tr.get('matchings_index') == index]
            geom = match.get('geometry') or {}
            if geom.get('type') != 'LineString' or len(source) < 3: continue
            path = [[p[1], p[0]] for p in geom.get('coordinates', [])]
            if len(path)<2 or length(path) > max(150, length(source)*2): continue
            if any(min(point_distance(p, a, b) for a, b in zip(path, path[1:])) > 35 for p in source): continue
            if any(min(point_distance(p, a, b) for a, b in zip(source, source[1:])) > 120 for p in path): continue
            parts.append(path)
            covered += len(source)
        if not parts or covered < len(points)*.9:
            raise ValueError('Sin ajuste confiable; se conserva la evidencia GPS')
        return parts

    def process_one(self, matcher=None, now=None):
        now = time.time() if now is None else now
        if not self.matching_enabled() and matcher is None: return False
        with self.lock, self.connect() as db:
            job = db.execute("SELECT * FROM observed_jobs WHERE status='pending' AND next_try<=? ORDER BY seen LIMIT 1", (now,)).fetchone()
            if not job: return False
            jobs = [job]
            points_by_stamp = {point[2]: point for point in json.loads(job['points'])}
            # Consecutive chunks from the same bus passage overlap by three
            # points. Matching them together spends one request and produces a
            # more coherent geometry without combining different trips.
            for candidate in db.execute('''SELECT * FROM observed_jobs
                    WHERE status='pending' AND next_try<=? AND id!=?
                      AND line=? AND unit=? AND passage=? AND route=? AND kind=?
                    ORDER BY seen LIMIT 12''',
                    (now, job['id'], job['line'], job['unit'], job['passage'],
                     job['route'], job['kind'])).fetchall():
                candidate_points = json.loads(candidate['points'])
                new_stamps = {point[2] for point in candidate_points} - points_by_stamp.keys()
                if len(points_by_stamp) + len(new_stamps) > 180:
                    break
                jobs.append(candidate)
                for point in candidate_points:
                    points_by_stamp[point[2]] = point
            points = [points_by_stamp[stamp] for stamp in sorted(points_by_stamp)]
            db.executemany("UPDATE observed_jobs SET status='processing',next_try=? WHERE id=?",
                           [(now+300, row['id']) for row in jobs])
            provider_identity = ('custom' if matcher else
                                 TomTomMatcher.ENDPOINT if self.match_provider == 'tomtom' else self.osrm_url)
            signature = hashlib.sha256((provider_identity+json.dumps(
                [[p[0],p[1],p[2]-points[0][2]] for p in points])).encode()).hexdigest()
            cached = db.execute('SELECT geometry FROM observed_match_cache WHERE key=? AND created>=?', (signature, now-30*86400)).fetchone()
        provider = 'custom' if matcher is not None else self.match_provider
        quality = {'input_points': len(points)}
        try:
            if cached:
                parts = json.loads(cached[0])
                quality['cache_hit'] = True
            else:
                if matcher is not None:
                    selected_matcher = matcher
                elif self.match_provider == 'tomtom':
                    if not self._reserve_match_request(now):
                        raise MatchBudgetExhausted('Presupuesto mensual TomTom agotado')
                    selected_matcher = self.tomtom_matcher
                else:
                    selected_matcher = self.match_geometry
                parts = selected_matcher(points)
                if selected_matcher is self.tomtom_matcher:
                    quality = dict(self.tomtom_matcher.last_quality)
                else:
                    quality.update(route_elements=len(parts), cache_hit=False)
            with self.lock, self.connect() as db:
                db.execute('INSERT OR REPLACE INTO observed_match_cache VALUES(?,?,?)', (signature, json.dumps(parts), now))
                self._record_audit(db, job['id'], provider, 'accepted', quality, now)
                if not self.shadow_mode:
                    index = self.indexes.get(job['line'])
                    for part in parts:
                        for geom_key, edge in street_edges(part):
                            middle = [(edge[0][i]+edge[1][i])/2 for i in (0, 1)]
                            if index and index.cells and index.measure(middle, job['route']) < 40: continue
                            identity = f"{job['line']}|{branch_key(job['route'])}|{geom_key}"
                            key = hashlib.sha256(identity.encode()).hexdigest()
                            db.execute('''INSERT INTO observed_edges VALUES(?,?,?,?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen)''',
                                (key,job['line'],branch_key(job['route']),job['route'],job['kind'],json.dumps(edge),job['seen']))
                            db.execute('''INSERT INTO observed_passages VALUES(?,?,?,?)
                                ON CONFLICT(edge,unit,passage) DO UPDATE SET seen=MAX(seen, excluded.seen)''',
                                (key,job['unit'],job['passage'],job['seen']))
                retained_points = job['points'] if self.shadow_mode else '[]'
                db.execute("UPDATE observed_jobs SET status='done',geometry=?,points=?,error='' WHERE id=?",
                           (json.dumps(parts), json.dumps(points) if self.shadow_mode else retained_points, job['id']))
                for merged in jobs[1:]:
                    db.execute("UPDATE observed_jobs SET status='merged',geometry='[]',points='[]',error=? WHERE id=?",
                               (f"merged:{job['id']}", merged['id']))
                    db.execute('DELETE FROM observed_match_audit WHERE job=?', (merged['id'],))
                self.cache.clear()
            return True
        except MatchBudgetExhausted as exc:
            current = datetime.fromtimestamp(now, timezone.utc)
            next_month = (current.replace(day=28) + timedelta(days=4)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
            with self.lock, self.connect() as db:
                db.executemany("UPDATE observed_jobs SET status='pending',next_try=?,error=? WHERE id=?",
                               [(next_month, str(exc), row['id']) for row in jobs])
            self.last_error = str(exc)
            return False
        except Exception as exc:
            with self.lock, self.connect() as db:
                if self.tomtom_matcher is not None and provider == 'tomtom':
                    quality = dict(self.tomtom_matcher.last_quality)
                self._record_audit(db, job['id'], provider, 'rejected', quality, now)
                for failed in jobs:
                    attempts = failed['attempts']+1
                    status = 'failed' if attempts >= self.max_job_attempts else 'pending'
                    db.execute('UPDATE observed_jobs SET attempts=?, status=?, next_try=?, error=? WHERE id=?',
                               (attempts, status, now+min(86400, 60*2**min(attempts,10)),
                                str(exc)[:200], failed['id']))
            self.last_error = str(exc)[:200]
            return False

    def maintain(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            # Raw positions and unprocessed chunks expire. Confirmed aggregate
            # street edges/passages remain as the compact long-term evidence.
            db.execute('DELETE FROM observed_samples WHERE stamp<?', (now-self.raw_retention,))
            db.execute("DELETE FROM observed_jobs WHERE status IN ('pending','failed') AND seen<?",
                       (now-self.raw_retention,))
            db.execute("UPDATE observed_jobs SET status='pending' WHERE status='processing' AND next_try<=?",
                       (now,))
            db.execute("UPDATE observed_jobs SET points='[]' WHERE status='done' AND seen<? AND points!='[]'",
                       (now-self.raw_retention,))
            db.execute("DELETE FROM observed_jobs WHERE status IN ('done','merged') AND seen<?", (now-30*86400,))
            db.execute('DELETE FROM observed_match_cache WHERE created<?', (now-30*86400,))
            db.execute('DELETE FROM observed_match_audit WHERE created<?', (now-30*86400,))
            db.execute('DELETE FROM observed_state WHERE updated<?', (now-self.raw_retention,))
            db.execute('DELETE FROM observed_api_usage WHERE updated<?', (now-400*86400,))
            self._trim_pending(db)

    def collector(self, catalog, positions, paths):
        from concurrent.futures import ThreadPoolExecutor
        next_catalog = 0; lines = []; last_maintenance = 0
        def collect(line):
            lid = str(line['id'])
            try:
                with self.connect() as db:
                    row = db.execute('SELECT checked FROM observed_official WHERE line=?', (lid,)).fetchone()
                if (not row or time.time()-row[0] > 6*3600) and time.time() >= self.path_retry.get(lid, 0):
                    self.path_retry[lid] = time.time()+300
                    data = paths(lid)
                    if data is not None: self.save_official(lid, data)
                positions(lid)  # The shared positions callback records each sample once.
            except Exception as exc:
                self.last_error = f'{lid}: {exc}'[:200]
        with ThreadPoolExecutor(max_workers=2) as executor:
            while not self.stop.is_set():
                start = time.time()
                try:
                    if start >= next_catalog:
                        fresh = catalog()
                        if fresh: lines = fresh
                        next_catalog = start+1800
                    list(executor.map(collect, lines))
                    self.flush_idle()
                    if start-last_maintenance>3600:
                        self.maintain(); last_maintenance=start
                    self.last_cycle = time.time()
                except Exception as exc:
                    self.last_error = str(exc)[:200]
                self.stop.wait(max(1, self.poll_seconds-(time.time()-start)))

    def run_match_batch(self, limit=None, now=None):
        """Process a bounded batch; cached results do not consume API budget."""
        limit = self.weekly_match_limit if limit is None else max(0, int(limit))
        attempted = completed = 0
        while attempted < limit and not self.stop.is_set():
            stamp = time.time() if now is None else now
            if self.match_provider == 'tomtom' and self.remaining_match_budget(stamp) <= 0:
                break
            with self.lock, self.connect() as db:
                eligible = db.execute("SELECT 1 FROM observed_jobs WHERE status='pending' AND next_try<=? LIMIT 1",
                                      (stamp,)).fetchone()
            if not eligible:
                break
            attempted += 1
            if self.process_one(now=stamp):
                completed += 1
        return {'attempted': attempted, 'completed': completed}

    def _seconds_until_weekly_run(self, now=None):
        now = time.time() if now is None else now
        local_now = datetime.fromtimestamp(now, MATCH_TIMEZONE)
        days = (self.match_weekday-local_now.weekday()) % 7
        target = (local_now + timedelta(days=days)).replace(
            hour=self.match_hour, minute=0, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=7)
        return max(1, target.timestamp()-now)

    def matcher_loop(self):
        if self.match_provider == 'tomtom':
            if self.match_run_on_start and not self.stop.is_set():
                try: self.run_match_batch()
                except Exception as exc: self.last_error = str(exc)[:200]
            while not self.stop.is_set():
                # Wake hourly so shutdowns and configuration changes are not
                # held by one week-long wait.
                until_run = self._seconds_until_weekly_run()
                if self.stop.wait(min(3600, until_run)):
                    break
                if until_run <= 3600:
                    try: self.run_match_batch()
                    except Exception as exc: self.last_error = str(exc)[:200]
            return
        while not self.stop.is_set():
            try: self.process_one()
            except Exception as exc: self.last_error = str(exc)[:200]
            self.stop.wait(10)  # one global bounded queue, independent of visitors
