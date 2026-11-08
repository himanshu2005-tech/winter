"""Managed-browser workflows keep one session from navigation through read.

The presentation (visible or background) is chosen by START/GOTO.  An
observation or interaction must never silently choose a different one,
because headed/headless is a process-level setting and changing it destroys
the page the previous action opened.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_playwright_observations_preserve_visibility():
    source = _source("browser.py")
    assert "def ensure_started(visible=None):" in source
    assert "def get_page(requested=None, visible=None):" in source
    assert 'elif operation == "snapshot":\n                    page = get_page(' in source
    assert 'elif operation == "tabs":\n                    ensure_started()' in source


def test_real_firefox_observations_preserve_visibility():
    source = _source("browser_real.py")
    assert "def ensure_started(visible=None):" in source
    assert "def get_tab(requested=None, visible=None):" in source
    # Snapshot may retain the handle so it can report a blocking native
    # dialog.  What matters is that observation does not pass a visibility
    # override and therefore cannot rebuild the session.
    assert ('elif operation == "snapshot":\n'
            '                    handle = get_tab(args.get("tab"))') in source
    assert 'elif operation == "tabs":\n                    ensure_started()' in source


def test_only_explicit_visibility_changes_rebuild_a_live_session():
    for filename, marker in (("browser.py", "context_visible"),
                             ("browser_real.py", 'state["visible"]')):
        source = _source(filename)
        start = source.index("def ensure_started(visible=None):")
        block = source[start:start + 1800]
        assert "visible is None or" in block
        assert "switching browser visibility" in block
        assert marker in block
