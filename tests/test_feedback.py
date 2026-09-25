import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

import feedback
import server

TEST_TMP_ROOT = Path(server.__file__).parent / "data"


def test_store():
    path = TEST_TMP_ROOT / f"_feedback_test_{uuid.uuid4().hex}.sqlite3"
    store = feedback.FeedbackStore(path)
    store.init()
    return store


def cleanup_store(store):
    for suffix in ("", "-wal", "-shm"):
        store.path.with_name(store.path.name + suffix).unlink(missing_ok=True)


def sample(**overrides):
    body = {
        "name": "",
        "category": "ruta",
        "lineId": "30",
        "message": "El recorrido no aparece actualizado.",
        "diagnosticsConsent": False,
    }
    body.update(overrides)
    return body


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = test_store()

    def tearDown(self):
        cleanup_store(self.store)

    def test_anonymous_comment_ignores_diagnostics_without_consent(self):
        record = feedback.validate_feedback(sample(diagnostics={"device": "Teléfono"}))
        self.assertEqual(record["name"], "")
        self.assertIsNone(record["diagnostics_json"])
        feedback_id, delete_token = self.store.create(record)
        self.assertTrue(delete_token)
        items, next_before = self.store.list()
        self.assertEqual(items[0]["id"], feedback_id)
        self.assertIsNone(items[0]["diagnostics"])
        self.assertIsNone(next_before)

    def test_diagnostics_require_explicit_consent_and_are_whitelisted(self):
        record = feedback.validate_feedback(sample(
            diagnosticsConsent=True, diagnostics={"device": "Teléfono", "browser": "Firefox"},
        ))
        self.store.create(record)
        self.assertEqual(self.store.list()[0][0]["diagnostics"]["browser"], "Firefox")
        with self.assertRaises(ValueError):
            feedback.validate_feedback(sample(diagnosticsConsent=True, diagnostics={"gps": "-25,-57"}))
        with self.assertRaises(ValueError):
            feedback.validate_feedback(sample(category=["ruta"]))
        with self.assertRaises(ValueError):
            feedback.validate_feedback(sample(lineId="../datos"))
        with self.assertRaises(ValueError):
            feedback.validate_feedback(sample(message="x" * 3001))

    def test_listing_status_pagination_and_delete(self):
        first = self.store.create(feedback.validate_feedback(sample(message="Primera observación")))[0]
        second = self.store.create(feedback.validate_feedback(sample(message="Segunda observación")))[0]
        items, before = self.store.list(limit=1)
        self.assertEqual(items[0]["id"], second)
        self.assertEqual(before, second)
        self.assertEqual(self.store.list(before=before)[0][0]["id"], first)
        self.assertTrue(self.store.set_status(first, "revisado"))
        self.assertEqual(len(self.store.list(status="nuevo")[0]), 1)
        self.assertEqual(len(self.store.list(status="revisado")[0]), 1)
        self.assertTrue(self.store.delete(first))
        self.assertFalse(self.store.delete(first))

    def test_retention_clears_diagnostics_before_comment(self):
        feedback_id = self.store.create(feedback.validate_feedback(sample(
            diagnosticsConsent=True, diagnostics={"device": "Computadora"},
        )))[0]
        conn = sqlite3.connect(self.store.path)
        conn.execute("UPDATE feedback SET created_at = ? WHERE id = ?",
                     (int(time.time()) - 31 * 86400, feedback_id))
        conn.commit()
        conn.close()
        self.store._last_prune = 0
        self.store.prune()
        self.assertIsNone(self.store.list()[0][0]["diagnostics"])
        conn = sqlite3.connect(self.store.path)
        conn.execute("UPDATE feedback SET created_at = ? WHERE id = ?",
                     (int(time.time()) - 181 * 86400, feedback_id))
        conn.commit()
        conn.close()
        self.store._last_prune = 0
        self.store.prune()
        self.assertEqual(self.store.list()[0], [])

    def test_news_validation_and_crud(self):
        with self.assertRaises(ValueError):
            feedback.validate_news({"title": "ab", "content": "12345"})
        with self.assertRaises(ValueError):
            feedback.validate_news({"title": "Valido", "content": "123"})
        with self.assertRaises(ValueError):
            feedback.validate_news({"title": "Valido", "content": "12345", "tag": "invalido"})

        valid = feedback.validate_news({
            "title": "Nueva función de paradas",
            "content": "Ahora podés ver paradas oficiales en el mapa.",
            "tag": "mejora",
        })
        self.assertEqual(valid["tag"], "mejora")
        news_id = self.store.create_news(valid)
        self.assertTrue(news_id)

        items = self.store.list_news()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], news_id)
        self.assertEqual(items[0]["title"], "Nueva función de paradas")
        self.assertEqual(items[0]["tag"], "mejora")

        self.assertTrue(self.store.delete_news(news_id))
        self.assertFalse(self.store.delete_news(news_id))
        self.assertEqual(self.store.list_news(), [])

    def test_release_news_is_published_only_once(self):
        record = feedback.validate_news({
            "title": "Nueva versión",
            "content": "Contenido de la actualización.",
            "tag": "mejora",
        })
        first_id, first_created = self.store.publish_release_news("release-test", record)
        second_id, second_created = self.store.publish_release_news("release-test", record)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.store.list_news()), 1)

    def test_sessions_are_revocable(self):
        password = "contraseña-muy-larga-y-unica"
        self.assertIsNone(self.store.login("incorrecta", password))
        token = self.store.login(password, password)
        self.assertTrue(self.store.session(token, password))
        self.assertIsNone(self.store.session(token, "otra-contraseña-muy-larga"))
        self.store.logout(token, password)
        self.assertIsNone(self.store.session(token, password))


class FeedbackHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = test_store()
        self.store_patch = patch.object(server, "feedback_store", self.store)
        self.store_patch.start()
        self.limit_patch = patch.object(server, "_rate_limiter", server.FixedWindowRateLimiter())
        self.limit_patch.start()
        self.password_patch = patch.dict(server.os.environ, {"FEEDBACK_ADMIN_PASSWORD": "test-admin-password-12345"})
        self.password_patch.start()
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        self.password_patch.stop()
        self.limit_patch.stop()
        self.store_patch.stop()
        cleanup_store(self.store)

    def request(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read()), response.headers

    def test_private_admin_api_and_csrf(self):
        self.assertTrue(self.request("/api/feedback/config")[1]["enabled"])
        code, data, _ = self.request("/api/feedback", sample())
        self.assertEqual(code, 201)
        self.assertTrue(data["code"].startswith("C-"))
        delete_code = data["code"]
        self.assertEqual(self.request("/api/admin/feedback")[0], 401)
        self.assertEqual(self.request("/api/admin/feedback/login", {"password": "bad"})[0], 401)
        code, _, headers = self.request("/api/admin/feedback/login", {"password": "test-admin-password-12345"})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        csrf = self.request("/api/admin/feedback/session", headers={"Cookie": cookie})[1]["csrf"]
        self.assertTrue(csrf)
        code, data, _ = self.request("/api/admin/feedback", headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("delete_token_hash", data["items"][0])
        self.assertEqual(self.request("/api/admin/feedback/status", {"id": 1, "status": "revisado"},
                                      {"Cookie": cookie})[0], 403)
        headers = {"Cookie": cookie, "X-Feedback-CSRF": csrf}
        self.assertEqual(self.request("/api/admin/feedback/status", {"id": 1, "status": "revisado"}, headers)[0], 200)
        self.assertEqual(self.request("/api/admin/feedback/logout", {}, headers)[0], 200)
        self.assertEqual(self.request("/api/admin/feedback", headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.request("/api/feedback/delete", {"code": delete_code + "x"})[0], 400)
        self.assertEqual(self.request("/api/feedback/delete", {"code": delete_code})[0], 200)
        self.assertEqual(self.store.list()[0], [])

    def test_feedback_is_disabled_until_admin_password_is_configured(self):
        with patch.dict(server.os.environ, {"FEEDBACK_ADMIN_PASSWORD": ""}):
            self.assertFalse(self.request("/api/feedback/config")[1]["enabled"])
            self.assertEqual(self.request("/api/feedback", sample())[0], 503)
            self.assertEqual(self.request("/api/admin/feedback/login", {"password": "x"})[0], 503)

    def test_feedback_is_disabled_when_password_is_unset(self):
        env_without_pass = dict(server.os.environ)
        env_without_pass.pop("FEEDBACK_ADMIN_PASSWORD", None)
        with patch.dict(server.os.environ, env_without_pass, clear=True):
            self.assertFalse(self.request("/api/feedback/config")[1]["enabled"])
            self.assertEqual(self.request("/api/feedback", sample())[0], 503)
            self.assertEqual(self.request("/api/admin/feedback/login", {"password": "bad"})[0], 503)

    def test_news_http_lifecycle(self):
        # Public GET when empty
        code, data, _ = self.request("/api/news")
        self.assertEqual(code, 200)
        self.assertEqual(data["items"], [])

        # Unauthorized admin POST
        self.assertEqual(self.request("/api/admin/news", {"title": "Test", "content": "Mensaje de prueba"})[0], 401)

        # Login admin
        code, _, headers = self.request("/api/admin/feedback/login", {"password": "test-admin-password-12345"})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = self.request("/api/admin/feedback/session", headers={"Cookie": cookie})[1]["csrf"]
        auth_headers = {"Cookie": cookie, "X-Feedback-CSRF": csrf}

        # Create news
        code, data, _ = self.request("/api/admin/news", {
            "title": "¡Actualización lanzada!",
            "content": "Ya podés consultar las novedades desde la tuerca de ajustes.",
            "tag": "novedad",
        }, auth_headers)
        self.assertEqual(code, 201)
        news_id = data["id"]

        # Public GET sees the new item
        code, data, _ = self.request("/api/news")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["title"], "¡Actualización lanzada!")
        self.assertEqual(data["items"][0]["tag"], "novedad")

        # Delete news via admin
        code, data, _ = self.request("/api/admin/news/delete", {"id": news_id}, auth_headers)
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])

        # Public GET is empty again
        code, data, _ = self.request("/api/news")
        self.assertEqual(code, 200)
        self.assertEqual(data["items"], [])

    def test_observed_review_is_private_and_validates_filters(self):
        self.assertEqual(self.request("/api/admin/observed-review")[0], 401)
        code, _, headers = self.request(
            "/api/admin/feedback/login", {"password": "test-admin-password-12345"}
        )
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = self.request("/api/admin/feedback/session", headers={"Cookie": cookie})[1]["csrf"]
        review = {"success": True, "items": [], "health": {}, "raw_retention_days": 7}
        with patch.object(server.observed_routes, "review", return_value=review) as mocked:
            code, data, _ = self.request(
                "/api/admin/observed-review?status=accepted&line=30&limit=25",
                headers={"Cookie": cookie},
            )
            self.assertEqual(code, 200)
            self.assertEqual(data, review)
            mocked.assert_called_once_with("accepted", "30", 25)
        self.assertEqual(self.request(
            "/api/admin/observed-review?status=invalid", headers={"Cookie": cookie}
        )[0], 400)
        auth = {"Cookie": cookie, "X-Feedback-CSRF": csrf}
        with patch.object(server.observed_routes, "match_provider", "tomtom"), \
             patch.object(server.observed_routes, "shadow_mode", True), \
             patch.object(server.observed_routes, "matching_enabled", return_value=True), \
             patch.object(server.observed_routes, "run_match_batch", return_value={"attempted": 1, "completed": 1}) as run, \
             patch.object(server.observed_routes, "health", return_value={}):
            code, data, _ = self.request("/api/admin/observed-match/run-one", {}, auth)
            self.assertEqual(code, 200)
            self.assertEqual(data["completed"], 1)
            run.assert_called_once_with(limit=1)
        with patch.object(server.observed_routes, "decide_review", return_value={"success": True, "status": "approved", "changed": True}) as decide:
            self.assertEqual(self.request("/api/admin/observed-review/decide", {
                "id": "a" * 64, "action": "approve"}, {"Cookie": cookie})[0], 403)
            code, data, _ = self.request("/api/admin/observed-review/decide", {
                "id": "a" * 64, "action": "approve", "note": "Sobre la calle"}, auth)
            self.assertEqual((code, data["status"]), (200, "approved"))
            decide.assert_called_once_with("a" * 64, "approve", "Sobre la calle", variant="original")
        with patch.object(server.observed_routes, "refine_review", return_value={
            "success": True, "changed": True, "removed_gps_points": 1}) as refine:
            self.assertEqual(self.request("/api/admin/observed-review/refine", {
                "id": "a" * 64}, {"Cookie": cookie})[0], 403)
            code, data, _ = self.request("/api/admin/observed-review/refine", {
                "id": "a" * 64}, auth)
            self.assertEqual((code, data["removed_gps_points"]), (200, 1))
            refine.assert_called_once_with("a" * 64)


if __name__ == "__main__":
    unittest.main()
