import json
import tempfile
from pathlib import Path

import build_intelligence


def test_react_web_uses_declared_scripts_only():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"react": "19.0.0", "react-dom": "19.0.0"},
            "scripts": {"build": "vite build", "test": "vitest"},
        }), encoding="utf-8")
        (root / "App.tsx").write_text("export default function App(){}", encoding="utf-8")
        data = build_intelligence.profile(root)
        assert data["artifact_kinds"] == ["react-web"]
        assert {item["command"] for item in data["declared_commands"]} == {
            "npm run build", "npm run test"}
        assert ".tsx" in data["unparsed_source_types"]


def test_react_native_is_not_misclassified_as_react_web():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"react": "19.0.0", "react-native": "0.80.0"},
            "scripts": {"start": "react-native start"},
        }), encoding="utf-8")
        assert build_intelligence.profile(root)["artifact_kinds"] == ["react-native"]


def test_python_and_blender_are_additive_and_ast_is_honest():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / ".vanta_blender").mkdir()
        (root / "build.py").write_text("def build():\n    return 1\n", encoding="utf-8")
        data = build_intelligence.profile(root)
        assert data["artifact_kinds"] == ["blender-3d", "python"]
        assert data["ast"] == "python:stdlib-ast"


def test_no_commands_are_invented_for_an_empty_project():
    with tempfile.TemporaryDirectory() as folder:
        data = build_intelligence.profile(folder)
        assert data["artifact_kinds"] == ["generic"]
        assert data["declared_commands"] == []


def test_show_code_panel_is_explicit_and_opens_only_code_page():
    import queue
    import action_bus
    import action_schema
    import actions
    import autonomy

    ui = queue.Queue()
    ctx = action_bus.ActionContext(
        "SHOW_CODE_PANEL", {}, ui, r"D:\winter", origin="user",
        legacy=actions.execute_actions)
    action_bus.BUS.dispatch(ctx)
    assert action_schema.validate("SHOW_CODE_PANEL", {}, has_body=False) is None
    assert "SHOW_CODE_PANEL" in autonomy.KNOWN_ACTIONS
    assert ctx.receipt == "[Success] Control Center open on Code."
    assert ui.get_nowait() == ("dashboard_page", "Code")
