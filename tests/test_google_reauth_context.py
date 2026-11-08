import json
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import actions
import session_context
from providers import google_api


class GoogleReauthAndContinuityTests(unittest.TestCase):
    def test_revoked_refresh_becomes_controlled_reauth_state(self):
        fake = Mock(valid=False, expired=True, refresh_token="refresh")
        fake.refresh.side_effect = google_api.RefreshError("revoked")
        with tempfile.TemporaryDirectory() as folder, \
             patch("security.keyring_manager.get_secret_chunked", return_value=json.dumps({"token": "x"})), \
             patch.object(google_api.Credentials, "from_authorized_user_info", return_value=fake):
            with self.assertRaises(google_api.GoogleReauthorizationRequired):
                google_api.get_credentials(folder)

    def test_contacts_sync_does_not_crash_on_revoked_grant(self):
        with patch.object(google_api, "get_credentials", side_effect=google_api.GoogleReauthorizationRequired("reauthorize")):
            self.assertEqual(google_api.sync_contacts("."), 0)

    @patch("providers.google_api.reauthorize_google", return_value="Google access was reauthorized successfully.")
    def test_reauthorize_action_returns_verified_local_result(self, reauthorize):
        queue = Queue()
        actions.execute_actions('<ACTION type="REAUTHORIZE_GOOGLE" />', {}, queue, ".", origin="user")
        reauthorize.assert_called_once_with(".")
        self.assertIn("Success", actions.execute_actions._last_output)
        self.assertIn("reauthorized successfully", actions.execute_actions._direct_user_result)

    def test_short_reply_resolves_against_proactive_briefing(self):
        session_context.start_session()
        with patch.object(session_context._graph_store, "upsert_dialogue_turn"), \
             patch.object(session_context, "save_conversation"):
            session_context.record_user(
                "Startup check: Gmail login expired and needs reauthorization.",
                origin="system_event",
            )
            session_context.record_assistant(
                "The Gmail connection expired, so it needs quick reauthorization."
            )
            session_context.record_user("Fine I'll grant, how should I", origin="user_stated")
            resolved = session_context.contextualize_user_input("Fine I'll grant, how should I")
        self.assertIn("RESOLVED CONVERSATIONAL REFERENCE", resolved)
        self.assertIn("Gmail connection expired", resolved)


if __name__ == "__main__":
    unittest.main()