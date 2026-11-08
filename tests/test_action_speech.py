# -*- coding: utf-8 -*-
"""Executor-level action speech: one short phrase, before real work."""

import os
import queue
import tempfile
import unittest
from unittest import mock

import action_bus
import action_schema
import actions
import execution_reliability


class ActionSpeechTests(unittest.TestCase):
    def tearDown(self):
        actions.set_action_speaker(None)
        actions.reset_task_ledger()

    def test_phrase_is_always_two_or_three_words(self):
        self.assertEqual(actions.normalise_action_speech(""), "Working on it")
        self.assertEqual(actions.normalise_action_speech("Rendering"),
                         "Rendering now")
        self.assertEqual(actions.normalise_action_speech(
            "Rendering the neural network immediately"),
            "Rendering the neural")
        self.assertEqual(actions.normalise_action_speech(
            "<SAY>Opening Blender now</SAY>"), "Opening Blender now")

    def test_missing_model_speech_is_derived_from_the_action(self):
        self.assertEqual(
            actions.action_speech({"type": "READ_EMAILS"}),
            "Reading emails")
        self.assertEqual(
            actions.action_speech({"type": "RUNTIME_APPLY"}),
            "Applying runtime")
        self.assertEqual(
            actions.action_speech({"type": "BROWSER_GOTO",
                                   "purpose": "Check pending jobs and internships"}),
            "Checking pending jobs")

    def test_model_speech_wins_over_derived_fallback(self):
        self.assertEqual(actions.action_speech(
            {"type": "READ_EMAILS", "speech": "Reviewing your inbox"}),
            "Reviewing your inbox")

    def test_missing_speech_never_announces_generic_filler(self):
        heard = []
        actions.set_action_speaker(heard.append)
        with mock.patch.object(actions.action_bus.BUS, "dispatch") as dispatch:
            dispatch.return_value = action_bus.FLOW_NEXT
            actions.execute_actions(
                '<ACTION type="LIST_WINDOWS" />', {}, queue.Queue(), ".",
                origin="user")
        self.assertEqual(heard, ["Checking windows"])
        self.assertNotIn("Working on it", heard)

    def test_speech_is_valid_global_action_metadata(self):
        self.assertIsNone(action_schema.validate(
            "CLICK", {"type": "CLICK", "name": "Save",
                      "speech": "Clicking Save"}))

    def test_announcement_finishes_before_dispatch(self):
        order = []
        actions.set_action_speaker(lambda phrase: order.append(("speech", phrase)))

        def dispatch(ctx):
            order.append(("dispatch", ctx.action_type))
            ctx.report("[Success] Found 1 window")
            return action_bus.FLOW_NEXT

        with mock.patch.object(actions.action_bus.BUS, "dispatch",
                               side_effect=dispatch):
            actions.execute_actions(
                '<ACTION type="LIST_WINDOWS" speech="Checking windows" />',
                {}, queue.Queue(), ".", origin="user")

        self.assertEqual(order[:2], [
            ("speech", "Checking windows"),
            ("dispatch", "LIST_WINDOWS"),
        ])

    def test_invalid_action_does_not_announce(self):
        heard = []
        actions.set_action_speaker(heard.append)
        actions.execute_actions(
            '<ACTION type="CLICK" speech="Clicking it" />',
            {}, queue.Queue(), ".", origin="user")
        self.assertEqual(heard, [])
        self.assertIn("needs name or x+y",
                      actions.execute_actions._last_output.lower())

    def test_interruption_during_speech_prevents_dispatch(self):
        import threading
        event = threading.Event()

        def announce(_phrase):
            event.set()

        actions.set_action_speaker(announce)
        with mock.patch.object(actions.action_bus.BUS, "dispatch") as dispatch:
            actions.execute_actions(
                '<ACTION type="LIST_WINDOWS" speech="Checking windows" />',
                {}, queue.Queue(), ".", origin="user",
                interrupt_event=event)
        dispatch.assert_not_called()
        self.assertIn("stopped before it ran",
                      actions.execute_actions._last_output)

    def test_deferred_file_write_announces_once_after_gate(self):
        heard = []
        actions.set_action_speaker(heard.append)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "note.txt")
            tag = (f'<ACTION type="WRITE_FILE" speech="Saving note" '
                   f'path="{path}" approved="true">hello</ACTION>')
            actions.execute_actions(tag, {}, queue.Queue(), folder,
                                    origin="user")
            self.assertEqual(heard, ["Saving note"])
            self.assertTrue(os.path.exists(path))

    def test_speech_does_not_change_action_identity(self):
        first = execution_reliability.fingerprint(
            "SEND_EMAIL", {"to": "a@example.com", "speech": "Sending mail"},
            "turn", "outcome")
        second = execution_reliability.fingerprint(
            "SEND_EMAIL", {"to": "a@example.com", "speech": "Emailing now"},
            "turn", "outcome")
        self.assertEqual(first, second)

    def test_post_loop_action_with_speech_reaches_real_handler(self):
        heard = []
        actions.set_action_speaker(heard.append)
        with mock.patch("alarms.list_alarms", return_value="No alarms") as listed:
            actions.execute_actions(
                '<ACTION type="LIST_ALARMS" speech="Checking alarms" />',
                {}, queue.Queue(), ".", origin="user")
        listed.assert_called_once_with(".")
        self.assertEqual(heard, ["Checking alarms"])
        self.assertNotIn("Unavailable", actions.execute_actions._last_output)


if __name__ == "__main__":
    unittest.main()
