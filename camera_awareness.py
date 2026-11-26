"""Privacy-bounded camera awareness for presence, study and standby security.

This is deliberately a sampler, not a recorder. Frames are captured at a low
frequency, analysed locally, deleted immediately, and only structured state is
kept. Explicit CAPTURE_CAMERA requests continue to use the normal retained
camera library.
"""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import asdict, dataclass
from io import BytesIO
import json
import os
import random
import re
import threading
import time


@dataclass
class AwarenessState:
    observed_at: float = 0.0
    present: bool | None = None
    attentive: bool | None = None
    face_count: int = 0
    person_count: int = 0
    recognized_people: tuple[str, ...] = ()
    himanshu_present: bool | None = None
    unknown_face_count: int = 0
    distance: str = "unknown"
    posture: str = "unknown"
    study_present_seconds: float = 0.0
    study_breaks: int = 0
    model_assessment: str = ""
    last_error: str = ""
    object_labels: tuple[str, ...] = ()
    scene_diff: tuple[str, ...] = ()
    last_event: str = ""


@dataclass(frozen=True)
class CascadeOutcome:
    """What one background sample did, without retaining its pixels."""
    gate: str
    state: AwarenessState
    events: tuple[str, ...] = ()
    diff_score: float = 0.0
    stage1_ran: bool = False
    face_scan_ran: bool = False


@dataclass
class _IdentityCache:
    expires_at: float = 0.0
    face_count: int = 0
    recognized_people: tuple[str, ...] = ()
    himanshu_present: bool | None = None
    unknown_face_count: int = 0


_state = AwarenessState()
_lock = threading.RLock()
_started = False
_stop = threading.Event()
_base_dir = ""
_present_since = 0.0
_last_study_sample = 0.0
_last_present_sample = 0.0
_monitor_generation = -1
_monitor_armed_at = 0.0
_monitor_hits = 0
_monitor_alerted = False
_last_reasoning_at = 0.0
_last_model_brief = ""
_input_queue = None
_last_frame_fingerprint: bytes | None = None
_identity_cache = _IdentityCache()
_verification_required = False
_verification_requested = threading.Event()
_verification_reason = ""

def _reset_visual_context():
    """Re-arm scene stability when an operating context ends."""
    global _last_study_sample, _present_since
    with _lock:
        _last_study_sample = 0.0
        _present_since = 0.0
    try:
        import visual_director
        visual_director.director.reset_scene()
    except Exception:
        pass


def _env_float(name, default, low, high):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(low, min(high, value))


def snapshot():
    with _lock:
        return AwarenessState(**asdict(_state))


def user_is_present(max_age=90.0):
    """Return True/False only for a fresh observation; otherwise None."""
    state = snapshot()
    if not state.observed_at or time.time() - state.observed_at > max_age:
        return None
    return state.present


# --------------------------------------------------------------------------- #
# A verified session
# --------------------------------------------------------------------------- #
# One camera frame in a dim room often cannot match a face even when it is his
# (a small, low-light, partly turned face), and every turn that fell on such a
# frame locked personal things away and asked him to prove himself again. A
# positive match now opens a session that lasts a few minutes and is renewed by
# every later match, so a single poor frame does not undo it. It ends at once on
# evidence that the person at the desk may have changed: the chair is seen
# empty, or only faces that are not his are visible.
_verified_at = 0.0


def trust_minutes():
    return _env_float("VANTA_IDENTITY_TRUST_MINUTES", 10, 1, 120)


def reset_trust():
    global _verified_at, _verification_required, _verification_reason
    with _lock:
        _verified_at = 0.0
        _verification_required = False
        _verification_reason = ""
        _verification_requested.clear()


def request_verification(reason="requested", immediate=True):
    """Require one fresh owner check without retaining a camera frame.

    The request is consumed by the single awareness worker, so lifecycle
    events cannot create competing webcam streams.  Requiring verification
    immediately makes the existing soul/idle mark guarded until a positive
    match arrives; presentation remains entirely owned by ``ui.py``.
    """
    global _verified_at, _verification_required, _verification_reason
    with _lock:
        _verified_at = 0.0
        _verification_required = True
        _verification_reason = str(reason or "requested")[:120]
        _clear_identity_cache()
    if immediate:
        _verification_requested.set()
    return True


def verification_required():
    with _lock:
        return bool(_verification_required)


def _note_identity(now=None):
    """Open, renew or end the verified session from the state just recorded."""
    global _verified_at, _verification_required
    now = now or time.time()
    with _lock:
        if _state.himanshu_present is True:
            _verified_at = now
            _verification_required = False
        elif _state.present is False:
            _verified_at = 0.0           # nobody there: whoever sits down is new
        elif _state.face_count and _state.himanshu_present is False:
            _verified_at = 0.0           # faces are visible and none of them his


def identity_trusted(now=None):
    """True while a recent positive match has not been contradicted."""
    now = now or time.time()
    with _lock:
        if _verification_required:
            return False
        if not _verified_at or now - _verified_at > trust_minutes() * 60:
            return False
        # The camera has to still be watching for a revocation to be possible.
        return bool(_state.observed_at and now - _state.observed_at <= 180.0)


def trust_status():
    """verified / guarded / override / unknown -- how the interface should look.

    'verified' is a current match or a live session; 'guarded' is someone at the
    desk who has not been matched; 'unknown' is no usable camera reading.
    """
    try:
        import protocol_manager
        if protocol_manager.identity_bypass_active():
            return "override"
    except Exception:
        pass
    try:
        import authentication_gate
        if authentication_gate.configured():
            return "verified" if authentication_gate.is_unlocked() else "unknown"
    except Exception:
        pass
    state = snapshot()
    if verification_required():
        return "guarded" if state.present is True else "unknown"
    if (state.observed_at and time.time() - state.observed_at <= 90.0
            and state.himanshu_present is True) or identity_trusted():
        return "verified"
    if guest_session_active():
        return "guarded"
    return "unknown"


def primary_user_is_present(max_age=90.0):
    """Fresh local biometric hint for delivery, never action authorization."""
    state = snapshot()
    fresh = bool(state.observed_at and time.time() - state.observed_at <= max_age)
    if verification_required():
        return False if fresh and state.present is True else None
    if fresh and state.himanshu_present is True:
        return True
    if identity_trusted():
        return True
    if fresh and state.present is True and not identity_enrolled():
        return True          # nobody to tell apart from: do not silence him
    return state.himanshu_present if fresh else None


def identity_enrolled():
    """Whether there is an enrolled owner to tell visitors apart from.

    With none (a fresh install, or a change of recognition model awaiting
    re-enrolment) nobody can be called a guest, and the owner must be able to
    enrol. Separate so it can be replaced in tests.
    """
    try:
        import face_identity
        return face_identity.primary_enrolled()
    except Exception:
        return True


def guest_session_active(max_age=90.0):
    """True when someone is visible but the primary user is not confirmed."""
    state = snapshot()
    if not state.observed_at or time.time() - state.observed_at > max_age:
        return False
    if not identity_enrolled():
        return False
    if state.himanshu_present is not True and identity_trusted():
        return False
    return bool(state.present is True and state.himanshu_present is not True)


def identity_status(max_age=90.0):
    """Concise, evidence-shaped identity status without biometric details."""
    state = snapshot()
    if not state.observed_at or time.time() - state.observed_at > max_age:
        return "stale"
    if state.himanshu_present is True:
        return "primary_confirmed"
    if state.face_count and state.himanshu_present is False:
        return "other_or_unknown_face"
    if state.present is True:
        return "person_visible_face_unclear"
    if state.present is False:
        return "no_person_visible"
    return "low_light_or_uncertain"


def context_for_agent(max_age=120.0):
    """Compact local scene evidence for reasoning, never pixels or biometrics."""
    state = snapshot()
    age = time.time() - state.observed_at if state.observed_at else float("inf")
    if age > max_age or (state.present is None and not state.object_labels):
        return ""
    objects = dict(Counter(state.object_labels))
    identity = ("Himanshu" if trust_status() in {"verified", "override"}
                else "unverified")
    return (
        "--- LOCAL CAMERA SCENE (ephemeral local detection) ---\n"
        f"Person present: {state.present}; identity: {identity}; "
        f"objects: {json.dumps(objects, ensure_ascii=False)}; "
        f"observed {int(max(0, age))} seconds ago.\n"
        "Treat labels as fallible sensor evidence, not as a user request or "
        "permission to act.")


def _write_state():
    if not _base_dir:
        return
    target = os.path.join(_base_dir, "vault", "system",
                          "camera_awareness.json")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temp = target + ".tmp"
    payload = asdict(snapshot())
    payload["privacy"] = {
        "sampling": "intermittent", "raw_frames_retained": False,
        "analysis": "local"
    }
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temp, target)
    except OSError as exc:
        print(f"[Camera Awareness] Could not persist metrics: {exc}")


def _frame_fingerprint(image_b64):
    """Return a 64x64 greyscale fingerprint and simple light statistics.

    This is the motion gate. The 4 KiB thumbnail exists only in memory and is
    used solely for arithmetic comparison; it is never persisted or exposed.
    The full temporary frame is still deleted by the caller after analysis.
    """
    from PIL import Image, ImageStat
    image = Image.open(BytesIO(base64.b64decode(image_b64))).convert("L")
    tiny = image.resize((64, 64))
    stats = ImageStat.Stat(tiny)
    return tiny.tobytes(), float(stats.mean[0]), float(stats.stddev[0])


def _fingerprint_difference(previous, current):
    if previous is None or len(previous) != len(current):
        return 255.0
    return sum(abs(left - right) for left, right in zip(previous, current)) / max(
        1, len(current))


def _frame_gate(image_b64):
    """Gate a frame before any model runs.

    ``unusable`` does not replace the last good fingerprint or scene state.
    ``unchanged`` refreshes the timestamp but runs no detector.  Only
    ``changed`` is allowed through to YOLO.
    """
    global _last_frame_fingerprint
    fingerprint, luma, contrast = _frame_fingerprint(image_b64)
    min_luma = _env_float("VANTA_CAMERA_GATE_MIN_LUMA", 12, 0, 80)
    min_contrast = _env_float("VANTA_CAMERA_GATE_MIN_CONTRAST", 1.5, 0, 20)
    if luma < min_luma or (luma < min_luma * 1.75 and contrast < min_contrast):
        return "unusable", 0.0, luma
    difference = _fingerprint_difference(_last_frame_fingerprint, fingerprint)
    threshold = _env_float("VANTA_CAMERA_FRAME_DIFF", 4.5, 0.5, 40)
    if _last_frame_fingerprint is None or difference >= threshold:
        _last_frame_fingerprint = fingerprint
        return "changed", difference, luma
    # Following the requested last-frame comparison avoids repeatedly paying
    # for small camera auto-exposure drift.  No pixels survive this assignment.
    _last_frame_fingerprint = fingerprint
    return "unchanged", difference, luma


def _detect_objects(shot):
    try:
        import vision_pipeline
        confidence = _env_float(
            "VANTA_CAMERA_OBJECT_CONFIDENCE", .28, .10, .80)
        detections, detector_ok = vision_pipeline.detect(
            shot.b64, confidence=confidence)
        return tuple(detections), bool(detector_ok)
    except Exception as exc:
        print(f"[Camera Awareness] Person detector unavailable: {exc}")
        return (), False


def _identity_fields(shot):
    try:
        import face_identity
        matches = tuple(face_identity.identify(shot.path))
    except Exception as exc:
        print(f"[Camera Awareness] Face identity unavailable: {exc}")
        matches = ()
    recognized = tuple(dict.fromkeys(
        match.person for match in matches if match.recognized))
    primary_name = os.getenv("VANTA_PRIMARY_PERSON", "Himanshu").casefold()
    primary_present = (
        any(name.casefold() == primary_name for name in recognized)
        if matches else None)
    unknown_faces = sum(1 for match in matches if not match.recognized)
    return len(matches), recognized, primary_present, unknown_faces


def _clear_identity_cache():
    global _identity_cache
    with _lock:
        _identity_cache = _IdentityCache()


def _cached_identity(now=None):
    now = now or time.time()
    with _lock:
        if not _identity_cache.expires_at or now >= _identity_cache.expires_at:
            return None
        return (_identity_cache.face_count,
                tuple(_identity_cache.recognized_people),
                _identity_cache.himanshu_present,
                _identity_cache.unknown_face_count)


def _remember_identity(fields, now=None):
    """Cache a match longer than an unclear/unknown result."""
    global _identity_cache
    now = now or time.time()
    face_count, recognized, primary_present, unknown_faces = fields
    if primary_present is True or recognized:
        ttl = _env_float("VANTA_IDENTITY_CACHE_MINUTES", 7, 1, 30) * 60
    else:
        ttl = _env_float("VANTA_IDENTITY_RETRY_SECONDS", 30, 8, 120)
    with _lock:
        _identity_cache = _IdentityCache(
            expires_at=now + ttl,
            face_count=int(face_count),
            recognized_people=tuple(recognized or ()),
            himanshu_present=primary_present,
            unknown_face_count=int(unknown_faces),
        )


def _should_scan_identity(person_count, previous, cached, diff_score,
                          detector_ok=True):
    """Stage 2 policy: appearance, expiry, or credible person replacement."""
    if not detector_ok:
        return True              # face detection is the local fallback
    if person_count <= 0:
        return False
    if previous.person_count <= 0 or cached is None:
        return True
    replacement_threshold = _env_float(
        "VANTA_IDENTITY_RECHECK_DIFF", 18, 5, 80)
    return diff_score >= replacement_threshold


def _analyse(shot, study=False, object_result=None, scan_face=True,
             cached_identity=None):
    """Return local scene state after the cascade has selected its stages."""
    detections, detector_ok = (object_result if object_result is not None
                               else _detect_objects(shot))
    persons = [item for item in detections if item.label == "person"]
    if scan_face:
        face_count, recognized, himanshu_present, unknown_faces = (
            _identity_fields(shot))
    elif cached_identity is not None:
        face_count, recognized, himanshu_present, unknown_faces = cached_identity
    else:
        face_count, recognized, himanshu_present, unknown_faces = (0, (), None, 0)

    # Explicit one-shot verification may deliberately run both local models;
    # a clear matched face is presence evidence even if a close crop kept YOLO
    # from finding a full COCO ``person`` box. In the background cascade face
    # scanning is not requested when a healthy YOLO sees no person.
    present = bool(persons or face_count)
    if not present and not detector_ok:
        present = None
    object_labels = tuple(sorted(str(item.label) for item in detections))

    # Explicit one-shot callers still receive an uncertainty marker in a dark
    # frame.  The background cascade normally stops at Gate 0 before here.
    if not present:
        try:
            import vision_pipeline
            if vision_pipeline.image_luminance(shot.b64) < float(
                    os.getenv("VANTA_LOW_LIGHT_LUMA", "58")):
                present = None
        except Exception:
            pass
    # Presence is a sensor fact. Attention, distance and posture are semantic
    # judgements and therefore belong to the vision-model assessment below,
    # not to a growing ladder of coordinate thresholds.
    attentive = None
    distance = "model_assessed" if study and present else "unknown"
    posture = "model_assessed" if study and present else "unknown"
    return (present, attentive, distance, posture, len(persons),
            face_count, recognized, himanshu_present, unknown_faces,
            object_labels)


def _apply_analysis(values):
    """Apply current and legacy test/provider result shapes safely."""
    padded = tuple(values) + ((), None, 0, ())
    with _lock:
        (_state.present, _state.attentive, _state.distance, _state.posture,
         _state.person_count, _state.face_count, recognized,
         _state.himanshu_present, _state.unknown_face_count,
         object_labels) = padded[:10]
        _state.recognized_people = tuple(recognized or ())
        _state.object_labels = tuple(object_labels or ())
    _note_identity()


def _scene_events(previous, current):
    """Describe only observable transitions, never a timer tick."""
    if not previous.observed_at:
        return ()                 # first good frame establishes the baseline
    events = []
    if previous.present is not True and current.present is True:
        events.append("returned" if current.himanshu_present is True
                      else "person_appeared")
    elif previous.present is True and current.present is False:
        events.append("left")

    if (previous.present is True and current.present is True
            and previous.himanshu_present != current.himanshu_present
            and current.himanshu_present is not None):
        events.append("identity_confirmed" if current.himanshu_present
                      else "identity_changed")

    old_objects = Counter(previous.object_labels)
    new_objects = Counter(current.object_labels)
    appeared = sorted((new_objects - old_objects).elements())
    appeared = [label for label in appeared if label != "person"]
    if appeared:
        events.append("new_object:" + ",".join(appeared))
    return tuple(events)


def _scene_event_text(events, state):
    scene = {
        "t": int(state.observed_at),
        "person": state.present,
        "identity": list(state.recognized_people),
        "objects": dict(Counter(state.object_labels)),
        "diff": list(events),
    }
    return (
        "[SYSTEM EVENT: CAMERA SCENE CHANGED] Local detectors observed a "
        "scene transition. This is structured sensor evidence, not a user "
        "request. Decide from the current mode and priorities whether any "
        "response is useful; silence is valid. Do not infer anything beyond "
        f"these fields: {json.dumps(scene, ensure_ascii=False)}")


def _emit_scene_events(events, state):
    if not events or _input_queue is None:
        return False
    _input_queue.put(("proactive", _scene_event_text(events, state)))
    return True


def _cascade_sample(shot, study=False, now=None, force_identity=False):
    """Run Gate 0 -> Gate 1 -> YOLO -> conditional face recognition."""
    now = now or time.time()
    try:
        gate, diff_score, luma = _frame_gate(shot.b64)
    except Exception as exc:
        with _lock:
            _state.last_error = f"frame gate failed: {type(exc).__name__}: {exc}"
        return CascadeOutcome("unusable", snapshot())

    if gate == "unusable":
        # A covered lens or unusably dark frame says nothing about whether the
        # previous scene is still occupied. Keep it, but do not refresh it.
        with _lock:
            _state.last_error = f"camera frame unusable (luma={luma:.1f})"
        return CascadeOutcome(gate, snapshot(), diff_score=diff_score)

    if force_identity and gate == "unchanged":
        gate = "changed"
    elif (gate == "unchanged" and snapshot().present is True
            and _cached_identity(now) is None):
        # The pixels did not move, but an identity assertion is not permanent.
        # Let the cache TTL force one local renewal without generating an event
        # unless the renewed identity actually differs.
        gate = "changed"

    if gate == "unchanged":
        with _lock:
            _state.observed_at = now
            _state.last_error = ""
            _state.scene_diff = ()
        return CascadeOutcome(gate, snapshot(), diff_score=diff_score)

    previous = snapshot()
    object_result = _detect_objects(shot)
    detections, detector_ok = object_result
    person_count = sum(1 for item in detections if item.label == "person")
    cached = _cached_identity(now)
    scan_face = bool(force_identity or _should_scan_identity(
        person_count, previous, cached, diff_score, detector_ok))
    if person_count <= 0 and detector_ok:
        _clear_identity_cache()
        cached = None

    values = _analyse(
        shot, study=study, object_result=object_result,
        scan_face=scan_face, cached_identity=None if scan_face else cached)
    _apply_analysis(values)
    with _lock:
        _state.observed_at = now
        _state.last_error = ""
    state = snapshot()

    if scan_face and state.present is True:
        _remember_identity((
            state.face_count, state.recognized_people,
            state.himanshu_present, state.unknown_face_count), now)
    elif state.present is False:
        _clear_identity_cache()

    events = _scene_events(previous, state)
    with _lock:
        _state.scene_diff = events
        _state.last_event = events[-1] if events else ""
    state = snapshot()
    return CascadeOutcome(
        gate, state, events=events, diff_score=diff_score,
        stage1_ran=True, face_scan_ran=scan_face)


def observe_once(base_dir=None, study=False, capture_fn=None):
    """Take one ephemeral sample. Exposed for deterministic recovery tests."""
    root = base_dir or _base_dir
    if capture_fn is None:
        import camera
        capture_fn = camera.capture
    shot = capture_fn(root, mode="awareness", reason="local awareness sample",
                      retain=False)
    if not shot.ok:
        with _lock:
            _state.last_error = shot.probe_summary() or shot.detail
        return snapshot()
    try:
        values = _analyse(shot, study=study)
        _apply_analysis(values)
        with _lock:
            _state.observed_at = time.time()
            _state.last_error = ""
        return snapshot()
    finally:
        try:
            os.remove(shot.path)
        except OSError:
            pass


def verify_primary_now(base_dir=None, capture_fn=None):
    """Perform a fresh ephemeral local check before withholding a response.

    This is not authentication and grants no action permission. It exists so
    a stale awareness sample can never become the basis for telling Himanshu
    that VANTA failed to recognize him without actually looking again.
    """
    request_verification("explicit verification", immediate=False)
    before = snapshot().observed_at
    state = observe_once(base_dir=base_dir, study=False,
                         capture_fn=capture_fn)
    fresh = bool(state.observed_at and state.observed_at > before)
    status = identity_status(max_age=10.0) if fresh else "capture_failed"
    print(f"[Face Identity] Fresh primary check: {status}.")
    return state, status


def _json_object(text):
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                 flags=re.IGNORECASE)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}


def _model_assess(shot, state, reason="periodic_study_review"):
    """Let the configured vision model interpret a gated scene.

    Local models decide what is present and the visual director decides whether
    a stable scene merits this relatively expensive semantic step.  This
    function deliberately contains no activity- or object-specific behaviour.
    """
    global _last_model_brief
    try:
        import mode_manager
        import visual_director
        mode = str(mode_manager.current_mode().value)
        events = tuple(state.scene_diff or ())
        scene = visual_director.scene_payload(
            state, mode=mode, events=events)
        request = visual_director.ReviewRequest(
            reason=str(reason or "contextual_check"),
            signature=visual_director.scene_signature(scene),
            events=events,
        )
        decision = visual_director.assess(shot.b64, scene, request)
        with _lock:
            _state.model_assessment = decision.summary
        if visual_director.director.allow(decision):
            _last_model_brief = decision.message
            # Study mode normally suppresses proactive chatter. A visual
            # decision that survived the director's strict interruption budget
            # therefore uses the local-alert boundary; other modes can defer a
            # non-urgent observation through their normal proactive queue.
            priority = ("critical" if decision.interrupt_now or mode == "study"
                        else "normal")
            _notify(decision.message, priority=priority,
                    reason=f"visual:{decision.topic_key or reason}")
        return decision
    except Exception as exc:
        print(f"[Camera Awareness] Model interpretation unavailable: "
              f"{type(exc).__name__}: {exc}")
        return {}


def _needs_semantic_review(state, study, monitor, now, last_reasoning_at,
                           interval):
    """Escalate pixels only for an opted-in study interpretation.

    Presence and standby security are object-detection problems and remain
    local.  This small policy boundary is intentionally testable independently
    of the long-running worker.
    """
    return bool(state.present and study and not monitor
                and now - last_reasoning_at >= interval)


def _notify(text, local_if_present=True, priority="normal", reason=""):
    primary_present = primary_user_is_present()
    if local_if_present and primary_present is True:
        # Deliver the model's already-reviewed sentence verbatim. Re-entering
        # it as a proactive prompt would ask a second model to reinterpret the
        # observation and could turn one restrained question into unrelated
        # narration. The local-alert boundary still serializes with active
        # work and enforces quiet/mode policy.
        try:
            target_queue = _input_queue
            if target_queue is None:
                import agent
                target_queue = agent.input_queue
            target_queue.put(("local_alert", {
                "text": text,
                "priority": str(priority).casefold(),
                "source_id": "camera-visual-awareness",
                "reason": reason or "model-assessed visual awareness",
            }))
            return
        except Exception as exc:
            print(f"[Camera Awareness] Local delivery unavailable: {exc}")
    try:
        from providers.telegram_bot import send_telegram_message
        send_telegram_message(text)
    except Exception as exc:
        print(f"[Camera Awareness] Telegram delivery failed: {exc}")


def route_proactive(text):
    """Send a proactive line remotely when a fresh sample says user is away.

    Returns True when this function delivered it, False when normal local
    speech should continue. Unknown/stale presence never suppresses speech.
    """
    try:
        import protocol_manager
        if protocol_manager.local_disclosure_blocked():
            primary_confirmed = False
        elif protocol_manager.identity_bypass_active():
            return False
        else:
            primary_confirmed = primary_user_is_present() is True
    except Exception:
        primary_confirmed = primary_user_is_present() is True
    if primary_confirmed:
        return False
    try:
        from providers.telegram_bot import send_telegram_message
        delivered = bool(send_telegram_message(text))
        if not delivered:
            print("[Camera Awareness] User appears away, but Telegram did not "
                  "accept the proactive message; local speech remains suppressed.")
        # Presence, not network success, decides whether an empty room should
        # be spoken to. The dashboard still retains the item for later review.
        return True
    except Exception as exc:
        print(f"[Camera Awareness] Could not reroute proactive speech: {exc}")
        return True


def _update_study(state, now):
    global _present_since, _last_study_sample
    elapsed = max(0.0, now - _last_study_sample) if _last_study_sample else 0.0
    _last_study_sample = now
    with _lock:
        if state.present:
            _state.study_present_seconds += min(elapsed, 180.0)
            if not _present_since:
                _present_since = now
        else:
            if _present_since:
                _state.study_breaks += 1
            _present_since = 0.0


def _lock_windows():
    import ctypes
    return bool(ctypes.windll.user32.LockWorkStation())


def _monitor_security(state, now):
    global _monitor_generation, _monitor_armed_at, _monitor_hits, _monitor_alerted
    import runtime_power
    generation = runtime_power.generation()
    if generation != _monitor_generation:
        _monitor_generation = generation
        _monitor_armed_at = now
        _monitor_hits = 0
        _monitor_alerted = False
    grace = _env_float("VANTA_STANDBY_MONITOR_GRACE_SECONDS", 15, 5, 120)
    if now - _monitor_armed_at < grace or _monitor_alerted:
        return
    _monitor_hits = _monitor_hits + 1 if state.present else 0
    required = int(_env_float("VANTA_STANDBY_MONITOR_CONFIRMATIONS", 2, 1, 4))
    if _monitor_hits < required:
        return
    _monitor_alerted = True
    _notify("VANTA standby monitor detected a person at your computer. Windows is being locked.",
            local_if_present=False)
    try:
        locked = _lock_windows()
        print(f"[Standby Monitor] Person confirmed; Windows lock requested={int(locked)}.")
    except Exception as exc:
        print(f"[Standby Monitor] Could not lock Windows: {exc}")


def _worker():
    global _last_present_sample, _last_reasoning_at, _verification_reason
    print("[Camera Awareness] Low-frequency local presence sampler started.")
    was_standby = False
    was_sleep = False
    was_study = False
    while not _stop.wait(1.0):
        try:
            import runtime_power
            import mode_manager
            try:
                import authentication_gate
                if authentication_gate.awaiting():
                    continue
            except Exception:
                pass
            monitor = runtime_power.monitor_enabled()
            active_mode = str(mode_manager.current_mode().value)
            study = active_mode == "study"
            standby = runtime_power.is_standby()
            sleeping = str(mode_manager.current_mode().value) == "sleep"
            if was_standby and not standby:
                request_verification("standby ended")
            if was_sleep and not sleeping:
                request_verification("sleep ended")
            if was_study and not study:
                _reset_visual_context()
            was_standby, was_sleep = standby, sleeping
            was_study = study
            now = time.time()
            forced = _verification_requested.is_set()
            interval = (_env_float("VANTA_STANDBY_MONITOR_INTERVAL", 6, 3, 60)
                        if monitor else
                        _env_float("VANTA_STUDY_CAMERA_INTERVAL", 12, 5, 300)
                        if study else
                        _env_float("VANTA_PRESENCE_CAMERA_INTERVAL", 60, 15, 300))
            # Someone is at the desk and has not been matched: look again soon
            # rather than leave personal things locked for a full interval.
            if (not monitor and snapshot().present is True
                    and not identity_trusted()):
                interval = min(interval, _env_float(
                    "VANTA_IDENTITY_RETRY_SECONDS", 15, 8, 120))
            if not forced and now - _last_present_sample < interval:
                continue
            # Ordinary standby means truly inactive: only monitor standby uses
            # the camera. Sleep mode also remains camera-free.
            if standby and not monitor:
                continue
            if sleeping:
                continue
            _last_present_sample = now
            # Keep the transient frame alive only through local/model analysis.
            import camera
            shot = camera.capture(_base_dir, mode="awareness",
                                  reason="local awareness sample", retain=False)
            if not shot.ok:
                with _lock:
                    _state.last_error = shot.probe_summary() or shot.detail
                if forced:
                    reason = _verification_reason
                    _verification_requested.clear()
                    print(f"[Face Identity] {reason}: capture_failed.")
                continue
            try:
                outcome = _cascade_sample(
                    shot, study=study, now=now, force_identity=forced)
                state = outcome.state
                # A lifecycle verification updates trust and the existing
                # alive/dead mark; it is not itself a reason to interrupt.
                if outcome.events and not forced:
                    # Presence transitions are useful to the ordinary agent
                    # (return briefings, away routing). Object changes go only
                    # to the visual director so raw detections do not become
                    # unsolicited narration.
                    lifecycle = tuple(
                        event for event in outcome.events
                        if not event.startswith("new_object:"))
                    _emit_scene_events(lifecycle, state)

                visual_request = None
                if not forced and not monitor and outcome.gate != "unusable":
                    try:
                        import visual_director
                        scene = visual_director.scene_payload(
                            state, mode=active_mode, events=outcome.events)
                        visual_request = visual_director.director.consider(
                            scene, events=outcome.events, now=now)
                    except Exception as exc:
                        print("[Visual Director] Review gate unavailable: "
                              f"{type(exc).__name__}: {exc}")
                if visual_request is not None:
                    _last_reasoning_at = now
                    _model_assess(shot, state, reason=visual_request.reason)
            finally:
                try:
                    os.remove(shot.path)
                except OSError:
                    pass
            if state.observed_at and outcome.gate != "unusable":
                if study:
                    _update_study(state, now)
                if monitor:
                    _monitor_security(state, now)
                _write_state()
            if forced:
                reason = _verification_reason
                _verification_requested.clear()
                print(f"[Face Identity] {reason}: {identity_status(max_age=10)}.")
        except Exception as exc:
            print(f"[Camera Awareness] Sample failed: {type(exc).__name__}: {exc}")
            _stop.wait(10 + random.random() * 5)


def start(base_dir, input_queue=None):
    global _started, _base_dir, _input_queue
    with _lock:
        _base_dir = os.path.abspath(base_dir)
        _input_queue = input_queue
        if _started:
            return False
        _started = True
        _stop.clear()
    threading.Thread(target=_worker, daemon=True,
                     name="vanta-camera-awareness").start()
    try:
        import authentication_gate
        if not authentication_gate.configured():
            request_verification("startup")
    except Exception:
        request_verification("startup")
    return True


def accept_primary_verification(now=None):
    """Publish a successful lifecycle-gate match to the existing soul state."""
    global _verified_at, _verification_required
    with _lock:
        _verified_at = now or time.time()
        _verification_required = False
        _state.observed_at = now or time.time()
        _state.present = True
        _state.himanshu_present = True
        _state.recognized_people = (os.getenv(
            "VANTA_PRIMARY_PERSON", "Himanshu"),)
    return True


def stop():
    _stop.set()
