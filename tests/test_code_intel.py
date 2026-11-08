# -*- coding: utf-8 -*-
"""Structural understanding of code, checked against code we control.

A coding model is only as good as what it is shown, and the two options
before this were to paste whole files into the prompt or to guess from
filenames. The first spends the context window on code irrelevant to the
change; the second edits code it has not read. Both produce the same failure:
a confident change to the wrong place.

These tests use a small fixture project rather than VANTA itself, so they
assert exact answers instead of "roughly this many symbols" -- a test that
only checks a count passes whatever the parser gets wrong.
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_intel

FIXTURE = {
    "alarms.py": '''
        """Alarm handling."""
        import datetime
        from memory.state_db import execute_write


        def set_alarm(time_str, label, recurrence="none"):
            """Set one alarm."""
            normalised = normalise_time(time_str)
            execute_write("INSERT", (normalised, label))
            return normalised


        def normalise_time(value):
            """Turn a spoken time into HH:MM."""
            return value.strip()


        class AlarmStore:
            """Where alarms live."""

            def __init__(self, path):
                self.path = path

            def add(self, alarm):
                """Add one."""
                return set_alarm(alarm.time, alarm.label)

            async def flush(self):
                return None
    ''',
    "ui/panel.py": '''
        import alarms


        def render(store):
            return store.add(None)
    ''',
    "broken.py": "def oops(:\n    pass\n",
    "notes.txt": "not python at all",
}


def build_fixture(root):
    for relative, body in FIXTURE.items():
        path = os.path.join(root, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(body).lstrip())
    # A directory that must never be indexed, containing something that would
    # dominate the map if it were.
    vendored = os.path.join(root, ".venv", "lib")
    os.makedirs(vendored, exist_ok=True)
    with open(os.path.join(vendored, "huge.py"), "w", encoding="utf-8") as h:
        h.write("def vendored_thing():\n    pass\n")


class Parsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.root = cls._dir.name
        build_fixture(cls.root)
        cls.modules = code_intel.index(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_only_python_files_are_indexed(self):
        self.assertIn("alarms.py", self.modules)
        self.assertIn("ui/panel.py", self.modules)
        self.assertNotIn("notes.txt", self.modules)

    def test_vendored_directories_are_skipped(self):
        """A virtualenv is larger than the project and none of it is ours."""
        self.assertFalse([path for path in self.modules if ".venv" in path])

    def test_functions_classes_and_methods_are_distinguished(self):
        kinds = {symbol.qualified: symbol.kind
                 for symbol in self.modules["alarms.py"].symbols}
        self.assertEqual(kinds["set_alarm"], "function")
        self.assertEqual(kinds["AlarmStore"], "class")
        self.assertEqual(kinds["AlarmStore.add"], "method")

    def test_a_line_range_covers_the_whole_definition(self):
        """The range is the point: it is what lets an edit replace a function."""
        symbol = code_intel.find_symbol(self.modules, "set_alarm")[0]
        body = code_intel.read_span(symbol.path, symbol.line, symbol.end_line)
        self.assertIn("def set_alarm", body)
        self.assertIn("return normalised", body)
        self.assertNotIn("def normalise_time", body,
                         "the range ran past the end of the function")

    def test_signatures_record_parameters_without_evaluating_defaults(self):
        symbol = code_intel.find_symbol(self.modules, "set_alarm")[0]
        self.assertIn("time_str", symbol.signature)
        self.assertIn("recurrence=...", symbol.signature)

    def test_the_first_docstring_line_is_kept(self):
        symbol = code_intel.find_symbol(self.modules, "normalise_time")[0]
        self.assertEqual(symbol.doc, "Turn a spoken time into HH:MM.")

    def test_async_definitions_are_marked(self):
        symbol = code_intel.find_symbol(self.modules, "AlarmStore.flush")[0]
        self.assertTrue(symbol.is_async)
        self.assertIn("async def", symbol.header())

    def test_a_file_that_does_not_parse_is_reported_not_hidden(self):
        """Usually it is the file being worked on."""
        module = self.modules["broken.py"]
        self.assertFalse(module.ok)
        self.assertIn("syntax error", module.error)

    def test_an_unparsed_file_appears_in_the_map(self):
        text = code_intel.repo_map(self.modules)
        self.assertIn("broken.py", text)


class Relationships(Parsing):
    """What "related code" actually means for a change."""

    def test_callers_are_found_by_the_call_not_the_word(self):
        callers = code_intel.callers_of(self.modules, "set_alarm")
        names = {symbol.qualified for symbol in callers}
        self.assertIn("AlarmStore.add", names)

    def test_a_definition_is_not_a_caller_of_itself(self):
        callers = code_intel.callers_of(self.modules, "set_alarm")
        self.assertNotIn("set_alarm", {s.qualified for s in callers})

    def test_importers_are_found(self):
        self.assertEqual(code_intel.importers_of(self.modules, "alarms"),
                         ["ui/panel.py"])

    def test_a_dotted_import_is_attributed_to_its_module(self):
        self.assertIn("alarms.py",
                      code_intel.importers_of(self.modules, "memory.state_db"))

    def test_an_unknown_name_yields_nothing(self):
        self.assertEqual(code_intel.callers_of(self.modules, "no_such_fn"), [])
        self.assertEqual(code_intel.importers_of(self.modules, ""), [])


class TheMapAndTheContext(Parsing):
    def test_the_map_names_files_and_line_numbers(self):
        text = code_intel.repo_map(self.modules)
        self.assertIn("alarms.py", text)
        self.assertIn("set_alarm", text)
        # A line number is what turns the map into something actionable.
        self.assertRegex(text, r"\d+\s+def set_alarm")

    def test_the_map_respects_its_character_budget(self):
        """It shares a context window with the actual work."""
        text = code_intel.repo_map(self.modules, max_chars=200)
        self.assertLessEqual(len(text), 260)
        self.assertIn("truncated", text)

    def test_context_returns_the_relevant_body_not_whole_files(self):
        text = code_intel.context_for(self.root, self.modules,
                                      "normalise the alarm time")
        self.assertIn("def normalise_time", text)
        self.assertIn("alarms.py", text)

    def test_context_prefers_a_name_match_over_a_mention(self):
        text = code_intel.context_for(self.root, self.modules,
                                      "normalise_time", limit=1)
        self.assertIn("def normalise_time", text)

    def test_context_respects_its_budget(self):
        text = code_intel.context_for(self.root, self.modules, "alarm",
                                      budget_chars=120)
        self.assertLessEqual(len(text), 400)

    def test_an_empty_request_returns_nothing(self):
        self.assertEqual(
            code_intel.context_for(self.root, self.modules, ""), "")

    def test_stats_report_what_could_not_be_read(self):
        numbers = code_intel.stats(self.modules)
        self.assertEqual(numbers["unparsed"], 1, numbers)
        self.assertGreater(numbers["symbols"], 5)


class AgainstTheRealProject(unittest.TestCase):
    """It has to work at the size it will actually be used at."""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.modules = code_intel.index(cls.root)

    def test_the_project_parses(self):
        numbers = code_intel.stats(self.modules)
        self.assertGreater(numbers["symbols"], 1000)
        self.assertEqual(numbers["unparsed"], 0,
                         "a project file stopped parsing")

    def test_a_known_function_is_located_exactly(self):
        symbol = code_intel.find_symbol(self.modules, "normalise_shape")[0]
        body = code_intel.read_span(symbol.path, symbol.line, symbol.end_line)
        self.assertIn("def normalise_shape", body)

    def test_known_call_sites_are_found(self):
        """It used to be execute_actions; the body now lives in the handler
        the action bus dispatches to, which is the answer that is useful."""
        names = {symbol.qualified
                 for symbol in code_intel.callers_of(self.modules,
                                                     "normalise_shape")}
        self.assertIn("_run_draw_on_screen", names)
        self.assertIn("WinterUI._draw_annotation", names)

    def test_the_map_fits_a_prompt(self):
        text = code_intel.repo_map(self.modules, max_chars=12000)
        self.assertLessEqual(len(text), 12100)


if __name__ == "__main__":
    unittest.main()
