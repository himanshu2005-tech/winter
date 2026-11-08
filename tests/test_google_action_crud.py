# -*- coding: utf-8 -*-
"""Calendar and Gmail contracts through VANTA's real action dispatcher.

No live Google account is touched.  These tests prove that the action markup,
schema, policy gate, handler and provider arguments stay connected end to end.
"""

import queue
import base64
from email import policy
from email.parser import BytesParser
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import actions
import approval_isolation


class GoogleActionCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = queue.Queue()

    def run_action(self, markup):
        actions.execute_actions(
            markup, {}, self.ui, self.tmp.name, origin="user")
        return actions.execute_actions._last_output

    @patch("providers.google_api.add_calendar_event",
           return_value="Event created: Review start=2026-10-20T13:00:00+05:30 id=e1")
    def test_add_calendar_forwards_every_advertised_field(self, add):
        output = self.run_action(
            '<ACTION type="ADD_CALENDAR_EVENT" summary="Review" '
            'purpose="user_commitment" date="2026-10-20" '
            'start_time="13:00" end_time="14:00" location="Lab" '
            'description="Bring report" />')
        add.assert_called_once_with(
            self.tmp.name, "13:00", "14:00", "Review",
            event_date="2026-10-20", location="Lab",
            description="Bring report")
        self.assertIn("Event created", output)

    @patch("providers.google_api.update_calendar_event",
           return_value="Event updated: Review start=2026-10-20T15:00:00+05:30 id=e2")
    def test_update_calendar_reaches_provider(self, update):
        output = self.run_action(
            '<ACTION type="UPDATE_CALENDAR_EVENT" event_id="e2" '
            'start_time="15:00" summary="Final review" />')
        update.assert_called_once_with(
            self.tmp.name, "e2", start_time="15:00", end_time="",
            summary="Final review", location="", description="")
        self.assertIn("Event updated", output)

    @patch("providers.google_api.delete_calendar_event",
           return_value="Event e3 deleted successfully.")
    def test_delete_calendar_reaches_provider_without_approval(self, delete):
        output = self.run_action(
            '<ACTION type="DELETE_CALENDAR_EVENT" event_id="e3" />')
        delete.assert_called_once_with(self.tmp.name, "e3")
        self.assertIn("deleted successfully", output)

    @patch("email_memory.remember_emails", return_value=1)
    @patch("email_briefing.triage", return_value={
        "severity": "routine", "summary": "One routine update.",
        "important_ids": []})
    @patch("providers.google_api.read_emails", return_value=(
        "[m1] (unread)\nFrom: Team <team@example.com>\n"
        "Subject: Update\nSnippet: Details\n---"))
    def test_read_gmail_is_summarized(self, read, _triage, _remember):
        with patch.dict(os.environ, {"VANTA_EMAIL_MODEL_CONTENT": "1"}):
            output = self.run_action(
                '<ACTION type="READ_EMAILS" query="is:unread" />')
        read.assert_called_once_with(
            self.tmp.name, max_results=5, query="is:unread")
        self.assertIn("One routine update", output)
        # The configured task model receives the same bounded Gmail snippet
        # already used by the triage model, so it can draft a grounded reply.
        # The action still surfaces one concise summary rather than dumping
        # every raw message onto the dashboard or TTS queue.
        self.assertIn("snippet: Details", output)

    @patch("providers.google_api.send_email",
           return_value=("Email sent successfully to friend@example.com; "
                         "subject='Running late'; id=sent-1"))
    def test_send_gmail_is_queued_before_provider(self, send):
        with approval_isolation.no_approval_writes() as attempted:
            output = self.run_action(
                '<ACTION type="SEND_EMAIL" to="friend@example.com" '
                'subject="Running late">I will be there at five.</ACTION>')
        send.assert_not_called()
        self.assertIn("Approval Required", output)
        self.assertIn("SEND_EMAIL", approval_isolation.queued_action_types(attempted))
        inserts = [params for query, params in attempted
                   if "insert into approvals" in query]
        self.assertEqual(len(inserts), 1)
        payload = json.loads(inserts[0][2])
        self.assertEqual(payload["body"], "I will be there at five.")

    @patch("providers.google_api.send_email",
           return_value=("Email sent successfully to jobs@example.com; "
                         "subject='Resume'; id=sent-2; "
                         "attachments=resume.pdf"))
    def test_attribute_body_and_attachment_reach_one_approval(self, send):
        with approval_isolation.no_approval_writes() as attempted:
            output = self.run_action(
                '<ACTION type="SEND_EMAIL" to="jobs@example.com" '
                'subject="Resume" body="Attached as requested." '
                'attachments="C:\\\\Users\\\\himan\\\\resume.pdf" />')
        send.assert_not_called()
        self.assertIn("Approval Required", output)
        inserts = [params for query, params in attempted
                   if "insert into approvals" in query]
        self.assertEqual(len(inserts), 1)
        payload = json.loads(inserts[0][2])
        self.assertEqual(payload["body"], "Attached as requested.")
        self.assertTrue(payload["attachments"].endswith("resume.pdf"))

    @patch("providers.google_api.send_email",
           return_value=("Email sent successfully to friend@example.com; "
                         "subject='Running late'; id=sent-1"))
    def test_approved_send_gmail_reaches_provider_once(self, send):
        output = self.run_action(
            '<ACTION type="SEND_EMAIL" to="friend@example.com" '
            'subject="Running late" approved="true">'
            'I will be there at five.</ACTION>')
        send.assert_called_once_with(
            self.tmp.name, "friend@example.com", "Running late",
            "I will be there at five.")
        self.assertIn("sent successfully", output)

    @patch("providers.google_api.send_email",
           return_value=("Email sent successfully to friend@example.com; "
                         "subject='Resume'; id=sent-3; attachments=resume.pdf"))
    def test_approved_send_forwards_attachment(self, send):
        path = os.path.join(self.tmp.name, "resume.pdf")
        with open(path, "wb") as handle:
            handle.write(b"pdf")
        output = self.run_action(
            '<ACTION type="SEND_EMAIL" to="friend@example.com" '
            f'subject="Resume" attachments="{path}" approved="true">'
            'Attached.</ACTION>')
        send.assert_called_once_with(
            self.tmp.name, "friend@example.com", "Resume", "Attached.",
            attachments=path)
        self.assertIn("attachments=resume.pdf", output)

    @patch("providers.google_api.modify_email",
           return_value="Email m2 marked read.")
    def test_mark_gmail_read_reaches_provider(self, modify):
        output = self.run_action(
            '<ACTION type="MARK_EMAIL_READ" message_id="m2" />')
        modify.assert_called_once_with(
            self.tmp.name, "m2", mark_read=True, archive=False)
        self.assertIn("marked read", output)

    @patch("providers.google_api.modify_email",
           return_value="Email m3 archived.")
    def test_archive_gmail_reaches_provider(self, modify):
        output = self.run_action(
            '<ACTION type="ARCHIVE_EMAIL" message_id="m3" />')
        modify.assert_called_once_with(
            self.tmp.name, "m3", mark_read=False, archive=True)
        self.assertIn("archived", output)


class GmailAttachmentProviderTests(unittest.TestCase):
    @patch("providers.google_api.get_credentials")
    def test_missing_attachment_fails_before_oauth_or_send(self, credentials):
        from providers import google_api

        result = google_api.send_email(
            ".", "jobs@example.com", "Resume", "Attached",
            attachments=r"C:\definitely-missing-vanta-resume.pdf")
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("does not exist", result)
        credentials.assert_not_called()

    @patch("providers.google_api.build")
    @patch("providers.google_api.get_credentials", return_value=object())
    def test_attachment_is_mime_encoded_and_receipted(self, _credentials,
                                                       build):
        from providers import google_api

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "resume.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-vanta")

            service = MagicMock()
            build.return_value = service
            service.users.return_value.messages.return_value.send.return_value \
                .execute.return_value = {"id": "gmail-123"}
            result = google_api.send_email(
                ".", "jobs@example.com", "Resume", "Attached",
                attachments=json.dumps([path]))

        request = (service.users.return_value.messages.return_value.send
                   .call_args.kwargs["body"])
        raw = base64.urlsafe_b64decode(request["raw"].encode())
        message = BytesParser(policy=policy.default).parsebytes(raw)
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "resume.pdf")
        self.assertEqual(attachments[0].get_content_type(), "application/pdf")
        self.assertEqual(attachments[0].get_payload(decode=True), b"%PDF-vanta")
        self.assertIn("id=gmail-123", result)
        self.assertIn("attachments=resume.pdf", result)


class CalendarUpdateNormalizationTests(unittest.TestCase):
    class Events:
        def __init__(self, event):
            self.event = event
            self.updated_body = None

        def get(self, **_kwargs):
            return self

        def update(self, body=None, **_kwargs):
            self.updated_body = body
            return self

        def execute(self):
            if self.updated_body is not None:
                return self.updated_body
            return dict(self.event)

    class Service:
        def __init__(self, event):
            self.api = CalendarUpdateNormalizationTests.Events(event)

        def events(self):
            return self.api

    def test_time_only_reschedule_preserves_date_and_duration(self):
        from providers import google_api
        event = {
            "id": "e4", "summary": "Review", "htmlLink": "https://x/e4",
            "start": {"dateTime": "2026-10-20T13:00:00+05:30"},
            "end": {"dateTime": "2026-10-20T14:00:00+05:30"},
        }
        service = self.Service(event)
        with patch.object(google_api, "get_credentials", return_value=object()), \
                patch.object(google_api, "build", return_value=service):
            receipt = google_api.update_calendar_event(
                ".", "e4", start_time="15:30")
        body = service.api.updated_body
        self.assertEqual(body["start"]["dateTime"],
                         "2026-10-20T15:30:00+05:30")
        self.assertEqual(body["end"]["dateTime"],
                         "2026-10-20T16:30:00+05:30")
        self.assertIn("15:30", receipt)
        self.assertIn("16:30", receipt)


if __name__ == "__main__":
    unittest.main()
