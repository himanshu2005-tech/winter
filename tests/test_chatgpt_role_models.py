# -*- coding: utf-8 -*-
"""Different roles may use different ChatGPT models.

Every line of every transcript shows the same model doing every job:

    [Router] cognitive_router -> chatgpt:gpt-5.6-luna
    [Router] planner          -> chatgpt:gpt-5.6-luna
    [Router] critic           -> chatgpt:gpt-5.6-luna
    [Router] turn             -> chatgpt:gpt-5.6-luna

That is not a coincidence. When the provider is ChatGPT, _model_for_provider
returns chatgpt_auth.preferred_model() and returns before it ever consults the
stage overrides or the active mode's choice. So WINTER_MODEL_CODER,
WINTER_MODEL_PLANNER, WINTER_MODEL_CRITIC and WINTER_MODEL_ROUTER have been
dead configuration for as long as ChatGPT has been the provider -- set, read
by nothing.

There is a real reason for the early return: ChatGPT account models live in a
different namespace from AIRouter's, and sending "zai/glm-5.3-flash" with an
OpenAI OAuth token is correctly rejected. The guard was right that a
cross-namespace id must not get through; it was wrong to also discard ids that
belong to this account. gpt-5.6-sol is available here and could not be asked
for.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_router

#: What this account actually offers, from a live listing.
ACCOUNT_MODELS = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                  "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def resolve(task_level, env=None, mode_model=""):
    """What model the router would use for this stage on ChatGPT."""
    environment = {key: "" for key in
                   ("WINTER_MODEL_PLANNER", "WINTER_MODEL_CRITIC",
                    "WINTER_MODEL_ROUTER", "WINTER_MODEL_VISION",
                    "WINTER_MODEL_CODER", "CHATGPT_MODEL_PLANNER",
                    "CHATGPT_MODEL_CRITIC", "CHATGPT_MODEL_ROUTER",
                    "CHATGPT_MODEL_VISION", "CHATGPT_MODEL_CODER")}
    environment.update(env or {})
    # Patch the modules' own attributes rather than swapping them in
    # sys.modules. "from providers import chatgpt_auth" is an attribute lookup
    # on an already-imported package, so a sys.modules entry is ignored once
    # anything else in the suite has imported it -- which made these pass when
    # run alone and fail inside a full run.
    from providers import chatgpt_auth
    import mode_manager
    with patch.dict(os.environ, environment), \
            patch.object(chatgpt_auth, "preferred_model",
                         return_value="gpt-5.6-luna"), \
            patch.object(chatgpt_auth, "list_models",
                         return_value=[{"slug": slug}
                                       for slug in ACCOUNT_MODELS]), \
            patch.object(mode_manager, "mode_model",
                         return_value=mode_model):
        return llm_router._model_for_provider("chatgpt", task_level)


class RolesCanDiffer(unittest.TestCase):
    def test_the_default_is_unchanged(self):
        """Nothing configured still means the account's preferred model."""
        self.assertEqual(resolve("turn"), "gpt-5.6-luna")

    def test_a_stage_override_is_honoured(self):
        self.assertEqual(
            resolve("planner", {"WINTER_MODEL_PLANNER": "gpt-5.6-terra"}),
            "gpt-5.6-terra")

    def test_the_coding_mode_can_ask_for_its_own_model(self):
        """The request: coding on 5.6 Sol, everything else as before."""
        self.assertEqual(resolve("turn", mode_model="gpt-5.6-sol"),
                         "gpt-5.6-sol")

    def test_provider_specific_coder_model_wins_over_legacy_namespace(self):
        from providers import chatgpt_auth
        import mode_manager
        policy = SimpleNamespace(model_env="WINTER_MODEL_CODER")
        with patch.dict(os.environ, {
                "WINTER_MODEL_CODER": "zai/glm-5.3-flash",
                "CHATGPT_MODEL_CODER": "gpt-5.6-sol"}), \
                patch.object(chatgpt_auth, "preferred_model",
                             return_value="gpt-5.6-luna"), \
                patch.object(chatgpt_auth, "list_models",
                             return_value=[{"slug": slug}
                                           for slug in ACCOUNT_MODELS]), \
                patch.object(mode_manager, "current_policy",
                             return_value=policy), \
                patch.object(mode_manager, "mode_model",
                             return_value="zai/glm-5.3-flash"):
            self.assertEqual(
                llm_router._model_for_provider("chatgpt", "turn"),
                "gpt-5.6-sol")

    def test_airouter_gets_its_own_coder_model(self):
        import mode_manager
        policy = SimpleNamespace(model_env="WINTER_MODEL_CODER")
        with patch.dict(os.environ, {
                "WINTER_MODEL_CODER": "gpt-5.6-sol",
                "AIROUTER_MODEL_CODER": "minimax/minimax-m2.5"}), \
                patch.object(mode_manager, "current_policy",
                             return_value=policy), \
                patch.object(mode_manager, "mode_model",
                             return_value="gpt-5.6-sol"), \
                patch.object(llm_router, "active_provider", "airouter"):
            self.assertEqual(
                llm_router._model_for_provider("airouter", "turn"),
                "minimax/minimax-m2.5")

    def test_coder_provider_pin_prevents_chatgpt_fallback(self):
        import mode_manager
        policy = SimpleNamespace(model_env="WINTER_MODEL_CODER")
        airouter_client = object()
        with patch.dict(os.environ, {
                "WINTER_PROVIDER_CODER": "airouter",
                "AIROUTER_MODEL_CODER": "minimax/minimax-m2.5"}), \
                patch.object(mode_manager, "current_policy",
                             return_value=policy), \
                patch.object(llm_router, "airouter_client", airouter_client), \
                patch.object(llm_router, "chatgpt_client", object()):
            candidates = llm_router._candidates("turn")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "airouter")
        self.assertEqual(candidates[0][1], airouter_client)
        self.assertEqual(candidates[0][2], "minimax/minimax-m2.5")

    def test_a_stage_override_beats_the_mode(self):
        """A stage is more specific than the mode it runs inside."""
        self.assertEqual(
            resolve("critic", {"WINTER_MODEL_CRITIC": "gpt-6-sol"},
                    mode_model="gpt-5.6-sol"),
            "gpt-6-sol")


class NamespacesStaySeparate(unittest.TestCase):
    """The reason the early return existed, which must keep holding."""

    def test_an_airouter_id_is_never_sent_to_chatgpt(self):
        for wrong in ("zai/glm-5.3-flash", "alibaba/qwen3-coder",
                      "anthropic/claude-3-haiku"):
            with self.subTest(model=wrong):
                self.assertEqual(
                    resolve("turn", {"WINTER_MODEL_CODER": wrong},
                            mode_model=wrong),
                    "gpt-5.6-luna",
                    "a cross-namespace id reached the ChatGPT endpoint")

    def test_a_model_this_account_lacks_is_refused(self):
        self.assertEqual(
            resolve("planner", {"WINTER_MODEL_PLANNER": "gpt-7-imaginary"}),
            "gpt-5.6-luna")

    def test_an_excluded_model_is_not_selected(self):
        """Astra is excluded deliberately; an override must not smuggle it in."""
        with patch.dict(os.environ, {"CHATGPT_EXCLUDED_MODELS": "gpt-6-astra"}):
            self.assertEqual(
                resolve("planner", {"WINTER_MODEL_PLANNER": "gpt-6-astra"}),
                "gpt-5.6-luna")

    def test_a_listing_failure_falls_back_rather_than_breaking(self):
        """If the account cannot be listed, behave as before."""
        from providers import chatgpt_auth
        with patch.dict(os.environ, {"WINTER_MODEL_PLANNER": "gpt-5.6-sol"}), \
                patch.object(chatgpt_auth, "preferred_model",
                             return_value="gpt-5.6-luna"), \
                patch.object(chatgpt_auth, "list_models",
                             side_effect=RuntimeError("offline")):
            self.assertEqual(
                llm_router._model_for_provider("chatgpt", "planner"),
                "gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()
