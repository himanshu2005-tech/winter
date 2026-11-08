import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry


class ActionRegistryTests(unittest.TestCase):
    def test_retrieves_relevant_actions_not_the_whole_catalog(self):
        catalogs = {
            "WORKSPACE": '''
              <ACTION type="READ_EMAILS" query="is:unread" />
                - Read matching Gmail messages.
              <ACTION type="ADD_CALENDAR_EVENT" summary="Exam" />
                - Add an event to Google Calendar.
            ''',
            "MACHINE": '''
              <ACTION type="SET_BRIGHTNESS" level="70" />
                - Change display brightness.
            ''',
            "DISCOVERY": '''
              <ACTION type="DISCOVER_ACTIONS" query="missing capability" />
                - Search the full action registry.
            ''',
        }
        action_registry.configure(catalogs)
        found = action_registry.search(
            ".", "read my unread email",
            allowed_catalogs={"WORKSPACE", "DISCOVERY"}, limit=2)
        names = [entry["name"] for entry in found]
        self.assertEqual(names[0], "READ_EMAILS")
        self.assertIn("DISCOVER_ACTIONS", names)
        self.assertNotIn("SET_BRIGHTNESS", names)

    def test_prompt_size_stays_bounded_with_thousands_of_actions(self):
        catalog = "\n".join(
            f'<ACTION type="TOOL_{i}" value="x" /> - capability number {i}'
            for i in range(2000)
        )
        discovery = (
            '<ACTION type="DISCOVER_ACTIONS" query="missing capability" /> '
            '- search all capabilities'
        )
        action_registry.configure({"SYSTEM": catalog, "DISCOVERY": discovery})
        found = action_registry.search(
            ".", "capability number 1777",
            allowed_catalogs={"SYSTEM", "DISCOVERY"}, limit=12)
        self.assertLessEqual(len(found), 13)
        self.assertLess(len(action_registry.render(found)), 15000)
        self.assertIn("TOOL_1777", [entry["name"] for entry in found])

    def test_explicit_action_name_gets_priority(self):
        action_registry.configure({
            "SYSTEM": '<ACTION type="READ_FILE" path="x" /> - read a file',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" /> - discover',
        })
        found = action_registry.search(
            ".", "The plan explicitly requires READ_FILE",
            allowed_catalogs={"SYSTEM", "DISCOVERY"}, limit=1)
        self.assertEqual(found[0]["name"], "READ_FILE")

    def test_self_closing_action_is_not_swallowed_by_later_block_action(self):
        action_registry.configure({
            "WORKSPACE": '''
              <ACTION type="READ_EMAILS" query="is:unread" />
              <ACTION type="SEND_EMAIL" to="x" subject="y">body</ACTION>
            ''',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        names = {entry["name"] for entry in action_registry._entries}
        self.assertIn("READ_EMAILS", names)
        self.assertIn("SEND_EMAIL", names)

    def test_natural_language_finds_compound_action_names(self):
        action_registry.configure({
            "SYSTEM": '''
              <ACTION type="WATCH_LIST" />
              <ACTION type="WATCH_STATUS" id="watch-x" />
            ''',
            "BROWSER": '''
              <ACTION type="BROWSER_GOTO" url="https://example.com" />
              <ACTION type="BROWSER_SNAPSHOT" />
            ''',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        watches = action_registry.search(
            ".", "list active price watches and their current status",
            {"SYSTEM", "DISCOVERY"}, 4)
        self.assertEqual({x["name"] for x in watches[:2]},
                         {"WATCH_STATUS", "WATCH_LIST"})
        browser = action_registry.search(
            ".", "open a URL in the browser and read the page",
            {"BROWSER", "DISCOVERY"}, 4)
        names = [x["name"] for x in browser[:3]]
        self.assertIn("BROWSER_GOTO", names)
        self.assertIn("BROWSER_SNAPSHOT", names)

    def test_drive_contents_routes_to_directory_listing(self):
        action_registry.configure({
            "SYSTEM": '''
              <ACTION type="LIST_DIRECTORY" path="D:\\" limit="200" />
                - List an exact local directory.
              <ACTION type="FIND_FILES" pattern="resume" />
                - Find a file whose name is not fully known.
              <ACTION type="RUN_COMMAND" cmd="Get-ChildItem" />
                - Run a PowerShell command.
            ''',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        found = action_registry.search(
            ".", "what folders and files are on my d drive",
            {"SYSTEM", "DISCOVERY"}, 3)
        self.assertEqual(found[0]["name"], "LIST_DIRECTORY")

    def test_shell_primitive_is_visible_before_a_second_discovery_turn(self):
        """Unsupported Windows queries can use the registered shell directly."""
        distractors = "\n".join(
            f'<ACTION type="OTHER_{i}" value="x" /> - wifi network profile {i}'
            for i in range(20)
        )
        action_registry.configure({
            "SYSTEM": distractors + '''
              <ACTION type="RUN_COMMAND" cmd="Get-Process" />
                - Run a PowerShell command for operating-system work.
            ''',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        found = action_registry.search(
            ".", "list the saved wifi profiles on this computer",
            allowed_catalogs=None, limit=12)
        names = [entry["name"] for entry in found]
        self.assertIn("RUN_COMMAND", names)
        self.assertIn("DISCOVER_ACTIONS", names)

    def test_discovery_returns_executable_syntax_not_only_a_name(self):
        action_registry.configure({
            "SYSTEM": '<ACTION type="LIST_DIRECTORY" path="D:\\" limit="200" />',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        result = action_registry.discover_text(
            ".", "show folders on a drive", limit=3)
        self.assertIn("CALL:", result)
        self.assertIn('<ACTION type="LIST_DIRECTORY"', result)
        self.assertIn('path="D:\\"', result)

    def test_microsoft_form_request_surfaces_managed_browser_workflow(self):
        action_registry.configure({
            "BROWSER": '''
              <ACTION type="BROWSER_START" />
              <ACTION type="BROWSER_GOTO" url="https://..." />
              <ACTION type="BROWSER_SNAPSHOT" />
              <ACTION type="BROWSER_CLICK" ref="e1" />
              <ACTION type="BROWSER_TYPE" ref="e2" text="..." />
              <ACTION type="BROWSER_FILL_FORM" fields="e2=value" />
              <ACTION type="BROWSER_SUBMIT" ref="e3" />
            ''',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        found = action_registry.search(
            ".", "open chrome and create a Microsoft Form with Name and Email fields",
            allowed_catalogs=None, limit=12)
        names = {entry["name"] for entry in found}
        self.assertTrue({"BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
                         "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_FILL_FORM",
                         "BROWSER_SUBMIT"}.issubset(names))

    def test_semantic_index_rescues_a_true_paraphrase(self):
        action_registry.configure({
            "BROWSER": '<ACTION type="BROWSER_FILL_FORM" fields="x=y" />',
            "WORKSPACE": '<ACTION type="READ_EMAILS" query="x" />',
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })

        def documents(values):
            return [[1.0, 0.0] if "BROWSER FILL FORM" in value
                    else [0.0, 1.0] for value in values]

        with patch("memory.embeddings.embed_texts", side_effect=documents), \
                patch("memory.embeddings.embed_text", return_value=[1.0, 0.0]):
            self.assertTrue(action_registry.warm_semantic_index(background=False))
            found = action_registry.search(
                ".", "collect responses online", allowed_catalogs=None, limit=1)
        selected = next(item for item in found
                        if item["name"] == "BROWSER_FILL_FORM")
        # The workflow bundle may place prerequisites before the semantic seed.
        # Presence proves that the paraphrase recovered the capability.
        self.assertIn("BROWSER_FILL_FORM", [item["name"] for item in found])
        self.assertTrue(selected.get("semantic_score", 1.0) >= 0.99
                        or "workflow companion" in selected.get("selection_reason", ""))

    def test_multi_outcome_retrieval_does_not_let_one_goal_hide_another(self):
        action_registry.configure({
            "WORKSPACE": '''
              <ACTION type="UPDATE_CALENDAR_EVENT" event_id="x" start_time="y" />
                - Change an existing Google Calendar event.
              <ACTION type="ADD_CALENDAR_EVENT" summary="x" start_time="y" />
                - Create a Google Calendar event.
              <ACTION type="ADD_TASK" title="x" /> - Create a task.
            ''',
            "CODE": "\n".join(
                f'<ACTION type="PROJECT_TOOL_{i}" value="x" /> - project build code'
                for i in range(20)),
            "DISCOVERY": '<ACTION type="DISCOVER_ACTIONS" query="x" />',
        })
        found = action_registry.search_outcomes(
            ".",
            ["correct the existing review to one PM",
             "remind me at four thirty by putting it on my schedule",
             "start my new software project"],
            limit=8)
        names = [item["name"] for item in found]
        self.assertIn("UPDATE_CALENDAR_EVENT", names)
        self.assertIn("ADD_CALENDAR_EVENT", names)
        self.assertEqual(len(names), len(set(names)))

    def test_capability_denial_is_detected_for_runtime_recovery(self):
        self.assertTrue(action_registry.is_capability_denial(
            "The calendar actions aren't currently available to me."))
        self.assertTrue(action_registry.is_capability_denial(
            "Screen capture isn't available right now."))
        self.assertTrue(action_registry.is_capability_denial(
            "Screen capture is not configured."))
        self.assertFalse(action_registry.is_capability_denial(
            "I need the event date before I can update it."))


if __name__ == "__main__":
    unittest.main()
