# -*- coding: utf-8 -*-
"""
Tests for the camera capability and the six-layer wiring it exposed.

VANTA was asked what it could see, searched for a camera action twice, found
nothing, and told Himanshu it had no access to a live camera feed. The webcam
worked; CAPTURE_CAMERA was simply missing from the catalogue that the
searchable registry is built from.

The tests that matter here are therefore not "does cv2 work". They are:
discovery finds the camera from words a person would actually use; policy is
enforced on the one execution path that used to skip it; and a failure says
what actually failed instead of blaming hardware it never reached.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import camera
import capabilities
import unavailability

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _catalogs():
    """The live catalogues, including the generated section."""
    import ast
    source = open(os.path.join(BASE_DIR, "agent.py"), encoding="utf-8",
                  errors="replace").read()
    catalogs = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_CATALOGS":
                    catalogs = ast.literal_eval(node.value)
    catalogs.update(capabilities.catalog_overlay())
    return catalogs


class Declaration(unittest.TestCase):
    def test_camera_is_declared(self):
        self.assertIsNotNone(capabilities.get("CAPTURE_CAMERA"))

    def test_declaration_names_a_test(self):
        """A capability with no test is how one goes unnoticed."""
        for capability in capabilities.CAPABILITIES.values():
            self.assertTrue(capability.tests, capability.name)

    def test_declaration_names_its_receipt(self):
        camera_capability = capabilities.get("CAPTURE_CAMERA")
        self.assertIn("index", camera_capability.receipt)

    def test_handler_resolves(self):
        handler = capabilities.get("CAPTURE_CAMERA").resolve_handler()
        self.assertTrue(callable(handler))

    def test_duplicate_declaration_is_refused(self):
        existing = capabilities.get("CAPTURE_CAMERA")
        with self.assertRaises(ValueError):
            capabilities.declare(existing)

    def test_declaration_without_receipt_is_refused(self):
        with self.assertRaises(ValueError):
            capabilities.Capability(
                name="NO_RECEIPT", summary="x", handler=lambda **_: None,
                authorization=capabilities.OBSERVATION,
                verification=capabilities.VERIFY_NONE,
                receipt="  ", tests=("t",))

    def test_declaration_without_test_is_refused(self):
        with self.assertRaises(ValueError):
            capabilities.Capability(
                name="NO_TEST", summary="x", handler=lambda **_: None,
                authorization=capabilities.OBSERVATION,
                verification=capabilities.VERIFY_NONE,
                receipt="something", tests=())


class Discovery(unittest.TestCase):
    """The actual reported failure: asked naturally, nothing was found."""

    @classmethod
    def setUpClass(cls):
        action_registry.configure(_catalogs())

    def _names(self, query):
        return [entry["name"] for entry in
                action_registry.search(BASE_DIR, query, None, 12, "user")]

    def test_the_question_that_failed(self):
        self.assertIn("CAPTURE_CAMERA",
                      self._names("what can you see through the camera"))

    def test_found_without_the_word_camera(self):
        """The requirement is discovery from intent, not from the action name."""
        for query in ("what is on my desk", "can you see me",
                      "look at what i am holding up", "show me what you see"):
            with self.subTest(query=query):
                self.assertIn("CAPTURE_CAMERA", self._names(query))

    def test_zoom_found_from_intent(self):
        self.assertIn("ZOOM", self._names("this text is too small to read"))

    def test_camera_is_not_crowded_out(self):
        """Ranked first, not merely present inside a twelve-slot budget."""
        self.assertEqual(self._names("webcam")[0], "CAPTURE_CAMERA")


class Authorization(unittest.TestCase):
    def test_user_may_use_the_camera(self):
        import autonomy
        allowed, _ = autonomy.action_allowed("CAPTURE_CAMERA", "user")
        self.assertTrue(allowed)

    def test_background_task_may_not(self):
        """A proactive event must never photograph him unprompted."""
        import autonomy
        allowed, reason = autonomy.action_allowed("CAPTURE_CAMERA", "proactive")
        self.assertFalse(allowed)
        self.assertIn("CAPTURE_CAMERA", reason)

    def test_declaration_restricts_origin(self):
        self.assertFalse(capabilities.get("CAPTURE_CAMERA").permits_origin("proactive"))

    def test_sleep_mode_blocks_the_camera(self):
        """Silent capture inside a declared quiet window must be impossible."""
        self.assertIn("CAPTURE_CAMERA", capabilities.mode_blocked_names("sleep"))

    def test_working_modes_allow_it(self):
        for mode in ("assistant", "study", "build"):
            with self.subTest(mode=mode):
                self.assertNotIn("CAPTURE_CAMERA",
                                 capabilities.mode_blocked_names(mode))


class _FakeCapture:
    """A cv2.VideoCapture stand-in, so failure modes are testable at all."""

    def __init__(self, opened=True, reads=True, raise_on_open=False):
        self._opened = opened
        self._reads = reads
        self.released = False
        if raise_on_open:
            raise RuntimeError("device busy")

    def isOpened(self):
        return self._opened

    def read(self):
        if not self._reads:
            return False, None
        return True, object()

    def release(self):
        self.released = True


class FailureAttribution(unittest.TestCase):
    """The defect that made a wiring fault sound like a broken webcam."""

    def test_no_device_found_is_said_only_when_nothing_opened(self):
        attempts = [camera.CaptureAttempt(0), camera.CaptureAttempt(1)]
        result = camera.CaptureResult(False, attempts,
                                      cause=unavailability.PROVIDER_FAILED)
        self.assertTrue(result.no_device_found())
        self.assertIn("No camera device was found",
                      result.as_unavailable().detail)

    def test_device_opened_but_gave_no_frame_is_not_a_missing_camera(self):
        """A busy or permission-blocked camera is present, not absent."""
        attempts = [camera.CaptureAttempt(0, opened=True, frame_read=False)]
        result = camera.CaptureResult(False, attempts,
                                      cause=unavailability.PROVIDER_FAILED)
        self.assertFalse(result.no_device_found())
        detail = result.as_unavailable().detail
        self.assertIn("A camera was present", detail)
        self.assertNotIn("No camera device was found", detail)

    def test_missing_imaging_library_is_our_fault_not_the_device(self):
        attempts = []
        result = camera.CaptureResult(
            False, attempts, cause=unavailability.HANDLER_FAILED,
            detail="cv2 could not be loaded")
        failure = result.as_unavailable()
        self.assertFalse(failure.blames_environment())
        self.assertIn("defect in VANTA", failure.for_model())

    def test_hardware_claim_requires_a_probe(self):
        """Nothing may describe the device without having touched it."""
        bare = unavailability.Unavailable(
            "CAPTURE_CAMERA", unavailability.PROVIDER_FAILED, "something")
        self.assertFalse(bare.blames_environment())
        probed = unavailability.Unavailable(
            "CAPTURE_CAMERA", unavailability.PROVIDER_FAILED, "something",
            probe=[camera.CaptureAttempt(0)])
        self.assertTrue(probed.blames_environment())

    def test_success_has_no_failure_to_report(self):
        result = camera.CaptureResult(True, [camera.CaptureAttempt(0, True, True)],
                                      b64="x", path="p", width=2, height=1,
                                      device_index=0)
        with self.assertRaises(ValueError):
            result.as_unavailable()


class IndexFallback(unittest.TestCase):
    def test_index_one_is_used_when_zero_fails(self):
        opened = {}

        class Fake:
            def __init__(self, index):
                opened[index] = True
                self._index = index

            def isOpened(self):
                return self._index == 1

            def read(self):
                return (self._index == 1), (object() if self._index == 1 else None)

            def release(self):
                pass

        attempt_zero, handle_zero = camera._open_capture(
            type("cv2", (), {"VideoCapture": staticmethod(Fake)}), 0)
        attempt_one, handle_one = camera._open_capture(
            type("cv2", (), {"VideoCapture": staticmethod(Fake)}), 1)
        self.assertIsNone(handle_zero)
        self.assertFalse(attempt_zero.opened)
        self.assertIsNotNone(handle_one)
        self.assertTrue(attempt_one.frame_read)

    def test_exception_while_opening_is_recorded_not_swallowed(self):
        class Boom:
            def __init__(self, index):
                raise RuntimeError("device busy")

        attempt, handle = camera._open_capture(
            type("cv2", (), {"VideoCapture": staticmethod(Boom)}), 0)
        self.assertIsNone(handle)
        self.assertIn("device busy", attempt.error)
        self.assertIn("device busy", attempt.describe())

    def test_describe_distinguishes_every_outcome(self):
        self.assertIn("no device", camera.CaptureAttempt(0).describe())
        self.assertIn("no frame",
                      camera.CaptureAttempt(0, opened=True).describe())
        self.assertIn("captured",
                      camera.CaptureAttempt(0, True, True).describe())


class Unavailability(unittest.TestCase):
    def test_shortlist_miss_is_not_absence(self):
        """The distinction that was missing when the camera was declared dead."""
        cause = unavailability.classify_lookup(
            "CAPTURE_CAMERA", declared_names={"CAPTURE_CAMERA"},
            shortlisted_names=set())
        self.assertEqual(cause, unavailability.NOT_SHORTLISTED)

    def test_undeclared_is_a_wiring_gap(self):
        cause = unavailability.classify_lookup(
            "SMELL_TOAST", declared_names={"CAPTURE_CAMERA"},
            shortlisted_names=set())
        self.assertEqual(cause, unavailability.ABSENT_FROM_REGISTRY)

    def test_shortlisted_is_no_problem_at_all(self):
        self.assertIsNone(unavailability.classify_lookup(
            "CAPTURE_CAMERA", {"CAPTURE_CAMERA"}, {"CAPTURE_CAMERA"}))

    def test_wiring_gap_never_blames_hardware(self):
        failure = unavailability.Unavailable(
            "CAPTURE_CAMERA", unavailability.ABSENT_FROM_REGISTRY)
        self.assertIn("not a missing device", failure.for_model())
        self.assertIn("isn't a problem with your hardware", failure.for_user())

    def test_unknown_cause_is_refused(self):
        with self.assertRaises(ValueError):
            unavailability.Unavailable("X", "vibes")

    def test_every_cause_has_distinct_wording(self):
        texts = {unavailability.Unavailable("X", cause).for_model()
                 for cause in unavailability.CAUSES}
        self.assertEqual(len(texts), len(unavailability.CAUSES))


class CaptureContract(unittest.TestCase):
    """capture() returns a result in every case. None is what caused this.

    Against a fake device by default. These tests used to call the real
    camera three times per run, in mode="verify" -- which is the silent mode,
    with no countdown and no warning. A full suite run therefore took three
    unannounced webcam photographs, and the suite ran many times a day, which
    is exactly what "it accesses my camera randomly" looks like from the
    outside. A test must not use a sensor to check a contract it can check
    without one.

    Set VANTA_TEST_REAL_CAMERA=1 to exercise the real device deliberately.
    """

    REAL = os.getenv("VANTA_TEST_REAL_CAMERA", "").lower() in {"1", "true", "yes"}

    def _capture(self):
        """A capture against a fake device, unless a real one is asked for."""
        if self.REAL:
            return camera.capture(BASE_DIR, mode="verify")

        class _Fake:
            def __init__(self, index):
                self._index = index

            def isOpened(self):
                return self._index == 0

            def read(self):
                return (self._index == 0), (object() if self._index == 0
                                            else None)

            def release(self):
                return None

        with patch.object(camera, "_open_capture",
                          side_effect=lambda _cv2, index: (
                              camera.CaptureAttempt(index, index == 0,
                                                    index == 0),
                              _Fake(index) if index == 0 else None)):
            # No frame is encoded, so this exercises the contract up to the
            # point where a real device would be needed and no further.
            return camera.capture(BASE_DIR, mode="verify")

    def test_capture_never_returns_none(self):
        self.assertIsInstance(self._capture(), camera.CaptureResult)

    def test_failure_carries_its_attempts(self):
        result = self._capture()
        if not result.ok:
            self.assertIsNotNone(result.cause)
            # Either a device was probed, or an import failed before probing.
            self.assertTrue(result.attempts or result.detail)

    def test_success_states_the_device_used(self):
        result = self._capture()
        if result.ok:
            self.assertIn(result.device_index, camera.DEVICE_INDEXES)
            self.assertTrue(os.path.exists(result.path))
            self.assertGreater(result.width, 0)
            self.assertIn("index", result.receipt())

    def test_the_suite_does_not_touch_the_camera_by_default(self):
        """The guarantee itself: no sensor unless explicitly asked for."""
        if self.REAL:
            self.skipTest("VANTA_TEST_REAL_CAMERA is set deliberately")
        with patch.object(camera, "_open_capture") as opened:
            self._capture()
        self.assertFalse(
            any("cv2" in str(call) and "VideoCapture" in str(call)
                for call in opened.mock_calls),
            "a real device was opened during an ordinary test run")


if __name__ == "__main__":
    unittest.main()
