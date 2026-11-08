from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_core_prompt_requires_contextual_personality_with_serious_boundaries():
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    compact = " ".join(source.split())
    assert "SOCIAL PRESENCE" in source
    assert "A joke must feel discovered, not pasted on" in compact
    assert "health, safety, grief, money, privacy" in compact
    assert "Personality never changes facts, permissions, scope" in compact


def test_personality_is_adapted_by_operating_mode():
    source = (ROOT / "mode_manager.py").read_text(encoding="utf-8")
    assert "context-aware wit" in source
    assert "banter must never break focus" in source
    assert "jokes away from uncertain or consequential claims" in source
    assert "when something breaks, name the failure" in source


def test_acknowledgements_are_generated_not_a_canned_joke_table():
    source = (ROOT / "ack.py").read_text(encoding="utf-8")
    assert "quick situational wit" in source
    assert "Return {{\"lines\"" in source
