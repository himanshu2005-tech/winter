
import os
import sys

# Every other test file carries this. Without it these four only passed
# under a runner that happened to put the project root on sys.path, so
# `python tests/test_x.py` failed with ModuleNotFoundError -- a suite that
# depends on how it is invoked is not a suite you can trust.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt
import threading
import unittest
from unittest import mock

import watches


UTC = dt.timezone.utc


class FakeStore:
    def __init__(self):
        self.watches = {}
        self.observations = []

    def query(self, cypher, **params):
        flat = " ".join(cypher.split())
        if "MERGE (w:Watch {id:$id})" in flat:
            watch = self.watches.setdefault(params["id"], {
                "id": params["id"], "created_at": params["now"], "trigger_count": 0})
            watch.update({
                "kind": params["kind"], "source": params["source"],
                "condition": params["condition"], "target": params["target"],
                "label": params["label"], "currency": params["currency"],
                "interval_seconds": params["interval"], "status": "active",
                "next_check": params["now"], "updated_at": params["now"],
                "last_error": None, "consecutive_failures": 0,
            })
            return [{"id": watch["id"], "created_at": watch["created_at"]}]
        if "WHERE w.id=$wanted" in flat:
            wanted = params["wanted"].casefold()
            found = [w for w in self.watches.values()
                     if w["id"] == params["wanted"] or w["label"].casefold() == wanted]
            return [{"watch": dict(found[0])}] if found else []
        if "WHERE $status IS NULL" in flat:
            rows = self.watches.values()
            if params["status"] is not None:
                rows = [w for w in rows if w["status"] == params["status"]]
            return [{"watch": dict(w)} for w in rows]
        if "SET w.status=$status" in flat:
            watch = self.watches[params["id"]]
            watch["status"] = params["status"]
            watch["updated_at"] = params["now"]
            if params["status"] == "active":
                watch["next_check"] = params["now"]
            return [{"id": watch["id"]}]
        if "CREATE (o:WatchObservation" in flat:
            watch = self.watches[params["id"]]
            self.observations.append(dict(params))
            watch.update({
                "last_checked": params["now"], "last_value": params["value"],
                "last_method": params["method"], "last_error": None,
                "consecutive_failures": 0, "next_check": params["next_check"],
                "last_triggered": params["met"],
            })
            if params["crossed"]:
                watch["trigger_count"] += 1
                watch["last_alert_at"] = params["now"]
            return [{"id": watch["id"]}]
        if "w.last_error=$error" in flat:
            self.watches[params["id"]].update({
                "last_checked": params["now"], "last_error": params["error"],
                "consecutive_failures": params["failures"],
                "next_check": params["next_check"],
            })
            return [{"id": params["id"]}]
        if "MATCH (w:Watch {status:'active'})" in flat:
            return [{"watch": dict(w)} for w in self.watches.values()
                    if w["status"] == "active" and w["next_check"] <= params["now"]]
        if "[:OBSERVED]->(o:WatchObservation)" in flat:
            rows = [o for o in reversed(self.observations) if o["id"] == params["id"]]
            return [{"observation": {
                "observed_at": o["now"], "value": o["value"], "method": o["method"]}}
                for o in rows[:params["limit"]]]
        raise AssertionError(f"Unhandled query: {flat}")


def add_price_watch(store):
    store.watches["watch-1"] = {
        "id": "watch-1", "created_at": "2026-01-01T00:00:00+00:00",
        "kind": "price", "source": "https://example.com/item",
        "condition": "below", "target": "100000.0", "label": "Laptop",
        "currency": "INR", "interval_seconds": 300, "status": "active",
        "next_check": "2026-01-01T00:00:00+00:00", "trigger_count": 0,
        "last_triggered": False, "consecutive_failures": 0,
    }


class WatchParsingTests(unittest.TestCase):
    def test_indian_number_and_interval_parsing(self):
        self.assertEqual(watches.parse_number("1 lakh"), 100000)
        self.assertEqual(watches.parse_number("1.5 crore"), 15000000)
        self.assertEqual(watches.parse_number("Rs 99,999"), 99999)
        self.assertEqual(watches.parse_interval("1m"), 300)
        self.assertEqual(watches.parse_interval("2h"), 7200)

    def test_jsonld_price_wins_over_unrelated_visible_prices(self):
        html = '''<html><title>Good Laptop</title>
        <div>INR 199999</div>
        <script type="application/ld+json">
        {"@type":"Product","name":"Good Laptop",
         "offers":{"price":"99999","priceCurrency":"INR"}}
        </script></html>'''
        self.assertEqual(watches.extract_price(html), (
            99999.0, "INR", "Good Laptop", "json-ld Product.offers"))

    def test_visible_price_must_be_unambiguous(self):
        with self.assertRaisesRegex(ValueError, "multiple visible prices"):
            watches.extract_price("<title>x</title> INR 90,000 INR 1,20,000")

    def test_private_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "private"):
            watches._assert_public_url("http://127.0.0.1/admin")

    def test_currency_mismatch_is_not_a_match(self):
        watch = {"kind": "price", "condition": "below", "target": "100000",
                 "currency": "INR"}
        with self.assertRaisesRegex(ValueError, "currency mismatch"):
            watches.condition_met(watch, {"value": 500, "currency": "USD"})


class WatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        add_price_watch(self.store)
        self.now = dt.datetime(2026, 1, 2, tzinfo=UTC)

    @staticmethod
    def observation(value, currency="INR"):
        return {"value": value, "currency": currency, "title": "Laptop",
                "method": "synthetic structured data",
                "source_url": "https://example.com/item"}

    def test_alerts_only_on_false_to_true_crossing(self):
        first = watches.check_one("watch-1", self.store,
                                  lambda _: self.observation(99999), self.now)
        second = watches.check_one("watch-1", self.store,
                                   lambda _: self.observation(99000),
                                   self.now + dt.timedelta(minutes=5))
        reset = watches.check_one("watch-1", self.store,
                                  lambda _: self.observation(110000),
                                  self.now + dt.timedelta(minutes=10))
        crossed_again = watches.check_one(
            "watch-1", self.store, lambda _: self.observation(98000),
            self.now + dt.timedelta(minutes=15))
        self.assertTrue(first["crossed"])
        self.assertEqual(first["watch_id"], "watch-1")
        self.assertEqual(first["kind"], "price")
        self.assertFalse(second["crossed"])
        self.assertFalse(reset["crossed"])
        self.assertTrue(crossed_again["crossed"])
        self.assertEqual(self.store.watches["watch-1"]["trigger_count"], 2)
        self.assertEqual(len(self.store.observations), 4)

    def test_failure_records_backoff_without_alert(self):
        result = watches.check_one(
            "watch-1", self.store,
            lambda _: (_ for _ in ()).throw(ValueError("bot challenge")),
            self.now)
        self.assertFalse(result["ok"])
        self.assertNotIn("alert", result)
        self.assertEqual(result["retry_in"], 600)
        self.assertEqual(self.store.watches["watch-1"]["consecutive_failures"], 1)

    def test_pause_resume_cancel_and_history(self):
        self.assertIn("paused", watches.set_status("Laptop", "paused", self.store))
        self.assertIn("paused", watches.status("watch-1", self.store))
        self.assertIn("active", watches.set_status("watch-1", "active", self.store))
        watches.check_one("watch-1", self.store, lambda _: self.observation(120000), self.now)
        self.assertIn("History for Laptop", watches.history("watch-1", store_obj=self.store))
        self.assertIn("cancelled", watches.set_status("watch-1", "cancelled", self.store))

    def test_cancelled_dependencies_are_inactive(self):
        self.assertFalse(watches.dependency_state(["watch-1"], self.store)["all_inactive"])
        watches.set_status("watch-1", "cancelled", self.store)
        result = watches.dependency_state(["watch-1"], self.store)
        self.assertTrue(result["all_inactive"])
        self.assertEqual(result["states"], {"watch-1": "cancelled"})

    def test_missing_dependency_does_not_disable_work(self):
        result = watches.dependency_state(["watch-missing"], self.store)
        self.assertFalse(result["all_inactive"])

    def test_worker_can_stop_cleanly(self):
        stop = threading.Event()
        stop.set()
        alerts = []
        watches.worker(alerts.append, self.store, poll_seconds=5, stop_event=stop)
        self.assertEqual(alerts, [])

    def test_deterministic_watch_identity(self):
        one = watches._watch_id("price", "https://example.com/a", "below", "100")
        two = watches._watch_id("price", "https://example.com/a", "below", "100")
        self.assertEqual(one, two)


class WatchPolicyTests(unittest.TestCase):
    def test_watch_action_schema_catches_incomplete_requests(self):
        import action_schema
        error = action_schema.validate(
            "WATCH_CREATE", {"type": "WATCH_CREATE", "kind": "price",
                             "condition": "below"})
        self.assertIn("source", error)
        self.assertIsNone(action_schema.validate(
            "WATCH_STATUS", {"type": "WATCH_STATUS", "label": "Laptop"}))

    def test_unknown_and_mutating_proactive_actions_are_blocked(self):
        import autonomy
        self.assertFalse(autonomy.action_allowed("WATCH_CREATE", "proactive")[0])
        self.assertFalse(autonomy.action_allowed("WATCH_CANCEL", "proactive")[0])
        self.assertFalse(autonomy.action_allowed("WATCH_MAGIC", "user")[0])

    def test_active_url_reader_has_no_click_or_set_value(self):
        import inspect
        import desktop
        source = inspect.getsource(desktop.active_browser_url)
        self.assertNotIn(".Click(", source)
        self.assertNotIn("SetValue", source)


if __name__ == "__main__":
    unittest.main()
