"""Gestión y persistencia de reportes comunitarios de paradas de colectivo."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time

try:
    from PIL import Image
    from PIL.ImageOps import exif_transpose
except ImportError:  # pragma: no cover
    Image = None
    exif_transpose = None

# Bounding box oficial que cubre todo el territorio paraguayo
PARAGUAY_BOUNDS = {
    "min_lat": -27.60,   # sur (Encarnación / Itapúa)
    "max_lat": -19.28,   # norte (Alto Paraguay)
    "min_lon": -62.65,   # oeste (Chaco / Boquerón)
    "max_lon": -54.25,   # este (Alto Paraná / Canindeyú)
}

PHOTO_BUDGET_BYTES = 50 * 1024 * 1024  # 50 MB para fotos pendientes
MAX_PHOTO_DIMENSION = 1280
WEBP_QUALITY = 75
MAX_COMPRESSED_SIZE = 300 * 1024  # 300 KB máx post-compresión


def in_paraguay(lat: float, lon: float) -> bool:
    """Verifica si las coordenadas caen dentro de la República del Paraguay."""
    try:
        flat = float(lat)
        flon = float(lon)
    except (TypeError, ValueError):
        return False
    return (PARAGUAY_BOUNDS["min_lat"] <= flat <= PARAGUAY_BOUNDS["max_lat"]
            and PARAGUAY_BOUNDS["min_lon"] <= flon <= PARAGUAY_BOUNDS["max_lon"])


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula distancia en metros entre dos coordenadas esféricas."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def compress_photo(raw_bytes: bytes) -> bytes:
    """Comprime una imagen cruda (JPEG/PNG/WebP) a formato WebP optimizado."""
    if Image is None:
        raise RuntimeError("Pillow no está instalado para comprimir fotos")
    if not raw_bytes or len(raw_bytes) < 8:
        raise ValueError("Archivo de imagen vacío o corrupto")

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.verify()
        # verify() invalida el objeto en algunas versiones, reabrimos para procesar
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise ValueError(f"Formato de imagen inválido o corrupto: {exc}")

    if exif_transpose:
        try:
            img = exif_transpose(img)
        except Exception:
            pass

    if img.mode in ("RGBA", "P", "LA"):
        # Fondo blanco para transparencias
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > MAX_PHOTO_DIMENSION:
        ratio = MAX_PHOTO_DIMENSION / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=4)
    result = buf.getvalue()

    if len(result) > MAX_COMPRESSED_SIZE:
        buf2 = io.BytesIO()
        img.save(buf2, format="WEBP", quality=50, method=4)
        result = buf2.getvalue()

    return result


class StopReportStore:
    def __init__(self, db_path: Path, photos_dir: Path, community_stops_path: Path):
        self.db_path = Path(db_path)
        self.photos_dir = Path(photos_dir)
        self.community_stops_path = Path(community_stops_path)
        self._prune_lock = threading.Lock()
        self._last_prune = 0
        self._file_lock = threading.Lock()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        """Crea tablas, índices y directorios necesarios."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.community_stops_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.community_stops_path.exists():
            with self._file_lock:
                self.community_stops_path.write_text("[]\n", encoding="utf-8")

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS stop_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                accuracy REAL NOT NULL,
                photo_filename TEXT NOT NULL,
                photo_size INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                reporter_name TEXT NOT NULL DEFAULT '',
                push_subscription TEXT,
                street_name TEXT,
                street_side TEXT,
                status TEXT NOT NULL DEFAULT 'pendiente',
                rejection_reason TEXT,
                admin_notes TEXT,
                reviewed_at INTEGER,
                notified_at INTEGER,
                approved_stop_id TEXT,
                nearby_lines TEXT
            )""")
            try:
                conn.execute("ALTER TABLE stop_reports ADD COLUMN nearby_lines TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS stop_reports_status ON stop_reports(status, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS stop_reports_client ON stop_reports(client_id, created_at DESC)")

    def photos_disk_usage(self) -> dict:
        """Calcula el uso en bytes del directorio de fotos pendientes."""
        if not self.photos_dir.exists():
            return {"used_bytes": 0, "used_mb": 0.0, "budget_mb": 50, "percent": 0.0, "accepting": True}
        total = sum(f.stat().st_size for f in self.photos_dir.iterdir() if f.is_file())
        return {
            "used_bytes": total,
            "used_mb": round(total / (1024 * 1024), 2),
            "budget_mb": 50,
            "percent": round(total / PHOTO_BUDGET_BYTES * 100, 1),
            "accepting": total < PHOTO_BUDGET_BYTES,
        }

    def count_recent_by_client(self, client_id: str, hours: int = 24) -> int:
        """Cuenta reportes creados por un dispositivo en las últimas N horas."""
        since = int(time.time()) - (hours * 3600)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM stop_reports WHERE client_id = ? AND created_at > ?",
                (client_id, since)
            ).fetchone()
            return int(row["c"]) if row else 0

    def find_nearby_pending(self, lat: float, lon: float, max_meters: float = 50.0) -> dict | None:
        """Busca reportes comunitarios pendientes o aprobados dentro del radio especificado."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, lat, lon, status, street_name FROM stop_reports WHERE status IN ('pendiente', 'aprobada')"
            ).fetchall()
        for r in rows:
            dist = haversine_meters(lat, lon, float(r["lat"]), float(r["lon"]))
            if dist <= max_meters:
                return {
                    "id": r["id"],
                    "status": r["status"],
                    "distanceMeters": int(round(dist)),
                    "streetName": r["street_name"] or "",
                }
        return None

    def create(self, lat: float, lon: float, accuracy: float, raw_photo_bytes: bytes,
               client_id: str, description: str = "", reporter_name: str = "",
               push_subscription: dict | None = None,
               nearby_lines: list[dict] | None = None) -> dict:
        """Comprime foto, guarda en disco y registra el reporte en estado 'pendiente'."""
        try:
            lat, lon, accuracy = float(lat), float(lon), float(accuracy)
        except (TypeError, ValueError):
            raise ValueError("Coordenadas o precision GPS invalidas")
        if not in_paraguay(lat, lon):
            raise ValueError("La ubicacion debe estar dentro del territorio paraguayo")
        if not math.isfinite(accuracy) or not 0 <= accuracy <= 100:
            raise ValueError("Precision GPS invalida")

        usage = self.photos_disk_usage()
        if not usage["accepting"]:
            raise RuntimeError("El buffer de fotos está lleno. Por favor intentá más tarde.")

        compressed_webp = compress_photo(raw_photo_bytes)
        now = int(time.time())
        token_hash = hashlib.sha256(compressed_webp[:64] + str(now).encode()).hexdigest()[:6]

        push_sub_json = json.dumps(push_subscription, ensure_ascii=False) if push_subscription else None
        nearby_lines_json = json.dumps(nearby_lines, ensure_ascii=False) if nearby_lines else None

        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO stop_reports (
                    created_at, lat, lon, accuracy, photo_filename, photo_size,
                    client_id, description, reporter_name, push_subscription, status,
                    nearby_lines
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente', ?)
            """, (
                now, round(lat, 6), round(lon, 6), round(accuracy, 1),
                "temp", len(compressed_webp), client_id, description.strip()[:500],
                reporter_name.strip()[:80], push_sub_json, nearby_lines_json
            ))
            report_id = cursor.lastrowid
            filename = f"sr_{report_id}_{token_hash}.webp"
            photo_path = self.photos_dir / filename
            photo_path.write_bytes(compressed_webp)

            conn.execute(
                "UPDATE stop_reports SET photo_filename = ? WHERE id = ?",
                (filename, report_id)
            )

        return {
            "id": report_id,
            "status": "pendiente",
            "photo_filename": filename,
            "photo_size": len(compressed_webp),
            "nearby_lines": nearby_lines or []
        }

    def get(self, report_id: int) -> dict | None:
        """Obtiene un reporte por ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM stop_reports WHERE id = ?", (report_id,)).fetchone()
            return dict(row) if row else None

    def get_photo_path(self, report_id: int) -> Path | None:
        """Obtiene la ruta a la foto en disco si existe."""
        report = self.get(report_id)
        if not report or not report.get("photo_filename") or report["photo_filename"] == "deleted":
            return None
        path = self.photos_dir / report["photo_filename"]
        return path if path.exists() else None

    def list_reports(self, status: str | None = None, before_id: int | None = None,
                     limit: int = 50) -> tuple[list[dict], int | None]:
        """Lista reportes para moderación con paginación."""
        limit = min(max(1, limit), 100)
        query = "SELECT * FROM stop_reports"
        params = []
        conditions = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if before_id:
            conditions.append("id < ?")
            params.append(before_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit + 1)

        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]

        for r in rows:
            if r.get("nearby_lines"):
                try:
                    r["nearby_lines"] = json.loads(r["nearby_lines"])
                except Exception:
                    r["nearby_lines"] = []
            else:
                r["nearby_lines"] = []

        next_before = None
        if len(rows) > limit:
            next_before = rows[limit - 1]["id"]
            rows = rows[:limit]

        return rows, next_before

    def review(self, report_id: int, action: str, admin_notes: str = "",
               street_name: str = "", street_side: str = "", stop_name: str = "",
               stop_type: str = "refugio", rejection_reason: str = "") -> dict:
        """
        Modera un reporte: aprueba o rechaza.
        BORRA INMEDIATAMENTE la foto física del disco para liberar almacenamiento (Zero-Retention).
        """
        now = int(time.time())
        action_clean = action.strip().lower()
        if action_clean not in ("aprobar", "rechazar", "duplicada"):
            raise ValueError(f"Acción inválida: {action}")

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM stop_reports WHERE id = ?", (report_id,)).fetchone()
            if not row:
                raise ValueError("Reporte no encontrado")

            report = dict(row)
            if report.get("status") != "pendiente":
                raise ValueError("El reporte ya fue moderado")
            photo_filename = report.get("photo_filename")
            push_sub = None
            if report.get("push_subscription"):
                try:
                    push_sub = json.loads(report["push_subscription"])
                except Exception:
                    pass

            approved_id = None
            new_stop_data = None

            if action_clean == "aprobar":
                approved_id = f"com_{report_id}"
                final_street = (street_name or report.get("street_name") or "Vía Pública").strip()[:120]
                final_name = (stop_name or f"{final_street} ({street_side or 'Parada'})").strip()[:150]
                final_type = stop_type if stop_type in ("refugio", "poste", "señal") else "refugio"
                final_side = street_side if street_side in ("ida", "vuelta") else None

                lines_auto = []
                if report.get("nearby_lines"):
                    try:
                        nlines = json.loads(report["nearby_lines"]) if isinstance(report["nearby_lines"], str) else report["nearby_lines"]
                        for nl in nlines:
                            lname = nl.get("line_name")
                            if lname and lname not in lines_auto:
                                lines_auto.append(lname)
                    except Exception:
                        pass

                new_stop_data = {
                    "id": approved_id,
                    "name": final_name,
                    "lat": report["lat"],
                    "lon": report["lon"],
                    "type": final_type,
                    "street": final_street,
                    "side": final_side,
                    "community": True,
                    "approved_at": now,
                    "lines": lines_auto
                }

                # Agregar a community_stops.json
                with self._file_lock:
                    stops_list = []
                    if self.community_stops_path.exists():
                        try:
                            stops_list = json.loads(self.community_stops_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise RuntimeError("No se pudo leer el archivo de paradas comunitarias") from exc
                        if not isinstance(stops_list, list) or any(not isinstance(stop, dict) for stop in stops_list):
                            raise RuntimeError("El archivo de paradas comunitarias tiene un formato inválido")
                    # Evitar duplicar ID si ya existía
                    stops_list = [s for s in stops_list if s.get("id") != approved_id]
                    stops_list.append(new_stop_data)
                    temp_path = self.community_stops_path.with_name(
                        f".{self.community_stops_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                    )
                    try:
                        temp_path.write_text(
                            json.dumps(stops_list, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8"
                        )
                        os.replace(temp_path, self.community_stops_path)
                    finally:
                        temp_path.unlink(missing_ok=True)

                conn.execute("""
                    UPDATE stop_reports SET
                        status = 'aprobada',
                        street_name = ?,
                        street_side = ?,
                        admin_notes = ?,
                        approved_stop_id = ?,
                        reviewed_at = ?,
                        photo_filename = 'deleted'
                    WHERE id = ?
                """, (final_street, final_side, admin_notes.strip(), approved_id, now, report_id))

            else:  # rechazar o duplicada
                reason = (rejection_reason or "Reporte no cumple con los requisitos").strip()[:300]
                conn.execute("""
                    UPDATE stop_reports SET
                        status = 'rechazada',
                        rejection_reason = ?,
                        admin_notes = ?,
                        reviewed_at = ?,
                        photo_filename = 'deleted'
                    WHERE id = ?
                """, (reason, admin_notes.strip(), now, report_id))

            # BORRADO INMEDIATO DE LA FOTO EN DISCO
            if photo_filename and photo_filename != "deleted":
                file_on_disk = self.photos_dir / photo_filename
                try:
                    file_on_disk.unlink(missing_ok=True)
                except Exception:
                    pass

        return {
            "id": report_id,
            "action": action_clean,
            "status": "aprobada" if action_clean == "aprobar" else "rechazada",
            "push_subscription": push_sub,
            "rejection_reason": rejection_reason if action_clean != "aprobar" else None,
            "approved_stop": new_stop_data,
            "client_id": report["client_id"],
        }

    def mark_notified(self, report_id: int):
        """Marca que el usuario fue notificado."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE stop_reports SET notified_at = ? WHERE id = ?",
                (int(time.time()), report_id)
            )

    def get_user_reports_status(self, client_id: str, report_ids: list[int] | None = None) -> list[dict]:
        """Devuelve el estado de reportes para un dispositivo (para notificaciones in-app)."""
        with self._connect() as conn:
            if report_ids:
                placeholders = ",".join("?" for _ in report_ids)
                params = [client_id] + report_ids
                rows = conn.execute(
                    f"SELECT id, status, rejection_reason, reviewed_at, street_name, created_at, approved_stop_id "
                    f"FROM stop_reports WHERE client_id = ? AND id IN ({placeholders}) ORDER BY id DESC",
                    params
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, status, rejection_reason, reviewed_at, street_name, created_at, approved_stop_id "
                    "FROM stop_reports WHERE client_id = ? ORDER BY id DESC LIMIT 20",
                    (client_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def prune(self):
        """Elimina fotos huérfanas de reportes pendientes con más de 30 días sin revisión."""
        now = int(time.time())
        with self._prune_lock:
            if now - self._last_prune < 3600:
                return
            self._last_prune = now

            cutoff = now - (30 * 86400)
            with self._connect() as conn:
                stale_pending = conn.execute(
                    "SELECT id, photo_filename FROM stop_reports WHERE status = 'pendiente' AND created_at < ?",
                    (cutoff,)
                ).fetchall()

                for row in stale_pending:
                    fname = row["photo_filename"]
                    if fname and fname != "deleted":
                        (self.photos_dir / fname).unlink(missing_ok=True)
                    conn.execute(
                        "UPDATE stop_reports SET status = 'rechazada', "
                        "rejection_reason = 'Reporte expirado sin moderación', "
                        "photo_filename = 'deleted' WHERE id = ?",
                        (row["id"],)
                    )
