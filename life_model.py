# -*- coding: utf-8 -*-
"""Durable, user-controlled personal context for VANTA.

This is intentionally not a raw surveillance log. It stores concise, reviewable
facts that make planning better over time: goals, routines, preferences, and
explicitly useful observations. It never grants permission for an action.
"""

import json
import os
from datetime import datetime

_RELATIVE_PATH = os.path.join("vault", "memory", "life_model.json")


def _path(base_dir):
    return os.path.join(base_dir, _RELATIVE_PATH)


def _default():
    return {
        "version": 1,
        "updated_at": None,
        "preferences": {
            "briefings": "spoken aloud and mirrored on the dashboard",
            "autonomy": "take initiative on clear, private, reversible organisation; ask before external changes",
        },
        "goals": [],
        "routines": [],
        "patterns": [],
    }


def load(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        result = _default()
        if isinstance(data, dict):
            result.update(data)
        return result
    except (OSError, json.JSONDecodeError):
        return _default()


def save(base_dir, data):
    os.makedirs(os.path.dirname(_path(base_dir)), exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(_path(base_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_goal(base_dir, title, horizon="ongoing", priority="medium"):
    data = load(base_dir)
    item = {
        "id": f"goal-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "title": title.strip(),
        "horizon": horizon.strip() or "ongoing",
        "priority": priority.strip().lower() or "medium",
        "status": "active",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    data.setdefault("goals", []).append(item)
    save(base_dir, data)
    return f"Added goal: {item['title']} ({item['priority']}, {item['horizon']})."


def set_preference(base_dir, key, value):
    data = load(base_dir)
    data.setdefault("preferences", {})[key.strip()] = value.strip()
    save(base_dir, data)
    return f"Saved preference: {key.strip()}."


def add_pattern(base_dir, observation, confidence="low"):
    data = load(base_dir)
    data.setdefault("patterns", []).append({
        "observation": observation.strip(),
        "confidence": confidence.strip().lower() or "low",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })
    data["patterns"] = data["patterns"][-50:]
    save(base_dir, data)
    return "Saved a reviewable life-pattern observation."


def context_for_agent(base_dir, max_goals=6, max_patterns=5):
    """Return compact, non-secret personal context suitable for planning."""
    data = load(base_dir)
    goals = [g for g in data.get("goals", []) if g.get("status") == "active"][-max_goals:]
    patterns = data.get("patterns", [])[-max_patterns:]
    lines = ["--- LIFE MODEL (user-controlled) ---"]
    prefs = data.get("preferences", {})
    if prefs:
        lines.append("Preferences: " + "; ".join(f"{k}={v}" for k, v in prefs.items()))
    if goals:
        lines.append("Active goals:")
        lines.extend(f"- [{g.get('priority', 'medium')}] {g.get('title', '')} ({g.get('horizon', 'ongoing')})" for g in goals)
    if patterns:
        lines.append("Observed patterns (reviewable hypotheses, not facts):")
        lines.extend(f"- [{p.get('confidence', 'low')}] {p.get('observation', '')}" for p in patterns)
    return "\n".join(lines)
