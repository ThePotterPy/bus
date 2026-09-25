import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from PIL import Image

import stop_reports
from stop_reports import StopReportStore, in_paraguay, compress_photo, haversine_meters


class TestStopReports(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "stop_reports.sqlite3"
        self.photos_dir = self.temp_dir / "stop_photos"
        self.community_stops_path = self.temp_dir / "community_stops.json"

        self.store = StopReportStore(self.db_path, self.photos_dir, self.community_stops_path)
        self.store.init()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_image_bytes(self, width=800, height=600, color="blue") -> bytes:
        img = Image.new("RGB", (width, height), color=color)
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def test_in_paraguay_bounds(self):
        # Asunción
        self.assertTrue(in_paraguay(-25.2867, -57.6470))
        # Ciudad del Este
        self.assertTrue(in_paraguay(-25.5097, -54.6111))
        # Encarnación
        self.assertTrue(in_paraguay(-27.3306, -55.8667))
        # Mariscal Estigarribia (Chaco)
        self.assertTrue(in_paraguay(-22.0294, -60.6075))

        # Fuera de Paraguay: Buenos Aires, Tokio, etc.
        self.assertFalse(in_paraguay(-34.6037, -58.3816))
        self.assertFalse(in_paraguay(35.6762, 139.6503))
        self.assertFalse(in_paraguay("invalido", 0))

    def test_compress_photo_webp(self):
        raw = self._create_sample_image_bytes(1600, 1200)
        compressed = compress_photo(raw)
        self.assertIsInstance(compressed, bytes)
        self.assertLess(len(compressed), len(raw))

        # Verificar que es un WebP válido
        img = Image.open(io.BytesIO(compressed))
        self.assertEqual(img.format, "WEBP")
        self.assertLessEqual(max(img.size), 1280)

    def test_compress_invalid_image_raises(self):
        with self.assertRaises(ValueError):
            compress_photo(b"not an image file content")

    def test_create_report_saves_photo_in_pending_state(self):
        photo_bytes = self._create_sample_image_bytes(400, 300)
        res = self.store.create(
            lat=-25.2850,
            lon=-57.5680,
            accuracy=12.5,
            raw_photo_bytes=photo_bytes,
            client_id="device-uuid-1",
            description="Refugio frente al shopping",
            reporter_name="Juan Perez",
            push_subscription={"endpoint": "https://push.example.com/1"}
        )

        report_id = res["id"]
        self.assertGreater(report_id, 0)
        self.assertEqual(res["status"], "pendiente")

        # Foto existe en disco mientras está pendiente
        photo_path = self.store.get_photo_path(report_id)
        self.assertIsNotNone(photo_path)
        self.assertTrue(photo_path.exists())

        # Verificar en base de datos
        row = self.store.get(report_id)
        self.assertEqual(row["status"], "pendiente")
        self.assertEqual(row["client_id"], "device-uuid-1")
        self.assertEqual(row["description"], "Refugio frente al shopping")
        self.assertIn("https://push.example.com/1", row["push_subscription"])

    def test_review_approve_deletes_photo_immediately(self):
        photo_bytes = self._create_sample_image_bytes(400, 300)
        res = self.store.create(-25.2850, -57.5680, 10.0, photo_bytes, "client-1")
        report_id = res["id"]

        photo_path = self.store.get_photo_path(report_id)
        self.assertTrue(photo_path.exists())

        # Admin aprueba
        review_res = self.store.review(
            report_id=report_id,
            action="aprobar",
            street_name="Av. Mcal. López",
            street_side="ida",
            stop_name="Mcal. López y San Martín",
            stop_type="refugio",
            admin_notes="Confirmado con cartel oficial"
        )

        self.assertEqual(review_res["action"], "aprobar")
        self.assertEqual(review_res["status"], "aprobada")

        # LA FOTO DEBE HABER SIDO ELIMINADA DE INMEDIATO DEL DISCO (Zero-Retention)
        self.assertFalse(photo_path.exists())
        self.assertIsNone(self.store.get_photo_path(report_id))

        # Verificar que se agregó a community_stops.json
        stops = json.loads(self.community_stops_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["id"], f"com_{report_id}")
        self.assertEqual(stops[0]["name"], "Mcal. López y San Martín")
        self.assertEqual(stops[0]["side"], "ida")
        self.assertTrue(stops[0]["community"])

    def test_review_reject_deletes_photo_immediately_and_saves_reason(self):
        photo_bytes = self._create_sample_image_bytes(400, 300)
        res = self.store.create(-25.2850, -57.5680, 10.0, photo_bytes, "client-1")
        report_id = res["id"]

        photo_path = self.store.get_photo_path(report_id)
        self.assertTrue(photo_path.exists())

        # Admin rechaza
        review_res = self.store.review(
            report_id=report_id,
            action="rechazar",
            rejection_reason="Foto muy borrosa, no se distingue el refugio"
        )

        self.assertEqual(review_res["status"], "rechazada")
        self.assertEqual(review_res["rejection_reason"], "Foto muy borrosa, no se distingue el refugio")

        # FOTO ELIMINADA DE INMEDIATO (Zero-Retention)
        self.assertFalse(photo_path.exists())
        self.assertIsNone(self.store.get_photo_path(report_id))

        # Registro en BD conserva motivo para notificar al usuario
        row = self.store.get(report_id)
        self.assertEqual(row["status"], "rechazada")
        self.assertEqual(row["rejection_reason"], "Foto muy borrosa, no se distingue el refugio")
        self.assertEqual(row["photo_filename"], "deleted")

    def test_get_user_reports_status_for_in_app_notifications(self):
        photo_bytes = self._create_sample_image_bytes(200, 200)
        r1 = self.store.create(-25.2850, -57.5680, 10.0, photo_bytes, "client-A")
        r2 = self.store.create(-25.2860, -57.5690, 10.0, photo_bytes, "client-A")
        r3 = self.store.create(-25.2870, -57.5700, 10.0, photo_bytes, "client-B")

        self.store.review(r1["id"], "rechazar", rejection_reason="No es parada oficial")
        self.store.review(r2["id"], "aprobar", stop_name="Parada Aprobada")

        # Consultar reportes para client-A
        user_statuses = self.store.get_user_reports_status("client-A")
        self.assertEqual(len(user_statuses), 2)

        statuses_by_id = {s["id"]: s for s in user_statuses}
        self.assertEqual(statuses_by_id[r1["id"]]["status"], "rechazada")
        self.assertEqual(statuses_by_id[r1["id"]]["rejection_reason"], "No es parada oficial")
        self.assertEqual(statuses_by_id[r2["id"]]["status"], "aprobada")

        # Filtrando por lista específica de IDs
        user_statuses_filtered = self.store.get_user_reports_status("client-A", report_ids=[r1["id"]])
        self.assertEqual(len(user_statuses_filtered), 1)
        self.assertEqual(user_statuses_filtered[0]["id"], r1["id"])

    def test_duplicate_community_detection(self):
        photo_bytes = self._create_sample_image_bytes(200, 200)
        self.store.create(-25.28500, -57.56800, 10.0, photo_bytes, "client-1")

        # Misma coordenada o a ~15 metros: debe detectar duplicado
        dup = self.store.find_nearby_pending(-25.28505, -57.56805, max_meters=50)
        self.assertIsNotNone(dup)
        self.assertLessEqual(dup["distanceMeters"], 20)

        # Lejos (> 1 km): no duplica
        no_dup = self.store.find_nearby_pending(-25.30000, -57.58000, max_meters=50)
        self.assertIsNone(no_dup)

    def test_count_recent_by_client_for_cooldown(self):
        photo_bytes = self._create_sample_image_bytes(200, 200)
        self.assertEqual(self.store.count_recent_by_client("client-cool", 24), 0)

        self.store.create(-25.2850, -57.5680, 10.0, photo_bytes, "client-cool")
        self.assertEqual(self.store.count_recent_by_client("client-cool", 24), 1)

        self.store.create(-25.2860, -57.5690, 10.0, photo_bytes, "client-cool")
        self.assertEqual(self.store.count_recent_by_client("client-cool", 24), 2)


if __name__ == "__main__":
    unittest.main()
