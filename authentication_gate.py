"""Lifecycle authentication gate for VANTA.

Startup and returns from sleep/standby are closed states.  One dedicated
worker owns the ephemeral camera captures until the enrolled primary face is
confirmed twice, or the paired Telegram chat supplies AUTH_PASSWORD.  No
captured frame is retained and the password never enters the agent queue.
"""
from __future__ import annotations

import os
import threading
import time

_lock = threading.RLock()
_unlocked = threading.Event()
_abort = threading.Event()
_running = False
_configured = False
_base_dir = ""
_ui_queue = None
_reason = ""
_telegram_notified = False
_boot_only = False
_allow_remote = True


def configured():
    with _lock:
        return _configured


def is_unlocked():
    return _unlocked.is_set()


def awaiting():
    return configured() and not is_unlocked() and not _abort.is_set()


def wait(timeout=None):
    """Wait for a lifecycle decision without ever interpreting user input."""
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    while awaiting():
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(.05)
    return is_unlocked()


def status():
    if _abort.is_set():
        return "aborted"
    return "unlocked" if is_unlocked() else "locked"


def _publish(kind, payload=None):
    queue = _ui_queue
    if queue is not None:
        queue.put((kind, payload if payload is not None else ""))


def configure(base_dir, ui_queue, *, boot_only=False, allow_remote=True):
    global _configured, _base_dir, _ui_queue, _boot_only, _allow_remote
    with _lock:
        _base_dir = os.path.abspath(base_dir)
        _ui_queue = ui_queue
        _boot_only = bool(boot_only)
        _allow_remote = bool(allow_remote)
        _configured = True


def require(reason="identity verification"):
    """Close the execution gate and present the existing lifecycle challenge."""
    global _reason, _telegram_notified
    with _lock:
        _reason = str(reason or "identity verification")[:100]
        _telegram_notified = False
        _abort.clear()
        _unlocked.clear()
    _publish("identity_gate_show", {"reason": _reason})
    return True


def unlock(method="face"):
    if _abort.is_set():
        return False
    _unlocked.set()
    _publish("identity_gate_hide", {"method": str(method)})
    # Preflight deliberately runs before Neo4j, workers and the full camera
    # awareness stack. The main process updates presence after it consumes the
    # signed one-use ticket.
    if not _boot_only:
        try:
            import camera_awareness
            camera_awareness.accept_primary_verification()
        except Exception:
            pass
    print(f"[Identity Gate] Unlocked via {method}.")
    return True


def abort():
    """Stop VANTA entirely; process exit releases every CPU/GPU model."""
    _abort.set()
    _unlocked.clear()
    _publish("identity_gate_status", {
        "message": "Closing VANTA and releasing its models.", "percent": 0})
    try:
        import face_identity
        face_identity.unload()
    except Exception:
        pass
    # Abort means no assistant process, not merely a closed dialog. Resolve
    # the already-running module without importing agent a second time.
    try:
        import sys
        core = sys.modules.get("agent") or sys.modules.get("__main__")
        shutdown = getattr(core, "_graceful_shutdown", None)
        if callable(shutdown):
            threading.Thread(
                target=shutdown, args=("from the identity gate",),
                daemon=True, name="vanta-auth-abort").start()
    except Exception:
        pass
    return True


def aborted():
    return _abort.is_set()


def _telegram_fallback():
    global _telegram_notified
    with _lock:
        if _telegram_notified:
            return
        _telegram_notified = True
    try:
        from providers.telegram_bot import send_telegram_message
        if os.getenv("AUTH_PASSWORD", ""):
            text = ("VANTA is waiting at the computer but could not confirm "
                    "your face. Reply here with your VANTA authentication "
                    "password to unlock it.")
        else:
            text = ("VANTA could not confirm your face. Remote unlock is "
                    "unavailable because AUTH_PASSWORD is not configured.")
        send_telegram_message(text)
    except Exception as exc:
        print(f"[Identity Gate] Telegram notice unavailable: {type(exc).__name__}.")


def handle_telegram(text, chat_id, owner_chat_id):
    """Consume remote input while locked; never expose the supplied secret."""
    if not awaiting():
        return False
    # A previously paired chat is part of the authentication channel. A new
    # chat cannot claim ownership merely by knowing the bot username.
    if owner_chat_id is None or int(chat_id) != int(owner_chat_id):
        return True
    import identity_auth
    outcome = identity_auth.verify_direct(text)
    try:
        from providers.telegram_bot import send_telegram_message
        if outcome == "ok":
            unlock("paired Telegram password")
            send_telegram_message("VANTA unlocked. The computer is active.")
        elif outcome == "locked":
            send_telegram_message(
                "Too many incorrect attempts. Remote unlock is temporarily locked.")
        else:
            send_telegram_message("That password did not match.")
    except Exception:
        pass
    return True


def _one_attempt():
    """Compatibility path for one-shot callers and focused unit tests."""
    import camera
    import face_identity
    shot = camera.capture(_base_dir, mode="authentication",
                          reason="lifecycle identity gate", retain=False)
    if not shot.ok:
        _publish("identity_gate_status", {
            "message": "Camera unavailable", "percent": 0})
        return False
    try:
        result = face_identity.verify_primary(shot.path)
        _publish("identity_gate_frame", {
            "image": shot.b64,
            "percent": int(round(max(0.0, min(1.0, result["similarity"])) * 100)),
            "matched": bool(result["matched"]),
            "face_found": bool(result["face_found"]),
            "message": ("Himanshu recognized" if result["matched"] else
                        "Align your face" if result["face_found"] else
                        "Move into the circle"),
        })
        return bool(result["matched"])
    finally:
        try:
            os.remove(shot.path)
        except OSError:
            pass


def _frame_payload(frame, result):
    """Create one ephemeral UI frame; no camera image is written to disk."""
    import camera
    result = result or {}
    matched = bool(result.get("matched"))
    found = bool(result.get("face_found"))
    return {
        "image": camera.encode_preview(frame),
        "box": tuple(result.get("box") or ()),
        "percent": int(round(max(
            0.0, min(1.0, float(result.get("similarity") or 0.0))) * 100)),
        "matched": matched,
        "face_found": found,
        "message": ("Himanshu recognized" if matched else
                    "Hold still" if found else "Center your face"),
    }


def _live_session(began):
    """Stream preview frames while recognition runs independently.

    InsightFace can take substantially longer than a webcam frame.  Running it
    inline made the preview freeze after each photograph.  A single background
    inference worker always receives a copy of the newest available frame;
    camera reads and UI updates continue at their own bounded rate.
    """
    from concurrent.futures import ThreadPoolExecutor
    import camera
    import face_identity

    preview_fps = max(2.0, min(15.0, float(os.getenv(
        "VANTA_AUTH_PREVIEW_FPS", "8"))))
    preview_interval = 1.0 / preview_fps
    verify_interval = max(.10, float(os.getenv(
        "VANTA_AUTH_VERIFY_INTERVAL", ".20")))
    latest = {
        "face_found": False, "matched": False, "similarity": 0.0,
        "box": (),
    }
    matches = 0
    last_preview = 0.0
    last_verify = 0.0
    read_failures = 0
    future = None
    executor = ThreadPoolExecutor(max_workers=1,
                                  thread_name_prefix="vanta-face-gate")

    try:
        with camera.live_capture() as stream:
            if not stream.ok:
                _publish("identity_gate_status", {
                    "message": "Camera unavailable", "percent": 0})
                return False

            while awaiting():
                now = time.monotonic()
                if future is not None and future.done():
                    try:
                        latest = future.result()
                        matches = matches + 1 if latest.get("matched") else 0
                        if matches >= 2:
                            unlock("local face")
                            break
                    except Exception as exc:
                        matches = 0
                        latest = {
                            "face_found": False, "matched": False,
                            "similarity": 0.0, "box": (),
                        }
                        _publish("identity_gate_status", {
                            "message": "Face verification unavailable",
                            "percent": 0})
                        print(f"[Identity Gate] Face attempt failed: "
                              f"{type(exc).__name__}.")
                    future = None

                frame = stream.read()
                if frame is None:
                    read_failures += 1
                    if read_failures >= 20:
                        _publish("identity_gate_status", {
                            "message": "Camera stream interrupted",
                            "percent": int(round(float(
                                latest.get("similarity") or 0.0) * 100))})
                        return False
                    _abort.wait(.03)
                    continue
                read_failures = 0

                if now - last_preview >= preview_interval:
                    _publish("identity_gate_frame",
                             _frame_payload(frame, latest))
                    last_preview = now

                if (future is None
                        and now - last_verify >= verify_interval):
                    verifier = (face_identity.verify_primary_image_boot
                                if _boot_only
                                else face_identity.verify_primary_image)
                    future = executor.submit(verifier, frame.copy())
                    last_verify = now

                if (_allow_remote and time.monotonic() - began >= float(os.getenv(
                        "VANTA_AUTH_TELEGRAM_AFTER_SECONDS", "10"))):
                    _telegram_fallback()
                _abort.wait(.01)
    finally:
        if future is not None:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return is_unlocked()


def run(reason="startup"):
    """Block the caller until authenticated or explicitly aborted."""
    global _running
    require(reason)
    with _lock:
        if _running:
            runner = False
        else:
            _running = True
            runner = True
    if not runner:
        while awaiting() and not aborted():
            time.sleep(.1)
        return is_unlocked()
    began = time.monotonic()
    try:
        while awaiting():
            try:
                if _live_session(began):
                    break
            except Exception as exc:
                _publish("identity_gate_status", {
                    "message": "Face verification unavailable", "percent": 0})
                print(f"[Identity Gate] Face attempt failed: {type(exc).__name__}.")
            if (_allow_remote and time.monotonic() - began >= float(os.getenv(
                    "VANTA_AUTH_TELEGRAM_AFTER_SECONDS", "10"))):
                _telegram_fallback()
            # A disconnected or sleeping camera is retried without spinning.
            _abort.wait(1.0)
    finally:
        with _lock:
            _running = False
    return is_unlocked()


def require_async(reason):
    """Close immediately, then verify on the one gate worker."""
    require(reason)
    threading.Thread(target=run, args=(reason,), daemon=True,
                     name="vanta-identity-gate").start()
    return True


def reset_for_tests(unlocked=False):
    global _running, _configured, _reason, _telegram_notified
    global _boot_only, _allow_remote
    with _lock:
        _running = False
        _configured = False
        _reason = ""
        _telegram_notified = False
        _boot_only = False
        _allow_remote = True
        _abort.clear()
        if unlocked:
            _unlocked.set()
        else:
            _unlocked.clear()
