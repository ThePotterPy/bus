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


WINDOW = 14 * 86400
GAP = 90
ASUNCION = timezone(timedelta(hours=-3))


def branch_key(name):
    text = unicodedata.normalize('NFD', str(name or '')).lower()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'\((?:i|v|ida|vuelta)\)', ' ', text)
    text = re.sub(r'(?:^|[\s_-])(?:ida|vuelta)(?=$|[\s_-])', ' ', text)
    # Keep route numbers: distinct numbered branches must not be merged.
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
    """Reject stale provider data; never reinterpret an offline bus as moving."""
    if str(unit.get('online', '1')).lower() in ('0', 'false'):
        return None
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
    def __init__(self, path, osrm_url='', poll_seconds=30):
        self.path = str(path)
        self.osrm_url = osrm_url.rstrip('/')
        self.poll_seconds = max(15, poll_seconds)
        self.lock = threading.RLock()
        self.indexes = {}
        self.stop = threading.Event()
        self.cache = {}
        self.last_error = ''
        self.last_cycle = 0
        self.path_retry = {}

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
            ''')
            for row in db.execute('SELECT line, data FROM observed_official'):
                self.indexes[row['line']] = OfficialIndex(json.loads(row['data']))

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
        db.execute('''INSERT OR IGNORE INTO observed_jobs
            (id,line,unit,route,passage,kind,points,seen) VALUES(?,?,?,?,?,?,?,?)''',
            (key, line, unit, state.get('route', ''), passage, state['kind'], json.dumps(points), points[-1][2]))

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
                    if db.execute('INSERT OR IGNORE INTO observed_samples VALUES(?,?,?,?,?,?)',
                                  (line, uid, stamp, *p, route)).rowcount == 0: continue
                    broken = prev and (stamp-prev[2] > GAP or distance(p, prev) > 350 or
                                        distance(p, prev)/(stamp-prev[2]) > 35 or
                                        branch_key(route) != branch_key(state.get('route')))
                    if broken:
                        self._queue(db, line, uid, state)
                        state = {}; prev = None
                    if index is None:
                        # A failed official download is not proof of a missing route.
                        state = {'prev': cur, 'route': route}
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
                        state.update(prev=cur, route=route)
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
                    ORDER BY e.last_seen DESC LIMIT 12000''', (now-WINDOW, now-WINDOW, line, int(history), now-WINDOW)).fetchall()
                alternatives = [dict(id=r['id'], points=json.loads(r['points']), route=r['route'],
                    kind=r['kind'], buses=r['buses'], passes=r['passes'], historical_buses=r['historical_buses'],
                    last_seen=r['last_seen'], archived=r['last_seen']<now-WINDOW) for r in edges]
                pending = [dict(id=r['id'], points=json.loads(r['points']), route=r['route'], unit=r['unit'],
                                kind=r['kind'], last_seen=r['seen']) for r in db.execute(
                    "SELECT * FROM observed_jobs WHERE line=? AND status!='done' AND seen>=? ORDER BY seen DESC LIMIT 120",
                    (line, now-WINDOW))]
                tails = []
                for row in db.execute('SELECT * FROM observed_state WHERE line=? AND updated>=?', (line, now-180)):
                    state = json.loads(row['state'])
                    if state.get('off') and state.get('tail'):
                        tails.append(dict(unit=row['unit'], points=state['tail'], route=state.get('route', '')))
            value = dict(success=True, line=line, alternatives=alternatives, pending=pending, tails=tails,
                         window_days=14, matching_enabled=bool(self.osrm_url), truncated=len(edges)>=12000)
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
        if not self.osrm_url and matcher is None: return False
        with self.lock, self.connect() as db:
            job = db.execute("SELECT * FROM observed_jobs WHERE status='pending' AND next_try<=? ORDER BY seen LIMIT 1", (now,)).fetchone()
            if not job: return False
            points = json.loads(job['points'])
            signature = hashlib.sha256((self.osrm_url+json.dumps([[p[0],p[1],p[2]-points[0][2]] for p in points])).encode()).hexdigest()
            cached = db.execute('SELECT geometry FROM observed_match_cache WHERE key=? AND created>=?', (signature, now-30*86400)).fetchone()
        try:
            parts = json.loads(cached[0]) if cached else (matcher or self.match_geometry)(points)
            with self.lock, self.connect() as db:
                db.execute('INSERT OR REPLACE INTO observed_match_cache VALUES(?,?,?)', (signature, json.dumps(parts), now))
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
                db.execute("UPDATE observed_jobs SET status='done',geometry=?,error='' WHERE id=?", (json.dumps(parts),job['id']))
                self.cache.clear()
            return True
        except Exception as exc:
            with self.lock, self.connect() as db:
                attempts = job['attempts']+1
                db.execute('UPDATE observed_jobs SET attempts=?, next_try=?, error=? WHERE id=?',
                           (attempts, now+min(86400, 60*2**min(attempts,10)), str(exc)[:200], job['id']))
            self.last_error = str(exc)[:200]
            return False

    def maintain(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM observed_samples WHERE stamp<?', (now-WINDOW,))
            db.execute("DELETE FROM observed_jobs WHERE status='done' AND seen<?", (now-30*86400,))
            db.execute('DELETE FROM observed_match_cache WHERE created<?', (now-30*86400,))
            db.execute('DELETE FROM observed_state WHERE updated<?', (now-WINDOW,))
            # Failed jobs and aggregate historical evidence are not erased.

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

    def matcher_loop(self):
        while not self.stop.is_set():
            try: self.process_one()
            except Exception as exc: self.last_error = str(exc)[:200]
            self.stop.wait(10)  # one global bounded queue, independent of visitors
