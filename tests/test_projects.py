# -*- coding: utf-8 -*-
"""
Tests for the project layer behind the Code page.

The bulk of this is containment. Every path here arrives from a language model
that is being asked to change files, so "stays inside the project" is the one
property that has to hold whatever it emits -- and a check that passes for
`../x` but not for a symlink, or for `../x` but not for `a/../../x`, is not a
check. The rest covers the refusals that make an edit trustworthy: no blind
overwrite, no ambiguous replacement, and a recoverable copy of whatever was
there before.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import projects


class ContainmentTests(unittest.TestCase):
    """Nothing may reach outside vault/projects/<project>."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_proj_")
        projects.create(self.base, "demo", "a project")
        projects.write_file(self.base, "demo", "src/main.py", "X = 1\n")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_parent_traversal_is_refused(self):
        for bad in ("../secret.txt", "../../agent.py", "src/../../../x.py",
                    "a/b/../../../../../etc/passwd", "..\\..\\agent.py",
                    "src/./../../outside.py"):
            with self.assertRaises(projects.ProjectError, msg=bad):
                projects.resolve(self.base, "demo", bad)

    def test_an_absolute_path_is_taken_as_project_relative(self):
        # Stripping the leading separator is what keeps "/etc/passwd" inside
        # the project instead of reaching the real one.
        _root, target = projects.resolve(self.base, "demo", "/etc/passwd")
        self.assertTrue(target.startswith(projects.project_path(self.base, "demo")))

    def test_a_drive_qualified_path_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows-specific spelling")
        with self.assertRaises(projects.ProjectError):
            projects.resolve(self.base, "demo", r"C:\Windows\System32\drivers\etc\hosts")

    def test_a_symlink_out_of_the_tree_is_refused(self):
        root = projects.project_path(self.base, "demo")
        outside = os.path.join(self.base, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret\n")
        link = os.path.join(root, "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks need privilege on this machine")
        # Resolution happens before the containment check precisely so that a
        # link pointing out of the tree is caught rather than followed.
        with self.assertRaises(projects.ProjectError):
            projects.resolve(self.base, "demo", "link.txt")

    def test_project_names_cannot_escape_either(self):
        for bad in ("../..", "../other", "..", "/", "a/b", "C:\\Windows"):
            with self.assertRaises(projects.ProjectError, msg=bad):
                projects.project_path(self.base, bad)

    def test_an_empty_path_is_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.resolve(self.base, "demo", "")

    def test_a_legitimate_nested_path_is_allowed(self):
        _root, target = projects.resolve(self.base, "demo", "src/deep/file.py")
        self.assertIn("src", target)


class FileOperationTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_proj_")
        projects.create(self.base, "demo", "a project")
        projects.write_file(self.base, "demo", "src/main.py",
                            "def go():\n    return 1\n")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_writing_then_reading_round_trips(self):
        text, meta = projects.read_file(self.base, "demo", "src/main.py")
        self.assertIn("def go():", text)
        self.assertFalse(meta["truncated"])

    def test_an_existing_file_is_not_overwritten_silently(self):
        with self.assertRaises(projects.ProjectError) as caught:
            projects.write_file(self.base, "demo", "src/main.py", "X = 2\n")
        self.assertIn("overwrite", str(caught.exception))

    def test_overwrite_works_when_asked_for(self):
        projects.write_file(self.base, "demo", "src/main.py", "X = 2\n",
                            overwrite=True)
        self.assertIn("X = 2",
                      projects.read_file(self.base, "demo", "src/main.py")[0])

    def test_an_edit_that_matches_nothing_says_so(self):
        with self.assertRaises(projects.ProjectError) as caught:
            projects.edit_file(self.base, "demo", "src/main.py", "not here", "x")
        self.assertIn("does not appear", str(caught.exception))

    def test_an_ambiguous_edit_is_refused_rather_than_guessed(self):
        projects.write_file(self.base, "demo", "dup.py", "a = 1\na = 1\n")
        with self.assertRaises(projects.ProjectError) as caught:
            projects.edit_file(self.base, "demo", "dup.py", "a = 1", "a = 2")
        self.assertIn("ambiguous", str(caught.exception))

    def test_all_replaces_every_occurrence_when_asked(self):
        projects.write_file(self.base, "demo", "dup.py", "a = 1\na = 1\n")
        projects.edit_file(self.base, "demo", "dup.py", "a = 1", "a = 2", count="all")
        self.assertNotIn("a = 1",
                         projects.read_file(self.base, "demo", "dup.py")[0])

    def test_every_edit_leaves_a_recoverable_copy_outside_the_project(self):
        projects.edit_file(self.base, "demo", "src/main.py", "return 1", "return 2")
        backups = os.path.join(self.base, "vault", "project-backups", "demo")
        self.assertTrue(os.path.isdir(backups))
        self.assertTrue(os.listdir(backups))
        # And it must not be inside the project, at any nesting depth, or the
        # backup becomes another source file the model reads back.
        listed = [row["path"] for row in projects.file_rows(self.base, "demo")]
        self.assertNotIn("src/.vanta-backups", listed)
        self.assertFalse(any("backup" in path for path in listed), listed)

    def test_deleting_asks_first(self):
        result = projects.delete_file(self.base, "demo", "src/main.py")
        self.assertTrue(result.startswith("[Approval Required]"))
        self.assertTrue(os.path.exists(
            os.path.join(projects.project_path(self.base, "demo"), "src", "main.py")))

    def test_a_confirmed_delete_removes_it(self):
        projects.delete_file(self.base, "demo", "src/main.py", confirmed=True)
        self.assertEqual([row["path"] for row in projects.file_rows(self.base, "demo")],
                         [])

    def test_binary_files_are_reported_not_dumped(self):
        root = projects.project_path(self.base, "demo")
        with open(os.path.join(root, "blob.dat"), "wb") as handle:
            handle.write(b"\x00\x01\x02" * 400)
        with self.assertRaises(projects.ProjectError) as caught:
            projects.read_file(self.base, "demo", "blob.dat")
        self.assertIn("binary", str(caught.exception))

    def test_reading_a_missing_file_says_which(self):
        with self.assertRaises(projects.ProjectError) as caught:
            projects.read_file(self.base, "demo", "nope.py")
        self.assertIn("nope.py", str(caught.exception))

    def test_an_oversized_write_is_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.write_file(self.base, "demo", "huge.txt",
                                "x" * (projects.MAX_WRITE_BYTES + 10))


class ListingTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_proj_")
        projects.create(self.base, "demo", "a project")
        projects.write_file(self.base, "demo", "src/main.py", "X = 1\n")
        projects.write_file(self.base, "demo", "README.md", "# demo\n")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_generated_directories_are_not_listed(self):
        root = projects.project_path(self.base, "demo")
        for noise in ("__pycache__", "node_modules", ".git", ".venv"):
            os.makedirs(os.path.join(root, noise), exist_ok=True)
            with open(os.path.join(root, noise, "junk.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("x = 1\n")
        listed = [row["path"] for row in projects.file_rows(self.base, "demo")]
        for noise in ("__pycache__", "node_modules", ".git", ".venv"):
            self.assertFalse(any(noise in path for path in listed), noise)

    def test_compiled_artefacts_are_not_listed(self):
        root = projects.project_path(self.base, "demo")
        for name in ("thing.pyc", "lib.dll", "photo.png"):
            with open(os.path.join(root, name), "wb") as handle:
                handle.write(b"x")
        listed = [row["path"] for row in projects.file_rows(self.base, "demo")]
        self.assertEqual(sorted(listed), ["README.md", "src/main.py"])

    def test_the_tree_names_the_folders_its_files_live_in(self):
        tree = projects.tree(self.base, "demo")
        self.assertIn("src/", tree)
        self.assertIn("main.py", tree)

    def test_the_project_metadata_file_is_hidden_from_listings(self):
        listed = [row["path"] for row in projects.file_rows(self.base, "demo")]
        self.assertNotIn(".vanta-project.json", listed)

    def test_search_reports_file_and_line(self):
        hits = projects.search(self.base, "demo", "X = 1")
        self.assertTrue(hits)
        self.assertIn("src/main.py:1", hits[0])

    def test_an_invalid_search_pattern_is_explained(self):
        with self.assertRaises(projects.ProjectError) as caught:
            projects.search(self.base, "demo", "[unclosed")
        self.assertIn("not a valid search pattern", str(caught.exception))

    def test_find_files_matches_on_name_or_path(self):
        self.assertIn("src/main.py",
                      projects.find_files(self.base, "demo", "*.py"))
        self.assertIn("src/main.py",
                      projects.find_files(self.base, "demo", "main.py"))

    def test_the_open_project_is_remembered(self):
        self.assertEqual(projects.get_active(self.base), "demo")

    def test_an_open_project_that_was_deleted_reads_as_none(self):
        shutil.rmtree(projects.project_path(self.base, "demo"))
        self.assertEqual(projects.get_active(self.base), "")

    def test_the_context_block_lists_the_files_the_coder_can_touch(self):
        block = projects.context_block(self.base)
        self.assertIn("OPEN PROJECT: demo", block)
        self.assertIn("src/main.py", block)
        self.assertIn("relative to the project root", block)

    def test_no_open_project_means_no_context_block(self):
        projects.set_active(self.base, "")
        self.assertEqual(projects.context_block(self.base), "")

    def test_creating_a_duplicate_is_refused(self):
        self.assertTrue(projects.create(self.base, "demo", "").startswith("[Error]"))

    def test_unique_spoken_partial_name_opens_inside_vault(self):
        result = projects.open_project(self.base, "dem")
        self.assertTrue(result.startswith("[Success]"))
        self.assertIn("demo", result)

    def test_ambiguous_partial_name_is_refused(self):
        projects.create(self.base, "demo-two", "another project")
        result = projects.open_project(self.base, "dem")
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("more than one vault project", result)

    def test_punctuation_normalised_name_resolves_in_vault(self):
        projects.create(self.base, "invoice-parser", "")
        self.assertEqual(
            projects.match_name(self.base, "Invoice Parser"),
            "invoice-parser")


class CoderModelTests(unittest.TestCase):
    """Build mode has to actually select a different model."""

    def test_build_mode_asks_for_a_coding_model(self):
        import mode_manager
        policy = mode_manager.POLICIES[mode_manager.WinterMode.BUILD]
        self.assertTrue(policy.model_default, "build mode names no model")
        self.assertTrue(policy.model_env, "the model must be overridable in .env")

    def test_modes_without_a_preference_return_nothing(self):
        import mode_manager
        policy = mode_manager.POLICIES[mode_manager.WinterMode.ASSISTANT]
        self.assertEqual(policy.model_default, "")

    def test_a_mode_model_never_reaches_the_vision_stage(self):
        # Build mode asks for a coding model, and a coding model has no image
        # support: the gateway answered 404 on every screen-aware turn until
        # the vision stage was made immune to the mode's preference.
        import llm_router
        import mode_manager

        manager = mode_manager.get_mode_manager()
        previous = manager.active_mode().value
        try:
            manager.switch("build", source="test")
            coder = mode_manager.mode_model()
            self.assertTrue(coder, "build mode stopped naming a model")
            chosen = llm_router._model_for_provider(
                llm_router.active_provider, "vision")
            self.assertNotEqual(chosen, coder,
                                "the coder model was handed the screenshots")
            # ChatGPT OAuth has its own model namespace. An AIRouter build-mode
            # model must never leak into it; the provider's supported model is
            # safer than a guaranteed 400. AIRouter still receives the coder.
            planner_model = llm_router._model_for_provider(
                llm_router.active_provider, "planner")
            if llm_router.active_provider == "chatgpt":
                self.assertNotEqual(planner_model, coder)
                self.assertTrue(planner_model.startswith("gpt-"))
            else:
                # An explicit stage model is more specific than the mode's
                # general coding model. This preserves the configured AIRouter
                # planner while the vision assertion above remains the subject
                # of this test.
                expected = (os.getenv("WINTER_MODEL_PLANNER", "").strip()
                            or coder)
                self.assertEqual(planner_model, expected)
        finally:
            manager.switch(previous, source="test")

    def test_the_vision_stage_is_declared_not_guessed(self):
        import llm_router
        self.assertIn("vision", llm_router.VISION_STAGES)

    def test_fallback_is_opt_in(self):
        # Empty by default: the alternatives were doing more harm than good,
        # and a silent swap to a rate-limited provider is worse than an error.
        import os

        import llm_router
        saved = os.environ.pop("WINTER_FALLBACK_PROVIDERS", None)
        try:
            self.assertEqual(llm_router.fallback_providers(), [])
            os.environ["WINTER_FALLBACK_PROVIDERS"] = "groq, gemini"
            self.assertEqual(llm_router.fallback_providers(), ["groq", "gemini"])
        finally:
            os.environ.pop("WINTER_FALLBACK_PROVIDERS", None)
            if saved is not None:
                os.environ["WINTER_FALLBACK_PROVIDERS"] = saved

    def test_configured_provider_has_code_owned_continuity_fallback(self):
        import os

        import llm_router
        # Candidates are skipped when a provider has no client, so the clients
        # have to exist before this means anything.
        llm_router.init_clients()
        saved = os.environ.pop("WINTER_FALLBACK_PROVIDERS", None)
        try:
            names = [provider for provider, _client, _model
                     in llm_router._candidates("routine")]
            if not names:
                self.skipTest("no provider is configured in this environment")
            self.assertEqual(names[0], llm_router.active_provider)
            if llm_router.active_provider == "chatgpt" and llm_router.airouter_client:
                self.assertEqual(names[:2], ["chatgpt", "airouter"])
            elif (llm_router.active_provider == "airouter"
                  and llm_router.chatgpt_client):
                self.assertEqual(names[:2], ["airouter", "chatgpt"])
            else:
                self.assertEqual(names, [llm_router.active_provider])
        finally:
            if saved is not None:
                os.environ["WINTER_FALLBACK_PROVIDERS"] = saved

    def test_a_fallback_provider_keeps_its_own_model(self):
        # An AIRouter model id handed to Groq fails outright, so a mode
        # preference must not follow the request onto a fallback provider.
        import llm_router
        for provider, env_key, default in llm_router.FALLBACK_CHAIN:
            if provider == llm_router.active_provider:
                continue
            self.assertEqual(llm_router._model_for_provider(provider, "routine"),
                             os.getenv(env_key, "").strip() or default)


if __name__ == "__main__":
    unittest.main()
