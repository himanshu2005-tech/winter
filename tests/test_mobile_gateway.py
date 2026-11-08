import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import mobile_gateway
import turn_context


class MobileGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.input_queue = queue.Queue()
        mobile_gateway.configure(self.temp.name, self.input_queue)
        mobile_gateway._events.clear()
        mobile_gateway._event_id = 0
        mobile_gateway._pair_attempts.clear()
        mobile_gateway._downloads.clear()
        self.environment = patch.dict(os.environ, {
            "AUTH_PASSWORD": "correct horse battery staple",
            "VANTA_MOBILE_ALLOWED_ORIGINS": "https://vanta-test.web.app",
        }, clear=False)
        self.environment.start()
        app = await mobile_gateway._create_app()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    async def pair(self):
        response = await self.client.post('/api/pair', json={
            "password": "correct horse battery staple",
            "device_name": "Test iPhone",
        }, headers={"Origin": "https://vanta-test.web.app"})
        self.assertEqual(response.status, 200)
        return (await response.json())["token"]

    async def test_pairing_hashes_token_and_never_persists_password(self):
        token = await self.pair()
        raw = (Path(self.temp.name) / "vault/mobile/sessions.json").read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn("correct horse battery staple", raw)
        row = json.loads(raw)[0]
        self.assertEqual(row["device_name"], "Test iPhone")
        self.assertTrue(mobile_gateway._valid_token(token))

    async def test_wrong_origin_is_rejected(self):
        response = await self.client.get('/health', headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)

    async def test_message_reenters_the_existing_mobile_channel(self):
        token = await self.pair()
        response = await self.client.post('/api/messages', json={"text": "check my calendar"},
                                          headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.input_queue.get_nowait(), "[Mobile]: check my calendar")
        self.assertEqual(turn_context.detect("[Mobile]: hello"), turn_context.MOBILE)
        self.assertEqual(turn_context.strip_prefix("[Mobile]: hello"), "hello")

    async def test_file_upload_is_atomic_and_queued_for_the_agent(self):
        token = await self.pair()
        form = FormData()
        form.add_field('file', b'hello from phone', filename='note.txt', content_type='text/plain')
        response = await self.client.post('/api/files', data=form,
                                          headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["name"], "note.txt")
        queued = self.input_queue.get_nowait()
        self.assertIn("[Mobile]: I sent you a file", queued)
        stored = list((Path(self.temp.name) / "vault/mobile/inbox").rglob("note.txt"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), b'hello from phone')

    async def test_voice_upload_uses_a_distinct_transcription_queue_mode(self):
        token = await self.pair()
        form = FormData()
        form.add_field('file', b'not-real-audio', filename='voice.webm',
                       content_type='audio/webm')
        response = await self.client.post('/api/voice', data=form,
                                          headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)
        mode, path = self.input_queue.get_nowait()
        self.assertEqual(mode, "mobile_audio")
        self.assertTrue(Path(path).exists())

    async def test_approval_requires_authentication(self):
        response = await self.client.post('/api/approvals/abc', json={"decision": "approve"})
        self.assertEqual(response.status, 401)

    async def test_offered_file_requires_token_and_downloads_exact_bytes(self):
        token = await self.pair()
        source = Path(self.temp.name) / "answer.pdf"
        source.write_bytes(b"synthetic-report")
        event = mobile_gateway.offer_file(source, title="Requested report")
        url = event["payload"]["url"]
        denied = await self.client.get(url)
        self.assertEqual(denied.status, 401)
        response = await self.client.get(
            url, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"synthetic-report")
        self.assertIn("answer.pdf", response.headers["Content-Disposition"])


class MobileEventQueueTests(unittest.TestCase):
    def test_mirroring_does_not_consume_desktop_event(self):
        target = mobile_gateway.EventQueue()
        with patch.object(mobile_gateway, 'publish_ui_event') as publish:
            target.put(("agent_response", "hello"))
        self.assertEqual(target.get_nowait(), ("agent_response", "hello"))
        publish.assert_called_once_with(("agent_response", "hello"))

    def test_mobile_prefix_is_not_echoed_to_the_phone(self):
        with patch.object(mobile_gateway, 'publish') as publish:
            mobile_gateway.publish_ui_event(("user_query", "[Mobile]: hello"))
        publish.assert_called_once_with("user_query", "hello")


if __name__ == '__main__':
    unittest.main()
