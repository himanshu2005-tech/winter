import time
import unittest
from unittest import mock
import inspect

import actions


class FakeLock:
    def __init__(self, held):
        self.held = held

    def locked(self):
        return self.held


class WakeWhileBusy(unittest.TestCase):
    def setUp(self):
        import agent
        self.agent = agent
        self._state = agent.current_state

    def tearDown(self):
        self.agent.current_state = self._state

    def test_idle_listens(self):
        self.agent.current_state = self.agent.STATE_IDLE
        self.assertTrue(self.agent._wake_may_listen())

    def test_a_running_task_is_listened_through(self):
        self.agent.current_state = self.agent.STATE_THINKING
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(True)):
            self.assertTrue(self.agent._wake_may_listen())

    def test_thinking_with_no_task_is_not_listening_state(self):
        self.agent.current_state = self.agent.STATE_THINKING
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(False)):
            self.assertFalse(self.agent._wake_may_listen())

    def test_speaking_uses_only_the_control_listener(self):
        self.agent.current_state = self.agent.STATE_SPEAKING
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(True)):
            self.assertFalse(self.agent._wake_may_listen())
            self.assertTrue(self.agent._wake_may_hear_control())

    def test_abort_silences_even_after_task_lock_releases(self):
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(False)), \
                mock.patch.object(self.agent, "_stop_speech_for_barge_in") as stop, \
                mock.patch.object(self.agent, "set_state"):
            self.assertTrue(self.agent._cancel_active_task("test"))
            stop.assert_called_once()

    def test_a_state_nobody_will_clear_is_released(self):
        self.agent.current_state = self.agent.STATE_THINKING
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(False)), \
                mock.patch.object(self.agent, "is_recording", False), \
                mock.patch.object(self.agent, "set_state") as set_state:
            first = self.agent._release_stuck_state(None)
            self.assertIsNotNone(first)
            set_state.assert_not_called()
            later = self.agent._release_stuck_state(
                time.time() - self.agent.STUCK_STATE_SECONDS - 1)
            self.assertIsNone(later)
            set_state.assert_called_once_with(self.agent.STATE_IDLE)

    def test_a_task_that_is_really_running_is_never_released(self):
        self.agent.current_state = self.agent.STATE_THINKING
        with mock.patch.object(self.agent, "_agent_lock", FakeLock(True)), \
                mock.patch.object(self.agent, "set_state") as set_state:
            self.assertIsNone(self.agent._release_stuck_state(time.time() - 999))
            set_state.assert_not_called()

    def test_audio_callback_feeds_the_detector_during_a_task(self):
        source = inspect.getsource(self.agent.audio_callback)
        self.assertIn("_wake_may_listen()", source)
        worker = inspect.getsource(self.agent.wakeword_worker)
        self.assertIn("_queue_interruption(spoken", worker)

    def test_script_runtime_cannot_be_imported_as_a_second_agent(self):
        """Background probes must reuse the one process-wide core instance.

        On Windows the desktop launcher executes ``agent.py`` as ``__main__``.
        ``idle_gate.agent_busy`` later does ``import agent``.  If the alias is
        not installed before ordinary imports, that import re-runs microphone
        initialization and leaves two wake listeners fighting over one input.
        """
        source = inspect.getsource(self.agent)
        alias = 'sys.modules.setdefault("agent", sys.modules[__name__])'
        self.assertIn(alias, source)
        self.assertLess(source.index(alias), source.index("import llm_router"))


class TwoBrowsers(unittest.TestCase):
    def setUp(self):
        actions._last_opened_url.update(url="", at=0.0)

    def test_open_url_says_it_is_his_browser(self):
        reports = []
        ctx = mock.Mock(attrs={"url": "https://placements.haveloc.com"},
                        ui_queue=mock.Mock())
        ctx.report = reports.append
        with mock.patch.object(actions, "_navigate_browser"):
            actions._run_open_url(ctx)
        text = reports[0]
        self.assertIn("HIS default browser", text)
        self.assertIn("BROWSER_GOTO", text)
        self.assertNotIn("controlled browser.", text)

    def test_reading_the_wrong_browser_is_flagged(self):
        actions._last_opened_url.update(url="https://placements.haveloc.com/jobs",
                                        at=time.time())
        note = actions.cross_browser_note('{"ok": true, "url": "about:blank"}')
        self.assertIn("BROWSER_GOTO", note)
        self.assertIn("HIS browser", note)

    def test_same_site_is_not_flagged(self):
        actions._last_opened_url.update(url="https://www.placements.haveloc.com/jobs",
                                        at=time.time())
        self.assertEqual(actions.cross_browser_note(
            '{"url": "https://placements.haveloc.com/home"}'), "")

    def test_nothing_opened_means_no_note(self):
        self.assertEqual(actions.cross_browser_note('{"url": "about:blank"}'), "")

    def test_old_open_is_forgotten(self):
        actions._last_opened_url.update(url="https://x.example", at=time.time() - 2000)
        self.assertEqual(actions.cross_browser_note('{"url": "about:blank"}'), "")

    def test_catalogue_tells_the_model_which_browser_reads(self):
        import agent
        text = agent.ACTION_CATALOGS["BROWSER"]
        self.assertIn("OPEN_URL opens a page in HIS OWN browser", text)
        self.assertIn("BROWSER_GOTO", text)

    def test_managed_firefox_skips_the_nightly_welcome_page(self):
        import inspect
        import browser
        source = inspect.getsource(browser)
        self.assertIn('"startup.homepage_welcome_url": "about:blank"', source)


if __name__ == "__main__":
    unittest.main()
