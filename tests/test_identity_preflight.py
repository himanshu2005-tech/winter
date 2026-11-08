# -*- coding: utf-8 -*-
"""Face authentication is the first process in the official startup path."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import face_identity


ROOT = Path(__file__).resolve().parents[1]


def _vector(index=0):
    value = np.zeros(512, dtype=np.float32)
    value[index] = 1.0
    return value


class BootIdentityCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.temp.name, "boot.json")
        self.ticket = os.path.join(self.temp.name, "ticket.json")
        self.patches = (
            mock.patch.object(face_identity, "BOOT_CACHE_PATH", self.cache),
            mock.patch.object(face_identity, "BOOT_TICKET_PATH", self.ticket),
            mock.patch.object(face_identity, "_key", return_value=b"k" * 32),
            mock.patch.object(face_identity, "_restrict_boot_file"),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def row(self, person="Himanshu", index=0):
        return {
            "person": person,
            "template_id": f"face-{index}",
            "ciphertext": face_identity._encrypt_embedding(_vector(index)),
            "model": face_identity.MODEL_NAME,
            "embedding_dim": 512,
        }

    def test_cache_contains_only_primary_encrypted_templates(self):
        count = face_identity.sync_boot_templates([
            self.row("Himanshu", 0), self.row("Madhu", 1)])
        self.assertEqual(count, 1)
        raw = Path(self.cache).read_text(encoding="utf-8")
        self.assertNotIn("Madhu", raw)
        self.assertNotIn(json.dumps(_vector(0).tolist()), raw)
        status = face_identity.boot_cache_status()
        self.assertTrue(status["ready"], status)
        self.assertEqual(status["templates"], 1)

    def test_tampering_fails_closed(self):
        face_identity.sync_boot_templates([self.row()])
        document = json.loads(Path(self.cache).read_text(encoding="utf-8"))
        document["payload"]["primary_person"] = "Someone Else"
        Path(self.cache).write_text(json.dumps(document), encoding="utf-8")
        self.assertFalse(face_identity.boot_cache_status()["ready"])

    def test_boot_match_never_reads_neo4j(self):
        face_identity.sync_boot_templates([self.row()])
        face = face_identity.DetectedFace(
            bbox=(0, 0, 120, 120), normalized_box=(0, 0, 1, 1),
            detection_score=.99, quality=.99, embedding=_vector(0))
        with mock.patch.object(face_identity, "detect_image",
                               return_value=(face,)), \
             mock.patch("memory.neo4j_store.store.active_face_templates",
                        side_effect=AssertionError("graph must remain offline")):
            result = face_identity.verify_primary_image_boot(
                np.zeros((120, 120, 3), dtype=np.uint8))
        self.assertTrue(result["matched"])

    def test_startup_ticket_is_recent_signed_and_one_use(self):
        face_identity.issue_startup_ticket()
        self.assertTrue(face_identity.consume_startup_ticket())
        self.assertFalse(face_identity.consume_startup_ticket())

    def test_stale_ticket_is_rejected_and_destroyed(self):
        payload = {"issued_at": time.time() - 999, "nonce": "old"}
        face_identity._write_signed(
            self.ticket, "startup-ticket", payload)
        self.assertFalse(face_identity.consume_startup_ticket(max_age=10))
        self.assertFalse(os.path.exists(self.ticket))


class StartupOrderTests(unittest.TestCase):
    def test_launcher_authenticates_before_neo4j_and_agent(self):
        source = (ROOT / "start.bat").read_text(encoding="utf-8").lower()
        preflight = source.index("identity_preflight.py")
        neo4j = source.index("start_neo4j.py")
        agent = source.index("agent.py")
        self.assertLess(preflight, neo4j)
        self.assertLess(neo4j, agent)
        between = source[preflight:neo4j]
        self.assertIn("if errorlevel 1", between)
        self.assertIn("exit /b 1", between)

    def test_preflight_does_not_import_heavy_services(self):
        source = (ROOT / "scripts" / "identity_preflight.py").read_text(
            encoding="utf-8")
        self.assertNotIn("neo4j_store", source)
        self.assertNotIn("import agent", source)
        self.assertNotIn("llm_router", source)

    def test_agent_consumes_ticket_once_or_falls_back_to_gate(self):
        source = (ROOT / "agent.py").read_text(encoding="utf-8")
        consume = source.index("consume_startup_ticket()")
        fallback = source.index('authentication_gate.run("startup")', consume)
        self.assertLess(consume, fallback)


class TelegramPreflightTests(unittest.TestCase):
    def test_consumed_auth_update_offset_survives_process_restart(self):
        from providers import telegram_bot
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "offset.txt")
            with mock.patch.object(telegram_bot, "_UPDATE_OFFSET_FILE", path):
                telegram_bot._save_update_offset(481)
            self.assertEqual(telegram_bot._load_update_offset(path), 481)

    def test_preflight_only_starts_remote_channel_with_both_secrets(self):
        source = (ROOT / "scripts" / "identity_preflight.py").read_text(
            encoding="utf-8")
        self.assertIn('remote_ready = bool(token and os.getenv("AUTH_PASSWORD"',
                      source)
        self.assertIn("if remote_ready:", source)


if __name__ == "__main__":
    unittest.main()
