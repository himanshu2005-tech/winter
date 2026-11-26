"""Opt-in, real-tool Build mode smoke tests.

Ordinary CI does not open desktop applications. Set VANTA_RUN_BUILD_SMOKE=1
on a development machine to verify the installed Node/npm and Blender runtime
against isolated temporary vault projects. No production project is touched.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import blender_projects
import build_intelligence
import mode_manager
import projects


pytestmark = pytest.mark.skipif(
    os.getenv("VANTA_RUN_BUILD_SMOKE") != "1",
    reason="set VANTA_RUN_BUILD_SMOKE=1 for installed-tool smoke tests",
)


def test_react_project_is_profiled_and_its_declared_build_really_runs():
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        pytest.skip("npm is not installed")
    with tempfile.TemporaryDirectory(prefix="vanta_react_smoke_") as base:
        projects.create(base, "react-smoke", "isolated smoke fixture", "React")
        package = {
            "name": "vanta-react-smoke",
            "private": True,
            "dependencies": {"react": "19.0.0", "react-dom": "19.0.0"},
            # No download is needed: Node parses the real application module.
            "scripts": {"build": "node --check src/App.js"},
        }
        projects.write_file(base, "react-smoke", "package.json",
                            json.dumps(package, indent=2))
        projects.write_file(
            base, "react-smoke", "src/App.js",
            "import React from 'react';\n"
            "export default function App() {\n"
            "  return React.createElement('main', null, 'VANTA smoke');\n"
            "}\n",
        )
        root = Path(projects.project_path(base, "react-smoke"))
        profile = build_intelligence.profile(root)
        assert "react-web" in profile["artifact_kinds"]
        assert {row["command"] for row in profile["declared_commands"]} == {
            "npm run build"}
        result = subprocess.run(
            [npm, "run", "build"], cwd=root, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "react-web" in projects.context_block(base)


def test_blender_project_applies_and_verifies_a_real_scene_revision():
    if not Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe").exists():
        pytest.skip("Blender 5.2 is not installed")
    old_mode = mode_manager.get_mode_manager().active_mode().value
    pid = None
    temporary = tempfile.TemporaryDirectory(
        prefix="vanta_blender_smoke_", ignore_cleanup_errors=True)
    try:
        mode_manager.get_mode_manager().switch("build", source="test")
        base = temporary.name
        receipt = blender_projects.apply(
            base, "blender-smoke",
            "import bpy\n"
            "bpy.ops.object.select_all(action='SELECT')\n"
            "bpy.ops.object.delete(use_global=False)\n"
            "bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))\n"
            "cube = bpy.context.active_object\n"
            "cube.name = 'VantaSmokeCube'\n"
            "cube['vanta_id'] = 'smoke-cube'\n",
            instruction="create one identified verification cube",
            timeout=60,
        )
        assert receipt.startswith("[Success]"), receipt
        assert "smoke-cube" in receipt
        status = blender_projects.status(base, "blender-smoke", timeout=30)
        assert status.startswith("[Success]"), status
        assert "smoke-cube" in status
        presented = blender_projects.present(
            base, "blender-smoke", enabled=True, timeout=30)
        assert presented.startswith("[Success]"), presented
        assert "Orbit, pan, and zoom remain enabled" in presented
        restored = blender_projects.present(
            base, "blender-smoke", enabled=False, timeout=30)
        assert restored.startswith("[Success]"), restored
        assert "normal Blender workspace is restored" in restored
        paths = blender_projects._paths(base, "blender-smoke")
        ready = blender_projects._read_json(paths["ready"], {}) or {}
        pid = ready.get("pid")
        assert paths["artifact"].exists()
    finally:
        mode_manager.get_mode_manager().switch(old_mode, source="test")
        if pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
            time.sleep(0.3)
        temporary.cleanup()
