# -*- coding: utf-8 -*-
"""Persistent executable projects: safety, history, and action wiring."""

import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import action_bus
import action_schema
import actions
import autonomy
import blender_projects
import project_runtime
import projects


VALID_CODE = '''import bpy
bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0))
obj = bpy.context.object
obj["vanta_id"] = "layer-1-node-1"
obj["layer_index"] = 1
obj["node_index"] = 1
'''


class BlenderProjectTests(unittest.TestCase):
    def test_validator_accepts_scene_code_and_blocks_machine_escape(self):
        self.assertEqual(blender_projects._validate_code(VALID_CODE), VALID_CODE)
        for unsafe in ("import os\nos.remove('x')", "open('x', 'w')",
                       "import requests\nrequests.get('https://x')"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(blender_projects.BlenderProjectError):
                    blender_projects._validate_code(unsafe)

    def test_presentation_mode_uses_the_live_bridge_without_a_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            projects.create(folder, "showcase", "presentation fixture", "Blender")
            root = Path(projects.project_path(folder, "showcase"))
            (root / "scene.blend").write_bytes(b"BLENDER" * 100)
            result = {"ok": True, "summary": {
                "object_count": 2, "collection_count": 1,
                "objects": [], "edges": []}}
            with mock.patch.object(blender_projects, "_ensure_bridge",
                                   return_value=({"pid": 4242}, False)), \
                 mock.patch.object(blender_projects, "_submit",
                                   return_value=result) as submit:
                receipt = blender_projects.present(
                    folder, "showcase", enabled=True)
            self.assertIn("presentation mode is active", receipt)
            self.assertIn("Orbit, pan, and zoom remain enabled", receipt)
            self.assertEqual(submit.call_args.args[1], "apply")
            self.assertFalse((root / ".vanta_blender" /
                              "presentation.py").exists())

    def test_presentation_script_is_model_only_and_reversible(self):
        enter = blender_projects._presentation_code(True)
        leave = blender_projects._presentation_code(False)
        self.assertIn("show_overlays = not presenting", enter)
        self.assertIn("bpy.ops.view3d.view_all", enter)
        self.assertIn("bpy.ops.screen.screen_full_area", enter)
        self.assertIn("enter = False", leave)

    def test_scene_quality_reports_duplicate_ids_and_visual_clutter(self):
        objects = [
            {"name": "Camera", "type": "CAMERA",
             "properties": {"vanta_id": "camera"}},
            {"name": "Camera.001", "type": "CAMERA",
             "properties": {"vanta_id": "camera"}},
        ]
        objects.extend(
            {"name": f"Label.{index}", "type": "FONT",
             "properties": {"vanta_id": f"label-{index // 2}"}}
            for index in range(18))
        warnings = blender_projects._quality_warnings({
            "objects": objects, "object_count": len(objects),
            "collection_count": 1, "edges": []})
        self.assertTrue(any("duplicate semantic IDs" in item for item in warnings))
        self.assertTrue(any("cameras exist" in item for item in warnings))
        self.assertTrue(any("visual clutter" in item for item in warnings))
        self.assertTrue(any("ungrouped" in item for item in warnings))

    def test_new_geometry_requires_durable_semantic_identity(self):
        with self.assertRaisesRegex(blender_projects.BlenderProjectError,
                                    "vanta_id"):
            blender_projects._validate_code(
                "import bpy\nbpy.ops.mesh.primitive_cube_add()")

    def test_connection_geometry_requires_verifiable_endpoints(self):
        code = (
            "import bpy\n"
            "bpy.ops.mesh.primitive_cylinder_add()\n"
            "edge = bpy.context.object\n"
            "edge['vanta_id'] = 'edge-1'\n")
        with self.assertRaisesRegex(blender_projects.BlenderProjectError,
                                    "vanta_source"):
            blender_projects._validate_code(code)

    def test_adapter_is_generic_and_unknown_adapters_fail_closed(self):
        self.assertIn("blender", project_runtime.adapters())
        with self.assertRaises(project_runtime.ProjectRuntimeError):
            project_runtime.status("x", "does-not-exist", "p")

    def test_apply_commits_immutable_revisions_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            def submitted(paths, operation, code_path="", **_kwargs):
                paths["artifact"].write_bytes(b"BLENDER" * 100)
                return {"ok": True, "summary": {
                    "object_count": 1, "collection_count": 1,
                    "objects": [{"name": "Node", "type": "MESH",
                                 "location": [0, 0, 0],
                                 "properties": {"vanta_id": "layer-1-node-1",
                                                "layer_index": 1}}],
                    "edges": []}}

            with mock.patch.object(blender_projects, "_ensure_bridge",
                                   return_value=({"pid": 4242}, True)), \
                 mock.patch.object(blender_projects, "_submit",
                                   side_effect=submitted):
                first = blender_projects.apply(
                    folder, "network", VALID_CODE, instruction="create node")
                second = blender_projects.apply(
                    folder, "network", VALID_CODE.replace("(0, 0, 0)", "(1, 0, 0)"),
                    instruction="move node")

            root = Path(projects.project_path(folder, "network"))
            runtime = root / blender_projects.RUNTIME_DIR
            manifest = json.loads((runtime / "manifest.json").read_text("utf-8"))
            self.assertIn("revision=1", first)
            self.assertIn("revision=2", second)
            self.assertEqual(manifest["revision"], 2)
            self.assertEqual(len(manifest["history"]), 2)
            self.assertTrue((runtime / "revisions" / "0001.py").exists())
            self.assertTrue((runtime / "revisions" / "0002.py").exists())
            self.assertTrue((root / "scene.blend").exists())
            context = projects.context_block(folder)
            self.assertIn("EXECUTABLE PROJECT RUNTIME", context)
            self.assertIn("layer-1-node-1", context)

    def test_failed_revision_is_retained_but_not_committed(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(blender_projects, "_ensure_bridge",
                                   return_value=({"pid": 77}, False)), \
                 mock.patch.object(blender_projects, "_submit",
                                   return_value={"ok": False, "error": "bad mesh"}):
                receipt = blender_projects.apply(folder, "failed", VALID_CODE)
            root = Path(projects.project_path(folder, "failed"))
            revisions = root / blender_projects.RUNTIME_DIR / "revisions"
            self.assertTrue(receipt.startswith("[Error]"))
            self.assertTrue((revisions / "0001.failed.py").exists())
            self.assertFalse((revisions / "0001.py").exists())

    def test_schema_and_code_owned_policy(self):
        self.assertIsNone(action_schema.validate(
            "RUNTIME_APPLY", {"project": "network", "adapter": "blender"},
            has_body=True))
        self.assertIn("RUNTIME_APPLY", autonomy.KNOWN_ACTIONS)
        self.assertIn("RUNTIME_PRESENT", autonomy.KNOWN_ACTIONS)
        self.assertIsNone(action_schema.validate(
            "RUNTIME_PRESENT", {"project": "network", "action": "enter"}))
        self.assertFalse(autonomy.action_allowed("RUNTIME_APPLY", "proactive")[0])
        self.assertTrue(autonomy.action_allowed("RUNTIME_APPLY", "user")[0])

    def test_action_dispatches_to_adapter_and_returns_evidence(self):
        ui = queue.Queue()
        ctx = action_bus.ActionContext(
            "RUNTIME_APPLY",
            {"adapter": "blender", "project": "network",
             "instruction": "connect nodes"}, ui, "D:\\winter",
            origin="user", response_text=(
                '<ACTION type="RUNTIME_APPLY" adapter="blender" '
                'project="network">import bpy</ACTION>'),
            legacy=actions.execute_actions)
        evidence = ("[Success] Blender project 'network' revision=3; "
                    "artifact=D:\\scene.blend; bridge_pid=42; objects=12")
        with mock.patch.object(project_runtime, "apply", return_value=evidence) as call:
            action_bus.BUS.dispatch(ctx)
        self.assertEqual(ctx.receipt, evidence)
        self.assertEqual(call.call_args.args[1:4], ("blender", "network", "import bpy"))

    def test_presentation_action_routes_enter_and_exit(self):
        ui = queue.Queue()
        for requested, enabled in (("enter", True), ("exit", False)):
            ctx = action_bus.ActionContext(
                "RUNTIME_PRESENT",
                {"adapter": "blender", "project": "network",
                 "action": requested}, ui, "D:\\winter", origin="user",
                response_text=(f'<ACTION type="RUNTIME_PRESENT" '
                               f'project="network" action="{requested}" />'),
                legacy=actions.execute_actions)
            with mock.patch.object(
                    project_runtime, "present",
                    return_value=("[Success] Blender presentation mode is active "
                                  "for 'network'; bridge_pid=42; objects=3")) as call:
                action_bus.BUS.dispatch(ctx)
            self.assertTrue(ctx.receipt.startswith("[Success]"), ctx.receipt)
            self.assertEqual(call.call_args.kwargs["enabled"], enabled)

    def test_parser_passes_block_code_and_different_revisions_are_not_duplicates(self):
        self.assertNotEqual(
            actions._ledger_key("RUNTIME_APPLY", {"project": "network"}, "code one"),
            actions._ledger_key("RUNTIME_APPLY", {"project": "network"}, "code two"))
        ui = queue.Queue()
        tag = ('<ACTION type="RUNTIME_APPLY" adapter="blender" '
               'project="network" instruction="add a node">import bpy\n'
               'print("revision")</ACTION>')
        evidence = ("[Success] Blender project 'network' revision=1; "
                    "artifact=D:\\scene.blend; bridge_pid=42; objects=1")
        actions.reset_task_ledger("build the scene")
        with mock.patch.object(project_runtime, "apply", return_value=evidence) as call:
            actions.execute_actions(tag, {}, ui, "D:\\winter", origin="user")
        self.assertEqual(call.call_args.args[3], 'import bpy\nprint("revision")')
        self.assertIn(evidence, actions.execute_actions._last_output)

    def test_redundant_close_after_self_closing_tag_does_not_fake_a_body(self):
        ui = queue.Queue()
        malformed = (
            '<ACTION type="RUNTIME_APPLY" adapter="blender" '
            'project="network" instruction="apply revision" /></ACTION>')
        with mock.patch.object(project_runtime, "apply") as apply:
            actions.execute_actions(malformed, {}, ui, "D:\\winter",
                                    origin="user")
        apply.assert_not_called()
        self.assertIn("needs __body__ or code",
                      actions.execute_actions._last_output)

    def test_correct_block_after_malformed_self_close_keeps_project_and_code(self):
        ui = queue.Queue()
        response = (
            '<ACTION type="RUNTIME_APPLY" adapter="blender" '
            'project="network">import bpy\nprint("ok")</ACTION>')
        evidence = ("[Success] Blender project 'network' revision=2; "
                    "artifact=D:\\scene.blend; bridge_pid=42; objects=1")
        with mock.patch.object(project_runtime, "apply",
                               return_value=evidence) as apply:
            actions.execute_actions(response, {}, ui, "D:\\winter",
                                    origin="user")
        self.assertEqual(apply.call_args.args[2], "network")
        self.assertEqual(apply.call_args.args[3], 'import bpy\nprint("ok")')

    def test_bridge_is_visible_persistent_and_saves_the_blend(self):
        source = blender_projects._BRIDGE_SOURCE
        self.assertIn("bpy.app.timers.register", source)
        self.assertIn("save_as_mainfile", source)
        self.assertIn('"vanta_source"', source)
        self.assertNotIn("--background", source)

    def test_submit_reports_a_dead_blender_as_a_crash_not_a_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / ".vanta_blender"
            paths = {
                "root": root, "runtime": runtime,
                "commands": runtime / "commands", "results": runtime / "results",
                "ready": runtime / "ready.json", "active": runtime / "active.json",
                "log": runtime / "blender.log",
            }
            paths["commands"].mkdir(parents=True)
            paths["results"].mkdir(parents=True)
            paths["active"].write_text(json.dumps({
                "id": "old", "operation": "apply", "code_path": "0001.py"
            }), encoding="utf-8")
            paths["log"].write_text("GPU backend stopped", encoding="utf-8")
            with mock.patch.object(blender_projects, "_process_alive", return_value=False):
                result = blender_projects._submit(
                    paths, "apply", "0001.py", timeout=30, bridge_pid=42)
            self.assertFalse(result["ok"])
            self.assertTrue(result["crashed"])
            self.assertIn("GPU backend stopped", result["error"])
            self.assertNotIn("may still be working", result["error"])

    def test_crashed_revision_is_preserved_and_not_claimed_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(blender_projects, "_ensure_bridge",
                                   return_value=({"pid": 77}, False)), \
                 mock.patch.object(blender_projects, "_submit",
                                   return_value={"ok": False, "crashed": True,
                                                 "error": "Blender process 77 exited"}):
                receipt = blender_projects.apply(folder, "crashed", VALID_CODE)
            root = Path(projects.project_path(folder, "crashed"))
            assert (root / blender_projects.RUNTIME_DIR / "revisions" /
                    "0001.failed.py").exists()
            self.assertIn("will not be run again automatically", receipt)


if __name__ == "__main__":
    unittest.main()
