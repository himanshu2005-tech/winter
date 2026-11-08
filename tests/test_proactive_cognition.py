import json
import queue

import proactive_cognition as pc


def response(**changes):
    data = {
        "decision": "quiet", "objective": "", "reason": "nothing changed",
        "priority": "routine", "next_check_minutes": 45,
        "evidence_refs": [], "confidence": 0.9,
    }
    data.update(changes)
    return json.dumps(data)


def test_model_owns_cross_resource_decision_and_timing(monkeypatch):
    monkeypatch.setattr(pc, "MIN_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(pc, "MAX_INTERVAL_SECONDS", 7200)
    seen = {}

    def llm(messages, **kwargs):
        seen["prompt"] = messages[-1]["content"]
        seen["kwargs"] = kwargs
        return response(
            decision="investigate",
            objective="Check whether the lower observed laptop price is the exact saved variant",
            reason="A price watch changed before a purchase decision",
            priority="normal", next_check_minutes=7,
            evidence_refs=["ACTIVE DURABLE WATCHES"], confidence=0.88)

    decision = pc.decide(
        "--- ACTIVE DURABLE WATCHES ---\nMacBook last=98000",
        llm, change_reasons=["watch observation changed"])
    assert decision.decision == "investigate"
    assert decision.next_check_seconds == 420
    assert decision.should_queue
    assert "Relate the available evidence" in seen["prompt"]
    assert seen["kwargs"]["task_level"] == "proactive_director"


def test_quiet_review_produces_no_user_event(tmp_path):
    inbox = queue.Queue()
    decision = pc.evaluate_once(
        str(tmp_path), lambda *_a, **_k: response(next_check_minutes=90),
        inbox, lambda: "ordinary unchanged evidence")
    assert decision.decision == "quiet"
    assert inbox.empty()


def test_repeated_proactive_objective_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "REPEAT_COOLDOWN_SECONDS", 999999)
    inbox = queue.Queue()
    raw = response(decision="notify", objective="Warn about the deadline",
                   reason="It moved closer", priority="important",
                   evidence_refs=["CALENDAR"])
    call = lambda *_a, **_k: raw
    first = pc.evaluate_once(str(tmp_path), call, inbox, lambda: "calendar")
    second = pc.evaluate_once(str(tmp_path), call, inbox, lambda: "calendar")
    assert first.should_queue and second.should_queue
    assert inbox.qsize() == 1
    origin, event = inbox.get_nowait()
    assert origin == "proactive"
    assert "Warn about the deadline" in event
    assert "authorization policy remains final" in event


def test_invalid_model_output_fails_quietly():
    decision = pc.decide("evidence", lambda *_a, **_k: "not json")
    assert decision.decision == "quiet"
    assert not decision.should_queue

