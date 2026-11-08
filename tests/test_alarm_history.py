
import os
import sys

# Every other test file carries this. Without it these four only passed
# under a runner that happened to put the project root on sys.path, so
# `python tests/test_x.py` failed with ModuleNotFoundError -- a suite that
# depends on how it is invoked is not a suite you can trust.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import unittest
from unittest import mock

import alarms


class AlarmHistoryTests(unittest.TestCase):
    def test_datetime_compatibility_accepts_string_python_and_neo4j_values(self):
        expected = datetime.datetime(2026, 10, 25, 8, 30,
                                     tzinfo=datetime.timezone.utc)

        class Neo4jDateTime:
            def to_native(self):
                return expected

        self.assertEqual(
            alarms._coerce_datetime("2026-10-25T08:30:00+00:00"), expected)
        self.assertIs(alarms._coerce_datetime(expected), expected)
        self.assertEqual(alarms._coerce_datetime(Neo4jDateTime()), expected)

    @mock.patch.object(alarms, "execute_read")
    def test_list_accepts_native_datetime_from_neo4j(self, read):
        stamp = datetime.datetime(2026, 10, 25, 8, 30,
                                  tzinfo=datetime.timezone.utc)
        read.side_effect = [
            [("alarm-1", stamp, "Breakfast", "none")],
            [],
        ]
        result = alarms.list_alarms()
        self.assertIn("Breakfast", result)

    @mock.patch.object(alarms.time, "sleep", side_effect=RuntimeError("stop"))
    @mock.patch.object(alarms, "execute_write")
    @mock.patch.object(alarms, "execute_read")
    def test_worker_accepts_native_datetime_from_neo4j(
            self, read, write, _sleep):
        fire = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=1)
        read.return_value = [("alarm-1", fire, "Breakfast", "none")]
        spoken = []
        with self.assertRaises(RuntimeError):
            alarms.alarm_worker(spoken.append)
        self.assertTrue(any("Breakfast" in item for item in spoken))
        self.assertTrue(any("status = 'fired'" in call.args[0]
                            for call in write.call_args_list))

    def test_data_errors_are_not_reported_as_neo4j_outages(self):
        self.assertFalse(alarms._looks_like_store_outage(
            TypeError("fromisoformat: argument must be str")))
        self.assertTrue(alarms._looks_like_store_outage(
            ConnectionError("connection refused")))

    @mock.patch.object(alarms.time, "sleep", side_effect=RuntimeError("stop"))
    @mock.patch.object(alarms, "execute_write")
    @mock.patch.object(alarms, "execute_read")
    def test_malformed_alarm_is_quarantined_without_blocking_valid_alarm(
            self, read, write, _sleep):
        fire = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=1)
        read.return_value = [
            ("broken", None, None, None),
            ("valid", fire, "Breakfast", "none"),
        ]
        spoken = []
        with self.assertRaises(RuntimeError):
            alarms.alarm_worker(spoken.append)

        calls = [call.args for call in write.call_args_list]
        self.assertIn(("UPDATE alarms SET status = 'invalid' WHERE id = ?",
                       ("broken",)), calls)
        self.assertIn(("UPDATE alarms SET status = 'fired' WHERE id = ?",
                       ("valid",)), calls)
        self.assertTrue(any("Breakfast" in item for item in spoken))

    @mock.patch.object(alarms, "execute_read")
    def test_list_includes_recent_missed_alarm_when_none_pending(self, read):
        stamp = "2026-09-29T08:00:00+05:30"
        read.side_effect = [[], [("alarm-1", stamp, "Wake up", "missed")]]
        result = alarms.list_alarms()
        self.assertIn("Current alarms: none", result)
        self.assertIn("Wake up (missed)", result)

    @mock.patch.object(alarms.time, "sleep", side_effect=RuntimeError("stop"))
    @mock.patch.object(alarms, "execute_write")
    @mock.patch.object(alarms, "execute_read")
    def test_startup_missed_notification_names_the_alarm(
            self, read, write, _sleep):
        fire = datetime.datetime.now().astimezone() - datetime.timedelta(hours=1)
        read.side_effect = [
            [("alarm-1", fire.isoformat(), "NxtWave assessment", "none")],
            [],
        ]
        spoken = []
        with self.assertRaises(RuntimeError):
            alarms.alarm_worker(spoken.append)
        self.assertTrue(any("NxtWave assessment" in item for item in spoken))
        self.assertTrue(any("missed" in item.lower() for item in spoken))


if __name__ == "__main__":
    unittest.main()
