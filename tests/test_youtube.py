import inspect
import json
import os
import unittest
from unittest import mock

import action_bus
import action_registry
import actions
import browser
import capabilities
import desktop
from providers import youtube_api


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "items": [{
                "id": {"videoId": "abc123XYZ_-"},
                "snippet": {
                    "title": "Spider-Man &amp; Friends",
                    "channelTitle": "Game Channel",
                    "publishedAt": "2026-10-01T00:00:00Z",
                    "description": "A walkthrough",
                    "thumbnails": {"high": {"url": "https://img.example/x.jpg"}},
                },
            }]
        }


class YouTubeDataApi(unittest.TestCase):
    def setUp(self):
        youtube_api._cache.clear()

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key_is_actionable_and_does_not_borrow_other_google_keys(self):
        os.environ["GEMINI_API_KEY"] = "not-for-youtube"
        result = youtube_api.search_receipt("spider man")
        self.assertIn("YOUTUBE_API_KEY", result)
        self.assertTrue(result.startswith("[Error]"), result)

    @mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "secret-key"}, clear=True)
    @mock.patch("providers.youtube_api.requests.get", return_value=FakeResponse())
    def test_search_uses_official_api_and_never_prints_the_key(self, get):
        result = youtube_api.search_receipt("spider man", limit=3)
        self.assertIn("Spider-Man & Friends", result)
        self.assertIn("video_id=abc123XYZ_-", result)
        self.assertNotIn("secret-key", result)
        (url,), kwargs = get.call_args
        self.assertEqual(url, youtube_api.API_ROOT + "/search")
        self.assertEqual(kwargs["params"]["type"], "video")
        self.assertEqual(kwargs["params"]["regionCode"], "IN")
        self.assertEqual(kwargs["params"]["key"], "secret-key")

    def test_video_id_is_accepted_from_common_youtube_urls(self):
        cases = {
            "https://youtu.be/one_2-3": "one_2-3",
            "https://www.youtube.com/watch?v=one_2-3&t=12": "one_2-3",
            "https://youtube.com/shorts/one_2-3": "one_2-3",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(youtube_api.extract_video_id(value), expected)


class BrowserContract(unittest.TestCase):
    @mock.patch("browser._call", return_value="receipt")
    def test_fullscreen_is_a_dedicated_browser_operation(self, call):
        self.assertEqual(browser.youtube_fullscreen(True, tab="t2"), "receipt")
        call.assert_called_once_with("youtube_fullscreen", enabled=True, tab="t2")

    @mock.patch("browser._call", return_value="receipt")
    def test_playback_is_a_dedicated_browser_operation(self, call):
        url = "https://www.youtube.com/watch?v=abc123XYZ_-"
        self.assertEqual(browser.youtube_play(url, tab="t2"), "receipt")
        call.assert_called_once_with("youtube_play", url=url, tab="t2")

    def test_both_browser_backends_verify_actual_fullscreen_state(self):
        import browser_real
        for module in (browser, browser_real):
            source = inspect.getsource(module)
            self.assertIn("document.fullscreenElement", source)
            self.assertIn("button.ytp-fullscreen-button", source)
            self.assertIn('"youtube_fullscreen"', source)


class CapabilityWiring(unittest.TestCase):
    def test_all_three_youtube_capabilities_are_declared_and_dispatchable(self):
        names = {"YOUTUBE_SEARCH", "YOUTUBE_PLAY", "YOUTUBE_FULLSCREEN"}
        self.assertTrue(names <= capabilities.names())
        self.assertTrue(names <= action_bus.BUS.names())

    def test_fullscreen_handler_reports_browser_readback(self):
        receipt = ('[Browser Receipt] {"ok":true,"operation":'
                   '"youtube_fullscreen","fullscreen":true}')
        ctx = action_bus.ActionContext(
            "YOUTUBE_FULLSCREEN", {"enabled": "true", "tab": "t2"},
            mock.Mock(), os.path.dirname(os.path.dirname(__file__)),
            legacy=actions.execute_actions)
        with mock.patch("browser.youtube_fullscreen", return_value=receipt):
            action_bus.BUS.dispatch(ctx)
        self.assertEqual(ctx.receipt, receipt)

    def test_vanta_shutdown_chord_cannot_be_synthesized(self):
        refusal = desktop._protected_combo(["ctrl", "alt", "f12"])
        self.assertIn("protected system combination", refusal)


class VoiceAndContextRegression(unittest.TestCase):
    def test_consumed_wake_transcript_is_cleared_before_followup_listening(self):
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        read = source.index('spoken = (getattr(engine, "trailing"')
        clear = source.index('engine.heard = ""', read)
        busy = source.index("busy = _agent_lock.locked()", read)
        self.assertLess(clear, busy)

    def test_inner_loop_imports_turn_context_where_it_uses_it(self):
        import agent
        source = inspect.getsource(agent._inner_run_agent_loop)
        self.assertIn("import turn_context", source)


class YouTubeActionRetrieval(unittest.TestCase):
    def setUp(self):
        action_registry.configure(capabilities.catalog_overlay())

    def _names(self, request):
        return [item["name"] for item in action_registry.search(
            ".", request, limit=5, origin="user")]

    def test_plain_fullscreen_request_prefers_verified_youtube_control(self):
        self.assertEqual(self._names("full screen")[0], "YOUTUBE_FULLSCREEN")
        self.assertEqual(
            self._names("make this youtube video full screen")[0],
            "YOUTUBE_FULLSCREEN")

    def test_play_and_search_requests_reach_the_specific_actions(self):
        self.assertEqual(
            self._names("play the spider man trailer on youtube")[0],
            "YOUTUBE_PLAY")
        self.assertEqual(
            self._names("find reinforcement learning videos on youtube")[0],
            "YOUTUBE_SEARCH")


if __name__ == "__main__":
    unittest.main()
