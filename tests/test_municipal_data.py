import unittest
from unittest.mock import Mock

import server


class MunicipalDataTests(unittest.TestCase):
    def test_municipal_data_loaded(self):
        self.assertGreaterEqual(len(server._asuncion_stops), 100)
        self.assertGreaterEqual(len(server._asuncion_traffic_lights), 150)
        self.assertGreaterEqual(len(server._asuncion_pois), 300)

    def test_find_nearest_official_stop(self):
        # Aviadores del Chaco y Cesar Lopez Moreira: -25.285991, -57.570986
        stop = server.find_nearest_official_stop(-25.2860, -57.5710, max_meters=150.0)
        self.assertIsNotNone(stop)
        self.assertIn("Aviadores del Chaco", stop["name"])
        self.assertLessEqual(stop["distanceMeters"], 30)

        # Punto lejano en San Lorenzo o Chaco: no debe encontrar parada
        far_stop = server.find_nearest_official_stop(-25.0, -57.0, max_meters=150.0)
        self.assertIsNone(far_stop)

    def test_search_pois(self):
        clinicas = server.search_pois("clinicas", limit=5)
        self.assertGreaterEqual(len(clinicas), 1)
        self.assertTrue(any("CLINICAS" in p["name"].upper() for p in clinicas))

        terminal = server.search_pois("terminal", limit=5)
        self.assertGreaterEqual(len(terminal), 1)

        empty = server.search_pois("x", limit=5)
        self.assertEqual(empty, [])

    def test_endpoints_responses(self):
        # /api/stops
        handler = object.__new__(server.Handler)
        handler.path = "/api/stops"
        handler._send_json_obj = Mock()
        handler.do_GET()
        handler._send_json_obj.assert_called_once()
        code, data = handler._send_json_obj.call_args.args[:2]
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertGreaterEqual(len(data["data"]), 100)

        # /api/traffic-lights
        handler = object.__new__(server.Handler)
        handler.path = "/api/traffic-lights"
        handler._send_json_obj = Mock()
        handler.do_GET()
        handler._send_json_obj.assert_called_once()
        code, data = handler._send_json_obj.call_args.args[:2]
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertGreaterEqual(len(data["data"]), 150)

        # /api/pois?q=salud
        handler = object.__new__(server.Handler)
        handler.path = "/api/pois?q=salud"
        handler._send_json_obj = Mock()
        handler.do_GET()
        handler._send_json_obj.assert_called_once()
        code, data = handler._send_json_obj.call_args.args[:2]
        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        self.assertGreater(len(data["data"]), 0)


if __name__ == "__main__":
    unittest.main()
