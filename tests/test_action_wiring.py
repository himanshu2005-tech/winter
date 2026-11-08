import tempfile
import unittest
import sys
from queue import Queue
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import threading

import actions
from actions import execute_actions
from planner import step_needs_vision
from vision import element_map, is_protected_window
import vision
import memory
import browser


class ActionWiringTests(unittest.TestCase):
    def test_listing_a_directory_is_read_only_and_screen_independent(self):
        import os
        import queue
        import tempfile
        import actions

        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(os.path.join(folder, "Projects"))
            with open(os.path.join(folder, "notes.txt"), "w", encoding="utf-8") as handle:
                handle.write("hello")
            ui = queue.Queue()
            response = f'<ACTION type="LIST_DIRECTORY" path="{folder}" />'
            actions.execute_actions(response, {}, ui, folder, origin="user")
            output = actions.execute_actions._last_output
            self.assertIn("[Folder] Projects", output)
            self.assertIn("[File] notes.txt", output)
            messages = []
            while not ui.empty():
                messages.append(ui.get_nowait())
            self.assertFalse(any(kind == "artifacts" for kind, _ in messages))
            self.assertFalse(any("Approval Required" in str(value)
                                 for _, value in messages))

    def test_action_receipts_never_open_the_details_surface_by_themselves(self):
        import queue
        import actions
        ui = queue.Queue()
        shown = actions._surface_action_result(
            ui, "TRENDING_NEWS", "1. Chennai technology story", "user")
        self.assertFalse(shown)
        self.assertTrue(ui.empty())

    def test_proactive_results_do_not_pop_over_current_work(self):
        import queue
        import actions
        ui = queue.Queue()
        shown = actions._surface_action_result(
            ui, "TRENDING_NEWS", "background news", "proactive")
        self.assertFalse(shown)
        self.assertTrue(ui.empty())

    def test_source_only_search_waits_for_the_synthesized_answer(self):
        import queue
        import actions
        ui = queue.Queue()
        raw = """RESULT
[Federated knowledge: compare two options]
1. Source headline
Source: Google News | Published: Tue, 20 Oct 2026 02:23:00 GMT
URL: https://example.com/source
"""
        shown = actions._surface_action_result(
            ui, "KNOWLEDGE_SEARCH", raw, "user")
        self.assertFalse(shown)
        self.assertTrue(ui.empty())

    def setUp(self):
        self.ui_queue = Queue()
        self.base_dir_context = tempfile.TemporaryDirectory()
        self.base_dir = self.base_dir_context.name

    def tearDown(self):
        self.base_dir_context.cleanup()

    def test_shared_element_map_is_available_without_a_screenshot(self):
        self.assertIsInstance(element_map, dict)

    def test_opening_project_in_ide_does_not_require_initial_vision(self):
        self.assertFalse(
            step_needs_vision("Launch the IDE and open the project directory")
        )

    def test_explicit_screen_interaction_still_requires_vision(self):
        self.assertTrue(step_needs_vision("Click the Submit button on screen"))

    def test_optional_memory_retrieval_times_out(self):
        import time

        started = time.monotonic()
        result = memory._bounded_call(
            lambda: time.sleep(0.25), 0.02, "fallback", "test retrieval"
        )
        self.assertEqual(result, "fallback")
        self.assertLess(time.monotonic() - started, 0.2)

    def test_user_project_editor_is_not_globally_protected(self):
        self.assertFalse(is_protected_window("graph - Cursor"))
        self.assertTrue(is_protected_window("agent.py - Cursor"))

    @patch("vision.win32gui.EnumWindows")
    def test_ui_map_never_enumerates_other_windows(self, enum_windows):
        previous = vision._capture_hwnd
        try:
            vision._capture_hwnd = None
            rects, sensitive = vision._collect_ui_elements()
        finally:
            vision._capture_hwnd = previous

        self.assertEqual(rects, [])
        self.assertEqual(sensitive, [])
        enum_windows.assert_not_called()

    @patch("providers.google_api.read_emails", return_value="MOCK EMAIL RESULT")
    def test_read_emails_dispatches_without_vision(self, read_emails):
        execute_actions(
            '<ACTION type="READ_EMAILS" query="is:unread" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        read_emails.assert_called_once_with(
            self.base_dir, max_results=5, query="is:unread"
        )
        self.assertIn("[Success]", execute_actions._last_output)
        self.assertIn("READ_EMAILS result", execute_actions._last_output)

    def test_block_style_python_action_reaches_dispatcher(self):
        execute_actions(
            '<ACTION type="RUN_PYTHON">print("BLOCK ACTION OK")</ACTION>',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        self.assertIn("BLOCK ACTION OK", execute_actions._last_output)

    def test_write_file_honors_an_absolute_destination(self):
        # approved="true" because writing a file requires confirmation and
        # this pass now enforces it. It did not before: the block form wrote
        # to disk with no approval and no receipt at all, so these tests were
        # passing through a gate that was not there.
        destination = Path(self.base_dir) / "requested" / "form_details.txt"
        execute_actions(
            f'<ACTION type="WRITE_FILE" path="{destination}" approved="true">Form link: https://forms.office.com/example</ACTION>',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "Form link: https://forms.office.com/example",
        )
        self.assertIn(str(destination), execute_actions._last_output)

    def test_write_file_does_not_overwrite_without_confirmation(self):
        destination = Path(self.base_dir) / "existing.txt"
        destination.write_text("keep me", encoding="utf-8")
        execute_actions(
            f'<ACTION type="WRITE_FILE" path="{destination}" approved="true">replace me</ACTION>',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        self.assertEqual(destination.read_text(encoding="utf-8"), "keep me")
        self.assertIn("Confirmation Required", execute_actions._last_output)

    def test_write_file_can_overwrite_after_explicit_confirmation(self):
        destination = Path(self.base_dir) / "existing.txt"
        destination.write_text("old", encoding="utf-8")
        execute_actions(
            f'<ACTION type="WRITE_FILE" path="{destination}" confirmed="true">new</ACTION>',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        self.assertEqual(destination.read_text(encoding="utf-8"), "new")

    def test_proactive_origin_cannot_run_commands(self):
        execute_actions(
            '<ACTION type="RUN_COMMAND" cmd="echo should-not-run" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="proactive",
        )

        self.assertIn("blocked", execute_actions._last_output.lower())

    @patch("actions._win32_click")
    @patch("actions._invoke_accessible_target", return_value=("Create a design", True))
    def test_named_click_prefers_accessibility_invoke(self, invoke, pixel_click):
        name, succeeded = actions._click_target_once(
            100, 200, "Create a design", self.ui_queue, self.base_dir
        )

        self.assertTrue(succeeded)
        self.assertEqual(name, "Create a design")
        invoke.assert_called_once()
        pixel_click.assert_not_called()

    @patch("actions.pyautogui.hotkey")
    @patch.object(actions, "FOREGROUND_OBSERVER_MODE", False)
    def test_protected_hotkey_is_blocked(self, hotkey):
        execute_actions(
            '<ACTION type="HOTKEY" keys="ctrl,alt,delete" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        # The real check: the dangerous combo never reached the keyboard.
        hotkey.assert_not_called()
        # The refusal message is not pinned. Which guard speaks first depends
        # on what happens to be in the foreground -- with a VANTA window
        # focused it refuses as "my own interface" rather than "Unsupported
        # Action", and both are correct. Asserting the wording made this pass
        # or fail on window focus, which is no test of safety at all.
        output = execute_actions._last_output
        self.assertTrue("Unsupported Action" in output or "Refused" in output,
                        f"ctrl+alt+delete was not refused: {output[:90]}")
        self.assertNotIn("[Success]", output)

    @patch("actions._navigate_browser")
    @patch.object(actions, "FOREGROUND_OBSERVER_MODE", True)
    def test_observer_mode_blocks_browser_opening(self, navigate):
        execute_actions(
            '<ACTION type="OPEN_URL" url="https://example.com" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        navigate.assert_not_called()
        self.assertIn("Foreground Observer Mode", execute_actions._last_output)

    @patch("actions.subprocess.run")
    @patch.object(actions, "FOREGROUND_OBSERVER_MODE", True)
    def test_observer_mode_blocks_desktop_launch_commands(self, run):
        execute_actions(
            '<ACTION type="RUN_COMMAND" cmd="Start-Process chrome" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )

        run.assert_not_called()
        self.assertIn("Desktop/application launch command was blocked", execute_actions._last_output)

    @patch("browser.goto", return_value='[Browser Receipt] {"ok":true}')
    def test_managed_browser_navigation_is_allowed_in_observer_mode(self, goto):
        execute_actions(
            '<ACTION type="BROWSER_GOTO" url="https://example.com" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
        )
        goto.assert_called_once_with("https://example.com", tab=None, visible=True)
        self.assertIn("Browser Receipt", execute_actions._last_output)

    @patch("browser.goto", return_value='[Browser Receipt] {"ok":true}')
    def test_background_navigation_is_headless_and_silent(self, goto):
        speaker = unittest.mock.Mock()
        actions.set_action_speaker(speaker)
        execute_actions(
            '<ACTION type="BROWSER_GOTO" url="https://example.com" '
            'background="true" purpose="your application status" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        speaker.assert_not_called()
        goto.assert_called_once_with("https://example.com", tab=None,
                                     visible=False)

    @patch("browser.goto", return_value='[Browser Receipt] {"ok":true}')
    def test_turn_level_background_suppresses_steps_and_speech(self, goto):
        speaker = unittest.mock.Mock()
        actions.set_action_speaker(speaker)
        actions.set_turn_execution_visibility("background")
        try:
            execute_actions(
                '<ACTION type="BROWSER_GOTO" url="https://example.com" />',
                element_map, self.ui_queue, self.base_dir, origin="user")
        finally:
            actions.set_turn_execution_visibility("foreground")
            actions.set_action_speaker(None)
        goto.assert_called_once_with("https://example.com", tab=None,
                                     visible=False)
        speaker.assert_not_called()
        messages = []
        while not self.ui_queue.empty():
            messages.append(self.ui_queue.get_nowait())
        self.assertFalse(any(kind in {"action", "info"}
                             for kind, _ in messages))

    def test_managed_browser_rejects_local_and_credential_urls(self):
        with self.assertRaises(ValueError):
            browser._validate_url("http://127.0.0.1/admin")
        with self.assertRaises(ValueError):
            browser._validate_url("https://user:pass@example.com/")

    @patch("providers.google_api.read_emails")
    def test_interruption_stops_before_next_action(self, read_emails):
        interrupted = threading.Event()
        interrupted.set()

        execute_actions(
            '<ACTION type="READ_EMAILS" query="is:unread" />',
            element_map,
            self.ui_queue,
            self.base_dir,
            origin="user",
            interrupt_event=interrupted,
        )

        read_emails.assert_not_called()
        self.assertIn("Interrupted", execute_actions._last_output)

    def test_dataset_preview_is_non_destructive_and_requires_confirmation(self):
        source = Path(self.base_dir) / "raw.csv"
        source.write_text("name,score\n Alice ,10\n Alice ,10\nBob,20\n", encoding="utf-8")
        original = source.read_text(encoding="utf-8")

        execute_actions(
            f'<ACTION type="CREATE_DATASET_CLEANING_PREVIEW" path="{source}" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertIn("Dataset Cleaning Preview", execute_actions._last_output)
        preview = Path(self.base_dir) / "vanta_previews" / "raw.vanta_preview.csv"
        self.assertTrue(preview.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), original)

        destination = Path(self.base_dir) / "kept.csv"
        execute_actions(
            f'<ACTION type="KEEP_DATASET_PREVIEW" preview_path="{preview}" destination_path="{destination}" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertFalse(destination.exists())
        self.assertIn("Confirmation Required", execute_actions._last_output)

        execute_actions(
            f'<ACTION type="KEEP_DATASET_PREVIEW" preview_path="{preview}" destination_path="{destination}" confirmed="true" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertTrue(destination.exists())
        self.assertIn("Kept reviewed dataset", execute_actions._last_output)

    @patch("email_memory.remember_emails", return_value=1)
    @patch("email_briefing.triage", return_value={
        "severity": "important", "summary": "One useful email needs attention.",
        "reason": "deadline", "important_ids": ["m1"]})
    @patch("providers.google_api.read_emails", return_value=(
        "[m1] (unread)\nFrom: Example\nSubject: Important\nSnippet: Act today.\n---"))
    @patch.dict("os.environ", {"VANTA_EMAIL_MODEL_CONTENT": "0"})
    def test_read_emails_returns_summary_not_raw_delivery_payload(
            self, read_emails, triage, remember):
        execute_actions(
            '<ACTION type="READ_EMAILS" query="is:unread" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertIsNone(execute_actions._direct_user_result)
        self.assertIn("One useful email needs attention", execute_actions._last_output)
        self.assertNotIn("Act today", execute_actions._last_output)
        triage.assert_called_once()
        remember.assert_called_once()

    @patch("alarms.set_alarm", return_value="Alarm set for 11:35 AM: Calm Down Reminder")
    @patch("providers.spotify_api.spotify_play", return_value="Spotify error: http status: 403")
    def test_multi_action_failure_is_not_overwritten_by_later_success(self, spotify, set_alarm):
        execute_actions(
            '<ACTION type="SPOTIFY_PLAY" query="calming ambient music" />'
            '<ACTION type="SET_ALARM" time="11:35" label="Calm Down Reminder" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertIn("Spotify error: http status: 403", execute_actions._last_output)
        self.assertIn("Alarm set for 11:35 AM", execute_actions._last_output)
    @patch("providers.google_api.read_emails", return_value="MAIL")
    def test_action_with_a_caption_instead_of_self_closing_still_runs(self, read_emails):
        # <ACTION ...>caption</ACTION> is how models often write it; it failed
        # with "No executable action was parsed" and was retried until the
        # step budget ran out.
        for text in ('<ACTION type="READ_EMAILS" query="is:unread">Checking mail</ACTION>',
                     '<action type="READ_EMAILS" query="is:unread" >Checking mail</action>',
                     "<ACTION type='READ_EMAILS' query='is:unread'>Checking mail</ACTION>"):
            read_emails.reset_mock()
            execute_actions(text, element_map, self.ui_queue, self.base_dir, origin="user")
            self.assertEqual(read_emails.call_count, 1, text)

    @patch("providers.google_api.read_emails", return_value="MAIL")
    def test_self_closing_with_a_stray_close_runs_exactly_once(self, read_emails):
        execute_actions('<ACTION type="READ_EMAILS" query="is:unread" />Checking</ACTION>',
                        element_map, self.ui_queue, self.base_dir, origin="user")
        self.assertEqual(read_emails.call_count, 1)

    @patch("providers.google_api.read_emails", return_value="MAIL")
    def test_action_quoted_inside_prose_is_not_run_twice(self, read_emails):
        execute_actions(
            'Sure. <ACTION type="READ_EMAILS" query="is:unread" /> then <ACTION type="READ_EMAILS" '
            'query="is:unread">Checking</ACTION>',
            element_map, self.ui_queue, self.base_dir, origin="user")
        self.assertGreaterEqual(read_emails.call_count, 1)

    @patch("providers.google_api.read_emails", return_value="MAIL")
    def test_truncated_action_cannot_reuse_previous_result(self, read_emails):
        execute_actions(
            '<ACTION type="READ_EMAILS" query="is:unread" />',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        execute_actions(
            '<ACTION type="READ_EMAILS" query="subject:',
            element_map, self.ui_queue, self.base_dir, origin="user",
        )
        self.assertEqual(read_emails.call_count, 1)
        self.assertIn("No executable action was parsed", execute_actions._last_output)

if __name__ == "__main__":
    unittest.main()
