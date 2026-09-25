import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from PIL import Image

import server


class StopReportsServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_admin_pw = os.environ.get("FEEDBACK_ADMIN_PASSWORD")
        os.environ["FEEDBACK_ADMIN_PASSWORD"] = "super-secret-password-12345"

        cls.temp_dir = Path(tempfile.mkdtemp())
        server.DATA_DIR = cls.temp_dir
        server.FEEDBACK_DB_FILE = cls.temp_dir / "feedback.sqlite3"
        server.SHARED_TRIPS_DB_FILE = cls.temp_dir / "shared_trips.sqlite3"
        server.COMMUNITY_STOPS_FILE = cls.temp_dir / "community_stops.json"
        server.STOP_PHOTOS_DIR = cls.temp_dir / "stop_photos"
        server.STOP_REPORTS_DB_FILE = cls.temp_dir / "stop_reports.sqlite3"

        server.feedback_store = server.FeedbackStore(server.FEEDBACK_DB_FILE)
        server.feedback_store.init()
        server.shared_trip_store = server.SharedTripStore(server.SHARED_TRIPS_DB_FILE)
        server.stop_report_store = server.StopReportStore(
            server.STOP_REPORTS_DB_FILE, server.STOP_PHOTOS_DIR, server.COMMUNITY_STOPS_FILE
        )
        server.stop_report_store.init()

        # Configurar ruta de prueba para validaciones de proximidad de colectivos
        test_routes = [
            {
                "line_id": "27",
                "line_name": "LINEA 27",
                "route_name": "Capiatá - Asunción",
                "direction": "Ida",
                "points": [(-25.28000, -57.62000), (-25.28500, -57.61800), (-25.29000, -57.61500), (-25.31000, -57.59000)],
                "cumulative": [0.0, 580.0, 1160.0, 3500.0],
                "bbox": (-25.31100, -57.62100, -25.27900, -57.58900),
                "gaps": set()
            }
        ]
        with server._all_paths_lock:
            server._all_paths_cache.clear()
            server._all_paths_cache["27"] = test_routes

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_port
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        if cls.orig_admin_pw is not None:
            os.environ["FEEDBACK_ADMIN_PASSWORD"] = cls.orig_admin_pw
        else:
            os.environ.pop("FEEDBACK_ADMIN_PASSWORD", None)

    def _login_admin(self) -> tuple[str, str]:
        req = Request(
            f"http://127.0.0.1:{self.port}/api/admin/feedback/login",
            data=json.dumps({"password": "super-secret-password-12345"}).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urlopen(req) as resp:
            cookie_hdr = resp.headers.get("Set-Cookie", "")
            cookie = SimpleCookie()
            cookie.load(cookie_hdr)
            token = cookie["feedback_admin"].value
            csrf = server.feedback_store.session(token, "super-secret-password-12345")
            return token, csrf

    def _build_multipart_payload(self, fields: dict, file_name: str, file_bytes: bytes, field_name="photo"):
        boundary = "----WebKitFormBoundaryX"
        lines = []
        for k, v in fields.items():
            lines.append(f"--{boundary}".encode())
            lines.append(f'Content-Disposition: form-data; name="{k}"'.encode())
            lines.append(b"")
            lines.append(str(v).encode("utf-8"))

        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"'.encode())
        lines.append(b"Content-Type: image/jpeg")
        lines.append(b"")
        lines.append(file_bytes)
        lines.append(f"--{boundary}--".encode())
        lines.append(b"")

        body = b"\r\n".join(lines)
        content_type = f"multipart/form-data; boundary={boundary}"
        return content_type, body

    def _create_sample_jpeg(self) -> bytes:
        img = Image.new("RGB", (400, 300), color="green")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def test_post_stop_report_and_user_status(self):
        jpeg_bytes = self._create_sample_jpeg()
        fields = {
            "lat": "-25.28500",
            "lon": "-57.61800",
            "accuracy": "15.0",
            "clientId": "test-client-uuid-1",
            "description": "Frente a plaza central",
            "name": "Carlos"
        }
        ct, body = self._build_multipart_payload(fields, "parada.jpg", jpeg_bytes)

        req = Request(
            f"http://127.0.0.1:{self.port}/api/stop-report",
            data=body,
            headers={"Content-Type": ct}
        )
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 201)
            data = json.loads(resp.read().decode())
            self.assertTrue(data["success"])
            report_id = data["id"]
            self.assertGreater(report_id, 0)

        # Consultar estado de usuario
        req_status = Request(f"http://127.0.0.1:{self.port}/api/stop-reports/my-status?clientId=test-client-uuid-1")
        with urlopen(req_status) as resp:
            self.assertEqual(resp.status, 200)
            status_data = json.loads(resp.read().decode())
            self.assertTrue(status_data["success"])
            self.assertEqual(len(status_data["reports"]), 1)
            self.assertEqual(status_data["reports"][0]["status"], "pendiente")

    def test_admin_flow_review_and_immediate_photo_deletion(self):
        token, csrf = self._login_admin()

        # 1. Crear un reporte
        jpeg_bytes = self._create_sample_jpeg()
        fields = {
            "lat": "-25.31000",
            "lon": "-57.59000",
            "accuracy": "10.0",
            "clientId": "test-client-uuid-admin-rev",
            "description": "Cerca de estación de servicio",
            "name": "Maria"
        }
        ct, body = self._build_multipart_payload(fields, "parada_rev.jpg", jpeg_bytes)
        req = Request(f"http://127.0.0.1:{self.port}/api/stop-report", data=body, headers={"Content-Type": ct})
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            report_id = data["id"]

        # 2. Listar como admin
        admin_req = Request(
            f"http://127.0.0.1:{self.port}/api/admin/stop-reports?status=pendiente",
            headers={"Cookie": f"feedback_admin={token}"}
        )
        with urlopen(admin_req) as resp:
            self.assertEqual(resp.status, 200)
            list_data = json.loads(resp.read().decode())
            self.assertTrue(list_data["success"])
            self.assertTrue(any(r["id"] == report_id for r in list_data["items"]))

        # 3. Ver foto antes de moderar
        photo_req = Request(
            f"http://127.0.0.1:{self.port}/api/admin/stop-reports/photo?id={report_id}",
            headers={"Cookie": f"feedback_admin={token}"}
        )
        with urlopen(photo_req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/webp")

        # 4. Rechazar como admin
        review_body = json.dumps({
            "id": report_id,
            "action": "rechazar",
            "rejection_reason": "No se observa señal ni garita",
            "notify_user": True
        }).encode()

        rev_req = Request(
            f"http://127.0.0.1:{self.port}/api/admin/stop-reports/review",
            data=review_body,
            headers={
                "Content-Type": "application/json",
                "Cookie": f"feedback_admin={token}",
                "X-Feedback-CSRF": csrf
            }
        )
        with urlopen(rev_req) as resp:
            self.assertEqual(resp.status, 200)
            rev_res = json.loads(resp.read().decode())
            self.assertTrue(rev_res["success"])

        # 5. La foto debe haber sido eliminada inmediatamente (404)
        with self.assertRaises(HTTPError) as ctx:
            urlopen(photo_req)
        self.assertEqual(ctx.exception.code, 404)

        # 6. El usuario ahora ve el motivo de rechazo en su consulta de estado
        req_status = Request(f"http://127.0.0.1:{self.port}/api/stop-reports/my-status?clientId=test-client-uuid-admin-rev")
        with urlopen(req_status) as resp:
            status_data = json.loads(resp.read().decode())
            rep = status_data["reports"][0]
            self.assertEqual(rep["status"], "rechazada")
            self.assertEqual(rep["rejection_reason"], "No se observa señal ni garita")

    def test_post_stop_report_rejected_when_far_from_bus_routes(self):
        """Verifica que un reporte a más de 150m de cualquier línea sea rechazado con 400."""
        jpeg_bytes = self._create_sample_jpeg()
        # Coordenadas en un punto rural o descampado sin líneas de bus
        fields = {
            "lat": "-25.20000",
            "lon": "-57.40000",
            "accuracy": "10.0",
            "clientId": "test-client-far",
            "description": "En medio del campo",
            "name": "Pedro"
        }
        ct, body = self._build_multipart_payload(fields, "parada_lejos.jpg", jpeg_bytes)
        req = Request(f"http://127.0.0.1:{self.port}/api/stop-report", data=body, headers={"Content-Type": ct})

        with self.assertRaises(HTTPError) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        err_data = json.loads(ctx.exception.read().decode())
        self.assertFalse(err_data["success"])
        self.assertIn("No se detectó ningún recorrido de colectivos cerca", err_data["error"])

    def test_check_location_endpoint(self):
        """Verifica el endpoint GET /api/stop-reports/check-location."""
        # 1. Punto cercano a la ruta 27 (-25.28500, -57.61800)
        url_near = f"http://127.0.0.1:{self.port}/api/stop-reports/check-location?lat=-25.28500&lon=-57.61800&accuracy=10"
        with urlopen(url_near) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertTrue(data["valid"])
            self.assertLess(data["distance_to_route_m"], 50)
            self.assertTrue(any(l["line_name"] == "LINEA 27" for l in data["lines"]))

        # 2. Punto lejano sin líneas
        url_far = f"http://127.0.0.1:{self.port}/api/stop-reports/check-location?lat=-25.20000&lon=-57.40000&accuracy=10"
        with urlopen(url_far) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertFalse(data["valid"])
            self.assertEqual(data["reason"], "no_bus_routes")


if __name__ == "__main__":
    unittest.main()
