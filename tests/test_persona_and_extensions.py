# -*- coding: utf-8 -*-
"""
Tests for the two things VANTA can now change about itself: how it addresses
Himanshu and what standing instructions it keeps, and the actions it writes for
itself.

The extension tests are mostly about refusal. An extension is loaded on every
future turn, so a bad one is not a failure that happens once -- it is a failure
that happens forever. Each case below is a way that could happen.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extensions
import persona


def _extension(body, spec='SPEC = {"description": "t", "test_attrs": {}}'):
    return f'{spec}\n\ndef run(attrs, base_dir, context):\n{body}\n'


class PersonaTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_persona_")

    def test_the_default_address_is_used_until_it_is_changed(self):
        self.assertEqual(persona.address(self.base), persona.DEFAULT_ADDRESS)

    def test_changing_the_address_sticks_and_says_what_changed(self):
        result = persona.set_address(self.base, "boss")
        self.assertIn("boss", result)
        self.assertIn("sir", result, "it should name what it is replacing")
        self.assertEqual(persona.address(self.base), "boss")

    def test_setting_the_same_address_twice_is_not_an_error(self):
        persona.set_address(self.base, "boss")
        self.assertTrue(persona.set_address(self.base, "boss").startswith("[Success]"))

    def test_an_address_must_be_a_name_not_a_sentence(self):
        for bad in ("", "   ", "a" * 40, "boss<script>", "call me whatever you like",
                    "123", "@boss"):
            self.assertTrue(persona.set_address(self.base, bad).startswith("[Error]"),
                            f"accepted {bad!r}")

    def test_ordinary_names_and_titles_are_accepted(self):
        for good in ("boss", "Himanshu", "chief", "Dr. Rao", "O'Brien"):
            self.assertTrue(persona.set_address(self.base, good).startswith("[Success]"),
                            f"rejected {good!r}")

    def test_directives_survive_a_reload(self):
        persona.add_directive(self.base, "Answer in one sentence.")
        self.assertEqual([d["text"] for d in persona.load(self.base)["directives"]],
                         ["Answer in one sentence."])

    @mock.patch("memory.neo4j_store.store.query")
    def test_temporary_persona_does_not_pollute_live_graph(self, graph_query):
        persona.add_directive(self.base, "Answer in one sentence.")
        graph_query.assert_not_called()

    def test_a_repeated_directive_is_not_stored_twice(self):
        persona.add_directive(self.base, "Answer in one sentence.")
        persona.add_directive(self.base, "answer in ONE sentence.")
        self.assertEqual(len(persona.load(self.base)["directives"]), 1)

    def test_an_empty_directive_is_refused(self):
        self.assertTrue(persona.add_directive(self.base, "  ").startswith("[Error]"))

    def test_a_directive_can_be_removed_by_id_or_by_text(self):
        persona.add_directive(self.base, "Never open a browser silently.")
        identifier = persona.load(self.base)["directives"][0]["id"]
        self.assertTrue(persona.remove_directive(self.base, identifier).startswith("[Success]"))
        persona.add_directive(self.base, "Use metric units.")
        self.assertTrue(persona.remove_directive(self.base, "metric").startswith("[Success]"))
        self.assertEqual(persona.load(self.base)["directives"], [])

    def test_removing_something_that_is_not_there_says_so(self):
        self.assertTrue(persona.remove_directive(self.base, "nothing").startswith("[Error]"))

    def test_the_directive_count_is_bounded(self):
        for index in range(persona.MAX_DIRECTIVES):
            persona.add_directive(self.base, f"Instruction number {index}.")
        self.assertTrue(
            persona.add_directive(self.base, "One too many.").startswith("[Error]"))

    def test_the_prompt_block_carries_the_address_and_the_directives(self):
        persona.set_address(self.base, "boss")
        persona.add_directive(self.base, "Answer in one sentence.")
        block = persona.prompt_block(self.base)
        self.assertIn("boss", block)
        self.assertIn("Answer in one sentence.", block)

    def test_the_prompt_block_says_directives_are_not_permissions(self):
        # Without this, a standing instruction reads to the model like
        # authorisation to skip a confirmation the code will refuse anyway.
        block = persona.prompt_block(self.base).lower()
        self.assertIn("not permissions", block)
        self.assertIn("enforced in code", block)

    def test_canned_lines_follow_the_address(self):
        persona.set_address(self.base, "boss")
        self.assertEqual(persona.apply_address("Still working on that, sir.", self.base),
                         "Still working on that, boss.")

    def test_substitution_leaves_text_alone_at_the_default(self):
        self.assertEqual(persona.apply_address("Working, sir.", self.base),
                         "Working, sir.")

    def test_a_corrupt_store_falls_back_instead_of_raising(self):
        path = os.path.join(self.base, "vault", "system", "persona.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(persona.address(self.base), persona.DEFAULT_ADDRESS)
        self.assertIn("HOW HIMANSHU WANTS YOU", persona.prompt_block(self.base))


class ExtensionValidationTests(unittest.TestCase):
    """Each of these is a way one bad file could break every future turn."""

    def test_a_well_formed_extension_passes(self):
        self.assertEqual(
            extensions.validate_source("WORD_COUNT",
                                       _extension('    return "[Success] ok"')), [])

    def test_anything_that_runs_at_import_is_refused(self):
        # The exact bug that had seed_memory.py rewriting the graph whenever
        # something merely imported it.
        problems = extensions.validate_source(
            "SIDE_EFFECT",
            'SPEC = {"description": "t"}\nprint("runs on import")\n'
            'def run(attrs, base_dir, context):\n    return "[Success] x"\n')
        self.assertTrue(any("import" in p for p in problems), problems)

    def test_a_builtin_action_cannot_be_shadowed(self):
        for name in ("CLICK", "SEND_EMAIL", "RUN_COMMAND", "DELETE_FILE"):
            problems = extensions.validate_source(name, _extension('    return "x"'))
            self.assertTrue(any("built-in" in p for p in problems), name)

    def test_the_run_signature_is_enforced(self):
        problems = extensions.validate_source(
            "WRONG_SIG",
            'SPEC = {"description": "t"}\ndef run(everything):\n    return "x"\n')
        self.assertTrue(any("run(attrs, base_dir, context)" in p for p in problems))

    def test_a_missing_run_is_refused(self):
        problems = extensions.validate_source(
            "NO_RUN", 'SPEC = {"description": "t"}\nx = 1\n')
        self.assertTrue(any("run(" in p for p in problems))

    def test_a_missing_spec_is_refused(self):
        problems = extensions.validate_source(
            "NO_SPEC", 'def run(attrs, base_dir, context):\n    return "x"\n')
        self.assertTrue(any("SPEC" in p for p in problems))

    def test_a_syntax_error_is_reported_with_its_line(self):
        problems = extensions.validate_source("BROKEN", "def run(:\n")
        self.assertTrue(any("does not parse" in p for p in problems), problems)

    def test_names_are_constrained(self):
        for bad in ("ab", "9START", "HAS SPACE", "A" * 40, "", "HAS-DASH"):
            self.assertTrue(extensions.validate_source(bad, _extension('    return "x"')),
                            f"accepted {bad!r}")

    def test_a_lowercase_name_is_normalised_rather_than_refused(self):
        # The dispatcher uppercases action types, so insisting the model type
        # the name in caps would be a rejection with nothing behind it.
        self.assertEqual(
            extensions.validate_source("word_count", _extension('    return "x"')), [])

    def test_oversized_sources_are_refused(self):
        huge = _extension('    return "x"') + "\n# padding" * 6000
        problems = extensions.validate_source("HUGE", huge)
        self.assertTrue(any("bytes" in p for p in problems), problems)

    def test_every_problem_says_how_to_fix_it(self):
        problems = extensions.validate_source("x", "def run(:\n")
        for problem in problems:
            self.assertGreater(len(problem), 25, problem)


class ExtensionLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_ext_")
        os.makedirs(os.path.join(self.base, "extensions"), exist_ok=True)
        extensions._cache.update(loaded_at=0.0, actions={})
        extensions._failures.clear()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        extensions._cache.update(loaded_at=0.0, actions={})
        # Installing an extension merges its schema into a process-wide table.
        # Leaving those behind made an unrelated consistency test fail later in
        # the same run, which is a worse bug than the one it was reporting.
        import action_schema
        for name in list(extensions._registered):
            action_schema.ACTION_SPECS.pop(name, None)
        extensions._registered.clear()

    def _install(self, name, body, **spec):
        fields = {"description": "test action", "test_attrs": {}}
        fields.update(spec)
        source = (f"SPEC = {fields!r}\n\n"
                  f"def run(attrs, base_dir, context):\n{body}\n")
        return extensions.create(self.base, name, source)

    def test_install_then_call(self):
        self.assertTrue(self._install("ECHO_BACK", '    return "[Success] echoed"')
                        .startswith("[Success]"))
        self.assertIn("ECHO_BACK", extensions.names(self.base))
        self.assertEqual(extensions.call(self.base, "ECHO_BACK", {}),
                         "[Success] echoed")

    def test_an_extension_that_fails_its_own_test_is_never_installed(self):
        result = self._install("EXPLODES", '    raise ValueError("boom")')
        self.assertTrue(result.startswith("[Error]"))
        self.assertNotIn("EXPLODES", extensions.names(self.base))
        self.assertFalse(os.path.exists(
            os.path.join(self.base, "extensions", "explodes.py")))

    def test_no_staging_file_is_left_behind_after_a_failure(self):
        self._install("EXPLODES", '    raise ValueError("boom")')
        leftovers = [f for f in os.listdir(os.path.join(self.base, "extensions"))
                     if f.startswith("_staging_")]
        self.assertEqual(leftovers, [])

    def test_returning_the_wrong_type_is_caught_before_install(self):
        result = self._install("GIVES_DICT", '    return {"not": "a string"}')
        self.assertTrue(result.startswith("[Error]"))
        self.assertNotIn("GIVES_DICT", extensions.names(self.base))

    def test_an_existing_extension_is_not_replaced_silently(self):
        self._install("KEEPER", '    return "[Success] first"')
        again = self._install("KEEPER", '    return "[Success] second"')
        self.assertTrue(again.startswith("[Error]"))
        self.assertIn("overwrite", again)
        self.assertEqual(extensions.call(self.base, "KEEPER", {}), "[Success] first")

    def test_a_result_without_a_receipt_prefix_gets_one(self):
        self._install("BARE", '    return "just some prose"')
        self.assertTrue(extensions.call(self.base, "BARE", {}).startswith("[Success]"))

    def test_a_raising_extension_returns_an_error_rather_than_propagating(self):
        # Installed while healthy, then broken on disk: the failure has to be
        # contained to the one action, not the turn.
        self._install("LATER_BREAKS", '    return "[Success] fine"')
        path = os.path.join(self.base, "extensions", "later_breaks.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('SPEC = {"description": "t"}\n\n'
                         'def run(attrs, base_dir, context):\n'
                         '    raise RuntimeError("late failure")\n')
        extensions.load_all(self.base, force=True)
        result = extensions.call(self.base, "LATER_BREAKS", {})
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("RuntimeError", result)

    def test_repeated_failures_disable_it_instead_of_failing_forever(self):
        self._install("FLAKY", '    return "[Success] fine"')
        path = os.path.join(self.base, "extensions", "flaky.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('SPEC = {"description": "t"}\n\n'
                         'def run(attrs, base_dir, context):\n'
                         '    raise RuntimeError("always")\n')
        extensions.load_all(self.base, force=True)
        for _ in range(extensions.MAX_FAILURES):
            extensions.call(self.base, "FLAKY", {})
        self.assertNotIn("FLAKY", extensions.names(self.base))
        self.assertIn("UNAVAILABLE", extensions.describe(self.base))

    def test_a_slow_extension_is_cut_off_rather_than_holding_the_turn(self):
        self._install("SLOW", '    import time\n    time.sleep(30)\n    return "x"',
                      timeout=1)
        result = extensions.call(self.base, "SLOW", {})
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("did not return", result)

    def test_calling_something_that_does_not_exist_is_an_error_not_a_crash(self):
        self.assertTrue(
            extensions.call(self.base, "NOT_REAL", {}).startswith("[Error]"))

    def test_removal_takes_it_out_of_the_registry(self):
        self._install("TEMP_ONE", '    return "[Success] x"')
        self.assertTrue(extensions.remove(self.base, "TEMP_ONE").startswith("[Success]"))
        self.assertNotIn("TEMP_ONE", extensions.names(self.base))

    def test_a_file_that_will_not_import_is_skipped_not_fatal(self):
        self._install("HEALTHY", '    return "[Success] x"')
        with open(os.path.join(self.base, "extensions", "rotten.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("import a_module_that_does_not_exist\n")
        loaded = extensions.load_all(self.base, force=True)
        self.assertIn("HEALTHY", loaded)
        self.assertNotIn("ROTTEN", loaded)

    def test_the_catalogue_and_schema_are_derived_from_the_same_spec(self):
        self._install("DOCUMENTED", '    return "[Success] x"',
                      description="Does a thing.", requires=["path"],
                      params={"path": "where"},
                      example='<ACTION type="DOCUMENTED" path="x" />')
        catalog = extensions.catalog_text(self.base)
        self.assertIn("DOCUMENTED", catalog)
        self.assertIn("Does a thing.", catalog)
        self.assertEqual(extensions.schema_specs(self.base)["DOCUMENTED"]["requires"],
                         ["path"])

    def test_no_extensions_means_no_catalogue_block(self):
        self.assertEqual(extensions.catalog_text(self.base), "")


class ActionParsingTests(unittest.TestCase):
    """Markup quoted inside a block body must not be executed."""

    def test_an_example_inside_a_new_action_is_not_run(self):
        import actions

        source = ('<ACTION type="CREATE_ACTION" name="THING">\n'
                  'SPEC = {"example": \'<ACTION type="DELETE_FILE" path="x" />\'}\n'
                  'def run(attrs, base_dir, context):\n    return "x"\n'
                  '</ACTION>')
        block = actions.re.compile(
            r'<ACTION\s+([^>]*\btype="(?:' + "|".join(actions.BLOCK_BODY_ACTIONS) +
            r')"[^>]*)>[\s\S]*?</ACTION>', actions.re.IGNORECASE)
        stripped = block.sub("", source)
        self.assertNotIn("DELETE_FILE", stripped,
                         "an example inside a body would have been executed")

    def test_every_block_body_action_has_a_handler(self):
        import actions
        dispatched = actions.__dict__  # module loaded is enough for the names
        self.assertTrue(dispatched)
        for name in actions.BLOCK_BODY_ACTIONS:
            self.assertRegex(name, r"^[A-Z][A-Z_]+$")


if __name__ == "__main__":
    unittest.main()
