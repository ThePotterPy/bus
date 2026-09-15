import io
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ServerSecurityTests(unittest.TestCase):
    def test_static_files_cannot_escape_static_directory(self):
        static = (Path(server.__file__).parent / "static").resolve()
        with patch.object(server, "STATIC_DIR", static):
            self.assertEqual(server.resolve_static_file("index.html"), (static / "index.html").resolve())
            self.assertIsNone(server.resolve_static_file("../server.py"))
            self.assertIsNone(server.resolve_static_file("missing.txt"))

    def test_manifest_has_correct_content_type(self):
        self.assertEqual(
            server.static_content_type(Path("manifest.json")),
            "application/manifest+json; charset=utf-8",
        )

    def test_basemap_uses_openfreemap_instead_of_osm_volunteer_tiles(self):
        index = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("https://tiles.openfreemap.org/styles/positron", index)
        self.assertNotIn("tile.openstreetmap.org", index)

    def test_live_gps_points_use_burgundy(self):
        static = Path(server.__file__).parent / "static"
        renderer = (static / "observed-routes.js").read_text(encoding="utf-8")
        index = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("const liveGpsColor = '#8B1E3F'", renderer)
        self.assertIn("background: #8B1E3F", index)

    def test_json_body_rejects_oversized_payload_without_reading_it(self):
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": str(server.MAX_JSON_BODY_BYTES + 1)}
        handler.rfile = io.BytesIO(b"")
        with self.assertRaises(server.RequestBodyError) as caught:
            handler._read_json_body()
        self.assertEqual(caught.exception.status, 413)

    def test_json_body_rejects_invalid_json(self):
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": "4"}
        handler.rfile = io.BytesIO(b"nope")
        with self.assertRaises(server.RequestBodyError) as caught:
            handler._read_json_body()
        self.assertEqual(caught.exception.status, 400)

    def test_json_body_accepts_small_object(self):
        raw = json.dumps({"lines": ["30", "mas_12"]}).encode()
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        self.assertEqual(handler._read_json_body(), {"lines": ["30", "mas_12"]})

    def test_public_identifiers_and_coordinates_are_validated(self):
        self.assertTrue(server.valid_line_id("30"))
        self.assertTrue(server.valid_line_id("mas_12"))
        self.assertFalse(server.valid_line_id("../server.py"))
        self.assertFalse(server.valid_line_id("x" * 81))
        self.assertTrue(server.valid_coordinates(-25.3, -57.6))
        self.assertFalse(server.valid_coordinates(float("nan"), -57.6))
        self.assertFalse(server.valid_coordinates(-95, -57.6))
        self.assertTrue(server.valid_client_id("device_123-abc"))
        self.assertFalse(server.valid_client_id("bad client"))

    def test_rate_limiter_resets_after_window(self):
        limiter = server.FixedWindowRateLimiter()
        with patch.object(server.time, "monotonic", side_effect=[10.0, 10.1, 10.2, 21.0]):
            self.assertTrue(limiter.allow("key", 2, 10)[0])
            self.assertTrue(limiter.allow("key", 2, 10)[0])
            self.assertFalse(limiter.allow("key", 2, 10)[0])
            self.assertTrue(limiter.allow("key", 2, 10)[0])

    def test_catalog_cache_keeps_last_complete_copy_when_jaha_fails(self):
        previous = [{"id": "30", "name": "LINEA 30"}]
        with (
            patch.object(server, "_combined_lines_cache", previous),
            patch.object(server, "_combined_lines_cache_time", time.time() - 1000),
            patch.object(server, "_fetch_all_lines_combined", return_value=([{"id": "mas_1"}], False)),
        ):
            self.assertEqual(server.get_all_lines_combined(), previous)

    def test_subscription_file_is_atomic_and_expired_entries_are_removed(self):
        path = Path(__file__).with_name("_subscriptions_test.json")
        temporary = path.with_suffix(".tmp")
        previous = server._subs
        path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        try:
            with patch.object(server, "SUBS_FILE", path):
                server._subs = {"active": {"updatedAt": time.time(), "line": "30"}}
                server.save_subs()
                self.assertTrue(path.is_file())
                self.assertFalse(temporary.exists())

                path.write_text(json.dumps({
                    "active": {"updatedAt": time.time(), "line": "30"},
                    "expired": {"updatedAt": time.time() - server.SUBSCRIPTION_TTL_SECONDS - 1, "line": "30"},
                }), encoding="utf-8")
                server.load_subs()
                self.assertEqual(set(server._subs), {"active"})
        finally:
            server._subs = previous
            path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
