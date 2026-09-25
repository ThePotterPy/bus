import unittest
from unittest.mock import patch, Mock

import server


def route_payload(name="Centro (I)"):
    return {
        "services": [{
            "service_id": "service-1",
            "name": "Centro",
            "routes": [{
                "route_id": "route-1",
                "name": name,
                "sections": [
                    {
                        "order": 2,
                        "traces": [
                            {"order": 2, "latitud": -25.0, "longitud": -56.98},
                            {"order": 1, "latitud": -25.0, "longitud": -56.99},
                        ],
                    },
                    {
                        "order": 1,
                        "traces": [
                            {"order": 1, "latitud": -25.0, "longitud": -57.00},
                            {"order": 2, "latitud": -25.0, "longitud": -56.99},
                        ],
                    },
                ],
            }],
        }],
    }


class TripPlannerTests(unittest.TestCase):
    def test_combined_direction_is_not_mislabelled(self):
        self.assertIsNone(server.planner_route_direction("Centro Ida y Vuelta"))
        self.assertIsNone(server.planner_route_direction("Avenida"))
        self.assertEqual(server.planner_route_direction("Centro (I)"), "Ida")
        self.assertEqual(server.planner_route_direction("Centro Vuelta"), "Vuelta")

    def test_geocoder_requires_configuration_and_never_calls_public_service(self):
        with patch.object(server, "GEOCODER_SEARCH_URL", ""), patch.object(server, "_tomtom_api_key", ""), patch.object(server.urllib.request, "urlopen") as request:
            status, result = server.search_address("Terminal")
        self.assertEqual(status, 503)
        self.assertFalse(result["success"])
        request.assert_not_called()

    def test_geocoder_rejects_invalid_queries(self):
        for query in (None, {}, "ab", "x" * 201):
            self.assertEqual(server.search_address(query)[0], 400)

    def test_geocoder_caches_and_normalizes_configured_provider_results(self):
        response = Mock()
        response.read.return_value = b'[{"lat":"-25.3","lon":"-57.6","display_name":"Terminal"},{"lat":"nan","lon":"-57"}]'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(server, "GEOCODER_SEARCH_URL", "https://geocoder.example/search"), patch.object(server, "_geocode_cache", {}), patch.object(server, "_geocode_last_request", 0), patch.object(server.urllib.request, "urlopen", return_value=response) as request:
            first = server.search_address("Terminal")
            second = server.search_address("terminal")
            limited = server.search_address("Centro")
        self.assertEqual(first[0], 200)
        self.assertEqual(len(first[1]["data"]), 1)
        self.assertEqual(first, second)
        self.assertEqual(limited[0], 429)
        request.assert_called_once()

    def test_geocoder_uses_tomtom_server_side_for_paraguay(self):
        response = Mock()
        response.read.return_value = (b'{"results":['
            b'{"position":{"lat":-25.3,"lon":-57.6},"address":{"countryCode":"PY","freeformAddress":"Terminal, Asunci\\u00f3n"}},'
            b'{"position":{"lat":-34,"lon":-58},"address":{"countryCode":"AR","freeformAddress":"Buenos Aires"}}]}')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(server, "GEOCODER_SEARCH_URL", ""), patch.object(server, "_tomtom_api_key", "server-secret"), \
             patch.object(server, "_geocode_cache", {}), patch.object(server, "_geocode_last_request", 0), \
             patch.object(server.urllib.request, "urlopen", return_value=response) as request:
            status, payload = server.search_address("Terminal")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["display_name"], "Terminal, Asunción")
        self.assertIn("countrySet=PY", request.call_args.args[0].full_url)
        self.assertNotIn("server-secret", str(payload))

    def setUp(self):
        self.line = {"id": "30", "name": "LINEA 30", "provider": "jaha"}

    def test_sections_are_joined_in_order_as_one_route(self):
        routes = server.extract_planner_routes(self.line, route_payload())
        self.assertEqual(len(routes), 1)
        self.assertEqual(
            routes[0]["points"],
            [(-25.0, -57.0), (-25.0, -56.99), (-25.0, -56.98)],
        )
        self.assertEqual(routes[0]["direction"], "Ida")
        self.assertGreater(routes[0]["length_m"], 1900)

    def test_projection_uses_the_segment_between_sparse_vertices(self):
        route = server.extract_planner_routes(self.line, route_payload())[0]
        projection = server.project_point_to_planner_route(-25.0, -56.995, route)
        self.assertIsNotNone(projection)
        self.assertLess(projection["distance_m"], 1)
        self.assertAlmostEqual(projection["lon"], -56.995, places=5)
        self.assertGreater(projection["arc_m"], 400)

    def test_trip_returns_branch_direction_and_boarding_points(self):
        route = server.extract_planner_routes(self.line, route_payload())[0]
        with patch.object(server, "_all_paths_cache", {"30": [route]}):
            options = server.plan_trip(-25.0002, -56.999, -25.0002, -56.981)

        self.assertEqual(len(options), 1)
        option = options[0]
        self.assertEqual(option["routeName"], "Centro (I)")
        self.assertEqual(option["direction"], "Ida")
        self.assertLess(option["originWalkMeters"], 30)
        self.assertLess(option["destinationWalkMeters"], 30)
        self.assertGreater(option["rideDistanceMeters"], 1700)
        self.assertIn("lat", option["board"])
        self.assertIn("lon", option["alight"])

    def test_reverse_trip_is_not_offered_on_one_way_geometry(self):
        route = server.extract_planner_routes(self.line, route_payload())[0]
        with patch.object(server, "_all_paths_cache", {"30": [route]}):
            options = server.plan_trip(-25.0, -56.981, -25.0, -56.999)
        self.assertEqual(options, [])

    def test_refresh_accepts_jaha_and_mas_lines(self):
        lines = [
            self.line,
            {"id": "mas_7", "name": "LINEA 7", "provider": "mas"},
        ]
        previous_cache = server._all_paths_cache
        previous_lines = server._all_lines_info
        previous_refresh = server._planner_last_refresh
        try:
            server._all_paths_cache = {}
            with patch.object(server, "fetch_planner_path", side_effect=[route_payload(), route_payload("Mercado (V)")]):
                loaded = server.refresh_planner_paths(lines, max_workers=1)
            self.assertEqual(loaded, 2)
            self.assertIn("30", server._all_paths_cache)
            self.assertIn("mas_7", server._all_paths_cache)
            self.assertEqual(server._all_paths_cache["mas_7"][0]["provider"], "mas")
        finally:
            server._all_paths_cache = previous_cache
            server._all_lines_info = previous_lines
            server._planner_last_refresh = previous_refresh

    def test_disconnected_sections_are_not_a_direct_trip(self):
        data = route_payload()
        section = data['services'][0]['routes'][0]['sections'][0]
        for trace in section['traces']:
            trace['latitud'] -= .02
        routes = server.extract_planner_routes(self.line, data)
        with patch.object(server, '_all_paths_cache', {'30': routes}):
            self.assertEqual(server.plan_trip(-25, -56.999, -25.02, -56.981), [])

    def test_nearby_terminals_do_not_imply_continuous_service(self):
        data = route_payload()
        data['services'][0]['routes'][0]['sections'] = [{'traces': [
            {'order': i, 'latitud': lat, 'longitud': lon}
            for i, (lat, lon) in enumerate([
                (-25, -57), (-25, -56.99), (-25.01, -56.99), (-25.0001, -57),
            ])
        ]}]
        routes = server.extract_planner_routes(self.line, data)
        with patch.object(server, '_all_paths_cache', {'30': routes}):
            self.assertEqual(server.plan_trip(-25.005, -56.99, -25, -56.997), [])

    def test_identical_points_do_not_offer_a_full_loop(self):
        routes = server.extract_planner_routes(self.line, route_payload())
        routes[0]['closed'] = True
        with patch.object(server, '_all_paths_cache', {'30': routes}):
            self.assertEqual(server.plan_trip(-25, -56.995, -25, -56.995), [])

    def test_failed_catalog_refresh_keeps_last_routes(self):
        routes = server.extract_planner_routes(self.line, route_payload())
        with patch.object(server, '_all_paths_cache', {'30': routes}):
            self.assertEqual(server.refresh_planner_paths([]), 1)
            self.assertEqual(server._all_paths_cache['30'], routes)

    def test_missing_get_coordinates_are_rejected(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/planificar'
        handler._allow_request = Mock(return_value=True)
        handler._send_json = Mock()
        handler.do_GET()
        self.assertEqual(handler._send_json.call_args.args[0], 400)

    def test_malformed_post_coordinates_are_rejected(self):
        for body in (None, [], {}, {'olat': 'NaN', 'olon': 0, 'dlat': 1, 'dlon': 1}):
            with self.subTest(body=body):
                handler = object.__new__(server.Handler)
                handler.path = '/api/planificar'
                handler._allow_request = Mock(return_value=True)
                handler._read_json_body = Mock(return_value=body)
                handler._send_json_obj = Mock()
                handler.do_POST()
                self.assertEqual(handler._send_json_obj.call_args.args[0], 400)


if __name__ == "__main__":
    unittest.main()
