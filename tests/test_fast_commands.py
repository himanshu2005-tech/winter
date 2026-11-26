# -*- coding: utf-8 -*-
"""
Tests for the no-model fast path.

The risk here is not that "mute" fails to mute. It is that something which only
MENTIONS muting gets silently executed as a command -- "why is chrome muted"
answered by muting the speakers, with no model ever consulted and nothing in the
logs to explain it. Most of this file is therefore about what must NOT fire.

The handlers are stubbed out rather than exercised: whether pycaw can reach an
audio endpoint is not what these tests are about, and a test suite that mutes
the machine it runs on is its own bug.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fast_commands


class FakeControls:
    """Stands in for system_controls, recording what was asked of it."""

    def __init__(self, volume=50, muted=False, brightness=60, fail=False):
        self.volume = volume
        self.muted = muted
        self.brightness = brightness
        self.fail = fail
        self.calls = []

    def _result(self, text):
        return "[Error] refused" if self.fail else f"[Success] {text}"

    def get_volume(self):
        return (None, None) if self.fail else (self.volume, self.muted)

    def set_volume(self, percent):
        self.calls.append(("volume", percent))
        if not self.fail:
            self.volume = percent
        return self._result(f"Volume {percent}%.")

    def set_mute(self, muted):
        self.calls.append(("mute", muted))
        if not self.fail:
            self.muted = muted
        return self._result("Muted." if muted else "Unmuted.")

    def get_brightness(self):
        return None if self.fail else self.brightness

    def set_brightness(self, percent):
        self.calls.append(("brightness", percent))
        return self._result(f"Brightness {percent}%.")

    def media(self, action):
        self.calls.append(("media", action))
        return self._result(f"Media: {action}.")


class _Stubbed(unittest.TestCase):
    """Swaps fast_commands' view of system_controls for a fake."""

    def setUp(self):
        self.controls = FakeControls()
        self._real = fast_commands._audio
        fast_commands._audio = lambda: self.controls

    def tearDown(self):
        fast_commands._audio = self._real


class NormalisationTests(unittest.TestCase):

    def test_address_and_politeness_are_stripped(self):
        for spoken in ("mute", "Mute.", "  MUTE  ", "Hey Vanta, mute",
                       "vanta mute", "please mute", "mute please",
                       "can you mute", "Vanta, please mute, thanks"):
            self.assertEqual(fast_commands.normalise(spoken), "mute", spoken)

    def test_apostrophes_become_spaces_not_nothing(self):
        # "what's" has to land on the same phrase the table registers, or the
        # entry is dead code that no utterance can ever reach.
        self.assertEqual(fast_commands.normalise("What's the date?"),
                         "what s the date")

    def test_empty_input_is_not_a_command(self):
        for spoken in ("", "   ", None, ".", "vanta"):
            self.assertIsNone(fast_commands.resolve(spoken), repr(spoken))


class RegistryTests(unittest.TestCase):

    def test_every_registered_phrase_is_reachable(self):
        """A phrase that does not survive normalise() can never be matched.

        This is the invariant that catches a typo in the table: registering
        "what's the time" with an apostrophe would look right and match nothing,
        because normalise() has already turned the apostrophe into a space by
        the time the lookup happens.
        """
        for phrase in fast_commands.phrases():
            self.assertEqual(fast_commands.normalise(phrase), phrase,
                             f"{phrase!r} is unreachable")

    def test_every_phrase_resolves_to_a_handler(self):
        for phrase in fast_commands.phrases():
            self.assertIsNotNone(fast_commands.resolve(phrase), phrase)


class WholeUtteranceTests(unittest.TestCase):
    """The core safety property: mentioning a command is not issuing it."""

    NOT_COMMANDS = (
        "why is chrome muted",
        "mute the tab not the system",
        "should I mute this call",
        "how do I mute a discord channel",
        "explain what muting does",
        "turn the lights off in my room",
        "what time is my flight",
        "what time should I leave for the airport",
        "the date of the exam",
        "battery life on the new macbook",
        "volume of a sphere",
        "teach me about batteries",
        "louder than a jet engine",
        "time complexity of quicksort",
        "play chess with me",
        "pause the deployment",
    )

    def test_mentions_are_not_commands(self):
        for spoken in self.NOT_COMMANDS:
            self.assertIsNone(fast_commands.resolve(spoken), spoken)

    def test_words_needed_elsewhere_are_left_alone(self):
        # "next" is the next board page during a lesson and "stop" is how a
        # running task gets interrupted. Claiming either would take away a
        # control that matters more than the shortcut.
        for spoken in ("next", "skip", "stop", "back", "board", "previous"):
            self.assertIsNone(fast_commands.resolve(spoken), spoken)

    def test_the_qualified_forms_still_work(self):
        for spoken in ("next song", "skip this song", "stop the music",
                       "previous track"):
            self.assertIsNotNone(fast_commands.resolve(spoken), spoken)


class AudioTests(_Stubbed):

    def test_mute_and_unmute(self):
        self.assertEqual(fast_commands.run("mute"), "Muted.")
        self.assertTrue(self.controls.muted)
        self.assertEqual(fast_commands.run("unmute"), "Unmuted.")
        self.assertFalse(self.controls.muted)

    def test_volume_steps_from_wherever_it_is(self):
        self.controls.volume = 40
        self.assertEqual(fast_commands.run("louder"), "Volume 50 percent.")
        self.assertEqual(fast_commands.run("quieter"), "Volume 40 percent.")

    def test_natural_small_reduction_phrases_take_the_fast_path(self):
        self.controls.volume = 60
        for spoken in ("turn it down a little", "make it a little quieter",
                       "the sound is too loud"):
            self.controls.volume = 60
            self.assertEqual(fast_commands.run(spoken), "Volume 50 percent.")

    def test_volume_is_clamped(self):
        self.controls.volume = 96
        self.assertEqual(fast_commands.run("volume up"), "Volume 100 percent.")
        self.controls.volume = 4
        self.assertEqual(fast_commands.run("volume down"), "Volume 0 percent.")

    def test_turning_it_up_also_unmutes(self):
        # Raising the volume on a muted speaker does nothing audible, which
        # looks like the command was ignored.
        self.controls.muted = True
        fast_commands.run("louder")
        self.assertFalse(self.controls.muted)

    def test_an_explicit_level(self):
        self.assertEqual(fast_commands.run("volume 35"), "Volume 35 percent.")
        self.assertEqual(fast_commands.run("set the volume to 80"),
                         "Volume 80 percent.")
        self.assertEqual(fast_commands.run("volume 35 percent"),
                         "Volume 35 percent.")

    def test_an_impossible_level_is_not_a_fast_command(self):
        for spoken in ("volume 400", "volume 101", "brightness 250"):
            self.assertIsNone(fast_commands.resolve(spoken), spoken)

    def test_media_keys(self):
        fast_commands.run("pause")
        fast_commands.run("next song")
        fast_commands.run("previous song")
        fast_commands.run("stop the music")
        self.assertEqual([c for c in self.controls.calls if c[0] == "media"],
                         [("media", "playpause"), ("media", "next"),
                          ("media", "previous"), ("media", "stop")])


class StudyMusicTests(unittest.TestCase):

    def test_generic_play_music_uses_verified_owned_player(self):
        from providers import ytmusic_api
        real = ytmusic_api.play_default
        try:
            ytmusic_api.play_default = lambda: (
                "[Success] Playing on YouTube Music. Now playing: Evening Mix")
            self.assertIs(fast_commands.resolve("play music"),
                          fast_commands._default_music)
            self.assertEqual(fast_commands.run("play music"),
                             "Playing: Evening Mix.")
        finally:
            ytmusic_api.play_default = real

    def test_generic_play_music_does_not_claim_unverified_success(self):
        from providers import ytmusic_api
        real = ytmusic_api.play_default
        try:
            ytmusic_api.play_default = lambda: "[Error] Player tab was closed"
            reply = fast_commands.run("play music")
            self.assertIn("couldn't verify", reply)
            self.assertNotEqual(reply, "Done.")
        finally:
            ytmusic_api.play_default = real

    def test_study_music_uses_verified_provider_result(self):
        from providers import ytmusic_api
        real = ytmusic_api.play
        try:
            ytmusic_api.play = lambda _query: (
                "[Success] Playing on YouTube Music. Now playing: Focus Mix")
            self.assertEqual(fast_commands.run("play study music"),
                             "Study music is playing: Focus Mix.")
            ytmusic_api.play = lambda _query: (
                "[Error] YouTube Music opened but nothing is playing")
            self.assertIn("did not launch another track",
                          fast_commands.run("play study music"))
        finally:
            ytmusic_api.play = real


class MusicReceiptTests(unittest.TestCase):

    def test_negative_now_playing_text_is_not_success(self):
        from providers.ytmusic_api import _play_receipt
        receipt = _play_receipt("study music",
                                "YouTube Music is open but nothing is playing.")
        self.assertTrue(receipt.startswith("[Error]"))
        self.assertNotIn("[Success]", receipt)

    def test_pause_control_is_live_playback_evidence(self):
        import json
        import sys
        import types
        from unittest.mock import patch
        from providers.ytmusic_api import _player_is_playing

        playing = types.SimpleNamespace(
            snapshot=lambda tab=None: "[Browser Snapshot] " + json.dumps({
                "elements": [{"role": "button", "name": "Pause"}]
            }))
        stopped = types.SimpleNamespace(
            snapshot=lambda tab=None: "[Browser Snapshot] " + json.dumps({
                "elements": [{"role": "button", "name": "Play"}]
            }))
        with patch.dict(sys.modules, {"browser": playing}):
            self.assertTrue(_player_is_playing("t1"))
        with patch.dict(sys.modules, {"browser": stopped}):
            self.assertFalse(_player_is_playing("t1"))

    def test_generic_title_does_not_override_live_playback_evidence(self):
        import json
        import sys
        import types
        from unittest.mock import patch
        from providers.ytmusic_api import now_playing

        browser = types.SimpleNamespace(
            tabs=lambda: "[Browser Tabs] " + json.dumps([{
                "tab": "t1", "url": "https://music.youtube.com/watch?v=x",
                "title": "YouTube Music"}]),
            snapshot=lambda tab=None: "[Browser Snapshot] " + json.dumps({
                "elements": [{"role": "button", "name": "Pause"}]}))
        with patch.dict(sys.modules, {"browser": browser}):
            self.assertEqual(
                now_playing(), "Now playing: audio in YouTube Music")

    def test_observed_track_is_success(self):
        from providers.ytmusic_api import _play_receipt
        receipt = _play_receipt("study music", "Now playing: Focus Mix")
        self.assertTrue(receipt.startswith("[Success]"))
        self.assertIn("Focus Mix", receipt)


    def test_natural_study_music_phrasing_stays_on_reflex_path(self):
        for spoken in ("play some music i want to study",
                       "put on a focus music playlist",
                       "start some lo fi beats"):
            self.assertIs(fast_commands.resolve(spoken), fast_commands._study_music)

    def test_unrelated_play_request_is_not_stolen(self):
        self.assertIsNone(fast_commands.resolve("play chess while i study"))


class BrightnessTests(_Stubbed):

    def test_brightness_steps(self):
        self.controls.brightness = 60
        self.assertEqual(fast_commands.run("brighter"), "Brightness 70 percent.")

    def test_an_external_monitor_falls_through_to_the_model(self):
        # No WmiMonitorBrightness means the machine cannot do it. The model can
        # explain why; the fast path must not report success.
        self.controls.fail = True
        self.assertIsNone(fast_commands.run("brighter"))


class HonestFailureTests(_Stubbed):
    """Slow is acceptable. Claiming something that did not happen is not."""

    def test_a_refused_call_returns_none(self):
        self.controls.fail = True
        for spoken in ("mute", "unmute", "louder", "volume 30", "pause"):
            self.assertIsNone(fast_commands.run(spoken), spoken)

    def test_a_raising_handler_returns_none(self):
        def explode():
            raise RuntimeError("no audio endpoint")
        fast_commands._audio = explode
        self.assertIsNone(fast_commands.run("mute"))

    def test_a_ui_command_needs_a_queue(self):
        self.assertIsNone(fast_commands.run("hide the board", None))

    def test_a_ui_command_with_a_queue_sends_the_message(self):
        import queue
        outbox = queue.Queue()
        self.assertEqual(fast_commands.run("hide the board", outbox),
                         "Report hidden.")
        self.assertEqual(outbox.get_nowait(), ("hide_result", ""))

    def test_screen_light_off_is_routed(self):
        import queue
        outbox = queue.Queue()
        self.assertEqual(fast_commands.run("lights off", outbox), "Light off.")
        self.assertEqual(outbox.get_nowait(), ("screen_light_off", ""))


class LocalAnswerTests(unittest.TestCase):

    def test_the_time_is_answered_without_a_model(self):
        reply = fast_commands.run("what time is it")
        self.assertRegex(reply, r"^It's \d{2}:\d{2}\.$")

    def test_the_date_is_answered_without_a_model(self):
        import datetime
        reply = fast_commands.run("what day is it")
        self.assertIn(datetime.datetime.now().strftime("%A"), reply)


class AuthenticationCommandTests(unittest.TestCase):

    def test_google_reauthorization_is_an_exact_fast_command(self):
        from unittest.mock import patch
        with patch("providers.google_api.reauthorize_google",
                   return_value="Google access was reauthorized successfully.") as oauth, \
             patch("providers.google_api.google_connection_status",
                   return_value={"ok": True, "message": "valid"}):
            self.assertEqual(
                fast_commands.run("Vanta, reauthorize Google"),
                "Google access was reauthorized and verified successfully.")
        oauth.assert_called_once_with(
            __import__("pathlib").Path(__file__).resolve().parents[1])

    def test_google_authentication_mentions_do_not_trigger_oauth(self):
        for spoken in ("why did google authentication expire",
                       "should I reconnect google",
                       "explain how google oauth works",
                       "is google connected"):
            self.assertIsNone(fast_commands.resolve(spoken), spoken)

    def test_google_oauth_failure_is_reported_instead_of_swallowed(self):
        from unittest.mock import patch
        with patch("providers.google_api.reauthorize_google",
                   side_effect=RuntimeError("consent window was closed")):
            reply = fast_commands.run("reauthorize google")
        self.assertIn("could not be completed", reply)
        self.assertIn("consent window was closed", reply)

    def test_google_oauth_success_is_verified_before_it_is_claimed(self):
        from unittest.mock import patch
        with patch("providers.google_api.reauthorize_google"), \
             patch("providers.google_api.google_connection_status",
                   return_value={"ok": False, "message": "grant rejected"}):
            reply = fast_commands.run("reauthorize google")
        self.assertIn("could not verify", reply)
        self.assertNotIn("verified successfully", reply)

    def test_google_reauthorization_precedes_the_model_pipeline(self):
        import inspect
        import agent
        source = inspect.getsource(agent.poll_input_queue)
        self.assertIn("fast_commands.resolve(text)", source)
        self.assertLess(source.index("fast_commands.resolve(text)"),
                        source.index("run_agent_loop"))


class WiringTests(unittest.TestCase):

    def test_the_fast_path_runs_before_any_model_call(self):
        """Placement is the whole point.

        Behind the planner it would save nothing -- the cost and the delay are
        already spent by then.
        """
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("fast_commands", source)
        fast_at = source.index("fast_commands")
        for later in ("_inner_run_agent_loop", "usage_before"):
            self.assertLess(fast_at, source.index(later),
                            f"fast commands must run before {later}")

    def test_a_fast_turn_is_still_recorded(self):
        # A turn the model never saw still happened, and "why is it muted"
        # afterwards has to be answerable.
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("save_conversation", source)


class MuteMeaningTests(unittest.TestCase):
    """"Mute" means two things, and which one depends on whether VANTA is talking.

    process_audio intercepts the mute phrases to stop TTS. Unconditionally, that
    made the system-mute fast command unreachable by voice -- the one input it
    was built for -- because the turn returned before run_agent_loop was ever
    called. Over speech, stopping the speech is right. Into silence, the word
    means the speakers.
    """

    def test_stopping_speech_is_conditional_on_speaking(self):
        import inspect
        import agent
        source = inspect.getsource(agent.process_audio)
        self.assertIn("MUTE_PHRASES", source)
        guard = source.index("if text_lower_stripped in MUTE_PHRASES")
        tail = source[guard:guard + 700]
        self.assertIn("STATE_SPEAKING", tail,
                      "mute must only stop TTS when something is being said")
        self.assertIn("was_barge_in", tail)

    def test_the_overlapping_phrases_are_still_fast_commands(self):
        # These appear in both places; the fast path must still own them when
        # nothing is speaking.
        for spoken in ("mute", "be quiet", "silence"):
            self.assertIsNotNone(fast_commands.resolve(spoken), spoken)

    def test_cancel_words_are_not_claimed_by_the_fast_path(self):
        # "stop" and "cancel" are how a running task gets interrupted. The fast
        # path must not shadow the brake.
        for spoken in ("stop", "cancel", "cancel that", "never mind", "nevermind"):
            self.assertIsNone(fast_commands.resolve(spoken), spoken)


class TelegramReplyTests(unittest.TestCase):
    """A request from Telegram comes from someone who is not at the machine.

    Speaking the answer into an empty room and leaving him to read it on a phone
    was the worst of both: he could not hear it, and anyone standing near the
    laptop could.
    """

    def test_a_telegram_turn_does_not_use_the_laptop_speaker(self):
        """The decision lives in speak(), not at the reply site.

        It was at the reply site first, which covered the final answer and
        missed every intermediate <SAY> -- so a Telegram request still spoke
        aloud three or four times. Covered properly by
        TurnChannelTests.test_a_telegram_turn_never_reaches_the_local_speaker.
        """
        import inspect
        import agent
        source = inspect.getsource(agent.speak)
        self.assertIn("turn_context.is_remote()", source)
        self.assertIn("_reply_aloud_on_telegram", source)

    def test_the_voice_reply_goes_out_as_audio(self):
        import inspect
        import agent
        source = inspect.getsource(agent._reply_aloud_on_telegram)
        self.assertIn("send_telegram_audio", source)
        self.assertIn("speak_to_file", source)

    def test_the_text_reply_is_never_lost_to_an_audio_failure(self):
        """A synthesis or upload failure must cost the voice, not the answer.

        The dashboard text is sent before anything is spoken, and the voice
        helper swallows its own failures rather than raising into the turn.
        """
        import inspect
        import agent
        source = inspect.getsource(agent._inner_run_agent_loop)
        text_at = source.index("send_telegram_message(dashboard_text)")
        # The speech path may trim text already emitted by streaming TTS, so
        # the concrete argument is now ``remainder``.  What matters here is the
        # delivery ordering: durable text reaches Telegram before any voice
        # delivery is attempted.
        speak_at = source.index("speak(remainder")
        self.assertLess(text_at, speak_at)
        helper = inspect.getsource(agent._reply_aloud_on_telegram)
        self.assertIn("except Exception", helper)
        self.assertIn("text was still sent", helper)

    def test_the_audio_file_is_cleaned_up(self):
        import inspect
        import agent
        source = inspect.getsource(agent._reply_aloud_on_telegram)
        self.assertIn("finally:", source)
        self.assertIn("os.remove(path)", source)

    def test_the_sender_reports_whether_telegram_accepted_it(self):
        # Otherwise a silent rejection leaves him waiting for a reply that was
        # never delivered.
        import inspect
        from providers import telegram_bot
        source = inspect.getsource(telegram_bot.send_telegram_audio)
        self.assertIn("return False", source)
        self.assertIn("return True", source)


class WakeReliabilityTests(unittest.TestCase):
    """Why it passed calibration and failed in a room."""

    def test_the_queue_survives_a_transcription(self):
        """The gate blocks ~500ms transcribing; the queue held 0.77s.

        So any sound in the room cost most of a second of deafness right after
        it, and "Vanta" spoken just after anything else was clipped in half
        before the gate saw it. Invisible when the word is said into silence.
        """
        import agent
        seconds = agent.WAKE_QUEUE_CHUNKS * 1024 / 16000
        self.assertGreater(seconds, 1.5,
                           "too shallow to cover a transcription")
        # Still a rolling window, not a recording.
        self.assertLess(seconds, 10)

    def test_the_wake_path_can_be_observed(self):
        # "It does not work" has several causes that look identical from
        # outside, and guessing between them wasted a round already.
        import wakeword
        self.assertTrue(hasattr(wakeword, "DEBUG"))
        import inspect
        source = inspect.getsource(wakeword.SpeechGateEngine.feed)
        self.assertIn("_debug(", source)

    def test_the_worker_reports_whether_audio_arrives_at_all(self):
        import inspect
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        self.assertIn("chunks in the last", source)
        self.assertIn("state=", source)


class NoWakeWordTests(unittest.TestCase):
    """Some things need no name, and most things still do.

    Full always-on listening is not a matter of trying harder -- everything said
    to another person becomes a command, VANTA hears its own voice, and every
    utterance costs 10-30k prompt tokens through a model. The fast commands are
    the subset where none of that applies: they never reach a model, act
    instantly, and undo in one word, so a false positive costs nothing.
    """

    def test_a_bare_fast_command_is_recognised_by_the_closed_set(self):
        for spoken in ("mute", "louder", "pause", "next song", "brighter",
                       "what time is it", "battery", "lights off"):
            self.assertIsNotNone(fast_commands.resolve(spoken), spoken)

    def test_empty_primary_stt_has_a_local_closed_set_recovery(self):
        import inspect
        import stt
        source = inspect.getsource(stt.recover_fast_command)
        self.assertIn("transcribe_samples_local", source)
        self.assertIn("fast_commands.resolve", source)
        agent_source = inspect.getsource(__import__("agent").process_audio)
        self.assertIn("recover_fast_command(audio_np)", agent_source)

    def test_push_to_talk_can_interrupt_speech_even_without_an_agent_lock(self):
        import inspect
        import agent
        source = inspect.getsource(agent.on_press)
        self.assertIn("speaking_now = current_state == STATE_SPEAKING", source)
        self.assertIn("or active_task or speaking_now", source)

    def test_conversation_about_those_words_is_not_a_command(self):
        """The protection is whole-utterance matching, not cleverness."""
        for spoken in ("why is chrome muted",
                       "mute the tab not the system",
                       "i should mute this",
                       "turn the lights off in my room",
                       "what time is my flight",
                       "the battery on this laptop is fine",
                       "play chess with me"):
            self.assertIsNone(fast_commands.resolve(spoken), spoken)

    def test_the_worker_only_acts_on_the_closed_set(self):
        import inspect
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        self.assertIn("fast_commands.resolve(heard)", source,
                      "anything outside the closed set must still need the name")
        # And it must be gated by the flag, so this is switchable.
        self.assertIn("WAKE_OPEN_COMMANDS", source)

    def test_nameless_fast_commands_only_run_in_a_live_conversation(self):
        """Silence/TV cannot ask for time after the 30-second window."""
        import inspect
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        self.assertIn("WAKE_OPEN_COMMANDS and in_conversation", source)

    def test_nameless_commands_use_the_unbiased_transcript(self):
        """The wake-name correction prompt must not manufacture a command."""
        import inspect
        import agent
        source = inspect.getsource(agent.wakeword_worker)
        self.assertIn('"unbiased_heard"', source)

    def test_nothing_in_the_open_set_is_irreversible(self):
        """Every phrase reachable without the wake word must be undoable.

        A false positive on "mute" costs one word to fix. A false positive on
        anything that sends, writes or deletes would not.
        """
        import fast_commands as fc
        forbidden = ("delete", "send", "email", "shutdown", "restart", "lock",
                     "sign out", "format", "install", "buy", "book", "pay")
        for phrase in fc.phrases():
            for word in forbidden:
                self.assertNotIn(word, phrase.lower(),
                                 f"'{phrase}' is reachable with no wake word")

    def test_the_detector_still_cannot_hear_vanta_itself(self):
        # Acting without a name makes self-triggering worse, not better, so the
        # idle gate matters more now than it did.
        import agent
        previous = agent.current_state
        try:
            agent.current_state = agent.STATE_SPEAKING
            self.assertFalse(agent._wake_may_listen())
        finally:
            agent.current_state = previous

    def test_the_gate_reports_what_it_heard(self):
        import wakeword
        gate = wakeword.SpeechGateEngine("vanta")
        self.assertEqual(gate.heard, "")
        # reset() must not wipe it: the caller reads it after feed() returns.
        gate.heard = "mute"
        gate.reset()
        self.assertEqual(gate.heard, "mute")

    def test_it_can_be_turned_off(self):
        import agent
        self.assertIsInstance(agent.WAKE_OPEN_COMMANDS, bool)


class TurnChannelTests(unittest.TestCase):
    """Where the turn came from decides where everything in it goes.

    Routing only the final reply was not enough: a turn says an acknowledgement,
    a line before a long action, and a note that something is waiting, each
    through speak(). A Telegram request therefore talked to an empty room three
    or four times and sent only its last sentence to the phone.
    """

    def setUp(self):
        import turn_context
        turn_context.end()

    def tearDown(self):
        import turn_context
        turn_context.end()

    def test_the_channel_is_detected_from_the_input(self):
        import turn_context as tc
        self.assertEqual(tc.detect("[Telegram]: hello"), tc.TELEGRAM)
        self.assertEqual(tc.detect("hello"), tc.LOCAL)
        self.assertEqual(tc.detect(""), tc.LOCAL)
        self.assertEqual(tc.detect(None), tc.LOCAL)

    def test_local_is_the_default(self):
        import turn_context as tc
        self.assertFalse(tc.is_remote())

    def test_the_marker_is_stripped_for_comparison(self):
        """"[Telegram]: approve" was compared against "approve" and matched
        nothing, so approving from a phone silently did nothing -- at exactly
        the moment he is not at the machine to click anything."""
        import turn_context as tc
        self.assertEqual(tc.strip_prefix("[Telegram]: approve"), "approve")
        self.assertEqual(tc.strip_prefix("  [Telegram]:  approve "), "approve")
        self.assertEqual(tc.strip_prefix("approve"), "approve")

    def test_speak_decides_the_destination_itself(self):
        # Not the call sites: there is no way to find every call site once and
        # for all, and three of them were missed the first time.
        import inspect
        import agent
        source = inspect.getsource(agent.speak)
        self.assertIn("turn_context.is_remote()", source)
        self.assertIn("_reply_aloud_on_telegram", source)
        # And it must decide before the local audio path runs.
        route_at = source.index("turn_context.is_remote()")
        self.assertLess(route_at, source.index("_barge_in_event.is_set()"))

    def test_the_channel_is_set_before_anything_can_speak(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("turn_context.begin", source)
        begin_at = source.index("turn_context.begin")
        self.assertLess(begin_at, source.index("_inner_run_agent_loop"))

    def test_the_channel_is_cleared_when_the_turn_ends(self):
        # Otherwise one Telegram turn silences the speaker for good.
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("turn_context.end()", source)

    def test_a_telegram_turn_never_reaches_the_local_speaker(self):
        import agent
        import turn_context
        calls = []
        original = agent._reply_aloud_on_telegram
        agent._reply_aloud_on_telegram = lambda text: calls.append(text)
        try:
            turn_context.begin(turn_context.TELEGRAM)
            agent.speak("first line", False)
            agent.speak("second line", False)
        finally:
            agent._reply_aloud_on_telegram = original
            turn_context.end()
        self.assertEqual(calls, ["first line", "second line"],
                         "every line of a remote turn must go to Telegram")


class RemoteApprovalTests(unittest.TestCase):
    """An approval prompt on a screen he is not looking at is no prompt."""

    def test_there_is_only_one_place_approvals_are_queued(self):
        """There were two, and RUN_COMMAND used the one without a prompt.

        So the buttons existed and the action that most needed them could not
        reach them -- which is exactly what happened in use.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "actions.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(source.count("INSERT INTO approvals"), 1)

    def test_the_request_follows_him_to_telegram(self):
        import inspect
        import actions
        source = inspect.getsource(actions._queue_for_approval)
        self.assertIn("turn_context.is_remote()", source)
        self.assertIn("send_telegram_message", source)
        # Telegram has no buttons, so the words to reply must be spelled out.
        self.assertIn("'approve'", source)
        self.assertIn("'reject'", source)

    def test_it_still_shows_on_screen_as_well(self):
        import inspect
        import actions
        source = inspect.getsource(actions._queue_for_approval)
        self.assertIn('"approval_request"', source)

    def test_telegram_words_reach_the_approval_path(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        self.assertIn("turn_context.strip_prefix(text)", source)


class OnDemandPanelTests(unittest.TestCase):
    """Adding a panel before its page exists must not break the queue."""

    def test_the_panel_refresh_tolerates_an_unbuilt_page(self):
        import inspect
        import control_center
        source = inspect.getsource(control_center.ControlCenterMixin._cc_refresh_panels)
        self.assertIn('getattr(self, "_cc_panel_host", None)', source)
        self.assertIn('getattr(self, "_cc_panels_signature", None)', source)


if __name__ == "__main__":
    unittest.main()
