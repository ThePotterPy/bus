"""Private feedback storage and short-lived administrator sessions."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SESSION_SECONDS = 12 * 3600
COMMENT_RETENTION_SECONDS = 180 * 86400
DIAGNOSTIC_RETENTION_SECONDS = 30 * 86400
CATEGORIES = {"error", "ruta", "sugerencia", "otro"}
DIAGNOSTIC_FIELDS = {"device", "platform", "browser", "viewport", "app"}
LINE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


NEWS_TAGS = {"novedad", "mejora", "aviso", "recorrido"}


def validate_news(body):
    if not isinstance(body, dict):
        raise ValueError("datos de novedad inválidos")
    title = body.get("title")
    content = body.get("content")
    tag = body.get("tag", "novedad")
    if not isinstance(title, str) or not 3 <= len(title.strip()) <= 120:
        raise ValueError("el título debe tener entre 3 y 120 caracteres")
    if not isinstance(content, str) or not 5 <= len(content.strip()) <= 5000:
        raise ValueError("el contenido debe tener entre 5 y 5000 caracteres")
    if not isinstance(tag, str) or tag.strip().lower() not in NEWS_TAGS:
        raise ValueError("etiqueta inválida")
    return {
        "title": title.strip(),
        "content": content.strip(),
        "tag": tag.strip().lower(),
    }


def validate_feedback(body):
    """Return a small, explicit record. Never persist request headers or IPs."""
    if not isinstance(body, dict) or body.get("website"):
        raise ValueError("comentario inválido")
    name = body.get("name", "")
    message = body.get("message")
    category = body.get("category")
    line_id = body.get("lineId", "")
    consent = body.get("diagnosticsConsent", False)
    if not isinstance(name, str) or len(name.strip()) > 80:
        raise ValueError("el nombre supera 80 caracteres")
    if not isinstance(message, str) or not 5 <= len(message.strip()) <= 3000:
        raise ValueError("el comentario debe tener entre 5 y 3000 caracteres")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("categoría inválida")
    if not isinstance(line_id, str) or (line_id and not LINE_ID.fullmatch(line_id)):
        raise ValueError("línea inválida")
    if not isinstance(consent, bool):
        raise ValueError("consentimiento inválido")
    diagnostics = None
    if consent:
        raw = body.get("diagnostics")
        if not isinstance(raw, dict) or set(raw) - DIAGNOSTIC_FIELDS:
            raise ValueError("datos técnicos inválidos")
        diagnostics = {}
        for key, value in raw.items():
            if not isinstance(value, str) or len(value) > 40 or not value.isprintable():
                raise ValueError("datos técnicos inválidos")
            diagnostics[key] = value
    return {
        "name": name.strip(),
        "message": message.strip(),
        "category": category,
        "line_id": line_id,
        "diagnostics_json": json.dumps(diagnostics, ensure_ascii=False) if diagnostics is not None else None,
        "consent_version": "1" if consent else None,
    }


class FeedbackStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._prune_lock = threading.Lock()
        self._last_prune = 0

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                line_id TEXT NOT NULL,
                message TEXT NOT NULL,
                diagnostics_json TEXT,
                consent_version TEXT,
                delete_token_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'nuevo'
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS feedback_status_id ON feedback(status, id DESC)")
            conn.execute("""CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tag TEXT NOT NULL DEFAULT 'novedad'
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS news_created_at ON news(created_at DESC)")
            conn.execute("""CREATE TABLE IF NOT EXISTS news_read_cursors (
                client_hash TEXT PRIMARY KEY,
                news_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS news_read_cursors_updated ON news_read_cursors(updated_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS release_news (
                release_key TEXT PRIMARY KEY,
                news_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                csrf TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )""")

    def prune(self):
        now = int(time.time())
        with self._prune_lock:
            if now - self._last_prune < 3600:
                return
            with self._connect() as conn:
                conn.execute("DELETE FROM feedback WHERE created_at < ?", (now - COMMENT_RETENTION_SECONDS,))
                conn.execute("""UPDATE feedback SET diagnostics_json = NULL
                    WHERE created_at < ? AND diagnostics_json IS NOT NULL""",
                    (now - DIAGNOSTIC_RETENTION_SECONDS,))
                conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
            self._last_prune = now

    def create(self, record):
        self.prune()
        delete_token = secrets.token_urlsafe(32)
        delete_token_hash = hashlib.sha256(delete_token.encode()).hexdigest()
        with self._connect() as conn:
            cursor = conn.execute("""INSERT INTO feedback
                (created_at, name, category, line_id, message, diagnostics_json, consent_version, delete_token_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
                int(time.time()), record["name"], record["category"], record["line_id"],
                record["message"], record["diagnostics_json"], record["consent_version"], delete_token_hash,
            ))
            return cursor.lastrowid, delete_token

    def list(self, status="todos", before=None, limit=50):
        self.prune()
        filters = []
        values = []
        if status != "todos":
            filters.append("status = ?")
            values.append(status)
        if before is not None:
            filters.append("id < ?")
            values.append(before)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, name, category, line_id, message, diagnostics_json, consent_version, status FROM feedback"
                + where + " ORDER BY id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            raw_diagnostics = item.pop("diagnostics_json")
            item["diagnostics"] = json.loads(raw_diagnostics) if raw_diagnostics else None
            items.append(item)
        return items, items[-1]["id"] if has_more else None

    def set_status(self, feedback_id, status):
        if status not in {"nuevo", "revisado"}:
            return False
        with self._connect() as conn:
            return conn.execute(
                "UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id)
            ).rowcount == 1

    def delete(self, feedback_id):
        with self._connect() as conn:
            return conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,)).rowcount == 1

    def delete_with_code(self, code):
        if not isinstance(code, str):
            return False
        match = re.fullmatch(r"C-(\d{6,12})-([A-Za-z0-9_-]{40,50})", code.strip())
        if not match:
            return False
        feedback_id = int(match.group(1))
        token_hash = hashlib.sha256(match.group(2).encode()).hexdigest()
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM feedback WHERE id = ? AND delete_token_hash = ?",
                (feedback_id, token_hash),
            ).rowcount == 1

    def create_news(self, record):
        with self._connect() as conn:
            cursor = conn.execute("""INSERT INTO news
                (created_at, title, content, tag)
                VALUES (?, ?, ?, ?)""", (
                int(time.time()), record["title"], record["content"], record["tag"],
            ))
            return cursor.lastrowid

    def publish_release_news(self, release_key, record):
        """Publish a deployment announcement once, even across restarts."""
        if not isinstance(release_key, str) or not re.fullmatch(r"[a-z0-9_-]{3,80}", release_key):
            raise ValueError("identificador de versión inválido")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT news_id FROM release_news WHERE release_key = ?", (release_key,),
            ).fetchone()
            if existing:
                return existing["news_id"], False
            created_at = int(time.time())
            cursor = conn.execute("""INSERT INTO news
                (created_at, title, content, tag) VALUES (?, ?, ?, ?)""", (
                created_at, record["title"], record["content"], record["tag"],
            ))
            conn.execute("""INSERT INTO release_news (release_key, news_id, created_at)
                VALUES (?, ?, ?)""", (release_key, cursor.lastrowid, created_at))
            return cursor.lastrowid, True

    def list_news(self, limit=30):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, title, content, tag FROM news ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _news_client_hash(client_id):
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("identificador de dispositivo inválido")
        return hashlib.sha256(client_id.encode("utf-8")).hexdigest()

    def get_news_read_id(self, client_id):
        client_hash = self._news_client_hash(client_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT news_id FROM news_read_cursors WHERE client_hash = ?",
                (client_hash,),
            ).fetchone()
            return int(row["news_id"]) if row else 0

    def mark_news_read(self, client_id, news_id):
        """Advance a device's news cursor without allowing it to move backward."""
        client_hash = self._news_client_hash(client_id)
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("DELETE FROM news_read_cursors WHERE updated_at < ?", (now - 400 * 86400,))
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM news").fetchone()["id"]
            bounded_id = min(max(0, int(news_id)), int(latest))
            conn.execute("""INSERT INTO news_read_cursors (client_hash, news_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(client_hash) DO UPDATE SET
                    news_id = MAX(news_read_cursors.news_id, excluded.news_id),
                    updated_at = excluded.updated_at""", (client_hash, bounded_id, now))
            row = conn.execute(
                "SELECT news_id FROM news_read_cursors WHERE client_hash = ?",
                (client_hash,),
            ).fetchone()
            return int(row["news_id"])

    def delete_news(self, news_id):
        with self._connect() as conn:
            return conn.execute("DELETE FROM news WHERE id = ?", (news_id,)).rowcount == 1

    def login(self, password, configured_password):
        if not configured_password or len(configured_password) < 16:
            return None
        if not isinstance(password, str) or not hmac.compare_digest(password.encode(), configured_password.encode()):
            return None
        self.prune()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = self._session_hash(token, configured_password)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_sessions (token_hash, csrf, expires_at) VALUES (?, ?, ?)",
                (token_hash, csrf, int(time.time()) + SESSION_SECONDS),
            )
        return token

    @staticmethod
    def _session_hash(token, configured_password):
        return hashlib.sha256(configured_password.encode() + b"\0" + token.encode()).hexdigest()

    def session(self, token, configured_password):
        if not token or len(token) > 128 or not configured_password:
            return None
        token_hash = self._session_hash(token, configured_password)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT csrf FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, int(time.time())),
            ).fetchone()
        return row["csrf"] if row else None

    def logout(self, token, configured_password):
        if token and len(token) <= 128 and configured_password:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?",
                    (self._session_hash(token, configured_password),),
                )
