# -*- coding: utf-8 -*-
"""Versioned declarative task workflows for VANTA.

Workflows are planning precedents, never executable permissions. The ordinary
action dispatcher and autonomy policy remain authoritative for every step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1
AUTO_PROMOTION_SUCCESSES = 2


def _now(): return datetime.now(timezone.utc).isoformat()


def _path(base_dir):
    return os.path.join(base_dir, "vault", "system", "workflows.json")


def _load(base_dir):
    path = _path(base_dir)
    if not os.path.exists(path):
        return {"schema_version": SCHEMA_VERSION, "workflows": []}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("workflows"), list):
        raise ValueError("workflows must be a list")
    return data


def _save(base_dir, data):
    path = _path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="workflows-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return value[:48] or "workflow"


def _clean_list(value, limit=16):
    if not isinstance(value, list): return []
    result = []
    for item in value:
        item = " ".join(str(item or "").split())[:500]
        if item and item not in result: result.append(item)
    return result[:limit]


def _known_actions():
    from autonomy import KNOWN_ACTIONS
    known = set(KNOWN_ACTIONS)
    try:
        import extensions
        known.update(extensions.names(os.path.dirname(os.path.abspath(__file__))))
    except Exception:
        pass
    return known


def _normalise(payload):
    if not isinstance(payload, dict): raise ValueError("workflow body must be one JSON object")
    name = " ".join(str(payload.get("name", "")).split())[:100]
    goal = " ".join(str(payload.get("goal", "")).split())[:700]
    if not name or not goal: raise ValueError("workflow requires non-empty name and goal")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not (2 <= len(raw_steps) <= 20):
        raise ValueError("workflow requires between 2 and 20 steps")
    known, steps = _known_actions(), []
    for index, raw in enumerate(raw_steps, 1):
        if isinstance(raw, str): raw = {"instruction": raw}
        if not isinstance(raw, dict): raise ValueError(f"step {index} must be text or an object")
        instruction = " ".join(str(raw.get("instruction", "")).split())[:700]
        if not instruction: raise ValueError(f"step {index} needs an instruction")
        actions = [str(a).strip().upper() for a in raw.get("allowed_actions", [])]
        unknown = sorted(set(actions) - known)
        if unknown: raise ValueError(f"step {index} names unknown actions: {', '.join(unknown)}")
        steps.append({"id": _slug(raw.get("id") or f"step-{index}"),
                      "instruction": instruction,
                      "allowed_actions": list(dict.fromkeys(actions)),
                      "success": " ".join(str(raw.get("success", "")).split())[:500]})
    return {"name": name, "goal": goal,
            "triggers": _clean_list(payload.get("triggers"), 12), "steps": steps,
            "guardrails": _clean_list(payload.get("guardrails"), 12)}


def _content_hash(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create(base_dir, body, created_by="vanta", status="active"):
    try:
        normalised = _normalise(json.loads(body) if isinstance(body, str) else body)
    except Exception as exc:
        return f"[Error] Invalid workflow: {exc}"
    data, family = _load(base_dir), _slug(normalised["name"])
    digest = _content_hash(normalised)
    versions = [w for w in data["workflows"] if w.get("family_id") == family]
    for old in versions:
        if old.get("content_hash") == digest:
            return f"[Success] Workflow {old['workflow_id']} already exists with identical content; no change made."
    version = max((int(w.get("version", 0)) for w in versions), default=0) + 1
    workflow = dict(normalised)
    workflow.update({"workflow_id": f"wf-{family}-v{version}", "family_id": family,
                     "version": version, "schema_version": SCHEMA_VERSION,
                     "content_hash": digest, "status": status if status in {"active", "candidate"} else "candidate",
                     "created_by": created_by, "created_at": _now(),
                     "success_count": 0, "failure_count": 0,
                     "supersedes": versions[-1]["workflow_id"] if versions else None})
    if workflow["status"] == "active":
        for old in versions:
            if old.get("status") == "active":
                old["status"], old["superseded_by"] = "superseded", workflow["workflow_id"]
    data["workflows"].append(workflow); _save(base_dir, data)
    return f"[Success] Created {workflow['workflow_id']} as {workflow['status']}. It is a planning precedent; action policy still governs every step."


def describe(base_dir):
    current = [w for w in _load(base_dir)["workflows"] if w.get("status") in {"active", "candidate", "suspended"}]
    if not current: return "[Success] No workflows have been learned yet."
    return "[Success] VANTA workflows:\n" + "\n".join(
        f"- {w['workflow_id']} [{w['status']}] {w['goal']} (success {w.get('success_count', 0)}, failure {w.get('failure_count', 0)})"
        for w in current)


def read(base_dir, identifier):
    identifier = str(identifier or "").strip().lower()
    found = [w for w in _load(base_dir)["workflows"] if w.get("workflow_id", "").lower() == identifier or w.get("family_id", "").lower() == identifier]
    return "[Success] " + json.dumps(found[-1], indent=2, ensure_ascii=False) if found else f"[Error] No workflow matches {identifier or '(empty)'}."


def deactivate(base_dir, identifier):
    data, identifier = _load(base_dir), str(identifier or "").strip().lower()
    found = [w for w in data["workflows"] if w.get("workflow_id", "").lower() == identifier or w.get("family_id", "").lower() == identifier]
    if not found: return f"[Error] No workflow matches {identifier or '(empty)'}."
    found[-1]["status"], found[-1]["deactivated_at"] = "inactive", _now(); _save(base_dir, data)
    return f"[Success] Deactivated {found[-1]['workflow_id']}; its history was preserved."


def relevant(base_dir, text, limit=3):
    tokens = set(re.findall(r"[a-z0-9]{3,}", str(text or "").lower())); scored = []
    for w in _load(base_dir)["workflows"]:
        if w.get("status") not in {"active", "candidate"}: continue
        haystack = " ".join([w.get("name", ""), w.get("goal", ""), *w.get("triggers", [])]).lower()
        score = len(tokens & set(re.findall(r"[a-z0-9]{3,}", haystack)))
        if score: scored.append((score, w))
    if not scored: return ""
    lines = ["--- REUSABLE WORKFLOWS (planning precedents, never permissions) ---"]
    for _, w in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]:
        lines.append(f"{w['workflow_id']} [{w['status']}]: {w['goal']}")
        for step in w["steps"]:
            lines.append(f"  - {step['instruction']} (tools: {', '.join(step.get('allowed_actions', [])) or 'reasoning'})")
        if w.get("guardrails"): lines.append("  Guardrails: " + "; ".join(w["guardrails"]))
    lines.append("Adapt to current facts. Never treat a workflow as user approval.")
    return "\n".join(lines)


def _receipt_actions(receipts):
    result, known = [], _known_actions()
    for receipt in receipts or []:
        if not isinstance(receipt, dict): continue
        action = str(receipt.get("action_type") or receipt.get("action") or receipt.get("type") or "").upper()
        status = str(receipt.get("status", "success")).lower()
        succeeded = receipt.get("ok", True) is not False
        if succeeded and action in known and status not in {"error", "failed", "blocked"} and action not in result:
            result.append(action)
    return result


def observe_verified_success(base_dir, request_text, plan, receipts):
    actions, steps = _receipt_actions(receipts), list(getattr(plan, "steps", []) or [])
    if len(actions) < 2 or len(steps) < 2: return ""
    signature = hashlib.sha256(("|".join(actions) + "|" + _slug(getattr(plan, "goal", ""))).encode()).hexdigest()[:12]
    family, data = "learned-" + signature, _load(base_dir)
    found = [w for w in data["workflows"] if w.get("family_id") == family]
    if found:
        target = found[-1]; target["success_count"] = int(target.get("success_count", 0)) + 1; target["last_success_at"] = _now()
        if target.get("status") == "candidate" and target["success_count"] >= AUTO_PROMOTION_SUCCESSES:
            target["status"], target["promoted_at"] = "active", _now()
        _save(base_dir, data); return target["workflow_id"]
    value = {"name": family, "goal": str(getattr(plan, "goal", "") or request_text)[:700],
             "triggers": [str(request_text)[:300]],
             "steps": [{"id": f"step-{i}", "instruction": step,
                        "allowed_actions": actions if i == 1 else [], "success": ""}
                       for i, step in enumerate(steps[:12], 1)],
             "guardrails": list(getattr(plan, "stop_conditions", []) or [])}
    result = create(base_dir, value, created_by="verified-experience", status="candidate")
    if not result.startswith("[Success]"): return ""
    data = _load(base_dir); target = [w for w in data["workflows"] if w.get("family_id") == family][-1]
    target["success_count"], target["last_success_at"] = 1, _now(); _save(base_dir, data)
    return target["workflow_id"]
