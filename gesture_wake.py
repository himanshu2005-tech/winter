# -*- coding: utf-8 -*-
"""Local, deliberate hand-gesture control channel for VANTA.

The camera frame never leaves this process and is never written by this
module.  Google's MediaPipe Gesture Recognizer supplies both hand landmarks
and its canned classifiers. Temporal gates turn noisy per-frame predictions
into deliberate edges; holding a pose never repeats a command.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
import urllib.request


MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/latest/gesture_recognizer.task"
)
MODEL_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"


def _env_float(name, default, low, high):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(float(low), min(float(high), value))


def _env_int(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(low), min(int(high), value))


def enabled():
    return os.getenv("VANTA_GESTURE_WAKE", "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def model_path(base_dir):
    return Path(base_dir) / "assets" / "models" / "gesture_recognizer.task"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(base_dir):
    """Return the verified official model, downloading atomically if absent."""
    target = model_path(base_dir)
    if target.is_file() and _sha256(target) == MODEL_SHA256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".task.download")
    try:
        request = urllib.request.Request(
            MODEL_URL, headers={"User-Agent": "VANTA/gesture-wake"})
        with urllib.request.urlopen(request, timeout=45) as response, \
                open(temporary, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if _sha256(temporary) != MODEL_SHA256:
            raise ValueError("downloaded model failed its SHA-256 check")
        os.replace(temporary, target)
        return target
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


class GestureGate:
    """Debounce one named pose into one event per deliberate hold/release."""

    def __init__(self, label, threshold=0.72, confirmations=3, releases=3,
                 cooldown=2.5):
        self.label = str(label)
        self.threshold = float(threshold)
        self.confirmations = max(1, int(confirmations))
        self.releases = max(1, int(releases))
        self.cooldown = max(0.0, float(cooldown))
        self._positive = 0
        self._negative = self.releases
        self._armed = True
        self._last_fire = float("-inf")

    def update(self, label, score, now=None):
        now = time.monotonic() if now is None else float(now)
        matched = (str(label or "") == self.label
                   and float(score or 0.0) >= self.threshold)
        if matched:
            self._negative = 0
            self._positive += 1
            if (self._armed and self._positive >= self.confirmations
                    and now - self._last_fire >= self.cooldown):
                self._armed = False
                self._last_fire = now
                return True
            return False

        self._positive = 0
        self._negative += 1
        if self._negative >= self.releases:
            self._armed = True
        return False


class PointingUpGate(GestureGate):
    """Backward-compatible wake gate used by the original tests/API."""

    def __init__(self, threshold=0.72, confirmations=3, releases=3,
                 cooldown=2.5):
        super().__init__("Pointing_Up", threshold, confirmations, releases,
                         cooldown)


class GestureSetGate:
    """Independent gates for the small, predictable interaction vocabulary."""

    # MediaPipe's bundled model defines these poses. Meanings are resolved by
    # the agent against live state; this module only says what pose was held.
    LABELS = ("Pointing_Up", "Open_Palm", "Closed_Fist", "Thumb_Up",
              "Thumb_Down", "Victory")

    def __init__(self, threshold=.72, confirmations=3, releases=3,
                 cooldown=2.5):
        self.gates = {
            label: GestureGate(
                label, threshold=threshold,
                # Destructive Abort and consent need a longer deliberate hold
                # than wake/listen or opening a panel.
                confirmations=confirmations + (2 if label in {
                    "Closed_Fist", "Thumb_Up", "Thumb_Down"} else 0),
                releases=releases, cooldown=cooldown)
            for label in self.LABELS
        }

    def update(self, label, score, now=None):
        fired = ""
        for name, gate in self.gates.items():
            if gate.update(label, score, now=now):
                fired = name
        return fired


def top_gesture(result):
    """Return (category, score), tolerating empty MediaPipe results."""
    try:
        categories = result.gestures[0]
        category = categories[0]
        return str(category.category_name or ""), float(category.score or 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return "", 0.0


class GestureWakeWorker:
    def __init__(self, base_dir, on_wake, may_listen=None):
        self.base_dir = os.path.abspath(base_dir)
        self.on_wake = on_wake
        self.may_listen = may_listen or (lambda: True)
        self.stop_event = threading.Event()
        self.thread = None
        self.gate = GestureSetGate(
            threshold=_env_float("VANTA_GESTURE_CONFIDENCE", .72, .5, .98),
            confirmations=_env_int("VANTA_GESTURE_CONFIRM_FRAMES", 3, 2, 8),
            releases=_env_int("VANTA_GESTURE_RELEASE_FRAMES", 3, 1, 12),
            cooldown=_env_float("VANTA_GESTURE_COOLDOWN", 2.5, 1.0, 15.0),
        )

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self.run, daemon=True, name="vanta-gesture-wake")
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()

    def run(self):
        if not enabled():
            print("[Gesture Wake] Disabled by VANTA_GESTURE_WAKE.")
            return
        try:
            import cv2
            import mediapipe as mp
            import camera

            path = ensure_model(self.base_dir)
            options = mp.tasks.vision.GestureRecognizerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(path)),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=.55,
                min_hand_presence_confidence=.55,
                min_tracking_confidence=.55,
            )
            recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(
                options)
            hub = camera.start_frame_hub()
        except Exception as exc:
            print(f"[Gesture Wake] Could not start: {type(exc).__name__}: {exc}")
            return

        fps = _env_float("VANTA_GESTURE_FPS", 5.0, 2.0, 12.0)
        interval = 1.0 / fps
        timestamp_ms = 0
        last_camera_warning = 0.0
        last_inference_warning = 0.0
        print("[Gestures] Local controls ready: point, palm, fist, thumbs, victory "
              "(camera frames are not stored).")
        try:
            while not self.stop_event.wait(interval):
                if not self.may_listen():
                    self.gate.update("", 0.0)
                    continue
                frame, _ = hub.latest(max_age=1.5, wait=.35)
                if frame is None:
                    now = time.monotonic()
                    if now - last_camera_warning >= 60:
                        detail = hub.status().get("error") or "no current frame"
                        print(f"[Gesture Wake] Camera unavailable: {detail}")
                        last_camera_warning = now
                    self.gate.update("", 0.0)
                    continue
                # The shared stream normally stays untouched. In a genuinely
                # dark scene, use the hub's short in-memory burst average so a
                # raised hand does not disappear into sensor noise. Nothing is
                # written and the enhancement is paid for only below the dark
                # threshold.
                if camera._mean_luma(frame) < camera.DARK_LUMA:
                    enhanced, _ = hub.latest(
                        max_age=1.5, wait=0.0, enhance_dark=True)
                    if enhanced is not None:
                        frame = enhanced
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb = __import__("numpy").ascontiguousarray(rgb)
                timestamp_ms = max(timestamp_ms + 1,
                                   int(time.monotonic() * 1000))
                try:
                    result = recognizer.recognize_for_video(
                        mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                        timestamp_ms)
                except Exception as exc:
                    # One malformed/driver-transition frame must not disable
                    # the hands-free channel for the rest of the process.
                    now = time.monotonic()
                    if now - last_inference_warning >= 60:
                        print(f"[Gesture Wake] Inference recovered after: "
                              f"{type(exc).__name__}: {exc}")
                        last_inference_warning = now
                    self.gate.update("", 0.0)
                    continue
                label, score = top_gesture(result)
                fired = self.gate.update(label, score)
                if fired:
                    print(f"[Gestures] {fired} ({score:.0%}).")
                    try:
                        self.on_wake({"gesture": fired, "confidence": score})
                    except Exception as exc:
                        print(f"[Gesture Wake] Activation failed: "
                              f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"[Gesture Wake] Worker stopped: {type(exc).__name__}: {exc}")
        finally:
            try:
                recognizer.close()
            except Exception:
                pass


_worker = None
_worker_lock = threading.RLock()


def start(base_dir, on_wake, may_listen=None):
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.thread is not None \
                and _worker.thread.is_alive():
            return False
        _worker = GestureWakeWorker(base_dir, on_wake, may_listen)
        return _worker.start()


def stop():
    with _worker_lock:
        worker = _worker
    if worker is not None:
        worker.stop()
