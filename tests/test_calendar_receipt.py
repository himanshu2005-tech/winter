# -*- coding: utf-8 -*-
"""
A calendar write has to come back with evidence of what was written.

ADD_CALENDAR_EVENT returned one line: "Event created: <htmlLink>". The event
was correct -- Saturday 17 October at 05:00, confirmed afterwards by both
LIST_CALENDAR_EVENTS and READ_CALENDAR -- but the receipt proved none of it.

The planner had written success criteria asking for "an ADD_CALENDAR_EVENT
receipt showing the title, Saturday 17 October 2026 at 05:00, and Asia/Kolkata
timezone". The receipt structurally could not contain that, so the critic
failed the turn twice. The model then went looking for the evidence: it read
the calendar again, still failed, and finally opened the event URL in a
browser -- where a bare google.com/calendar/event?eid=... redirects to the
Google Calendar marketing page. Fifteen model calls, 118,900 tokens, a browser
window on a product page, and a final honest refusal to claim verification for
work that had in fact been done perfectly.

Nothing was wrong with the calendar. The receipt was simply silent, and a
silent receipt makes verification impossible and sends the model hunting.

The Google API already returns everything needed: the stored id, summary,
start time and timezone. The fix is to report what the server actually saved,
which is also a stronger claim than echoing back what was requested -- it is a
readback, not a restatement.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import google_api


#: What the Google API really hands back on insert.
CREATED = {
    "id": "c6gm724vqbh6o22bdof0bbv0b0",
    "summary": "Apply for Chennai gate pass",
    "htmlLink": "https://www.google.com/calendar/event?eid=YzZn",
    "start": {"dateTime": "2026-10-17T05:00:00+05:30",
              "timeZone": "Asia/Kolkata"},
    "end": {"dateTime": "2026-10-17T05:15:00+05:30",
            "timeZone": "Asia/Kolkata"},
}


class _Events:
    def __init__(self, created):
        self._created = created
        self.body = None

    def insert(self, calendarId=None, body=None):
        self.body = body
        return self

    def execute(self):
        return self._created


class _Service:
    def __init__(self, created):
        self._events = _Events(created)

    def events(self):
        return self._events


def _create(created=None, **kwargs):
    service = _Service(created if created is not None else CREATED)
    with patch.object(google_api, "get_credentials", return_value=object()), \
            patch("googleapiclient.discovery.build", return_value=service):
        receipt = google_api.add_calendar_event(
            kwargs.pop("base_dir", "."),
            kwargs.pop("start_time", "2026-10-17T05:00:00+05:30"),
            kwargs.pop("end_time", "2026-10-17T05:15:00+05:30"),
            kwargs.pop("summary", "Apply for Chennai gate pass"),
            **kwargs)
    return receipt, service


class TheReceiptProvesWhatWasStored(unittest.TestCase):
    def test_receipt_names_the_event_title(self):
        receipt, _service = _create()
        self.assertIn("Apply for Chennai gate pass", receipt)

    def test_receipt_states_the_date(self):
        receipt, _service = _create()
        self.assertIn("2026-10-17", receipt)

    def test_receipt_states_the_time(self):
        receipt, _service = _create()
        self.assertIn("05:00", receipt)

    def test_receipt_states_the_timezone(self):
        """The criterion the critic asked for twice and never got."""
        receipt, _service = _create()
        self.assertIn("Asia/Kolkata", receipt)

    def test_receipt_carries_the_event_id(self):
        """An id is what makes the event addressable afterwards."""
        receipt, _service = _create()
        self.assertIn("c6gm724vqbh6o22bdof0bbv0b0", receipt)

    def test_receipt_still_carries_the_link(self):
        receipt, _service = _create()
        self.assertIn("https://", receipt)

    def test_receipt_reports_the_server_value_not_the_request(self):
        """A readback, not an echo. The stored value is the evidence."""
        stored = dict(CREATED)
        stored["start"] = {"dateTime": "2026-10-17T09:30:00+05:30",
                           "timeZone": "Asia/Kolkata"}
        receipt, _service = _create(
            created=stored, start_time="2026-10-17T05:00:00+05:30")
        self.assertIn("09:30", receipt)
        self.assertNotIn("05:00", receipt)

    def test_an_all_day_event_reports_its_date(self):
        stored = {"id": "abc", "summary": "Birthday",
                  "htmlLink": "https://x", "start": {"date": "2026-10-24"},
                  "end": {"date": "2026-10-24"}}
        receipt, _service = _create(created=stored, summary="Birthday")
        self.assertIn("2026-10-24", receipt)
        self.assertIn("Birthday", receipt)

    def test_a_missing_timezone_does_not_break_the_receipt(self):
        stored = dict(CREATED)
        stored["start"] = {"dateTime": "2026-10-17T05:00:00+05:30"}
        receipt, _service = _create(created=stored)
        self.assertIn("2026-10-17", receipt)
        self.assertIn("Apply for Chennai gate pass", receipt)

    def test_a_failure_is_still_reported_as_an_error(self):
        class Broken:
            def events(self):
                raise RuntimeError("calendar unavailable")

        with patch.object(google_api, "get_credentials",
                          return_value=object()), \
                patch("googleapiclient.discovery.build",
                      return_value=Broken()):
            receipt = google_api.add_calendar_event(
                ".", "2026-10-17T05:00:00+05:30",
                "2026-10-17T05:15:00+05:30", "x")
        self.assertIn("error", receipt.lower())

    def test_the_requested_event_still_reaches_the_api(self):
        """The receipt change must not alter what gets created."""
        _receipt, service = _create()
        self.assertEqual(service.events().body["summary"],
                         "Apply for Chennai gate pass")

    def test_location_and_description_are_not_silently_dropped(self):
        stored = dict(CREATED)
        stored["location"] = "Main campus"
        stored["description"] = "Bring the ID card"
        receipt, service = _create(
            created=stored,
            location="Main campus", description="Bring the ID card")
        self.assertEqual(service.events().body["location"], "Main campus")
        self.assertEqual(service.events().body["description"],
                         "Bring the ID card")
        self.assertIn("location=Main campus", receipt)
        self.assertIn("description=Bring the ID card", receipt)

    def test_an_all_day_event_has_an_exclusive_next_day_end(self):
        _receipt, service = _create(
            start_time="", end_time="", event_date="2026-10-24",
            summary="Birthday")
        self.assertEqual(service.events().body["start"]["date"],
                         "2026-10-24")
        self.assertEqual(service.events().body["end"]["date"],
                         "2026-10-25")


class TheReceiptSatisfiesVerification(unittest.TestCase):
    """The criteria the planner actually wrote, against the new receipt."""

    def test_every_criterion_the_critic_demanded_is_present(self):
        receipt, _service = _create()
        for criterion in ("Apply for Chennai gate pass", "2026-10-17",
                          "05:00", "Asia/Kolkata"):
            with self.subTest(criterion=criterion):
                self.assertIn(criterion, receipt)

    def test_receipt_is_not_merely_a_url(self):
        """A bare link is what sent the model to a marketing page."""
        receipt, _service = _create()
        without_url = receipt.replace(CREATED["htmlLink"], "")
        self.assertGreater(len(without_url.strip()), 40)


if __name__ == "__main__":
    unittest.main()
