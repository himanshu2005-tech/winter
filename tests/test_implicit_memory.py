import datetime
import queue
from unittest import mock

import commitments
import routines
from memory.retention import (
    apply_operational_memory,
    normalize_candidate,
    operational_effect,
)


def _candidate(**overrides):
    item = {
        "subject": "Himanshu",
        "predicate": "HAS_PREFERENCE",
        "object": "Keep spoken answers concise",
        "confidence": 0.98,
        "quote": "keep spoken answers concise from now on",
        "memory_kind": "assistant_preference",
        "persistent": True,
        "instruction": "Keep spoken answers concise",
    }
    item.update(overrides)
    return item


def test_standing_preference_is_typed_without_a_remember_keyword():
    text = "keep spoken answers concise from now on"
    item = normalize_candidate(_candidate(), text)
    assert item["predicate"] == "HAS_PREFERENCE"
    assert operational_effect(item) == "directive"


def test_fabricated_quote_cannot_change_future_behaviour():
    item = normalize_candidate(
        _candidate(quote="words the user never said"),
        "keep spoken answers concise from now on",
    )
    assert item["quote"] == ""
    assert operational_effect(item) == "graph_only"


def test_tentative_or_undated_plan_does_not_create_a_commitment():
    text = "I might meet someone tomorrow"
    item = normalize_candidate(_candidate(
        memory_kind="commitment", object="meet someone",
        quote=text, persistent=False, commitment_status="tentative",
        hard_time="",
    ), text)
    assert operational_effect(item) == "graph_only"


def test_confirmed_exact_commitment_is_operational_memory():
    text = "I have my exam on 2026-11-02 at 09:00"
    item = normalize_candidate(_candidate(
        memory_kind="commitment", object="exam", quote=text,
        persistent=False, commitment_status="confirmed",
        hard_time="2026-11-02T09:00:00+05:30",
    ), text)
    assert item["hard_time"]
    assert operational_effect(item) == "commitment"


@mock.patch("persona.add_directive", return_value="[Success] stored")
def test_implicit_assistant_preference_reaches_persona(add_directive, tmp_path):
    text = "keep spoken answers concise from now on"
    item = normalize_candidate(_candidate(), text)
    result = apply_operational_memory(item, "turn-1", base_dir=tmp_path)
    assert "stored" in result
    add_directive.assert_called_once_with(
        str(tmp_path), "Keep spoken answers concise",
        source="implicit_user_preference:turn-1")


def test_commitment_store_is_idempotent_and_preserves_evidence(tmp_path):
    item = {
        "what": "Project review",
        "hard_time": "2026-11-02T09:00:00",
        "source_id": "turn-9",
        "evidence": "I have my project review then",
    }
    assert "Tracking" in commitments.add(str(tmp_path), item)
    assert "Already tracking" in commitments.add(str(tmp_path), item)
    saved = commitments._load(str(tmp_path))
    assert len(saved) == 1
    assert saved[0]["source_id"] == "turn-9"


def test_recurring_instruction_is_idempotent_by_schedule_and_instruction(tmp_path):
    first = routines.add(
        str(tmp_path), "Morning brief", "Summarise my day", "daily 08:00")
    second = routines.add(
        str(tmp_path), "Daily overview", "Summarise my day", "daily 08:00")
    assert "scheduled" in first
    assert "Already scheduled" in second
    assert len(routines._load(str(tmp_path))) == 1


def test_due_routine_is_queued_even_when_assistant_is_busy(tmp_path):
    routines.add(str(tmp_path), "Brief", "Summarise my day", "daily 08:00")
    items = routines._load(str(tmp_path))
    now = datetime.datetime.now()
    items[0]["next_run"] = (now - datetime.timedelta(minutes=1)).isoformat()
    routines._save(str(tmp_path), items)
    inbox = queue.Queue()

    assert routines.dispatch_due(
        str(tmp_path), inbox, is_busy=lambda: True, now=now) == 1
    origin, message = inbox.get_nowait()
    assert origin == "proactive"
    assert "SCHEDULED ROUTINE" in message


def test_background_retention_module_has_no_alarm_side_effect():
    # A regression guard: memory extraction may remember an obligation but an
    # interrupting alarm is owned by the explicit action pipeline.
    source = __import__("inspect").getsource(apply_operational_memory)
    assert "set_alarm" not in source


def test_removed_procedure_is_retired_not_relearned(tmp_path):
    text = "do not test me after teaching anymore"
    item = normalize_candidate(_candidate(
        object="test me after teaching", instruction="test me after teaching",
        quote=text, memory_kind="procedure", persistent=False,
        operation="remove", trigger="when teaching"), text)
    assert operational_effect(item) == "remove_procedure"
    with mock.patch("learned_procedures.retire",
                    return_value="[Success] retired") as retire:
        assert "retired" in apply_operational_memory(
            item, "turn-remove", base_dir=tmp_path)
        retire.assert_called_once_with("test me after teaching")
