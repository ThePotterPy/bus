"""Temporary, privacy-preserving shared bus and stop sessions."""

import hashlib
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULT_TRIP_SECONDS = 2 * 60 * 60
MAX_TRIP_SECONDS = 3 * 60 * 60
STOP_SHARE_SECONDS = 60 * 60
HEARTBEAT_STALE_SECONDS = 90
HEARTBEAT_END_SECONDS = 15 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SharedTripStore:
    """Stores only trip state. Passenger coordinates are never persisted."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._last_prune = 0.0

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        with self.lock, self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_trips (
                    public_hash TEXT PRIMARY KEY,
                    owner_hash TEXT NOT NULL,
                    client_hash TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    line_id TEXT,
                    unit_id TEXT,
                    stop_id TEXT,
                    destination_stop_id TEXT,
                    started_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    max_expires_at REAL NOT NULL,
                    last_heartbeat REAL,
                    last_verified_at REAL,
                    away_since REAL,
                    away_count INTEGER NOT NULL DEFAULT 0,
                    extended INTEGER NOT NULL DEFAULT 0,
                    end_reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS shared_trips_expiry
                    ON shared_trips(expires_at, updated_at);
            """)

    @staticmethod
    def _tokens():
        return secrets.token_urlsafe(24), secrets.token_urlsafe(32)

    def create(self, client_id, *, intent, line_id=None, unit_id=None,
               stop_id=None, destination_stop_id=None, now=None):
        now = time.time() if now is None else float(now)
        public_token, owner_token = self._tokens()
        on_bus = bool(line_id and unit_id)
        duration = DEFAULT_TRIP_SECONDS if on_bus else STOP_SHARE_SECONDS
        status = "on_bus" if on_bus else ("waiting" if intent == "waiting" else "planning")
        with self.lock, self.connect() as db:
            # One active share per browser identity; previous links stop cleanly.
            db.execute("""UPDATE shared_trips SET status='ended',end_reason='replaced',updated_at=?
                          WHERE client_hash=? AND status!='ended'""",
                       (now, _digest(client_id)))
            db.execute("""INSERT INTO shared_trips (
                public_hash,owner_hash,client_hash,intent,status,line_id,unit_id,
                stop_id,destination_stop_id,started_at,expires_at,max_expires_at,
                last_heartbeat,last_verified_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                _digest(public_token), _digest(owner_token), _digest(client_id), intent,
                status, line_id, unit_id, stop_id, destination_stop_id, now,
                now + duration, now + MAX_TRIP_SECONDS,
                now if on_bus else None, now if on_bus else None, now,
            ))
        self.prune(now)
        return {
            "publicToken": public_token,
            "ownerToken": owner_token,
            "expiresAt": now + duration,
            "maxExpiresAt": now + MAX_TRIP_SECONDS,
        }

    def _row(self, db, public_token):
        if not isinstance(public_token, str) or not 20 <= len(public_token) <= 128:
            return None
        return db.execute("SELECT * FROM shared_trips WHERE public_hash=?",
                          (_digest(public_token),)).fetchone()

    @staticmethod
    def _owns(row, owner_token):
        return bool(row and isinstance(owner_token, str) and
                    secrets.compare_digest(row["owner_hash"], _digest(owner_token)))

    def get(self, public_token, now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not row:
                return None
            if row["status"] != "ended" and now >= row["expires_at"]:
                db.execute("UPDATE shared_trips SET status='ended',end_reason='expired',updated_at=? WHERE public_hash=?",
                           (now, row["public_hash"]))
                row = self._row(db, public_token)
            elif (row["status"] == "on_bus" and row["last_heartbeat"] and
                  now - row["last_heartbeat"] >= HEARTBEAT_END_SECONDS):
                db.execute("UPDATE shared_trips SET status='ended',end_reason='signal_lost',updated_at=? WHERE public_hash=?",
                           (now, row["public_hash"]))
                row = self._row(db, public_token)
        result = dict(row)
        for key in ("public_hash", "owner_hash", "client_hash"):
            result.pop(key, None)
        if result["status"] == "on_bus":
            verified_age = now - (result.get("last_verified_at") or result["started_at"])
            if result.get("away_count"):
                result["verification"] = "checking"
            else:
                result["verification"] = "unavailable" if verified_age >= HEARTBEAT_STALE_SECONDS else "confirmed"
        else:
            result["verification"] = None
        return result

    def get_owned(self, public_token, owner_token, now=None):
        """Return the public trip state only when the owner secret matches."""
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token):
                return None
        return self.get(public_token, now=now)

    def board(self, public_token, owner_token, line_id, unit_id, destination_stop_id=None, now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token) or row["status"] == "ended":
                return False
            db.execute("""UPDATE shared_trips SET status='on_bus',intent='trip',line_id=?,unit_id=?,
                          destination_stop_id=COALESCE(?,destination_stop_id),last_heartbeat=?,
                          last_verified_at=?,away_since=NULL,away_count=0,
                          expires_at=MIN(?,max_expires_at),updated_at=? WHERE public_hash=?""",
                       (line_id, unit_id, destination_stop_id, now, now,
                        now + DEFAULT_TRIP_SECONDS, now, row["public_hash"]))
            return True

    def heartbeat(self, public_token, owner_token, *, reliable, distance_m=None, now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token) or row["status"] != "on_bus":
                return None
            away_count, away_since = row["away_count"], row["away_since"]
            status, reason = row["status"], row["end_reason"]
            last_verified = row["last_verified_at"]
            if reliable and distance_m is not None:
                if distance_m <= 700:
                    away_count, away_since, last_verified = 0, None, now
                else:
                    away_count += 1
                    away_since = away_since or now
                    if away_count >= 3 and now - away_since >= 60:
                        status, reason = "ended", "left_bus"
            db.execute("""UPDATE shared_trips SET status=?,end_reason=?,last_heartbeat=?,
                          last_verified_at=?,away_since=?,away_count=?,updated_at=?
                          WHERE public_hash=?""", (
                status, reason, now, last_verified, away_since, away_count, now,
                row["public_hash"],
            ))
        return {"status": status, "distanceMeters": round(distance_m) if distance_m is not None else None}

    def set_destination(self, public_token, owner_token, stop_id, now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token) or row["status"] == "ended":
                return False
            db.execute("UPDATE shared_trips SET destination_stop_id=?,updated_at=? WHERE public_hash=?",
                       (stop_id, now, row["public_hash"]))
            return True

    def extend(self, public_token, owner_token, now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token) or row["status"] == "ended" or row["extended"]:
                return False
            expires = min(row["max_expires_at"], row["expires_at"] + 3600)
            db.execute("UPDATE shared_trips SET expires_at=?,extended=1,updated_at=? WHERE public_hash=?",
                       (expires, now, row["public_hash"]))
            return True

    def end(self, public_token, owner_token, reason="owner_ended", now=None):
        now = time.time() if now is None else float(now)
        with self.lock, self.connect() as db:
            row = self._row(db, public_token)
            if not self._owns(row, owner_token):
                return False
            db.execute("UPDATE shared_trips SET status='ended',end_reason=?,updated_at=? WHERE public_hash=?",
                       (reason, now, row["public_hash"]))
            return True

    def prune(self, now=None):
        now = time.time() if now is None else float(now)
        if now - self._last_prune < 300:
            return
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM shared_trips WHERE (status='ended' AND updated_at<?) OR max_expires_at<?",
                       (now - 86400, now - 86400))
        self._last_prune = now
