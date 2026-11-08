import os
import base64
from io import BytesIO
import tempfile
import time
import unittest
from unittest import mock
import json

from PIL import Image

import camera_awareness
import protocol_manager
import runtime_power


class _Shot:
    def __init__(self, path, ok=True):
        self.path = path
        self.ok = ok
        self.b64 = "aW1hZ2U="
        self.detail = ""

    def probe_summary(self):
        return "capture failed" if not self.ok else "captured"


class CameraAwarenessTests(unittest.TestCase):
    def setUp(self):
        # An owner is enrolled unless a test says otherwise; the real answer
        # would come from the live graph.
        patcher = mock.patch.object(camera_awareness, "identity_enrolled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nobody_is_a_guest_until_the_owner_is_enrolled(self):
        # Otherwise a fresh install, or a new recognition model awaiting
        # re-enrolment, treats the owner as a stranger and locks him out of
        # enrolling.
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = None
        with mock.patch.object(camera_awareness, "identity_enrolled", return_value=False):
            self.assertFalse(camera_awareness.guest_session_active())
            self.assertTrue(camera_awareness.primary_user_is_present())

    def test_enrolling_a_face_is_never_a_guest_restricted_action(self):
        import autonomy
        for name in ("REMEMBER_FACE", "FORGET_FACE", "WHO_IS_THIS"):
            self.assertFalse(autonomy.is_personal_action(name), name)

    def test_primary_enrolled_reads_the_current_models_templates(self):
        import face_identity
        import numpy as np
        vec = np.zeros(512, dtype=np.float32)
        with mock.patch.object(face_identity, "_templates", return_value=(("Himanshu", "t", vec),)):
            self.assertTrue(face_identity.primary_enrolled())
        with mock.patch.object(face_identity, "_templates", return_value=(("Madhu", "t", vec),)):
            self.assertFalse(face_identity.primary_enrolled())
        with mock.patch.object(face_identity, "_templates", return_value=()):
            self.assertFalse(face_identity.primary_enrolled())
        with mock.patch.object(face_identity, "_templates", side_effect=RuntimeError("down")):
            self.assertTrue(face_identity.primary_enrolled())      # unreadable: stay strict

    def tearDown(self):
        protocol_manager.deactivate()
        protocol_manager.answer("cancel")
        runtime_power.resume()
        camera_awareness._monitor_generation = -1
        camera_awareness._monitor_hits = 0
        camera_awareness._monitor_alerted = False
        camera_awareness.reset_trust()
        camera_awareness._last_frame_fingerprint = None
        camera_awareness._clear_identity_cache()
        camera_awareness._reset_visual_context()
        import visual_director
        visual_director.director.reset()
        camera_awareness._input_queue = None
        with camera_awareness._lock:
            camera_awareness._state = camera_awareness.AwarenessState()

    @staticmethod
    def _image_b64(value):
        image = Image.new("L", (80, 80), color=value)
        buffer = BytesIO()
        image.save(buffer, "PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def _see(self, present=True, himanshu=None, faces=0, age=0.0):
        """Record a camera reading the way the sampler does, then renew trust."""
        values = (present, None, "unknown", "unknown", 1 if present else 0, faces,
                  ("Himanshu",) if himanshu else (), himanshu, 0 if himanshu else
                  (faces if himanshu is False else 0))
        camera_awareness._apply_analysis(values)
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time() - age

    def test_a_match_opens_a_session_that_a_poor_frame_does_not_end(self):
        self._see(himanshu=True, faces=1)
        self.assertEqual(camera_awareness.trust_status(), "verified")
        # Next frame: someone is there but the face is too dark to match.
        self._see(himanshu=None, faces=0)
        self.assertTrue(camera_awareness.identity_trusted())
        self.assertFalse(camera_awareness.guest_session_active())
        self.assertTrue(camera_awareness.primary_user_is_present())
        self.assertEqual(camera_awareness.trust_status(), "verified")

    def test_without_a_match_the_person_is_a_guest(self):
        self._see(himanshu=None, faces=0)
        self.assertFalse(camera_awareness.identity_trusted())
        self.assertTrue(camera_awareness.guest_session_active())
        self.assertEqual(camera_awareness.trust_status(), "guarded")

    def test_the_session_expires(self):
        self._see(himanshu=True, faces=1)
        camera_awareness._verified_at = time.time() - (camera_awareness.trust_minutes() * 60 + 5)
        self._see(himanshu=None, faces=0)
        self.assertFalse(camera_awareness.identity_trusted())
        self.assertTrue(camera_awareness.guest_session_active())

    def test_an_empty_chair_ends_it_so_a_newcomer_must_be_matched(self):
        self._see(himanshu=True, faces=1)
        self._see(present=False)
        self.assertFalse(camera_awareness.identity_trusted())
        self._see(himanshu=None, faces=0)           # someone sits down, face unclear
        self.assertTrue(camera_awareness.guest_session_active())

    def test_another_face_without_his_ends_it(self):
        self._see(himanshu=True, faces=1)
        self._see(himanshu=False, faces=1)
        self.assertFalse(camera_awareness.identity_trusted())
        self.assertTrue(camera_awareness.guest_session_active())

    def test_darkness_alone_does_not_end_it(self):
        self._see(himanshu=True, faces=1)
        self._see(present=None, himanshu=None, faces=0)
        self.assertTrue(camera_awareness.identity_trusted())

    def test_a_dead_camera_cannot_keep_a_session_alive(self):
        self._see(himanshu=True, faces=1)
        self._see(himanshu=None, faces=0, age=600)
        self.assertFalse(camera_awareness.identity_trusted())

    def test_owner_override_shows_as_such(self):
        protocol_manager.activate("owner_override")
        self.assertEqual(camera_awareness.trust_status(), "override")

    def test_lifecycle_request_makes_existing_match_guarded_until_reverified(self):
        self._see(himanshu=True, faces=1)
        self.assertEqual(camera_awareness.trust_status(), "verified")
        camera_awareness.request_verification("standby ended", immediate=False)
        self.assertTrue(camera_awareness.verification_required())
        self.assertEqual(camera_awareness.trust_status(), "guarded")
        self._see(himanshu=True, faces=1)
        self.assertFalse(camera_awareness.verification_required())
        self.assertEqual(camera_awareness.trust_status(), "verified")

    def test_forced_sample_runs_face_id_even_when_frame_is_unchanged(self):
        person = mock.Mock(label="person")
        shot = _Shot("unused.jpg")
        with mock.patch.object(camera_awareness, "_frame_gate",
                               return_value=("unchanged", 0.1, 100)), \
             mock.patch.object(camera_awareness, "_detect_objects",
                               return_value=((person,), True)), \
             mock.patch.object(camera_awareness, "_identity_fields",
                               return_value=(1, ("Himanshu",), True, 0)) as faces:
            result = camera_awareness._cascade_sample(
                shot, now=100, force_identity=True)
        self.assertTrue(result.stage1_ran)
        self.assertTrue(result.face_scan_ran)
        self.assertEqual(result.state.himanshu_present, True)
        faces.assert_called_once()

    def test_camera_context_exposes_labels_without_pixels(self):
        self._see(himanshu=True, faces=1)
        with camera_awareness._lock:
            camera_awareness._state.object_labels = ("person", "bottle", "bottle")
        block = camera_awareness.context_for_agent()
        self.assertIn('"bottle": 2', block)
        self.assertNotIn("base64", block.lower())

    def test_standby_monitor_is_explicit_state(self):
        runtime_power.enter(None, monitor=True)
        self.assertTrue(runtime_power.is_standby())
        self.assertTrue(runtime_power.monitor_enabled())
        runtime_power.resume()
        self.assertFalse(runtime_power.monitor_enabled())

    def test_ordinary_standby_does_not_arm_camera_monitor(self):
        runtime_power.enter(None)
        self.assertTrue(runtime_power.is_standby())
        self.assertFalse(runtime_power.monitor_enabled())

    def test_sample_is_ephemeral(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sample.jpg")
            with open(path, "wb") as handle:
                handle.write(b"jpeg")

            def capture(*args, **kwargs):
                self.assertFalse(kwargs["retain"])
                return _Shot(path)

            with mock.patch.object(camera_awareness, "_analyse",
                                   return_value=(True, True, "comfortable",
                                                 "neutral", 1, 1)):
                state = camera_awareness.observe_once(root, study=True,
                                                      capture_fn=capture)
            self.assertTrue(state.present)
            self.assertTrue(state.attentive)
            self.assertFalse(os.path.exists(path))

    def test_frame_gate_skips_dark_and_unchanged_frames_before_models(self):
        camera_awareness._last_frame_fingerprint = None
        dark = self._image_b64(0)
        normal = self._image_b64(100)
        changed = self._image_b64(180)
        self.assertEqual(camera_awareness._frame_gate(dark)[0], "unusable")
        # The unusable frame did not become the baseline.
        self.assertEqual(camera_awareness._frame_gate(normal)[0], "changed")
        self.assertEqual(camera_awareness._frame_gate(normal)[0], "unchanged")
        self.assertEqual(camera_awareness._frame_gate(changed)[0], "changed")

    def test_no_change_reuses_state_without_yolo_or_face_models(self):
        shot = _Shot("unused.jpg")
        with camera_awareness._lock:
            camera_awareness._state = camera_awareness.AwarenessState(
                observed_at=10, present=True, person_count=1,
                himanshu_present=True, recognized_people=("Himanshu",))
        camera_awareness._remember_identity(
            (1, ("Himanshu",), True, 0), now=10)
        with mock.patch.object(camera_awareness, "_frame_gate",
                               return_value=("unchanged", 0.5, 100)), \
             mock.patch.object(camera_awareness, "_detect_objects") as objects, \
             mock.patch.object(camera_awareness, "_identity_fields") as faces:
            result = camera_awareness._cascade_sample(shot, now=20)
        self.assertEqual(result.gate, "unchanged")
        self.assertFalse(result.stage1_ran)
        self.assertEqual(result.state.observed_at, 20)
        objects.assert_not_called()
        faces.assert_not_called()

    def test_person_appearance_scans_face_then_reuses_identity_cache(self):
        person = mock.Mock(label="person")
        shot = _Shot("unused.jpg")
        with mock.patch.object(camera_awareness, "_frame_gate",
                               side_effect=(("changed", 25, 100),
                                            ("changed", 6, 100))), \
             mock.patch.object(camera_awareness, "_detect_objects",
                               return_value=((person,), True)), \
             mock.patch.object(camera_awareness, "_identity_fields",
                               return_value=(1, ("Himanshu",), True, 0)) as faces:
            first = camera_awareness._cascade_sample(shot, now=100)
            second = camera_awareness._cascade_sample(shot, now=110)
        self.assertTrue(first.face_scan_ran)
        self.assertFalse(second.face_scan_ran)
        self.assertEqual(second.state.recognized_people, ("Himanshu",))
        self.assertEqual(faces.call_count, 1)

    def test_large_change_rechecks_identity_even_while_person_label_remains(self):
        previous = camera_awareness.AwarenessState(
            observed_at=10, present=True, person_count=1,
            himanshu_present=True, recognized_people=("Himanshu",))
        cached = (1, ("Himanshu",), True, 0)
        self.assertFalse(camera_awareness._should_scan_identity(
            1, previous, cached, 6, True))
        self.assertTrue(camera_awareness._should_scan_identity(
            1, previous, cached, 25, True))

    def test_expired_identity_cache_bypasses_unchanged_frame_skip(self):
        person = mock.Mock(label="person")
        shot = _Shot("unused.jpg")
        with camera_awareness._lock:
            camera_awareness._state = camera_awareness.AwarenessState(
                observed_at=100, present=True, person_count=1,
                face_count=1, himanshu_present=True,
                recognized_people=("Himanshu",))
        camera_awareness._clear_identity_cache()
        with mock.patch.object(camera_awareness, "_frame_gate",
                               return_value=("unchanged", 0.2, 100)), \
             mock.patch.object(camera_awareness, "_detect_objects",
                               return_value=((person,), True)), \
             mock.patch.object(camera_awareness, "_identity_fields",
                               return_value=(1, ("Himanshu",), True, 0)) as faces:
            result = camera_awareness._cascade_sample(shot, now=600)
        self.assertEqual(result.gate, "changed")
        self.assertTrue(result.face_scan_ran)
        faces.assert_called_once()

    def test_events_are_diffs_not_timer_ticks_or_initial_state(self):
        initial = camera_awareness.AwarenessState()
        owner = camera_awareness.AwarenessState(
            observed_at=20, present=True, person_count=1, face_count=1,
            himanshu_present=True, recognized_people=("Himanshu",),
            object_labels=("person", "bottle"))
        self.assertEqual(camera_awareness._scene_events(initial, owner), ())
        empty = camera_awareness.AwarenessState(
            observed_at=10, present=False, person_count=0)
        events = camera_awareness._scene_events(empty, owner)
        self.assertIn("returned", events)
        self.assertIn("new_object:bottle", events)
        unchanged = camera_awareness.AwarenessState(**vars(owner))
        unchanged.observed_at = 30
        self.assertEqual(camera_awareness._scene_events(owner, unchanged), ())

    def test_person_leaving_clears_cached_identity(self):
        camera_awareness._remember_identity(
            (1, ("Himanshu",), True, 0), now=100)
        with camera_awareness._lock:
            camera_awareness._state = camera_awareness.AwarenessState(
                observed_at=100, present=True, person_count=1,
                himanshu_present=True, recognized_people=("Himanshu",))
        shot = _Shot("unused.jpg")
        with mock.patch.object(camera_awareness, "_frame_gate",
                               return_value=("changed", 30, 100)), \
             mock.patch.object(camera_awareness, "_detect_objects",
                               return_value=((), True)), \
             mock.patch.object(camera_awareness, "_identity_fields") as faces:
            result = camera_awareness._cascade_sample(shot, now=120)
        self.assertFalse(result.state.present)
        self.assertIn("left", result.events)
        self.assertIsNone(camera_awareness._cached_identity(now=121))
        faces.assert_not_called()

    def test_dark_detector_miss_is_unknown_not_absent(self):
        shot = _Shot("unused.jpg")
        with mock.patch("vision_pipeline.detect", return_value=((), True)), \
             mock.patch("vision_pipeline.image_luminance", return_value=8.0), \
             mock.patch("face_identity.identify", return_value=()):
            values = camera_awareness._analyse(shot, study=False)
        self.assertIsNone(values[0])

    def test_local_face_identity_distinguishes_himanshu_from_guests(self):
        import face_identity
        person = mock.Mock(label="person")
        himanshu = face_identity.FaceMatch(
            status="recognized", bbox=(0, 0, 10, 10),
            normalized_box=(0, 0, .1, .1), detection_score=.9,
            person="Himanshu", similarity=.8)
        guest = face_identity.FaceMatch(
            status="unknown", bbox=(20, 0, 30, 10),
            normalized_box=(.2, 0, .3, .1), detection_score=.9)
        shot = _Shot("unused.jpg")
        with mock.patch("vision_pipeline.detect",
                        return_value=((person,), True)), \
             mock.patch("face_identity.identify",
                        return_value=(himanshu, guest)):
            values = camera_awareness._analyse(shot)
        self.assertTrue(values[7])
        self.assertEqual(values[6], ("Himanshu",))
        self.assertEqual(values[8], 1)

    def test_other_visible_face_does_not_receive_personal_briefing(self):
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = False
            camera_awareness._state.recognized_people = ("Madhu",)
        with mock.patch("providers.telegram_bot.send_telegram_message",
                        return_value=True) as send:
            self.assertTrue(camera_awareness.route_proactive("Private update"))
        send.assert_called_once_with("Private update")

    def test_himanshu_may_receive_local_briefing(self):
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = True
            camera_awareness._state.recognized_people = ("Himanshu",)
        with mock.patch("providers.telegram_bot.send_telegram_message") as send:
            self.assertFalse(camera_awareness.route_proactive("Welcome back"))
        send.assert_not_called()

    def test_owner_override_allows_local_briefing_without_face_match(self):
        protocol_manager.recommend("owner_override", "routine")
        protocol_manager.answer("yes")
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = False
        with mock.patch("providers.telegram_bot.send_telegram_message") as send:
            self.assertFalse(camera_awareness.route_proactive("Requested update"))
        send.assert_not_called()

    def test_lockdown_routes_locally_present_owner_to_remote_channel(self):
        protocol_manager.recommend("lockdown", "routine")
        protocol_manager.answer("yes")
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = True
        with mock.patch("providers.telegram_bot.send_telegram_message",
                        return_value=True) as send:
            self.assertTrue(camera_awareness.route_proactive("Private update"))
        send.assert_called_once_with("Private update")

    def test_visible_person_without_clear_face_is_guest_safe(self):
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = True
            camera_awareness._state.himanshu_present = None
            camera_awareness._state.face_count = 0
        self.assertTrue(camera_awareness.guest_session_active())
        self.assertEqual(camera_awareness.identity_status(),
                         "person_visible_face_unclear")

    def test_fresh_check_replaces_stale_nonmatch_before_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "fresh.jpg")
            with open(path, "wb") as handle:
                handle.write(b"jpeg")

            def capture(*_args, **_kwargs):
                return _Shot(path)

            values = (True, None, "unknown", "unknown", 1, 1,
                      ("Himanshu",), True, 0)
            with mock.patch.object(camera_awareness, "_analyse",
                                   return_value=values):
                state, status = camera_awareness.verify_primary_now(
                    root, capture_fn=capture)
        self.assertTrue(state.himanshu_present)
        self.assertEqual(status, "primary_confirmed")

    def test_monitor_requires_repeated_detection_after_grace(self):
        runtime_power.enter(None, monitor=True)
        state = camera_awareness.AwarenessState(
            observed_at=time.time(), present=True)
        now = time.time()
        camera_awareness._monitor_generation = runtime_power.generation()
        camera_awareness._monitor_armed_at = now - 60
        with mock.patch.object(camera_awareness, "_notify") as notify, \
             mock.patch.object(camera_awareness, "_lock_windows",
                               return_value=True) as lock, \
             mock.patch.dict(os.environ, {
                 "VANTA_STANDBY_MONITOR_CONFIRMATIONS": "2",
                 "VANTA_STANDBY_MONITOR_GRACE_SECONDS": "5",
             }):
            camera_awareness._monitor_security(state, now)
            lock.assert_not_called()
            camera_awareness._monitor_security(state, now + 6)
            lock.assert_called_once()
            notify.assert_called_once()

    def test_stale_presence_does_not_suppress_local_speech(self):
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time() - 1000
            camera_awareness._state.present = False
        self.assertIsNone(camera_awareness.user_is_present(max_age=90))

    def test_confirmed_absence_suppresses_local_even_if_telegram_is_offline(self):
        with camera_awareness._lock:
            camera_awareness._state.observed_at = time.time()
            camera_awareness._state.present = False
        with mock.patch("providers.telegram_bot.send_telegram_message",
                        return_value=False) as send:
            self.assertTrue(camera_awareness.route_proactive("Update"))
        send.assert_called_once_with("Update")

    def test_ergonomic_interpretation_comes_from_vision_model(self):
        state = camera_awareness.AwarenessState(
            observed_at=time.time(), present=True, face_count=1,
            study_present_seconds=3600)
        shot = _Shot("unused.jpg")
        answer = json.dumps({
            "summary": "The current posture looks comfortable.",
            "speak": False, "brief": "", "confidence": .8,
        })
        with mock.patch("llm_router.call_llm", return_value=answer) as model, \
             mock.patch.object(camera_awareness, "_notify") as notify:
            camera_awareness._model_assess(shot, state)
        self.assertIn("comfortable", camera_awareness.snapshot().model_assessment)
        notify.assert_not_called()
        messages = model.call_args.args[0]
        self.assertEqual(messages[0]["content"][1]["type"], "image_url")

    def test_ordinary_presence_never_escalates_pixels(self):
        state = camera_awareness.AwarenessState(
            observed_at=time.time(), present=True, person_count=1)
        self.assertFalse(camera_awareness._needs_semantic_review(
            state, study=False, monitor=False, now=1000,
            last_reasoning_at=0, interval=300))
        self.assertFalse(camera_awareness._needs_semantic_review(
            state, study=False, monitor=True, now=1000,
            last_reasoning_at=0, interval=300))

    def test_study_mode_may_request_infrequent_semantic_review(self):
        state = camera_awareness.AwarenessState(
            observed_at=time.time(), present=True, person_count=1)
        self.assertTrue(camera_awareness._needs_semantic_review(
            state, study=True, monitor=False, now=1000,
            last_reasoning_at=0, interval=300))
        self.assertFalse(camera_awareness._needs_semantic_review(
            state, study=True, monitor=False, now=100,
            last_reasoning_at=0, interval=300))

    def test_vision_model_decides_whether_scene_deserves_a_nudge(self):
        import visual_director
        state = camera_awareness.AwarenessState(
            observed_at=time.time(), present=True, person_count=1,
            himanshu_present=True, object_labels=("person", "cell phone"))
        shot = _Shot("unused.jpg")
        answer = json.dumps({
            "decision": "suggest",
            "summary": "Himanshu appears to be actively using a phone.",
            "message": "Phone away for this focus block, sir.",
            "topic_key": "focus-distraction",
            "confidence": .86,
            "cooldown_minutes": 20,
            "interrupt_now": True,
        })
        with mock.patch("llm_router.call_llm", return_value=answer) as model, \
             mock.patch.object(visual_director, "relevant_graph_context",
                               return_value="") as graph, \
             mock.patch.object(camera_awareness, "_notify") as notify:
            decision = camera_awareness._model_assess(
                shot, state, reason="stable_scene_change")
        self.assertTrue(decision.should_deliver)
        notify.assert_called_once_with(
            "Phone away for this focus block, sir.", priority="critical",
            reason="visual:focus-distraction")
        graph.assert_called_once()
        prompt = model.call_args.args[0][0]["content"][0]["text"]
        self.assertIn("object-detector labels are fallible evidence", prompt)
        self.assertIn('"cell phone": 1', prompt)

    def test_critical_model_nudge_uses_mode_aware_local_alert_channel(self):
        target = mock.Mock()
        camera_awareness._input_queue = target
        with mock.patch.object(camera_awareness, "primary_user_is_present",
                               return_value=True):
            camera_awareness._notify(
                "A model-authored focus nudge.", priority="critical",
                reason="visual:focus")
        mode, payload = target.put.call_args.args[0]
        self.assertEqual(mode, "local_alert")
        self.assertEqual(payload["priority"], "critical")
        self.assertEqual(payload["text"], "A model-authored focus nudge.")
        self.assertEqual(payload["source_id"], "camera-visual-awareness")

    def test_normal_visual_question_is_delivered_verbatim_not_reprompted(self):
        target = mock.Mock()
        camera_awareness._input_queue = target
        with mock.patch.object(camera_awareness, "primary_user_is_present",
                               return_value=True):
            camera_awareness._notify(
                "Would you like me to account for this break?",
                priority="normal", reason="visual:break-context")
        mode, payload = target.put.call_args.args[0]
        self.assertEqual(mode, "local_alert")
        self.assertEqual(payload["text"],
                         "Would you like me to account for this break?")
        self.assertEqual(payload["priority"], "normal")


if __name__ == "__main__":
    unittest.main()
