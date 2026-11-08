# -*- coding: utf-8 -*-
"""
The integrity matrix, for every action rather than just the camera.

A capability needs six things to be usable: documentation the model can read,
an entry in the searchable registry built from that documentation, an input
schema, an authorization decision, a mode classification, and something that
runs. Six files own those, none of them compared notes, and CAPTURE_CAMERA had
two of the six for as long as it took someone to ask what VANTA could see.

These tests exist so the next gap is a failing test rather than a false claim
about hardware. They assert properties of the matrix, not a frozen inventory:
pinning the exact set of actions would turn every new capability into a test
edit, which is the kind of friction that gets tests deleted.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capabilities
import integrity

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(BASE_DIR, name), encoding="utf-8",
              errors="replace") as handle:
        return handle.read()


class LiveMatrix(unittest.TestCase):
    """Run against the real modules, the way it runs at startup."""

    @classmethod
    def setUpClass(cls):
        cls.problems = integrity.check_live()
        cls.kinds = integrity.by_kind(cls.problems)

    def test_no_executor_without_authorization(self):
        """Something that can run but is refused by default-deny is dead code."""
        self.assertEqual(
            [str(p) for p in self.kinds.get(integrity.MISSING_AUTHORIZATION, [])],
            [])

    def test_no_mode_policy_reference_is_unknown(self):
        self.assertEqual(
            [str(p) for p in self.kinds.get(integrity.UNKNOWN_MODE_REFERENCE, [])],
            [])

    def test_no_duplicate_type_attributes(self):
        """Two type attributes mean the documented name is not the dispatched one."""
        self.assertEqual(
            [str(p) for p in self.kinds.get(integrity.DUPLICATE_DEFINITION, [])],
            [])

    def test_every_declared_handler_resolves(self):
        self.assertEqual(
            [str(p) for p in self.kinds.get(integrity.HANDLER_UNRESOLVABLE, [])],
            [])

    def test_nothing_authorized_is_entirely_unwired(self):
        """The camera's exact shape: authorized, classified, and nowhere else."""
        self.assertEqual(
            [str(p) for p in
             self.kinds.get(integrity.EXECUTOR_WITHOUT_REGISTRATION, [])],
            [])

    def test_declared_capabilities_are_fully_wired(self):
        """Every declared capability clears every check, with no exceptions."""
        declared = capabilities.names()
        offenders = sorted({p.action for p in self.problems
                            if p.action in declared})
        self.assertEqual(offenders, [])

    def test_camera_is_no_longer_undiscoverable(self):
        undiscoverable = {p.action for p in
                          self.kinds.get(integrity.UNDISCOVERABLE, [])}
        self.assertNotIn("CAPTURE_CAMERA", undiscoverable)
        self.assertNotIn("ZOOM", undiscoverable)

    def test_every_executable_action_is_discoverable(self):
        """The runtime registry must expose every non-retired executor."""
        undiscoverable = self.kinds.get(integrity.UNDISCOVERABLE, [])
        self.assertEqual(
            [str(p) for p in undiscoverable], [],
            "capabilities became undiscoverable")

    def test_every_action_has_an_input_schema(self):
        missing = self.kinds.get(integrity.MISSING_SCHEMA, [])
        self.assertEqual(
            [str(p) for p in missing], [],
            "actions bypass input validation")


class MatrixMechanics(unittest.TestCase):
    """The checker itself, on inputs where the right answer is known."""

    def test_detects_the_camera_shape(self):
        """Authorized and mode-classified but documented nowhere."""
        problems = integrity.check(
            catalogs={"X": '<ACTION type="READ_FILE" path="a" />'},
            actions_source="action_type == 'READ_FILE'",
            mode_source="_OBSERVATION_ACTIONS = frozenset({'SEE_THING'})",
            schema_names={"READ_FILE", "SEE_THING"},
            authorized_names={"READ_FILE", "SEE_THING"})
        kinds = {p.kind for p in problems if p.action == "SEE_THING"}
        self.assertIn(integrity.EXECUTOR_WITHOUT_REGISTRATION, kinds)

    def test_detects_executor_without_authorization(self):
        problems = integrity.check(
            catalogs={"X": '<ACTION type="DO_IT" />'},
            actions_source="action_type == 'DO_IT'",
            mode_source="", schema_names={"DO_IT"}, authorized_names=set())
        self.assertIn(integrity.MISSING_AUTHORIZATION,
                      {p.kind for p in problems})

    def test_detects_registration_without_executor(self):
        problems = integrity.check(
            catalogs={"X": '<ACTION type="GHOST" />'},
            actions_source="", mode_source="",
            schema_names={"GHOST"}, authorized_names={"GHOST"})
        self.assertIn(integrity.REGISTRATION_WITHOUT_EXECUTOR,
                      {p.kind for p in problems})

    def test_detects_undiscoverable(self):
        problems = integrity.check(
            catalogs={}, actions_source="action_type == 'HIDDEN'",
            mode_source="", schema_names={"HIDDEN"},
            authorized_names={"HIDDEN"})
        self.assertIn(integrity.UNDISCOVERABLE, {p.kind for p in problems})

    def test_detects_missing_schema(self):
        problems = integrity.check(
            catalogs={"X": '<ACTION type="RAW" />'},
            actions_source="action_type == 'RAW'", mode_source="",
            schema_names=set(), authorized_names={"RAW"})
        self.assertIn(integrity.MISSING_SCHEMA, {p.kind for p in problems})

    def test_detects_unknown_mode_reference(self):
        problems = integrity.check(
            catalogs={}, actions_source="",
            mode_source="_CODE_ACTIONS = frozenset({'MYSTERY'})",
            schema_names=set(), authorized_names=set())
        self.assertIn(integrity.UNKNOWN_MODE_REFERENCE,
                      {p.kind for p in problems})

    def test_detects_real_duplicate_type_attribute(self):
        problems = integrity.check(
            catalogs={"X": '<ACTION type="A" type="B" />'},
            actions_source="", mode_source="", schema_names=set(),
            authorized_names=set())
        self.assertIn(integrity.DUPLICATE_DEFINITION,
                      {p.kind for p in problems})

    def test_qualified_type_attributes_are_not_duplicates(self):
        """control_type= and search_type= are not a second type attribute.

        A regex without a word boundary matches their tails and reports healthy
        entries as broken. That false alarm nearly sent a fix to GITHUB_SEARCH,
        which was already correct.
        """
        self.assertEqual(integrity.duplicate_type_attributes(
            {"X": '<ACTION type="CLICK" control_type="Button" />'
                  '<ACTION type="GITHUB_SEARCH" search_type="code" />'}), [])

    def test_nested_example_tags_are_not_capabilities(self):
        """A tag inside another tag's body is a worked example."""
        names = integrity.documented_names({
            "SELF": '<ACTION type="CREATE_ACTION" name="WORD_COUNT">\n'
                    'example: <ACTION type="WORD_COUNT" path="a.md" />\n'
                    '</ACTION>'})
        self.assertIn("CREATE_ACTION", names)
        self.assertNotIn("WORD_COUNT", names)

    def test_aliases_are_not_reported_as_drift(self):
        """MOUSE_MOVE normalises before the policy check; it is not a gap."""
        problems = integrity.check(
            catalogs={}, actions_source="action_type == 'MOUSE_MOVE'",
            mode_source="", schema_names=set(), authorized_names=set())
        self.assertEqual([p for p in problems if p.action == "MOUSE_MOVE"], [])

    def test_declared_capability_counts_as_executable(self):
        """It runs from the agent loop, not the dispatcher. Still runnable."""
        problems = integrity.check(
            catalogs={"C": '<ACTION type="CAPTURE_CAMERA" />'},
            actions_source="", mode_source="",
            schema_names={"CAPTURE_CAMERA"},
            authorized_names={"CAPTURE_CAMERA"},
            declared=capabilities.CAPABILITIES)
        self.assertEqual([str(p) for p in problems
                          if p.action == "CAPTURE_CAMERA"], [])

    def test_clean_input_yields_no_problems(self):
        self.assertEqual(integrity.check(
            catalogs={"X": '<ACTION type="FINE" />'},
            actions_source="action_type == 'FINE'", mode_source="",
            schema_names={"FINE"}, authorized_names={"FINE"}), [])

    def test_summary_says_so_when_clean(self):
        self.assertIn("consistent", integrity.summary([]))


class ExecutableExtraction(unittest.TestCase):
    """All four dispatch paths count, or three look like missing executors."""

    def test_equality_chain(self):
        self.assertIn("A_THING", integrity.executable_names(
            "if action_type == 'A_THING':"))

    def test_membership_tuple(self):
        self.assertIn("B_THING", integrity.executable_names(
            "if action_type in ('B_THING', 'C_THING'):"))

    def test_post_loop_set(self):
        self.assertIn("SET_ALARM", integrity.executable_names(
            'POST_LOOP_ACTIONS = frozenset({"SET_ALARM"})'))

    def test_body_tag_alternation(self):
        """WRITE_FILE dispatches only through this regex."""
        self.assertIn("WRITE_FILE", integrity.executable_names(
            r'r\'<ACTION\s+([^>]*\btype="(?:SAVE_FILE|WRITE_FILE)"[^>]*)>\''))

    def test_live_source_finds_all_four(self):
        names = integrity.executable_names(_read("actions.py"))
        for expected in ("SET_ALARM", "WRITE_FILE", "READ_FILE", "OPEN_URL"):
            with self.subTest(action=expected):
                self.assertIn(expected, names)


class StartupReport(unittest.TestCase):
    def test_running_agent_script_is_reused_without_importing_it_again(self):
        fake_main = types.SimpleNamespace(
            __file__=os.path.join(BASE_DIR, "agent.py"),
            ACTION_CATALOGS=("already running",),
        )
        with mock.patch.dict(sys.modules, {"__main__": fake_main}):
            with mock.patch("importlib.import_module") as import_module:
                selected = integrity._active_agent_module()
        self.assertIs(selected, fake_main)
        import_module.assert_not_called()

    def test_never_raises(self):
        """Boot must not depend on the registries being tidy."""
        self.assertIsInstance(integrity.report_at_startup(), list)


if __name__ == "__main__":
    unittest.main()
