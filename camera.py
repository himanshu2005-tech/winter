# -*- coding: utf-8 -*-
"""
camera.py -- capturing a frame, and knowing why when it does not.

vision.take_webcam_b64 returns a base64 string or None. The None is the
problem: it is returned when index 0 and index 1 both refuse to open, when a
device opens but hands back no frame, and when anything at all raises. Three
different situations, one indistinguishable answer.

The agent loop then turned that None into a fixed sentence:

    "[Error] Could not capture webcam. No camera detected."

Only the first of the three causes is "no camera detected". The others are a
busy device, a driver fault, a revoked privacy permission, or a missing cv2 --
none of which are the user's camera being absent, and all of which were
reported to the model as exactly that. The model then repeated it as fact. That
is how a software fault became a confident claim about someone's hardware.

So this module probes, records what each index actually did, and returns that
record whether it succeeded or not. "No camera detected" is now something only
said when every index was tried and none opened -- a finding, not a default.
"""

import base64
from collections import deque
import os
import tempfile
import threading
import time

import unavailability

#: Device indexes to try, in order. 0 is the built-in camera on this machine;
#: 1 is where an external one lands when plugged in. Both are tried because
#: either can be the working device depending on what is connected.
DEVICE_INDEXES = (0, 1)

#: Frames discarded before the one that is kept. The first frames off a
#: just-opened camera are routinely black or badly exposed while auto-exposure
#: settles, and a black frame analysed as content is worse than a slow capture.
WARMUP_FRAMES = 5
WARMUP_SECONDS = 0.5

#: A dark room is mostly sensor noise: at a mean brightness of 8 out of 255
#: a single frame is more grain than face. Averaging N frames cuts the noise by
#: roughly the square root of N, which is what lets a face be recognised at
#: night instead of being a smear. Only paid for when the first frame is dark.
DARK_LUMA = 45.0
BURST_FRAMES = 10
#: Extra settling for a dark scene: auto-exposure walks up in steps and needs
#: longer than it does in daylight.
DARK_EXTRA_WARMUP_SECONDS = 1.0

JPEG_QUALITY = 90
_capture_lock = threading.RLock()
_frame_hub_lock = threading.RLock()
_frame_hub = None


class CaptureAttempt:
    """What one device index actually did."""

    __slots__ = ("index", "opened", "frame_read", "error")

    def __init__(self, index, opened=False, frame_read=False, error=""):
        self.index = index
        self.opened = opened
        self.frame_read = frame_read
        self.error = str(error or "")

    def describe(self):
        if self.error:
            return f"index {self.index}: {self.error}"
        if not self.opened:
            return f"index {self.index}: no device"
        if not self.frame_read:
            return f"index {self.index}: opened but returned no frame"
        return f"index {self.index}: captured"

    def __repr__(self):
        return f"CaptureAttempt({self.describe()})"


class CaptureResult:
    """A capture, successful or not, with the evidence either way.

    This is the receipt the capability declares: the path, the dimensions and
    the device index that actually produced the frame. A caller cannot claim a
    photo was taken without one, and cannot describe the cause of a failure
    except from `attempts`.
    """

    __slots__ = ("ok", "b64", "path", "width", "height", "device_index",
                 "attempts", "cause", "detail")

    def __init__(self, ok, attempts, b64=None, path=None, width=0, height=0,
                 device_index=None, cause=None, detail=""):
        self.ok = bool(ok)
        self.attempts = tuple(attempts)
        self.b64 = b64
        self.path = path
        self.width = int(width or 0)
        self.height = int(height or 0)
        self.device_index = device_index
        self.cause = cause
        self.detail = str(detail or "")

    # -------------------------------------------------------------- #

    def no_device_found(self):
        """True only when every index was tried and none opened.

        The single condition under which saying "no camera" is honest.
        """
        return bool(self.attempts) and not any(a.opened for a in self.attempts)

    def receipt(self):
        """Evidence of success, in the terms the declaration promised."""
        if not self.ok:
            return ""
        return (f"{self.path} ({self.width}x{self.height}) "
                f"from camera index {self.device_index}")

    def probe_summary(self):
        """What was tried. The basis for any statement about the device."""
        return "; ".join(attempt.describe() for attempt in self.attempts)

    def as_unavailable(self, capability="CAPTURE_CAMERA"):
        """The failure, typed, so the wording follows the real cause."""
        if self.ok:
            raise ValueError("capture succeeded; there is nothing to report")
        if self.no_device_found():
            detail = (f"No camera device was found. Tried "
                      f"{self.probe_summary()}.")
        elif self.cause == unavailability.HANDLER_FAILED:
            detail = self.detail or self.probe_summary()
        else:
            detail = (f"A camera was present but the capture failed: "
                      f"{self.probe_summary()}."
                      + (f" {self.detail}" if self.detail else ""))
        return unavailability.Unavailable(
            capability,
            self.cause or unavailability.PROVIDER_FAILED,
            detail,
            # The probe is what earns the right to describe the device at all.
            probe=self.attempts,
        )

    def for_model(self):
        """The text handed back when the capture did not produce an image."""
        return self.as_unavailable().for_model()

    def __repr__(self):
        return (f"CaptureResult(ok={self.ok}, index={self.device_index}, "
                f"attempts={[a.describe() for a in self.attempts]})")


def _mean_luma(frame):
    return float(frame.mean()) if frame is not None else 0.0


def _brighten_if_dark(cv2, handle, frame):
    """In a dark scene, wait for exposure and average a burst of frames.

    Returns the original frame untouched in daylight, so the usual capture
    costs nothing extra.
    """
    import numpy as np
    if _mean_luma(frame) >= DARK_LUMA:
        return frame
    # Ask the driver for more light first; not every driver honours these, and
    # none of them failing matters.
    for prop, value in ((getattr(cv2, "CAP_PROP_AUTO_EXPOSURE", None), 0.75),):
        if prop is not None:
            try:
                handle.set(prop, value)
            except Exception:
                pass
    time.sleep(DARK_EXTRA_WARMUP_SECONDS)
    frames = []
    for _ in range(BURST_FRAMES):
        ok, grabbed = handle.read()
        if ok and grabbed is not None:
            frames.append(grabbed.astype("float32"))
    if not frames:
        return frame
    stacked = np.mean(frames, axis=0)
    return lift_dark_stack(cv2, stacked)


def lift_dark_stack(cv2, stacked, target=95.0):
    """Brighten an averaged dark frame BEFORE it is rounded to 8 bits.

    A scene at 6/255 has only a handful of distinct levels per channel;
    rounding it to bytes and brightening afterwards gives bands and blotches,
    and denoising those blurs the face away. Averaging produces fractional
    values, so the lift is applied to those, on brightness only, with the
    colour pulled toward neutral because chroma noise is what a face
    recogniser mistakes for a different person.
    """
    import numpy as np
    mean = max(0.5, float(stacked.mean()))
    if mean >= target:
        return np.clip(stacked, 0, 255).astype("uint8")
    gamma = float(np.log(target / 255.0) / np.log(min(mean, 250.0) / 255.0))
    gamma = min(1.0, max(0.32, gamma))
    lifted = 255.0 * np.power(np.clip(stacked, 0, 255) / 255.0, gamma)
    grey = lifted.mean(axis=2, keepdims=True)
    lifted = grey + (lifted - grey) * 0.45          # desaturate the noise
    out = np.clip(lifted, 0, 255).astype("uint8")
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return cv2.bilateralFilter(out, 5, 25, 5)


def _open_capture(cv2, index):
    """Try one index, reporting exactly how far it got."""
    attempt = CaptureAttempt(index)
    handle = None
    try:
        handle = cv2.VideoCapture(index)
        if not handle.isOpened():
            handle.release()
            return attempt, None
        attempt.opened = True
        # Opening is not evidence of a working device: a camera held by
        # another application, or blocked by the OS privacy setting, opens
        # and then reads nothing.
        if not handle.read()[0]:
            attempt.frame_read = False
            handle.release()
            return attempt, None
        attempt.frame_read = True
        return attempt, handle
    except Exception as exc:
        attempt.error = f"{type(exc).__name__}: {exc}"
        if handle is not None:
            try:
                handle.release()
            except Exception:
                pass
        return attempt, None


class FrameHub:
    """One recoverable webcam stream shared by local perception consumers.

    Windows camera drivers commonly allow only one open handle.  Gesture wake,
    face verification and low-frequency awareness therefore cannot each own a
    VideoCapture without intermittently reporting that the camera vanished.
    The hub retains only a short in-memory frame ring; it never writes video or
    camera frames to disk.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread = None
        self._frame = None
        self._recent = deque(maxlen=BURST_FRAMES)
        self._captured_at = 0.0
        self._device_index = None
        self._handle = None
        self._error = ""
        self._generation = 0

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="vanta-camera-frame-hub")
            self._thread.start()
            return True

    def stop(self):
        self._stop.set()
        handle = None
        with self._condition:
            handle = self._handle
            self._condition.notify_all()
        if handle is not None:
            try:
                handle.release()
            except Exception:
                pass

    def running(self):
        with self._condition:
            return bool(self._thread is not None and self._thread.is_alive()
                        and not self._stop.is_set())

    def status(self):
        with self._condition:
            age = (time.monotonic() - self._captured_at
                   if self._captured_at else None)
            return {
                "running": self.running(),
                "device_index": self._device_index,
                "frame_age": age,
                "generation": self._generation,
                "error": self._error,
            }

    def latest(self, max_age=1.5, wait=0.0, enhance_dark=False):
        deadline = time.monotonic() + max(0.0, float(wait or 0.0))
        with self._condition:
            while True:
                age = (time.monotonic() - self._captured_at
                       if self._captured_at else float("inf"))
                if self._frame is not None and age <= max_age:
                    frame = self._frame.copy()
                    recent = ([item.copy() for item in self._recent]
                              if enhance_dark else [])
                    index = self._device_index
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return None, None
                self._condition.wait(min(remaining, 0.25))

        if enhance_dark and _mean_luma(frame) < DARK_LUMA and len(recent) >= 3:
            try:
                import cv2
                import numpy as np
                frame = lift_dark_stack(
                    cv2, np.mean([f.astype("float32") for f in recent], axis=0))
            except Exception:
                pass
        return frame, index

    def _publish(self, frame, index):
        with self._condition:
            self._frame = frame
            self._recent.append(frame)
            self._captured_at = time.monotonic()
            self._device_index = index
            self._error = ""
            self._condition.notify_all()

    def _mark_down(self, detail):
        with self._condition:
            self._frame = None
            self._recent.clear()
            self._captured_at = 0.0
            self._device_index = None
            self._error = str(detail or "camera unavailable")[:300]
            self._condition.notify_all()

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            handle = None
            attempts = []
            try:
                import cv2
                # Serialize only device acquisition. Once the hub is live all
                # public camera readers subscribe to it rather than reopening
                # the hardware.
                with _capture_lock:
                    for index in DEVICE_INDEXES:
                        attempt, handle = _open_capture(cv2, index)
                        attempts.append(attempt)
                        if handle is not None:
                            break
                if handle is None:
                    self._mark_down("; ".join(a.describe() for a in attempts))
                    self._stop.wait(backoff)
                    backoff = min(30.0, backoff * 1.8)
                    continue

                used_index = attempts[-1].index
                try:
                    handle.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    handle.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    handle.set(cv2.CAP_PROP_FPS, 10)
                except Exception:
                    pass
                with self._condition:
                    self._handle = handle
                    self._generation += 1
                time.sleep(WARMUP_SECONDS)
                for _ in range(WARMUP_FRAMES):
                    if self._stop.is_set():
                        break
                    handle.read()

                backoff = 1.0
                failures = 0
                while not self._stop.is_set():
                    ok, frame = handle.read()
                    if not ok or frame is None:
                        failures += 1
                        if failures >= 8:
                            raise RuntimeError(
                                "camera stream returned eight empty frames")
                        self._stop.wait(0.03)
                        continue
                    failures = 0
                    self._publish(frame, used_index)
            except Exception as exc:
                self._mark_down(f"{type(exc).__name__}: {exc}")
                if not self._stop.is_set():
                    self._stop.wait(backoff)
                    backoff = min(30.0, backoff * 1.8)
            finally:
                if handle is not None:
                    try:
                        handle.release()
                    except Exception:
                        pass
                with self._condition:
                    if self._handle is handle:
                        self._handle = None
        self._mark_down("camera frame hub stopped")


def frame_hub():
    global _frame_hub
    with _frame_hub_lock:
        if _frame_hub is None:
            _frame_hub = FrameHub()
        return _frame_hub


def start_frame_hub():
    hub = frame_hub()
    hub.start()
    return hub


def stop_frame_hub():
    with _frame_hub_lock:
        hub = _frame_hub
    if hub is not None:
        hub.stop()


class SharedLiveCapture:
    """LiveCapture-compatible reader backed by the shared frame hub."""

    def __init__(self):
        self.hub = start_frame_hub()
        self.device_index = None
        self.detail = ""
        self._opened = False

    @property
    def ok(self):
        if not self._opened:
            return False
        status = self.hub.status()
        return status["running"] and status["frame_age"] is not None \
            and status["frame_age"] <= 2.0

    def open(self):
        frame, index = self.hub.latest(max_age=2.0, wait=4.0)
        self._opened = frame is not None
        self.device_index = index
        if not self._opened:
            self.detail = self.hub.status().get("error") or "no live camera frame"
        return self

    def read(self):
        frame, index = self.hub.latest(max_age=2.0, wait=0.8)
        self.device_index = index
        if frame is None:
            self.detail = self.hub.status().get("error") or "camera stream returned no frame"
        return frame

    def close(self):
        self._opened = False

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class LiveCapture:
    """One long-lived camera handle for a transient live preview.

    The ordinary :func:`capture` API intentionally opens, warms and closes the
    camera for a single durable photograph.  Repeating that API for a login
    preview creates a slide show and repeatedly resets auto-exposure.  This
    class holds the same global camera lock for its lifetime, reads frames
    continuously, and never writes or records them.
    """

    def __init__(self):
        self.handle = None
        self.cv2 = None
        self.device_index = None
        self.attempts = []
        self.detail = ""
        self._owns_lock = False

    @property
    def ok(self):
        return self.handle is not None

    def open(self):
        if self.handle is not None:
            return self
        _capture_lock.acquire()
        self._owns_lock = True
        try:
            import cv2
            self.cv2 = cv2
            for index in DEVICE_INDEXES:
                attempt, handle = _open_capture(cv2, index)
                self.attempts.append(attempt)
                if handle is not None:
                    self.handle = handle
                    self.device_index = index
                    break
            if self.handle is None:
                self.close()
                return self

            # Let exposure settle once.  Unlike single-frame capture, the
            # handle stays open after this and subsequent frames arrive at the
            # device's normal cadence.
            time.sleep(WARMUP_SECONDS)
            for _ in range(WARMUP_FRAMES):
                self.handle.read()
            return self
        except Exception as exc:
            self.detail = f"{type(exc).__name__}: {exc}"
            self.close()
            return self

    def read(self):
        if self.handle is None:
            return None
        try:
            ok, frame = self.handle.read()
            if ok and frame is not None:
                return frame
            self.detail = "camera stream returned no frame"
        except Exception as exc:
            self.detail = f"{type(exc).__name__}: {exc}"
        return None

    def close(self):
        if self.handle is not None:
            try:
                self.handle.release()
            except Exception:
                pass
        self.handle = None
        if self._owns_lock:
            self._owns_lock = False
            _capture_lock.release()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def live_capture():
    """Return an unopened ephemeral stream suitable for a ``with`` block.

    Once gesture wake has started the shared hub already owns the device, so
    lifecycle face verification subscribes to that stream.  Before the hub is
    started (notably the first startup gate), preserve the original exclusive
    capture path.
    """
    with _frame_hub_lock:
        hub = _frame_hub
    return SharedLiveCapture() if hub is not None and hub.running() else LiveCapture()


def encode_preview(frame, max_width=480, quality=72):
    """Encode an in-memory BGR frame for the UI without persisting it."""
    import cv2
    if frame is None:
        return ""
    height, width = frame.shape[:2]
    limit = max(160, int(max_width))
    if width > limit:
        scale = limit / float(width)
        frame = cv2.resize(
            frame, (limit, max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA)
    ok, packed = cv2.imencode(
        ".jpg", frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), max(40, min(90, int(quality)))])
    if not ok:
        return ""
    return base64.b64encode(packed.tobytes()).decode("ascii")


def capture(base_dir, mode="look", reason="", retain=True):
    """Take one frame from the first working camera.

    Returns a CaptureResult in every case, including failure. It never returns
    None, because None is what made the old failure path guess.
    """
    # Gesture wake keeps one shared stream alive. Reuse its freshest frame so
    # explicit captures, awareness and the recognizer never contend for a
    # second Windows camera handle.
    with _frame_hub_lock:
        hub = _frame_hub
    if hub is not None and hub.running():
        frame, index = hub.latest(max_age=2.0, wait=2.0, enhance_dark=True)
        if frame is None:
            status = hub.status()
            attempt = CaptureAttempt(index if index is not None else 0)
            attempt.opened = status.get("device_index") is not None
            attempt.error = status.get("error") or "shared camera stream is stale"
            return CaptureResult(False, (attempt,),
                                 cause=unavailability.PROVIDER_FAILED)
        attempt = CaptureAttempt(index, opened=True, frame_read=True)
        return _frame_to_result(
            base_dir, frame, index, (attempt,), mode, reason, retain)

    # Before the hub starts, preserve the original exclusive path.
    with _capture_lock:
        return _capture_locked(base_dir, mode=mode, reason=reason,
                               retain=retain)


def _frame_to_result(base_dir, frame, used_index, attempts, mode, reason,
                     retain):
    """Encode one already-captured BGR frame into the normal camera receipt."""
    try:
        import cv2
        from PIL import Image
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if retain:
            import camera_library
            camera_library.purge_expired(base_dir)
            path = camera_library.new_path(base_dir, "jpg")
        else:
            folder = os.path.join(base_dir, "runtime", "camera_awareness")
            os.makedirs(folder, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="sample-", suffix=".jpg",
                                        dir=folder)
            os.close(fd)
        image.save(path, "JPEG", quality=JPEG_QUALITY)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return CaptureResult(
                False, attempts, cause=unavailability.HANDLER_FAILED,
                detail=f"The frame was captured but {path} was not written.")
        with open(path, "rb") as handle_file:
            encoded = base64.b64encode(handle_file.read()).decode()
        if retain:
            camera_library.record(
                base_dir, path, width=image.width, height=image.height,
                device_index=used_index, mode=mode, reason=reason)
        return CaptureResult(True, attempts, b64=encoded, path=path,
                             width=image.width, height=image.height,
                             device_index=used_index)
    except Exception as exc:
        return CaptureResult(
            False, attempts, cause=unavailability.HANDLER_FAILED,
            detail=(f"A frame was captured from index {used_index} but "
                    f"encoding it failed ({type(exc).__name__}: {exc})."))


def _capture_locked(base_dir, mode="look", reason="", retain=True):
    attempts = []
    try:
        import cv2
        import numpy  # noqa: F401  -- cv2 frames are numpy arrays
        from PIL import Image
    except Exception as exc:
        # A missing or broken imaging dependency is VANTA's own problem, and
        # must never be reported as the camera being absent.
        return CaptureResult(
            False, attempts, cause=unavailability.HANDLER_FAILED,
            detail=(f"The imaging libraries could not be loaded "
                    f"({type(exc).__name__}: {exc}), so no camera was "
                    f"attempted."))

    handle = None
    for index in DEVICE_INDEXES:
        attempt, handle = _open_capture(cv2, index)
        attempts.append(attempt)
        if handle is not None:
            break

    if handle is None:
        return CaptureResult(False, attempts,
                             cause=unavailability.PROVIDER_FAILED)

    used_index = attempts[-1].index
    try:
        time.sleep(WARMUP_SECONDS)
        for _ in range(WARMUP_FRAMES):
            handle.read()
        ok, frame = handle.read()
        if ok and frame is not None:
            frame = _brighten_if_dark(cv2, handle, frame)
    except Exception as exc:
        attempts[-1].error = f"{type(exc).__name__}: {exc}"
        return CaptureResult(False, attempts,
                             cause=unavailability.PROVIDER_FAILED)
    finally:
        try:
            handle.release()
        except Exception:
            pass

    if not ok or frame is None:
        attempts[-1].frame_read = False
        attempts[-1].error = "opened and warmed up but the final read returned no frame"
        return CaptureResult(False, attempts,
                             cause=unavailability.PROVIDER_FAILED)

    return _frame_to_result(
        base_dir, frame, used_index, attempts, mode, reason, retain)
