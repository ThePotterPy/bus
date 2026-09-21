import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

import server
from shared_trips import SharedTripStore


TEST_ROOT = Path(server.__file__).parent / "data"


def make_store():
    store = SharedTripStore(TEST_ROOT / f"_shared_trip_test_{uuid.uuid4().hex}.sqlite3")
    store.init()
    return store


def cleanup(store):
    for suffix in ("", "-wal", "-shm"):
        store.path.with_name(store.path.name + suffix).unlink(missing_ok=True)


class SharedTripStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        cleanup(self.store)

    def test_trip_has_separate_tokens_and_never_stores_coordinates(self):
        created = self.store.create("device-1", intent="trip", line_id="30", unit_id="101", now=1000)
        self.assertNotEqual(created["publicToken"], created["ownerToken"])
        viewed = self.store.get(created["publicToken"], now=1001)
        self.assertEqual(viewed["status"], "on_bus")
        self.assertNotIn("owner_hash", viewed)
        db = sqlite3.connect(self.store.path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(shared_trips)")}
        finally:
            db.close()
        self.assertFalse({"lat", "lon", "latitude", "longitude"} & columns)

    def test_three_reliable_away_readings_over_a_minute_end_trip(self):
        created = self.store.create("device-1", intent="trip", line_id="30", unit_id="101", now=1000)
        public, owner = created["publicToken"], created["ownerToken"]
        self.store.heartbeat(public, owner, reliable=True, distance_m=900, now=1010)
        self.store.heartbeat(public, owner, reliable=True, distance_m=950, now=1040)
        result = self.store.heartbeat(public, owner, reliable=True, distance_m=1000, now=1071)
        self.assertEqual(result["status"], "ended")
        self.assertEqual(self.store.get(public, now=1072)["end_reason"], "left_bus")

    def test_bad_gps_does_not_count_as_away_and_stop_share_can_become_bus_trip(self):
        created = self.store.create("device-1", intent="waiting", stop_id="refugio-1", now=1000)
        public, owner = created["publicToken"], created["ownerToken"]
        self.assertTrue(self.store.board(public, owner, "30", "101", now=1100))
        self.store.heartbeat(public, owner, reliable=False, distance_m=None, now=1150)
        viewed = self.store.get(public, now=1151)
        self.assertEqual(viewed["status"], "on_bus")
        self.assertEqual(viewed["away_count"], 0)

    def test_owner_secret_is_required_for_changes(self):
        created = self.store.create("device-1", intent="going", stop_id="refugio-1", now=1000)
        self.assertIsNone(self.store.get_owned(created["publicToken"], "wrong", now=1001))
        self.assertIsNotNone(self.store.get_owned(created["publicToken"], created["ownerToken"], now=1001))
        self.assertFalse(self.store.set_destination(created["publicToken"], "wrong", "refugio-2", now=1001))
        self.assertTrue(self.store.set_destination(created["publicToken"], created["ownerToken"], "refugio-2", now=1001))

    def test_signal_loss_has_grace_period_then_ends(self):
        created = self.store.create("device-1", intent="trip", line_id="30", unit_id="101", now=1000)
        public = created["publicToken"]
        self.assertEqual(self.store.get(public, now=1091)["verification"], "unavailable")
        ended = self.store.get(public, now=1901)
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(ended["end_reason"], "signal_lost")


class SharedTripHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.store_patch = patch.object(server, "shared_trip_store", self.store)
        self.store_patch.start()
        self.bus = {
            "unit": "101", "lat": -25.28599, "lon": -57.57098,
            "status": "EN MOVIMIENTO", "route": "Centro", "time": "12:00:00",
            "speed": 20, "bearing": 90, "provider": "jaha",
        }
        self.bus_patch = patch.object(server, "current_bus", side_effect=lambda line, unit: self.bus if line == "30" and unit == "101" else None)
        self.current_bus_mock = self.bus_patch.start()
        self.limit_patch = patch.object(server, "_rate_limiter", server.FixedWindowRateLimiter())
        self.limit_patch.start()
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        self.limit_patch.stop()
        self.bus_patch.stop()
        self.store_patch.stop()
        cleanup(self.store)

    def request(self, path, body):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_bus_share_exposes_bus_and_stops_but_not_passenger_coordinates(self):
        status, created = self.request("/api/shared-trip/start", {
            "clientId": "device-1", "kind": "bus", "intent": "trip",
            "lineId": "30", "unitId": "101", "lat": -25.28599,
            "lon": -57.57098, "accuracy": 15, "destinationStopId": "refugio-1",
        })
        self.assertEqual(status, 201)
        status, viewed = self.request("/api/shared-trip/view", {"token": created["publicToken"]})
        self.assertEqual(status, 200)
        self.assertEqual(viewed["trip"]["bus"]["unit"], "101")
        self.assertEqual(viewed["trip"]["destination"]["id"], "refugio-1")
        self.assertNotIn("ownerToken", json.dumps(viewed))
        self.assertNotIn("lat", {key.lower() for key in viewed["trip"] if key != "bus"})
        self.assertNotIn("lon", {key.lower() for key in viewed["trip"] if key != "bus"})

    def test_waiting_at_stop_requires_proximity(self):
        body = {"clientId": "device-1", "kind": "stop", "intent": "waiting", "stopId": "refugio-1", "accuracy": 20}
        status, _ = self.request("/api/shared-trip/start", {**body, "lat": 0, "lon": 0})
        self.assertEqual(status, 409)
        stop = server.official_stop("refugio-1")
        status, _ = self.request("/api/shared-trip/start", {**body, "lat": stop["lat"], "lon": stop["lon"]})
        self.assertEqual(status, 201)

    def test_invalid_kind_intent_pair_is_rejected(self):
        status, _ = self.request("/api/shared-trip/start", {
            "clientId": "device-1", "kind": "stop", "intent": "trip", "stopId": "refugio-1",
        })
        self.assertEqual(status, 400)

    def test_wrong_owner_does_not_trigger_bus_lookup(self):
        created = self.store.create("device-1", intent="trip", line_id="30", unit_id="101")
        self.current_bus_mock.reset_mock()
        status, _ = self.request("/api/shared-trip/update", {
            "token": created["publicToken"], "ownerToken": "wrong", "action": "heartbeat",
            "lat": self.bus["lat"], "lon": self.bus["lon"], "accuracy": 10,
        })
        self.assertEqual(status, 403)
        self.current_bus_mock.assert_not_called()

    def test_board_rejects_unknown_destination_before_bus_lookup(self):
        created = self.store.create("device-1", intent="waiting", stop_id="refugio-1")
        self.current_bus_mock.reset_mock()
        status, _ = self.request("/api/shared-trip/update", {
            "token": created["publicToken"], "ownerToken": created["ownerToken"], "action": "board",
            "lineId": "30", "unitId": "101", "destinationStopId": "does-not-exist",
            "lat": self.bus["lat"], "lon": self.bus["lon"], "accuracy": 10,
        })
        self.assertEqual(status, 403)
        self.current_bus_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
