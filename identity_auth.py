# -*- coding: utf-8 -*-
"""
identity_auth.py -- a typed password as a second proof that it is really him.

Saying "I am Himanshu" proves nothing: anyone can say it. A face that matches
his enrolled face is strong evidence; when the camera cannot give it (a dark
room, a first enrolment on a new model, a face that has changed) the password in
.env (AUTH_PASSWORD) is the other way to prove it.

THE SECRET NEVER ENTERS THE TEXT PIPELINE
-----------------------------------------
A password typed into the ordinary input would be logged, shown in the chat,
saved to conversation memory and sent to the language model. So it does not use
that path. A challenge makes the interface show a masked box and send what is
typed back as its own kind of message (`auth_secret`), which lands here and goes
no further: it is compared in constant time and dropped. Nothing in this module
prints, logs or stores it, or even its length.

A wrong answer re-prompts; three wrong answers lock every challenge for five
minutes, so a stranger cannot sit and guess. A challenge expires after a minute.
"""

import hmac
import os
import secrets
import threading
import time

TTL_SECONDS = 60
MAX_TRIES = 3
LOCKOUT_SECONDS = 300

_lock = threading.RLock()
_pending = None
_locked_until = 0.0


def _expected():
    return os.getenv("AUTH_PASSWORD", "")


def configured():
    return bool(_expected())


def locked_for():
    """Seconds of lockout remaining (0 when not locked)."""
    with _lock:
        return max(0.0, _locked_until - time.time())


def _matches(secret):
    expected = _expected()
    if not expected or secret is None:
        return False
    return hmac.compare_digest(str(secret).encode("utf-8"), expected.encode("utf-8"))


def is_password(text):
    """True when a normal message is, word for word, the password.

    Typed or spoken into the chat by mistake. It must not reach the model or
    the history; the caller drops it and says so.
    """
    expected = _expected()
    if len(expected) < 4:
        return False
    plain = " ".join(str(text or "").split()).strip(" .,!?;:\"'").casefold()
    return plain == " ".join(expected.split()).strip(" .,!?;:\"'").casefold()


def verify_direct(secret):
    """Constant-time, rate-limited check for a trusted out-of-band channel."""
    global _locked_until
    with _lock:
        if _locked_until > time.time():
            return "locked"
        if _matches(secret):
            verify_direct._tries = 0
            return "ok"
        # Remote lifecycle auth deliberately shares the same brute-force
        # lockout as the masked local challenge, without retaining the secret.
        attempts = int(getattr(verify_direct, "_tries", 0)) + 1
        verify_direct._tries = attempts
        if attempts >= MAX_TRIES:
            verify_direct._tries = 0
            _locked_until = time.time() + LOCKOUT_SECONDS
            return "locked"
        return "wrong"


def pending():
    with _lock:
        return dict(_pending) if _pending else None


def request(purpose, on_success, ui_queue, prompt="Password", on_cancel=None):
    """Ask for the password. Returns (started, message).

    `on_success` runs (on its own thread) only after a correct password.
    """
    global _pending
    if not configured():
        return False, "No password is set. Add AUTH_PASSWORD to .env."
    left = locked_for()
    if left:
        return False, (f"Too many wrong tries. Locked for another "
                       f"{int(left // 60) + 1} minute(s).")
    token = secrets.token_urlsafe(8)
    with _lock:
        _pending = {"id": token, "purpose": purpose, "on_success": on_success,
                    "on_cancel": on_cancel, "expires": time.time() + TTL_SECONDS,
                    "tries": 0, "prompt": prompt}
    ui_queue.put(("auth_prompt", {"id": token, "prompt": prompt}))
    timer = threading.Timer(TTL_SECONDS + 1, _expire, args=(token, ui_queue))
    timer.daemon = True
    timer.start()
    return True, "Type the password in the box on screen."


def _expire(token, ui_queue):
    global _pending
    with _lock:
        if not _pending or _pending["id"] != token:
            return
        _pending = None
    ui_queue.put(("auth_close", token))


def cancel(token=None, ui_queue=None):
    global _pending
    with _lock:
        if not _pending or (token and _pending["id"] != token):
            return False
        callback, _pending = _pending.get("on_cancel"), None
    if callable(callback):
        try:
            callback()
        except Exception:
            pass
    return True


def submit(token, secret, ui_queue):
    """Check what was typed. Returns one of: ok, wrong, locked, expired, none."""
    global _pending, _locked_until
    callback = None
    with _lock:
        if not _pending or _pending["id"] != token:
            return "none"
        if time.time() > _pending["expires"]:
            _pending = None
            return "expired"
        left = _locked_until - time.time()
        if left > 0:
            _pending = None
            return "locked"
        if _matches(secret):
            callback, _pending = _pending["on_success"], None
            outcome = "ok"
        else:
            _pending["tries"] += 1
            if _pending["tries"] >= MAX_TRIES:
                _pending = None
                _locked_until = time.time() + LOCKOUT_SECONDS
                outcome = "locked"
            else:
                outcome = "wrong"
                prompt = _pending["prompt"]
                # Same challenge, asked again.
                ui_queue.put(("auth_prompt", {"id": token, "prompt": "Wrong password. Try again"}))
    if outcome == "ok" and callable(callback):
        threading.Thread(target=callback, daemon=True, name="vanta-auth-success").start()
    return outcome


def reset():
    """For tests."""
    global _pending, _locked_until
    with _lock:
        _pending, _locked_until = None, 0.0
        verify_direct._tries = 0
