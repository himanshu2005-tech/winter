"""Contextual, restrained interpretation of VANTA's local camera evidence.

The camera pipeline owns perception.  This module owns neither object labels nor
behaviour rules: it decides *when* a stable scene is worth semantic review and
validates the vision model's proposed response.  That separation lets new
activities be understood without adding an ``if object == ...`` ladder while
keeping an unreliable model from becoming a constant source of interruptions.

No frame is persisted here.  Only small hashes and intervention metadata are
kept in memory.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import json
import os
import re
import threading
import time


_DECISIONS = frozenset({"quiet", "acknowledge", "suggest", "ask", "alert"})


@dataclass(frozen=True)
class ReviewRequest:
    reason: str
    signature: str
    events: tuple[str, ...]


@dataclass(frozen=True)
class VisualDecision:
    decision: str = "quiet"
    summary: str = ""
    message: str = ""
    topic_key: str = ""
    confidence: float = 0.0
    cooldown_minutes: int = 30
    interrupt_now: bool = False

    @property
    def should_deliver(self):
        return self.decision != "quiet" and bool(self.message)


def _env_float(name, default, low, high):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(low, min(high, value))


def _words(value):
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _similar(left, right):
    a, b = _words(left), _words(right)
    if not a or not b:
        return False
    return len(a & b) / max(1, len(a | b)) >= 0.72


def _bounded_number(value, default, low, high, cast=float):
    try:
        value = cast(value)
    except (TypeError, ValueError):
        value = cast(default)
    return max(low, min(high, value))


def scene_payload(state, *, mode="assistant", events=()):
    """Convert an awareness state into non-pixel, fallible sensor evidence."""
    return {
        "person_present": state.present,
        "primary_user_present": state.himanshu_present,
        "recognized_people": list(state.recognized_people),
        "unknown_face_count": int(state.unknown_face_count or 0),
        "objects": dict(Counter(state.object_labels)),
        "scene_changes": list(events or state.scene_diff or ()),
        "mode": str(mode or "assistant"),
        "study_present_seconds": round(float(state.study_present_seconds or 0)),
        "study_breaks": int(state.study_breaks or 0),
    }


def scene_signature(scene):
    stable = {
        "person_present": scene.get("person_present"),
        "primary_user_present": scene.get("primary_user_present"),
        "recognized_people": sorted(scene.get("recognized_people") or []),
        "unknown_face_count": scene.get("unknown_face_count", 0),
        "objects": scene.get("objects") or {},
        "mode": scene.get("mode") or "assistant",
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class VisualDirector:
    """A generic novelty and interruption governor around model judgement."""

    def __init__(self):
        self._lock = threading.RLock()
        self._candidate = ""
        self._candidate_samples = 0
        self._candidate_eventful = False
        self._last_review_signature = ""
        self._last_review_at = 0.0
        self._topic_last = {}
        self._messages = deque(maxlen=12)
        self._interventions = deque(maxlen=24)
        self._question_until = 0.0

    def reset_scene(self):
        """Forget scene stability without forgetting anti-nag history."""
        with self._lock:
            self._candidate = ""
            self._candidate_samples = 0
            self._candidate_eventful = False

    def reset(self):
        """Reset all ephemeral governor state (primarily for clean restarts/tests)."""
        with self._lock:
            self._candidate = ""
            self._candidate_samples = 0
            self._candidate_eventful = False
            self._last_review_signature = ""
            self._last_review_at = 0.0
            self._topic_last.clear()
            self._messages.clear()
            self._interventions.clear()
            self._question_until = 0.0

    def note_user_turn(self):
        """A new human turn settles the one-question-at-a-time guard."""
        with self._lock:
            self._question_until = 0.0

    def consider(self, scene, *, events=(), now=None):
        """Return a semantic-review request only at a useful boundary.

        Stable new scenes require repeated local observations.  Periodic review
        is intentionally sparse and gives the model a chance to notice an
        activity whose meaning is not represented by an object label.
        """
        now = float(now or time.time())
        if scene.get("person_present") is not True:
            self.reset_scene()
            return None
        if str(scene.get("mode") or "").casefold() in {"sleep", "standby"}:
            return None

        signature = scene_signature(scene)
        with self._lock:
            if signature == self._candidate:
                self._candidate_samples += 1
                self._candidate_eventful = self._candidate_eventful or bool(
                    events or scene.get("scene_changes"))
            else:
                self._candidate = signature
                self._candidate_samples = 1
                self._candidate_eventful = bool(
                    events or scene.get("scene_changes"))

            stable_samples = int(_env_float(
                "VANTA_VISUAL_STABLE_SAMPLES", 2, 1, 5))
            review_gap = _env_float(
                "VANTA_VISUAL_MIN_REVIEW_SECONDS", 90, 30, 1800)
            periodic = _env_float(
                "VANTA_VISUAL_PERIODIC_SECONDS", 900, 180, 7200)
            since = now - self._last_review_at
            changed = signature != self._last_review_signature
            eventful = self._candidate_eventful

            if since < review_gap:
                return None
            if changed and eventful and self._candidate_samples >= stable_samples:
                reason = "stable_scene_change"
            elif since >= periodic and self._candidate_samples >= stable_samples:
                reason = "contextual_check"
            else:
                return None

            self._last_review_signature = signature
            self._last_review_at = now
            self._candidate_eventful = False
            return ReviewRequest(reason, signature, tuple(events or ()))

    def allow(self, decision, *, now=None):
        """Apply generic anti-nag rules to an already contextual decision."""
        now = float(now or time.time())
        if not decision.should_deliver:
            return False
        minimum = _env_float("VANTA_VISUAL_MIN_CONFIDENCE", .68, .3, .98)
        if decision.confidence < minimum:
            return False
        with self._lock:
            while self._interventions and now - self._interventions[0] > 3600:
                self._interventions.popleft()
            hourly = int(_env_float(
                "VANTA_VISUAL_MAX_INTERRUPTS_PER_HOUR", 3, 1, 8))
            if decision.decision != "alert" and len(self._interventions) >= hourly:
                return False
            if decision.decision == "ask" and now < self._question_until:
                return False

            topic = decision.topic_key or hashlib.sha256(
                decision.message.casefold().encode("utf-8")).hexdigest()[:16]
            last_topic = self._topic_last.get(topic)
            cooldown = max(
                _env_float("VANTA_VISUAL_TOPIC_COOLDOWN_SECONDS", 600, 60, 7200),
                float(decision.cooldown_minutes) * 60.0,
            )
            if last_topic is not None and now - last_topic < cooldown:
                return False
            if any(_similar(decision.message, old) for _, old in self._messages):
                return False

            self._topic_last[topic] = now
            self._messages.append((now, decision.message))
            self._interventions.append(now)
            if decision.decision == "ask":
                self._question_until = now + _env_float(
                    "VANTA_VISUAL_QUESTION_TIMEOUT_SECONDS", 1200, 120, 7200)
            return True


director = VisualDirector()


def parse_decision(raw):
    if not isinstance(raw, dict):
        return VisualDecision()
    decision = str(raw.get("decision") or "").strip().casefold()
    # Compatibility with the previous vision contract while callers migrate.
    if decision not in _DECISIONS:
        decision = "suggest" if raw.get("speak") is True else "quiet"
    message = " ".join(str(raw.get("message") or raw.get("brief") or "").split())
    summary = " ".join(str(raw.get("summary") or "").split())
    topic = "-".join(re.findall(
        r"[a-z0-9]+", str(raw.get("topic_key") or "").casefold()))[:80]
    confidence = _bounded_number(raw.get("confidence"), 0, 0, 1)
    cooldown = int(_bounded_number(
        raw.get("cooldown_minutes"), 30, 5, 240, int))
    return VisualDecision(
        decision=decision,
        summary=summary[:600],
        message=message[:260],
        topic_key=topic,
        confidence=confidence,
        cooldown_minutes=cooldown,
        interrupt_now=bool(raw.get("interrupt_now", False)),
    )


def relevant_graph_context(scene, limit=6):
    """Fetch only current, non-sensitive facts relevant to this review."""
    if scene.get("primary_user_present") is not True:
        return ""
    try:
        from memory.neo4j_store import store
        terms = " ".join((scene.get("mode") or "", *(
            str(k) for k in (scene.get("objects") or {}).keys())))
        query = (
            "Himanshu preferences routines goals current activity context "
            + terms
        )
        rows = store.search_facts(
            query, limit=max(1, int(limit)), entity_keys=["Himanshu"])
        lines = []
        for row in rows:
            if str(row.get("sensitivity") or "private") not in {
                    "public", "private"}:
                continue
            if str(row.get("review_status") or "accepted") not in {
                    "accepted", "reviewed", "confirmed"}:
                continue
            lines.append(
                f"{row.get('subject')} --{row.get('predicate')}--> "
                f"{row.get('object')}"
            )
        return "\n".join(lines[:limit])
    except Exception as exc:
        print(f"[Visual Director] Graph context unavailable: {type(exc).__name__}")
        return ""


def assess(image_b64, scene, request, call_llm=None):
    """Ask the configured vision model for one contextual, structured verdict."""
    if call_llm is None:
        import llm_router
        call_llm = llm_router.call_llm
    graph_context = relevant_graph_context(scene)
    prompt = (
        "You are VANTA's visual-awareness director. The local perception layer "
        "has already decided that this scene is stable enough to inspect. Use "
        "the image, current operating mode, scene changes, and relevant current "
        "user facts to understand what is happening. Infer only what is visibly "
        "supported; object-detector labels are fallible evidence.\n\n"
        "Behave like a perceptive, mature person, not a surveillance feed. "
        "Default to quiet. Speak only when a brief observation, suggestion, or "
        "question has clear value now. Do not narrate objects, quiz the user for "
        "curiosity, repeat advice, diagnose health, infer emotion or sensitive "
        "traits, or turn every new object into a conversation. Ask only when the "
        "answer would materially improve assistance. Keep any message natural, "
        "specific, and one sentence.\n\n"
        "Return JSON only with: decision (quiet|acknowledge|suggest|ask|alert), "
        "summary (neutral private assessment), message (empty when quiet), "
        "topic_key (short semantic topic), confidence (0..1), cooldown_minutes "
        "(5..240), and interrupt_now (true only when delay defeats the value).\n"
        f"Review reason: {request.reason}\n"
        f"Scene evidence: {json.dumps(scene, ensure_ascii=False)}\n"
        f"Relevant current graph facts:\n{graph_context or '(none selected)'}"
    )
    result = call_llm([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{image_b64}"}},
    ]}], task_level="vision", quiet=True)
    raw = str(result or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                 flags=re.IGNORECASE)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return VisualDecision()
        try:
            value = json.loads(match.group(0))
        except (TypeError, ValueError):
            return VisualDecision()
    return parse_decision(value)
