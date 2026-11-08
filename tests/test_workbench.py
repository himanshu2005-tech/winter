# -*- coding: utf-8 -*-
"""The coding surface: what it promises and what it refuses.

Most of this file is about the two ways a surface like this destroys work.

The first is reloading a buffer that has unsaved edits. VANTA writes to the
project constantly while implementing, and every write publishes
"project_changed". If that reloads the editor unconditionally, the half-typed
function someone was in the middle of is gone, with no undo, because the
reload resets the undo stack too.

The second is saving a file the editor only partly read. `projects.read_file`
caps a read at 200KB and says so; writing that view back deletes everything
after the cap. The editor cannot tell by looking -- the text ends, the way text
does.

The widget layer is not exercised here. There is no display in CI and a test
that needs one gets skipped, which is the same as not being written. What is
tested is the logic that was extracted out of the widgets for exactly that
reason, plus the contract the rest of the system holds the surface to: the
action, the capability declaration and the receipt.
"""

import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capabilities
import code_intel
import workbench


class TheCapabilityIsDeclaredProperly(unittest.TestCase):
    """The surface is reachable the same way every other capability is."""

    @classmethod
    def setUpClass(cls):
        # The registry is configured by the running system, not on import, so
        # a test that searches it has to configure it the same way -- exactly
        # as the retrieval corpus does.
        import action_registry
        import agent
        catalogs = dict(agent.ACTION_CATALOGS)
        catalogs.update(capabilities.catalog_overlay())
        action_registry.configure(catalogs)
        action_registry.warm_semantic_index(background=False)

    def setUp(self):
        self.capability = capabilities.get("CODING_MODE")

    def test_it_exists(self):
        self.assertIsNotNone(self.capability,
                             "CODING_MODE is not declared, so nothing can "
                             "find it -- the CAPTURE_CAMERA failure again")

    def test_its_handler_resolves(self):
        """A handler named as a string is only real if it imports."""
        self.assertIs(self.capability.resolve_handler(),
                      workbench.open_request)

    def test_it_is_authorized_and_schema_checked(self):
        import action_schema
        import autonomy
        self.assertIn("CODING_MODE", autonomy.KNOWN_ACTIONS)
        self.assertIn("CODING_MODE", action_schema.ACTION_SPECS)

    def test_it_is_in_the_catalogue_the_registry_reads(self):
        """The camera was authorized and undiscoverable. Not again."""
        import agent
        text = "\n".join(agent.ACTION_CATALOGS.values())
        self.assertIn("CODING_MODE", text)

    def test_it_is_retrievable_by_what_someone_would_say(self):
        import action_registry
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for phrase in ("open my project", "activate coding mode",
                       "let's write some code", "work on the codebase"):
            with self.subTest(phrase=phrase):
                names = [row["name"] for row in
                         action_registry.search_outcomes(
                             base, [phrase], None, 12, "user")]
                self.assertIn("CODING_MODE", names,
                              f"{phrase!r} did not reach the coding surface")

    def test_it_never_opens_in_sleep(self):
        """A full-screen editor at 3am is the most intrusive thing here."""
        self.assertNotIn("sleep", self.capability.modes)

    def test_only_a_person_may_open_it(self):
        self.assertEqual(self.capability.origins, ("user",))


class OpeningItEntersBuildMode(unittest.TestCase):
    """Otherwise it is the right editor in front of the wrong model.

    Build mode is what makes a turn read WINTER_MODEL_CODER, which is the
    only reason coding runs on the coding model at all.
    """

    def test_build_mode_reads_the_coder_model_setting(self):
        import mode_manager
        policy = mode_manager.POLICIES[mode_manager.WinterMode.BUILD]
        self.assertEqual(policy.model_env, "WINTER_MODEL_CODER")

    def test_the_dispatcher_switches_into_it(self):
        import action_bus
        branch = action_bus.BUS.handler_source("CODING_MODE")
        self.assertTrue(branch, "CODING_MODE has no handler on the bus")
        self.assertIn('switch(', branch)
        self.assertIn('"build"', branch)

    def test_it_only_switches_when_the_surface_actually_opened(self):
        """A failed open that still changed the mode is a mode nobody asked
        for, with no editor to explain it."""
        import action_bus
        branch = action_bus.BUS.handler_source("CODING_MODE")
        self.assertLess(branch.index('res.startswith("[Success]")'),
                        branch.index('switch('))


class TheReceiptIsReadBack(unittest.TestCase):
    """"I queued a message" and "the surface is up" are different facts."""

    class Queue:
        def __init__(self):
            self.messages = []

        def put(self, item):
            self.messages.append(item)

    def setUp(self):
        self.queue = self.Queue()
        workbench._set_state(open=False, project="", files=0)
        self.addCleanup(workbench._set_state, open=False, project="", files=0)

    def test_a_surface_that_comes_up_reports_what_it_opened(self):
        workbench._set_state(open=True, project="portfolio", files=11)
        result = workbench.open_request(self.queue, timeout=0.5)
        self.assertIn("[Success]", result)
        self.assertIn("portfolio", result)
        self.assertIn("11 files", result)

    def test_a_surface_that_never_comes_up_is_a_failure(self):
        """No display, or a wedged UI thread. Not a success either way."""
        result = workbench.open_request(self.queue, timeout=0.2)
        self.assertIn("[Error]", result)
        self.assertNotIn("[Success]", result)

    def test_the_request_reaches_the_ui_queue(self):
        workbench.open_request(self.queue, timeout=0.05)
        self.assertEqual(self.queue.messages[0][0], "workbench_open")

    def test_a_project_that_does_not_exist_is_refused_before_opening(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "vault", "projects", "real"))
            result = workbench.open_request(self.queue, project="imaginary",
                                            base_dir=base, timeout=0.05)
            self.assertIn("[Error]", result)
            self.assertEqual(self.queue.messages, [],
                             "it asked the UI to open a project that is not "
                             "there")

    def test_a_unique_spoken_near_match_is_resolved_before_ui_open(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "vault", "projects", "portfolio"))
            workbench.open_request(self.queue, project="portfol",
                                   base_dir=base, timeout=0.05)
            self.assertEqual(self.queue.messages[0],
                             ("workbench_open", "portfolio"))

    def test_the_state_snapshot_is_readable_from_another_thread(self):
        """The model may ask what is open; it cannot touch a widget."""
        import threading
        seen = []
        workbench._set_state(open=True, project="alpha")
        threading.Thread(target=lambda: seen.append(
            workbench.workbench_state())).start()
        time.sleep(0.2)
        self.assertEqual(seen[0]["project"], "alpha")

    def test_the_snapshot_is_a_copy(self):
        """A caller that mutates what it reads must not change the truth."""
        snapshot = workbench.workbench_state()
        snapshot["project"] = "tampered"
        self.assertNotEqual(workbench.workbench_state()["project"], "tampered")


SOURCE = textwrap.dedent('''
    """A module."""
    import os


    def plain(value):
        """Do the thing."""
        return value


    class Holder:
        """Holds."""

        def method(self):
            return 1

        def other(self):
            return 2
''').lstrip()


class TheOutlineComesFromTheAst(unittest.TestCase):
    def setUp(self):
        self.module = code_intel.parse_module("holder.py", source=SOURCE)

    def test_definitions_are_listed_in_source_order(self):
        rows = workbench.outline_rows(self.module)
        self.assertEqual([row["label"].strip().split()[-1] for row in rows],
                         ["plain", "Holder", "method", "other"])

    def test_methods_are_indented_under_their_class(self):
        rows = workbench.outline_rows(self.module)
        method = [row for row in rows if "method" in row["label"]][0]
        self.assertTrue(method["label"].startswith("    "))

    def test_every_row_carries_the_line_to_jump_to(self):
        for row in workbench.outline_rows(self.module):
            self.assertGreater(row["line"], 0)

    def test_a_file_that_does_not_parse_says_so_rather_than_emptying(self):
        """An empty outline reads as "this file has no code in it"."""
        broken = code_intel.parse_module("x.py", source="def oops(:\n  pass\n")
        rows = workbench.outline_rows(broken)
        self.assertEqual(len(rows), 1)
        self.assertIn("syntax error", rows[0]["label"])

    def test_no_module_means_no_rows(self):
        self.assertEqual(workbench.outline_rows(None), [])

    def test_a_file_with_no_definitions_says_so(self):
        """Empty and broken look identical, and only one needs acting on."""
        bare = code_intel.parse_module("x.py", source="import os\nprint(1)\n")
        rows = workbench.outline_rows(bare)
        self.assertEqual([row["kind"] for row in rows], ["empty"])


class WhatTheRequestPointsAt(unittest.TestCase):
    """The context line in the agent pane -- the surface's own honesty."""

    def setUp(self):
        module = code_intel.parse_module("holder.py", source=SOURCE)
        self.modules = {"holder.py": module}

    def test_a_named_definition_is_reported_with_its_line(self):
        shown = workbench.relevant_symbols(self.modules, "fix plain please")
        self.assertTrue(shown[0].startswith("plain:"), shown)

    def test_nothing_matching_reports_nothing_rather_than_a_guess(self):
        self.assertEqual(
            workbench.relevant_symbols(self.modules, "buy milk tomorrow"), [])

    def test_an_empty_request_matches_nothing(self):
        self.assertEqual(workbench.relevant_symbols(self.modules, ""), [])

    def test_the_same_definition_is_not_listed_twice(self):
        """A generated copy under a build directory defines it again."""
        modules = dict(self.modules)
        modules["build/copy/holder.py"] = code_intel.parse_module(
            "build/copy/holder.py", source=SOURCE)
        shown = workbench.relevant_symbols(modules, "plain")
        self.assertEqual(len(shown), len(set(shown)), shown)

    def test_it_is_bounded(self):
        shown = workbench.relevant_symbols(
            self.modules, "plain holder method other", limit=2)
        self.assertEqual(len(shown), 2)


class Highlighting(unittest.TestCase):
    """Positions, not colours: an off-by-one paints the quote and not the
    string, and that is invisible in a screenshot."""

    def test_python_keywords_strings_and_comments(self):
        spans = workbench.highlight_spans(
            'def go():\n    return "hi"  # note\n', "a.py")
        self.assertIn(("kw", "1.0", "1.3"), spans)
        self.assertIn(("def", "1.4", "1.6"), spans)
        self.assertIn(("str", "2.11", "2.15"), spans)
        self.assertIn(("com", "2.17", "2.23"), spans)

    def test_a_multiline_string_is_one_span(self):
        """The reason Python goes through the tokeniser at all."""
        spans = workbench.highlight_spans('x = """a\nb\nc"""\n', "a.py")
        self.assertIn(("str", "1.4", "3.4"), spans)

    def test_a_python_file_mid_edit_still_gets_colour(self):
        """It does not tokenise, and it is the file being worked on."""
        spans = workbench.highlight_spans("def oops(:\n    return 1\n", "a.py")
        self.assertTrue(spans, "all colour was lost on an unbalanced bracket")

    def test_javascript(self):
        spans = workbench.highlight_spans(
            'const x = 1; // note\nfunction go() { return "hi"; }\n', "a.js")
        self.assertIn(("kw", "1.0", "1.5"), spans)
        self.assertIn(("com", "1.13", "1.20"), spans)
        self.assertIn(("str", "2.23", "2.27"), spans)

    def test_a_keyword_inside_a_string_is_not_highlighted_twice(self):
        spans = workbench.highlight_spans('const a = "return";\n', "a.js")
        self.assertEqual([span for span in spans if span[0] == "kw"],
                         [("kw", "1.0", "1.5")])

    def test_a_hash_comment_on_the_first_line_is_found(self):
        """Config files start with one, and an unanchored pattern misses it."""
        spans = workbench.highlight_spans("# top\nkey: 1\n", "a.yml")
        self.assertIn(("com", "1.0", "1.5"), spans)

    def test_it_runs_on_a_real_file_of_real_size(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "code_intel.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        started = time.time()
        spans = workbench.highlight_spans(source, "code_intel.py")
        elapsed = time.time() - started
        self.assertGreater(len(spans), 100)
        self.assertLess(elapsed, 1.0,
                        "too slow to run while someone is typing")

    def test_empty_text_is_not_an_error(self):
        self.assertEqual(workbench.highlight_spans("", "a.py"), [])


class WhereAmI(unittest.TestCase):
    """The status bar's answer, which is innermost-wins."""

    def setUp(self):
        self.module = code_intel.parse_module("holder.py", source=SOURCE)

    def test_inside_a_function(self):
        self.assertEqual(workbench.enclosing_symbol(self.module, 7), "plain")

    def test_inside_a_method_the_answer_is_the_method_not_the_class(self):
        line = [symbol.line for symbol in self.module.symbols
                if symbol.name == "method"][0]
        self.assertEqual(workbench.enclosing_symbol(self.module, line),
                         "Holder.method")

    def test_between_definitions_there_is_no_answer(self):
        self.assertEqual(workbench.enclosing_symbol(self.module, 2), "")

    def test_an_unparsed_module_answers_nothing_rather_than_guessing(self):
        broken = code_intel.parse_module("x.py", source="def oops(:\n")
        self.assertEqual(workbench.enclosing_symbol(broken, 1), "")


class WhatTheEditorWillOpen(unittest.TestCase):
    def test_source_and_prose_are_text(self):
        for name in ("app.py", "src/main.ts", "notes.md", "config.yml",
                     "README", "Dockerfile", "deep/dir/style.css"):
            with self.subTest(name=name):
                self.assertTrue(workbench.is_text_file(name))

    def test_binaries_are_not(self):
        for name in ("logo.png", "data.sqlite", "lib.dll", "clip.mp4",
                     "archive.zip"):
            with self.subTest(name=name):
                self.assertFalse(workbench.is_text_file(name))


class TheStructuralMapGoesIntoThePrompt(unittest.TestCase):
    """What the coder model is actually shown about the project."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        with open(os.path.join(self.root, "app.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(SOURCE)
        code_intel._MAP_CACHE.clear()

    def test_it_names_definitions_with_their_lines(self):
        block = code_intel.structure_block(self.root)
        self.assertIn("app.py", block)
        self.assertIn("plain", block)
        self.assertRegex(block, r"\d+\s+def plain")

    def test_it_says_the_numbers_are_line_numbers(self):
        """A bare column of integers means nothing to a reader."""
        self.assertIn("line", code_intel.structure_block(self.root).lower())

    def test_a_project_with_no_python_gets_no_heading(self):
        """A heading with nothing under it reads as "there is no code here"."""
        with tempfile.TemporaryDirectory() as empty:
            with open(os.path.join(empty, "notes.md"), "w") as handle:
                handle.write("# hello")
            self.assertEqual(code_intel.structure_block(empty), "")

    def test_it_respects_its_budget(self):
        block = code_intel.structure_block(self.root, max_chars=200)
        self.assertLessEqual(len(block), 500)

    def test_an_edit_invalidates_the_cache(self):
        """A cached map of the file as it was is worse than no map."""
        first = code_intel.structure_block(self.root)
        self.assertNotIn("brand_new", first)
        time.sleep(0.01)
        with open(os.path.join(self.root, "app.py"), "a",
                  encoding="utf-8") as handle:
            handle.write("\n\ndef brand_new():\n    return 3\n")
        self.assertIn("brand_new", code_intel.structure_block(self.root))

    def test_an_unchanged_project_is_not_reparsed(self):
        code_intel.structure_block(self.root)
        with patch.object(code_intel, "index",
                          side_effect=AssertionError("re-parsed")):
            code_intel.structure_block(self.root)

    def test_the_open_project_context_carries_it(self):
        import projects
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        project_dir = os.path.join(base, "vault", "projects", "alpha")
        os.makedirs(project_dir)
        with open(os.path.join(project_dir, "app.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(SOURCE)
        projects.set_active(base, "alpha")
        code_intel._MAP_CACHE.clear()
        block = projects.context_block(base)
        self.assertIn("app.py", block)
        self.assertIn("def plain", block,
                      "the coder is still being shown only filenames")

    def test_a_structure_failure_does_not_lose_the_file_list(self):
        """The list is the part the file actions need; it must survive."""
        import projects
        base = tempfile.mkdtemp()
        project_dir = os.path.join(base, "vault", "projects", "alpha")
        os.makedirs(project_dir)
        with open(os.path.join(project_dir, "app.py"), "w") as handle:
            handle.write(SOURCE)
        projects.set_active(base, "alpha")
        with patch.object(code_intel, "structure_block",
                          side_effect=RuntimeError("boom")):
            block = projects.context_block(base)
        self.assertIn("app.py", block)
        self.assertIn("OPEN PROJECT", block)


class TheSurfaceIsWiredIntoTheUi(unittest.TestCase):
    """Without this the module exists and nothing ever calls it.

    Asserted against the source because the alternative is constructing a
    WinterUI, which needs a display. Narrow on purpose: each assertion is one
    message the surface cannot work without, not the shape of the code around
    it.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui.py"), encoding="utf-8") as handle:
            cls.ui = handle.read()

    def test_the_mixin_is_mounted(self):
        self.assertIn("WorkbenchMixin", self.ui)

    def test_every_message_the_surface_sends_itself_is_handled(self):
        for message in ("workbench_open", "workbench_close",
                        "workbench_index"):
            with self.subTest(message=message):
                self.assertIn(f'"{message}"', self.ui)

    def test_opening_a_project_raises_the_surface(self):
        """What the request was: "open project" gives a new full screen."""
        self.assertRegex(self.ui, r'project_opened[\s\S]{0,400}_wb_open')

    def test_the_agent_pane_is_fed_by_the_real_streams(self):
        for hook in ("_wb_note_response", "_wb_note_tool"):
            with self.subTest(hook=hook):
                self.assertIn(hook, self.ui)

    def test_standby_takes_the_surface_down(self):
        self.assertRegex(self.ui, r'"standby"[\s\S]{0,900}_wb_hide')

    def test_the_model_is_told_what_is_on_screen(self):
        """Asked "what am I looking at", it cannot read a widget."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("workbench_state()", source)
        self.assertIn("CODING SURFACE", source)


class TheMixinRefusesToDestroyWork(unittest.TestCase):
    """The two ways an editor loses someone's typing.

    Exercised against the mixin's own methods with the widgets stubbed, so
    the decisions are tested without a display.
    """

    class Surface(workbench.WorkbenchMixin):
        """Just enough of WinterUI to run the decisions."""

        def __init__(self):
            self._wb_win = object()
            self._wb_project = "alpha"
            self._wb_file = "app.py"
            self._wb_original = "before"
            self._wb_dirty = False
            self._wb_truncated = False
            self._wb_rows = ["app.py"]
            self._wb_indexing = False
            self._wb_modules = {}
            self.said = []
            self.reloaded = 0
            self.written = []

        # -- stubs for everything that would need a widget --------------- #
        def _wb_exists(self):
            return True

        def _wb_say(self, tag, text):
            self.said.append((tag, text))

        def _wb_reload(self):
            self.reloaded += 1

        def _wb_fill_files(self):
            pass

        def _wb_refresh_status(self):
            pass

        def _wb_refresh_outline(self):
            pass

        def _wb_start_index(self):
            pass

    def setUp(self):
        self.surface = self.Surface()

    def test_a_dirty_buffer_is_never_reloaded_when_vanta_writes(self):
        self.surface._wb_dirty = True
        with patch("projects.file_rows", return_value=[{"path": "app.py"}]):
            self.surface._wb_project_changed("alpha")
        self.assertEqual(self.surface.reloaded, 0,
                         "an unsaved edit was discarded because VANTA touched "
                         "the project")
        self.assertTrue(any("not reloading" in text
                            for _tag, text in self.surface.said),
                        "it silently did not reload; say so")

    def test_a_clean_buffer_is_reloaded_so_you_see_what_vanta_did(self):
        with patch("projects.file_rows", return_value=[{"path": "app.py"}]):
            self.surface._wb_project_changed("alpha")
        self.assertEqual(self.surface.reloaded, 1)

    def test_a_change_to_another_project_is_ignored(self):
        with patch("projects.file_rows", return_value=[{"path": "app.py"}]):
            self.surface._wb_project_changed("something-else")
        self.assertEqual(self.surface.reloaded, 0)

    def test_saving_a_truncated_view_is_refused(self):
        """Writing back the first 200KB of a larger file deletes the rest."""
        self.surface._wb_truncated = True
        with patch("projects.write_file") as write:
            self.surface._wb_save()
        write.assert_not_called()
        self.assertTrue(any(tag == "bad" for tag, _text in self.surface.said))

    def test_switching_files_with_unsaved_edits_is_refused_not_silent(self):
        self.surface._wb_dirty = True
        with patch("projects.read_file") as read:
            self.surface._wb_open_file("other.py")
        read.assert_not_called()
        self.assertTrue(any("unsaved" in text
                            for _tag, text in self.surface.said))

    def test_an_instruction_is_not_sent_over_a_buffer_that_would_not_save(self):
        """Otherwise the model edits the file on disk, which is not the file
        on screen, and one then overwrites the other."""
        self.surface._wb_dirty = True
        self.surface._wb_truncated = True
        sent = []
        self.surface._in_q = type("Q", (), {"put": lambda _s, item:
                                            sent.append(item)})()
        self.surface._wb_instruction = type(
            "T", (), {"get": lambda _s, *_a: "add a retry",
                      "delete": lambda _s, *_a: None})()
        self.surface._wb_show_context = lambda _text: None
        self.surface._wb_agent_note = type(
            "L", (), {"configure": lambda _s, **_k: None})()
        self.surface._wb_implement()
        self.assertEqual(sent, [])
        self.assertTrue(any(tag == "bad" for tag, _text in self.surface.said))

    def test_a_binary_file_is_declined_rather_than_decoded(self):
        with patch("projects.read_file") as read:
            self.surface._wb_open_file("logo.png")
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
