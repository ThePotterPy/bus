import json
import time
import unittest
from unittest.mock import patch

import server


def positions(*coords):
    return 200, json.dumps({"success": True, "data": [
        {"lat": lat, "lon": lon} for lat, lon in coords
    ]}).encode()


def subscription(lat=-25.3, lon=-57.6):
    return {
        "line": "30", "lineName": "Línea 30", "radiusKm": 1,
        "lat": lat, "lon": lon, "pushSubscription": {},
        "insideRadius": False, "updatedAt": time.time(),
    }


class NotificationTests(unittest.TestCase):
    def test_subscribe_restore_status_and_unsubscribe(self):
        handler = object.__new__(server.Handler)
        body = {
            "clientId": "device-1", "line": "30", "lineName": "Línea 30",
            "radiusKm": 0.5, "lat": -25.3, "lon": -57.6,
            "pushSubscription": {"endpoint": "test"},
        }
        replies = []
        handler._send_json_obj = lambda status, data: replies.append((status, data))
        handler._allow_request = lambda *args: True
        with (patch.object(server, "_subs", {}) as state,
              patch.object(server, "PUSH_AVAILABLE", True),
              patch.object(server, "known_line_ids", return_value={"30"}),
              patch.object(server.Handler, "_read_json_body", return_value=body),
              patch.object(server, "save_subs")):
            handler.path = "/api/notify/subscribe"
            handler.do_POST()
            self.assertEqual(replies[-1], (200, {"success": True}))
            state["device-1"]["insideRadius"] = True

            # Same subscription after a reload must not reset arrival detection.
            handler.do_POST()
            self.assertTrue(state["device-1"]["insideRadius"])
            handler.path = "/api/notify/status?clientId=device-1"
            handler.do_GET()
            self.assertTrue(replies[-1][1]["active"])
            self.assertEqual(replies[-1][1]["data"]["radiusKm"], 0.5)
            self.assertNotIn("lat", replies[-1][1]["data"])

        # Unsubscribe uses the same clientId and leaves no active state.
        with (patch.object(server, "_subs", {"device-1": subscription()}) as state,
              patch.object(server.Handler, "_read_json_body", return_value={"clientId": "device-1"}),
              patch.object(server, "save_subs")):
            handler.path = "/api/notify/unsubscribe"
            handler.do_POST()
            self.assertEqual(state, {})

    def test_missing_positions_preserve_state_but_not_indefinitely(self):
        sub = {"radiusKm": 1, "insideRadius": True, "lastObservedAt": 100}
        self.assertEqual(server.proximity_transition(sub, None, 120), (None, True))
        self.assertEqual(server.proximity_transition(sub, None, 401), (None, False))
        self.assertEqual(server.proximity_transition(sub, 1.1, 120), (None, True))
        self.assertEqual(server.proximity_transition(sub, 1.2, 120), (None, False))

    def test_new_bus_arrival_triggers_once_until_it_really_leaves(self):
        sub = subscription()
        with (patch.object(server, "_subs", {"device-1": sub}),
              patch.object(server, "get_positions_cached", return_value=positions((-25.3001, -57.6))) as fetch,
              patch.object(server, "send_push", return_value="sent") as push,
              patch.object(server, "save_subs")):
            server.check_notifications_once()
            self.assertTrue(sub["insideRadius"])
            self.assertEqual(push.call_count, 1)
            self.assertEqual(push.call_args.args[-1], "30")

            fetch.return_value = positions()
            server.check_notifications_once()
            self.assertTrue(sub["insideRadius"])
            fetch.return_value = positions((-25.310, -57.6))
            server.check_notifications_once()  # GPS just outside 1 km
            self.assertTrue(sub["insideRadius"])
            fetch.return_value = positions((-25.320, -57.6))
            server.check_notifications_once()
            self.assertFalse(sub["insideRadius"])
            fetch.return_value = positions((-25.3001, -57.6))
            server.check_notifications_once()
            self.assertEqual(push.call_count, 2)

    def test_failed_push_retries_later_and_expired_one_is_removed(self):
        sub = subscription()
        with (patch.object(server, "_subs", {"device-1": sub}) as state,
              patch.object(server, "get_positions_cached", return_value=positions((-25.3001, -57.6))),
              patch.object(server, "send_push", side_effect=["failed", "expired"]) as push,
              patch.object(server, "save_subs")):
            server.check_notifications_once()
            self.assertFalse(sub["insideRadius"])
            self.assertGreater(sub["pushRetryAfter"], time.time())
            server.check_notifications_once()
            self.assertEqual(push.call_count, 1)
            sub["pushRetryAfter"] = 0
            server.check_notifications_once()
            self.assertNotIn("device-1", state)

    def test_refreshing_same_subscription_keeps_inside_state(self):
        existing = subscription()
        existing["insideRadius"] = True
        self.assertTrue(server.same_notification_watch(existing, "30", 1, -25.3005, -57.6))
        self.assertFalse(server.same_notification_watch(existing, "187", 1, -25.3005, -57.6))
        self.assertFalse(server.same_notification_watch(existing, "30", 0.5, -25.3005, -57.6))
        self.assertFalse(server.same_notification_watch(existing, "30", 1, -25.32, -57.6))


if __name__ == "__main__":
    unittest.main()
