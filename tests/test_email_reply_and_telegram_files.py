# -*- coding: utf-8 -*-
"""Exact Gmail replies and bidirectional Telegram file transport."""

import base64
from email import policy
from email.parser import BytesParser
import json
import os
import queue
import tempfile
import unittest
from unittest import mock

import actions
import approval_isolation
from providers import google_api, telegram_bot


class GmailReplyProviderTests(unittest.TestCase):
    @mock.patch("providers.google_api.build")
    @mock.patch("providers.google_api.get_credentials", return_value=object())
    def test_reply_uses_exact_message_thread_and_rfc_headers(self, _creds, build):
        service = mock.MagicMock()
        build.return_value = service
        original = {
            "id": "original-17", "threadId": "thread-9",
            "payload": {"headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "Reply-To", "value": "Replies <reply@example.com>"},
                {"name": "Subject", "value": "Project update"},
                {"name": "Message-ID", "value": "<rfc-original@example.com>"},
                {"name": "References", "value": "<older@example.com>"},
            ]},
        }
        messages = service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value = original
        messages.send.return_value.execute.return_value = {
            "id": "sent-18", "threadId": "thread-9"}

        result = google_api.reply_email(
            ".", "original-17", "Thanks, I will review it.")

        messages.get.assert_called_once_with(
            userId="me", id="original-17", format="metadata",
            metadataHeaders=["From", "Reply-To", "Subject", "Message-ID",
                             "References"])
        request = messages.send.call_args.kwargs["body"]
        self.assertEqual(request["threadId"], "thread-9")
        parsed = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(request["raw"].encode()))
        self.assertEqual(parsed["To"], "reply@example.com")
        self.assertEqual(parsed["Subject"], "Re: Project update")
        self.assertEqual(parsed["In-Reply-To"], "<rfc-original@example.com>")
        self.assertIn("<older@example.com>", parsed["References"])
        self.assertIn("<rfc-original@example.com>", parsed["References"])
        self.assertIn("original_id=original-17", result)
        self.assertIn("thread_id=thread-9", result)
        self.assertIn("id=sent-18", result)

    @mock.patch("providers.google_api.get_credentials")
    def test_missing_reply_attachment_fails_before_oauth(self, credentials):
        result = google_api.reply_email(
            ".", "m1", "Attached", attachments="missing-vanta-file.pdf")
        self.assertTrue(result.startswith("[Error]"))
        credentials.assert_not_called()


class GmailReplyActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ui = queue.Queue()

    def run_action(self, markup):
        actions.execute_actions(markup, {}, self.ui, self.temp.name,
                                origin="user")
        return actions.execute_actions._last_output

    @mock.patch("providers.google_api.reply_email")
    def test_reply_is_queued_once_before_provider(self, reply):
        with approval_isolation.no_approval_writes() as attempted:
            output = self.run_action(
                '<ACTION type="REPLY_EMAIL" message_id="m-specific">'
                'Yes, that works for me.</ACTION>')
        reply.assert_not_called()
        self.assertIn("Approval Required", output)
        queued = approval_isolation.queued_action_types(attempted)
        # The approval is durably mirrored to Neo4j and SQLite.  It is still
        # one logical approval, not two executable requests.
        self.assertIn("REPLY_EMAIL", queued)
        inserts = [params for query, params in attempted
                   if "insert into approvals" in query]
        payload = json.loads(inserts[0][2])
        self.assertEqual(payload["message_id"], "m-specific")
        self.assertEqual(payload["body"], "Yes, that works for me.")

    @mock.patch("providers.google_api.reply_email", return_value=(
        "[Success] original_id=m-specific; id=sent; thread_id=t1; "
        "recipient=x@example.com; subject='Re: Hi'; attachments=none"))
    def test_approved_reply_reaches_provider_once(self, reply):
        output = self.run_action(
            '<ACTION type="REPLY_EMAIL" message_id="m-specific" '
            'approved="true">Yes.</ACTION>')
        reply.assert_called_once_with(self.temp.name, "m-specific", "Yes.")
        self.assertIn("thread_id=t1", output)

    def test_read_digest_keeps_exact_reply_identifiers(self):
        digest = actions._email_digest_for_model(
            "[m77] (unread)\nFrom: A <a@example.com>\n"
            "Reply-To: replies@example.com\nThread-ID: t88\n"
            "Subject: Hello\nAttachments: None\nSnippet: private\n---")
        self.assertIn("message_id=m77", digest)
        self.assertIn("thread_id=t88", digest)
        self.assertIn("reply_to: replies@example.com", digest)


class FakeResponse:
    def __init__(self, data=None, content=b"", status=200, headers=None):
        self._data = data or {}
        self.status_code = status
        self._content = content
        self.content = content or b"json"
        self.text = json.dumps(self._data)
        self.headers = headers or {}

    def json(self):
        return self._data

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]


class TelegramFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = (telegram_bot._TOKEN, telegram_bot._ACTIVE_CHAT_ID,
                    telegram_bot._OWNER_CHAT_ID)
        telegram_bot._TOKEN = "test-token"
        telegram_bot._ACTIVE_CHAT_ID = 42
        telegram_bot._OWNER_CHAT_ID = 42

    def tearDown(self):
        (telegram_bot._TOKEN, telegram_bot._ACTIVE_CHAT_ID,
         telegram_bot._OWNER_CHAT_ID) = self.old

    def test_send_document_uses_send_document_and_returns_message_id(self):
        path = os.path.join(self.temp.name, "report.pdf")
        with open(path, "wb") as handle:
            handle.write(b"%PDF-report")
        session = mock.MagicMock()
        session.post.return_value = FakeResponse(
            {"ok": True, "result": {"message_id": 991}})
        with mock.patch.object(telegram_bot, "_session", return_value=session):
            result = telegram_bot.send_telegram_document(path, "Here it is")
        url = session.post.call_args.args[0]
        self.assertTrue(url.endswith("/sendDocument"))
        self.assertEqual(session.post.call_args.kwargs["data"]["chat_id"], 42)
        self.assertIn("message_id=991", result)
        self.assertIn("file=report.pdf", result)

    def test_missing_outbound_file_never_calls_network(self):
        session = mock.MagicMock()
        with mock.patch.object(telegram_bot, "_session", return_value=session):
            result = telegram_bot.send_telegram_document(
                os.path.join(self.temp.name, "missing.zip"))
        self.assertTrue(result.startswith("[Error]"))
        session.post.assert_not_called()

    def test_incoming_document_is_sanitized_saved_indexed_and_enqueued(self):
        session = mock.MagicMock()
        session.get.side_effect = [
            FakeResponse({"ok": True, "result": {"file_path": "docs/a.bin"}}),
            FakeResponse(content=b"telegram payload",
                         headers={"Content-Length": "16"}),
        ]
        input_queue = queue.Queue()
        message = {
            "chat": {"id": 42}, "caption": "Please inspect this",
            "document": {"file_id": "fid-1", "file_unique_id": "unique",
                         "file_name": "../../health.zip",
                         "mime_type": "application/zip"},
        }
        with mock.patch.object(telegram_bot, "_session", return_value=session), \
                mock.patch.object(telegram_bot, "send_telegram_message",
                                  return_value=True):
            handled = telegram_bot._handle_incoming_attachment(
                message, input_queue, self.temp.name, update_id=77)
        self.assertTrue(handled)
        prompt = input_queue.get_nowait()
        self.assertTrue(prompt.startswith("[Telegram]:"))
        self.assertIn("Please inspect this", prompt)
        listing = telegram_bot.list_received_files(self.temp.name)
        self.assertIn("health.zip", listing)
        self.assertNotIn("..", os.path.basename(
            listing.split("path=", 1)[1].split(" |", 1)[0]))
        saved_path = listing.split("path=", 1)[1].split(" |", 1)[0]
        with open(saved_path, "rb") as handle:
            self.assertEqual(handle.read(), b"telegram payload")

    def test_file_from_unpaired_chat_is_not_downloaded(self):
        session = mock.MagicMock()
        message = {"chat": {"id": 99},
                   "document": {"file_id": "fid", "file_name": "x.txt"}}
        with mock.patch.object(telegram_bot, "_session", return_value=session):
            handled = telegram_bot._handle_incoming_attachment(
                message, queue.Queue(), self.temp.name)
        self.assertTrue(handled)
        session.get.assert_not_called()

    def test_list_action_reports_the_real_local_path(self):
        inbox = os.path.join(self.temp.name, "vault", "telegram", "inbox")
        os.makedirs(inbox)
        path = os.path.join(inbox, "notes.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("hello")
        telegram_bot._append_received({
            "name": "notes.txt", "path": path, "size": 5,
            "mime": "text/plain", "received_at": "2026-10-25T12:00:00+05:30",
        }, self.temp.name)
        ui = queue.Queue()
        actions.execute_actions(
            '<ACTION type="LIST_TELEGRAM_FILES" limit="5" />', {}, ui,
            self.temp.name, origin="user")
        self.assertIn(os.path.abspath(path), actions.execute_actions._last_output)


if __name__ == "__main__":
    unittest.main()
