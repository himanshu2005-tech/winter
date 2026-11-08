import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_center_files import (
    configured_roots,
    content_hash,
    is_safe_text_path,
    list_vault_directory,
    read_vault_file,
    save_vault_file,
    scan_text_files,
    unified_diff,
    vault_root,
)


def run():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        vault = vault_root(base)
        notes = vault / "notes.txt"
        notes.write_text("line one\nline two\n", encoding="utf-8")
        nested = vault / "study"
        nested.mkdir()
        (nested / "plan.md").write_text("study plan", encoding="utf-8")
        (vault / ".env").write_text("SECRET=x", encoding="utf-8")
        (vault / "token.json").write_text("{}", encoding="utf-8")
        (vault / "image.png").write_bytes(b"binary")
        outside = base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        assert configured_roots(base) == (vault,)
        listing = list_vault_directory(vault)
        assert listing["current"] == "."
        assert {(row["kind"], row["name"]) for row in listing["entries"]} == {
            ("folder", "study"), ("file", "notes.txt")
        }
        nested_listing = list_vault_directory(vault, "study")
        assert nested_listing["entries"][0]["kind"] == "parent"
        assert is_safe_text_path(notes, (vault,))
        assert not is_safe_text_path(vault / ".env", (vault,))
        assert not is_safe_text_path(outside, (vault,))
        try:
            list_vault_directory(vault, "../")
            raise AssertionError("path escape was not blocked")
        except PermissionError:
            pass

        loaded = read_vault_file(vault, "notes.txt")
        assert loaded["content"] == "line one\nline two\n"
        assert loaded["hash"] == content_hash(loaded["content"])
        diff = unified_diff(loaded["content"], "line one\nnew line\n", "notes.txt")
        assert "-line two" in diff and "+new line" in diff

        saved = save_vault_file(vault, "notes.txt", "line one\nnew line\n", loaded["hash"])
        assert saved["changed"] is True
        assert Path(saved["backup"]).is_file()
        assert notes.read_text(encoding="utf-8") == "line one\nnew line\n"
        try:
            save_vault_file(vault, "notes.txt", "stale", loaded["hash"])
            raise AssertionError("stale write was not rejected")
        except RuntimeError:
            pass

        rows = scan_text_files((vault,))
        assert {row["display"] for row in rows} == {"notes.txt", str(Path("study") / "plan.md")}

    root = Path(__file__).resolve().parents[1]
    ui_source = (root / "ui.py").read_text(encoding="utf-8")
    agent_source = (root / "agent.py").read_text(encoding="utf-8")
    cc_source = (root / "control_center.py").read_text(encoding="utf-8")

    # These used to grep ui.py for quoted tab names, which broke twice over:
    # the pages moved to control_center.py, and "Modes" no longer exists at all
    # now that there are two modes and the switch lives on Home. Asking the
    # module is both accurate and immune to where the strings happen to sit.
    import control_center
    rail = [name for _section, names in control_center.NAV_SECTIONS
            for name in names]
    assert rail, "the rail is empty"
    assert "Home" in rail
    for name in rail:
        assert name in control_center.PAGE_BUILDERS, f"{name} has no builder"
        method = control_center.PAGE_BUILDERS[name]
        assert hasattr(control_center.ControlCenterMixin, method), method

    # The vault editor and its inline diff. "Reload + Diff" was a button label
    # that no longer exists in either file; the widgets it referred to do, so
    # the widgets are what gets checked.
    both = ui_source + cc_source
    for feature in ("file_editor", "file_diff", "_send_vault_command",
                    "_update_vault_diff"):
        assert feature in both, feature
    assert "read-only workspace browser" not in both

    assert 'ui_queue.put(("toggle_dashboard", ""))' in agent_source
    assert "graph_result" in ui_source
    assert 'ui_queue.put(("hide_dashboard", ""))' not in agent_source
    assert "show_lockscreen" not in agent_source
    print("Control Center Vault checks passed")


if __name__ == "__main__":
    run()