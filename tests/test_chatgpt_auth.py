import base64
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from providers import chatgpt_auth
import llm_router


def b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class ChatGPTAuthTests(unittest.TestCase):
    def test_validates_signed_identity_and_nonce(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_numbers()
        header = {"alg": "RS256", "kid": "test"}
        claims = {
            "iss": chatgpt_auth.ISSUER, "aud": "oaiapp_test", "sub": "subject",
            "email": "test@example.com", "nonce": "nonce", "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        unsigned = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
        signature = private.sign(unsigned.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        token = f"{unsigned}.{b64(signature)}"
        jwk = {"kid": "test", "kty": "RSA", "e": b64(public.e.to_bytes(3, "big")),
               "n": b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big"))}
        with patch.object(chatgpt_auth, "_jwks", return_value=[jwk]):
            self.assertEqual(
                chatgpt_auth.validate_id_token(token, "oaiapp_test", "nonce")["sub"],
                "subject")
            with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
                chatgpt_auth.validate_id_token(token, "oaiapp_test", "wrong")

    def test_access_token_refreshes_and_rotates_atomically(self):
        old = {"client_id": "oaiapp_test", "refresh_token": "old", "access_token": "expired",
               "saved_at": 0, "expires_in": 1,
               "scopes": ["chatgpt.tokens.use.direct"]}
        replacement = {"access_token": "new", "refresh_token": "new-refresh",
                       "expires_in": 3600, "scope": chatgpt_auth.SCOPES}
        saved = []
        with patch.object(chatgpt_auth, "load_profile", return_value=old), \
             patch.object(chatgpt_auth, "_token_request", return_value=replacement), \
             patch.object(chatgpt_auth, "_save_profile", side_effect=saved.append):
            self.assertEqual(chatgpt_auth.access_token(), "new")
        self.assertEqual(saved[0]["refresh_token"], "new-refresh")

    def test_responses_input_translates_images(self):
        result = llm_router._responses_input([{
            "role": "user", "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]}])
        self.assertEqual(result[0]["content"][0]["type"], "input_text")
        self.assertEqual(result[0]["content"][1]["type"], "input_image")

    def test_chatgpt_always_has_airouter_continuity_fallback(self):
        old_provider = llm_router.active_provider
        old_chatgpt, old_airouter = llm_router.chatgpt_client, llm_router.airouter_client
        try:
            llm_router.active_provider = "chatgpt"
            llm_router.chatgpt_client = object()
            llm_router.airouter_client = object()
            with patch.object(chatgpt_auth, "preferred_model", return_value="account-model"):
                names = [row[0] for row in llm_router._candidates(None)]
            self.assertEqual(names[:2], ["chatgpt", "airouter"])
        finally:
            llm_router.active_provider = old_provider
            llm_router.chatgpt_client, llm_router.airouter_client = old_chatgpt, old_airouter

    def test_airouter_stays_primary_with_chatgpt_as_fallback(self):
        old_provider = llm_router.active_provider
        old_chatgpt, old_airouter = llm_router.chatgpt_client, llm_router.airouter_client
        try:
            llm_router.active_provider = "airouter"
            llm_router.airouter_client = object()
            llm_router.chatgpt_client = object()
            with patch.object(chatgpt_auth, "preferred_model",
                              return_value="account-model"):
                names = [row[0] for row in llm_router._candidates(None)]
            self.assertEqual(names[:2], ["airouter", "chatgpt"])
        finally:
            llm_router.active_provider = old_provider
            llm_router.chatgpt_client, llm_router.airouter_client = old_chatgpt, old_airouter

    def test_initializing_an_authenticated_fallback_does_not_promote_it(self):
        old_provider = llm_router.active_provider
        try:
            llm_router.active_provider = "airouter"
            with patch.object(chatgpt_auth, "access_token",
                              return_value="oauth-token"), \
                 patch.object(llm_router, "OpenAI", return_value=object()):
                llm_router.init_clients()
            self.assertEqual(llm_router.active_provider, "airouter")
            self.assertIsNotNone(llm_router.chatgpt_client)
        finally:
            llm_router.active_provider = old_provider

    def test_prefers_sol_over_catalog_first_astra_or_luna(self):
        catalog = [
            {"slug": "gpt-6-astra", "display_name": "Astra"},
            {"slug": "gpt-5.6-terra", "display_name": "Terra"},
            {"slug": "gpt-5.6-luna", "display_name": "Luna"},
            {"slug": "gpt-6.1-sol", "display_name": "Sol"},
        ]
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(chatgpt_auth, "list_models", return_value=catalog):
            import os
            old_model = os.environ.pop("CHATGPT_MODEL", None)
            old_excluded = os.environ.pop("CHATGPT_EXCLUDED_MODELS", None)
            try:
                self.assertEqual(chatgpt_auth.preferred_model(), "gpt-6.1-sol")
            finally:
                if old_model is not None:
                    os.environ["CHATGPT_MODEL"] = old_model
                if old_excluded is not None:
                    os.environ["CHATGPT_EXCLUDED_MODELS"] = old_excluded

    def test_explicitly_excluded_astra_is_rejected(self):
        catalog = [{"slug": "gpt-6-astra", "display_name": "Astra"}]
        with patch.dict("os.environ", {
                "CHATGPT_MODEL": "gpt-6-astra",
                "CHATGPT_EXCLUDED_MODELS": "gpt-6-astra"}, clear=False), \
             patch.object(chatgpt_auth, "list_models", return_value=catalog):
            with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
                chatgpt_auth.preferred_model()

    def test_airouter_stage_override_never_leaks_into_chatgpt(self):
        with patch.dict("os.environ", {
                "WINTER_MODEL_ROUTER": "zai/glm-5.3-flash"}, clear=False), \
             patch.object(chatgpt_auth, "preferred_model",
                          return_value="gpt-5.6-luna"):
            self.assertEqual(
                llm_router._model_for_provider("chatgpt", "cognitive_router"),
                "gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()
