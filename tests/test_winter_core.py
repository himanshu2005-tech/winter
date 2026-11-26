# -*- coding: utf-8 -*-
"""Coverage for the modules added to VANTA's pipeline.

These test decisions, not integrations: schedule arithmetic, schema rejection,
verdict parsing, sandbox isolation, provenance phrasing. Nothing here touches
the live graph, the network, or the real vault -- a test suite that needs
Neo4j running is one nobody runs.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_schema
import codetools
import critic
import planner
import routines


class ActionSchemaTests(unittest.TestCase):
    """A wrong attribute must fail loudly, never fall through silently."""

    def test_missing_required_attribute_is_rejected(self):
        error = action_schema.validate("TYPE", {"type": "TYPE", "txt": "hello"})
        self.assertIsNotNone(error)
        self.assertIn("text", error)

    def test_any_of_group_accepts_either_form(self):
        self.assertIsNone(action_schema.validate("CLICK", {"name": "Save"}))
        self.assertIsNone(action_schema.validate("CLICK", {"x": "10", "y": "20"}))
        self.assertIsNotNone(action_schema.validate("CLICK", {}))

    def test_partial_any_of_group_is_rejected(self):
        # x without y cannot click anything.
        self.assertIsNotNone(action_schema.validate("CLICK", {"x": "10"}))

    def test_unknown_attribute_is_named(self):
        error = action_schema.validate("NOTIFY", {"text": "hi", "urgentt": "true"})
        self.assertIsNotNone(error)
        self.assertIn("urgentt", error)

    def test_body_satisfies_content_requirement(self):
        self.assertIsNone(
            action_schema.validate("WRITE_CODE", {"path": "a.py"}, has_body=True))

    def test_unlisted_action_passes_through(self):
        self.assertIsNone(action_schema.validate("LIST_WINDOWS", {}))

    def test_empty_attribute_counts_as_missing(self):
        self.assertIsNotNone(action_schema.validate("FOCUS_WINDOW", {"title": "   "}))


class RoutineScheduleTests(unittest.TestCase):
    def test_every_documented_schedule_form_parses(self):
        for text in ["daily 08:00", "weekdays 09:30", "weekends 11:00",
                     "weekly mon 18:00", "monthly 1 09:00", "every 45m", "hourly"]:
            spec, error = routines.parse_schedule(text)
            self.assertIsNone(error, f"{text} failed: {error}")
            self.assertIsInstance(routines.next_run(spec), datetime.datetime)

    def test_unparseable_schedule_explains_the_options(self):
        spec, error = routines.parse_schedule("sometime tomorrow")
        self.assertIsNone(spec)
        self.assertIn("daily 08:00", error)

    def test_invalid_clock_time_is_rejected(self):
        self.assertIsNone(routines.parse_schedule("daily 99:00")[0])

    def test_weekdays_never_lands_on_a_weekend(self):
        spec, _ = routines.parse_schedule("weekdays 08:00")
        # Start from a Friday evening; the next run must be Monday.
        friday = datetime.datetime(2026, 9, 25, 20, 0)
        self.assertLessEqual(routines.next_run(spec, friday).weekday(), 4)

    def test_weekends_never_lands_on_a_weekday(self):
        spec, _ = routines.parse_schedule("weekends 11:00")
        monday = datetime.datetime(2026, 9, 21, 12, 0)
        self.assertGreaterEqual(routines.next_run(spec, monday).weekday(), 5)

    def test_next_run_is_always_in_the_future(self):
        now = datetime.datetime(2026, 9, 21, 9, 0)
        for text in ["daily 08:00", "weekly mon 08:00", "hourly"]:
            spec, _ = routines.parse_schedule(text)
            self.assertGreater(routines.next_run(spec, now), now, text)


class RoutineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_add_list_pause_delete(self):
        self.assertIn("[Success]", routines.add(
            self.dir, "Brief", "Check the calendar", "weekdays 08:00"))
        self.assertIn("Brief", routines.listing(self.dir))
        self.assertIn("paused", routines.set_enabled(self.dir, "Brief", False))
        self.assertIn("[Success]", routines.remove(self.dir, "Brief"))
        self.assertIn("No routines", routines.listing(self.dir))

    def test_duplicate_name_is_refused(self):
        routines.add(self.dir, "Brief", "x", "daily 08:00")
        self.assertIn("[Error]", routines.add(self.dir, "Brief", "y", "daily 09:00"))

    def test_routine_without_instruction_is_refused(self):
        self.assertIn("[Error]", routines.add(self.dir, "Empty", "", "daily 08:00"))

    def test_due_routine_fires_once_then_reschedules(self):
        routines.add(self.dir, "Hydrate", "Drink water", "every 90m")
        items = routines._load(self.dir)
        items[0]["next_run"] = (datetime.datetime.now()
                                - datetime.timedelta(minutes=5)).isoformat()
        routines._save(self.dir, items)

        self.assertEqual(len(routines.due_now(self.dir)), 1)
        # A second poll in the same window must not fire it again.
        self.assertEqual(routines.due_now(self.dir), [])
        self.assertEqual(routines._load(self.dir)[0]["runs"], 1)

    def test_paused_routine_never_fires(self):
        routines.add(self.dir, "Paused", "x", "every 5m")
        routines.set_enabled(self.dir, "Paused", False)
        items = routines._load(self.dir)
        items[0]["next_run"] = "2000-01-01T00:00:00"
        routines._save(self.dir, items)
        self.assertEqual(routines.due_now(self.dir), [])

    @mock.patch("watches.dependency_state", return_value={
        "all_inactive": True,
        "states": {"watch-12345678": "cancelled"},
    })
    def test_routine_with_only_inactive_watch_dependencies_auto_pauses(self, dependency_state):
        routines.add(
            self.dir, "Price", "Run WATCH_CHECK for watch-12345678", "every 5m")
        items = routines._load(self.dir)
        items[0]["next_run"] = "2000-01-01T00:00:00"
        routines._save(self.dir, items)
        self.assertEqual(routines.due_now(self.dir), [])
        saved = routines._load(self.dir)[0]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["runs"], 0)
        self.assertIn("inactive", saved["disabled_reason"])
        dependency_state.assert_called_once_with(["watch-12345678"])

    @mock.patch("watches.dependency_state", side_effect=RuntimeError("neo4j offline"))
    def test_watch_dependency_outage_does_not_disable_routine(self, _dependency_state):
        routines.add(
            self.dir, "Price", "Run WATCH_CHECK for watch-12345678", "every 5m")
        items = routines._load(self.dir)
        items[0]["next_run"] = "2000-01-01T00:00:00"
        routines._save(self.dir, items)
        self.assertEqual(len(routines.due_now(self.dir)), 1)
        self.assertTrue(routines._load(self.dir)[0]["enabled"])

    def test_expired_routine_is_removed_without_firing(self):
        routines.add(self.dir, "Temporary", "x", "daily 08:00",
                     expires_on="2026-10-01")
        fired = routines.due_now(
            self.dir, now=datetime.datetime(2026, 10, 2, 9, 0))
        self.assertEqual(fired, [])
        self.assertEqual(routines._load(self.dir), [])

    def test_routine_expiry_is_visible_and_validated(self):
        self.assertIn("expires after 2026-10-12", routines.add(
            self.dir, "Temporary", "x", "daily 08:00",
            expires_on="2026-10-12"))
        self.assertIn("expires after 2026-10-12", routines.listing(self.dir))
        self.assertIn("[Error]", routines.add(
            self.dir, "Bad", "x", "daily 08:00", expires_on="12 October"))

    def test_missed_runs_do_not_stack_up(self):
        """A machine asleep for a week must not fire seven catch-up briefings."""
        routines.add(self.dir, "Daily", "x", "daily 08:00")
        items = routines._load(self.dir)
        items[0]["next_run"] = (datetime.datetime.now()
                                - datetime.timedelta(days=7)).isoformat()
        routines._save(self.dir, items)
        self.assertEqual(len(routines.due_now(self.dir)), 1)
        self.assertGreater(
            datetime.datetime.fromisoformat(routines._load(self.dir)[0]["next_run"]),
            datetime.datetime.now())


class CriticTests(unittest.TestCase):
    def _plan(self, criteria):
        return planner.Plan({"goal": "g", "deliverable": "d", "steps": ["a"],
                             "success_criteria": criteria})

    def test_mind_state_criteria_are_unverifiable(self):
        observable, unobservable = critic.observable_criteria(
            ["Himanshu acknowledges the update", "the event appears in a receipt"])
        self.assertEqual(len(observable), 1)
        self.assertEqual(len(unobservable), 1)

    def test_unreachable_verifier_never_blocks_completion(self):
        def boom(messages, task_level=None):
            raise RuntimeError("API down")
        verdict = critic.review(self._plan(["x in receipt"]),
                                [{"type": "A", "ok": True, "output": "done"}], "", boom)
        self.assertEqual(verdict.verdict, "unverifiable")
        self.assertFalse(verdict.failed)

    def test_prose_instead_of_json_degrades_safely(self):
        verdict = critic.review(self._plan(["x in receipt"]),
                                [{"type": "A", "ok": True, "output": "done"}], "",
                                lambda m, task_level=None: "I think it worked!")
        self.assertEqual(verdict.verdict, "unverifiable")

    def test_malformed_fields_do_not_crash(self):
        verdict = critic.review(
            self._plan(["x in receipt"]), [{"type": "A", "ok": True, "output": "d"}], "",
            lambda m, task_level=None: json.dumps(
                {"verdict": "banana", "reason": 123, "missing": "notalist"}))
        self.assertEqual(verdict.verdict, "unverifiable")
        self.assertEqual(verdict.missing, [])

    def test_failure_verdict_is_reported(self):
        verdict = critic.review(
            self._plan(["x in receipt"]), [{"type": "A", "ok": True, "output": "d"}], "",
            lambda m, task_level=None: json.dumps(
                {"verdict": "fail", "reason": "no receipt", "missing": ["the event"]}))
        self.assertTrue(verdict.failed)
        self.assertIn("the event", critic.correction_prompt(verdict, self._plan([])))

    def test_receipts_are_labelled_by_outcome(self):
        rendered = critic.format_receipts([
            {"type": "ADD", "ok": True, "output": "created"},
            {"type": "READ", "ok": False, "output": "[Error] boom"}])
        self.assertIn("[OK] ADD", rendered)
        self.assertIn("[FAILED] READ", rendered)

    def test_output_only_deliverable_does_not_demand_action_receipts(self):
        plan = planner.Plan({"goal": "plan my week", "deliverable": "weekly plan",
                             "steps": [], "needed_catalogs": [], "trivial": False,
                             "success_criteria": ["the routine covers every day"]})
        called = []
        verdict = critic.review(plan, [], "the complete routine", lambda *_a, **_k: called.append(1))
        self.assertEqual(verdict.verdict, "unverifiable")
        self.assertFalse(called, "a text deliverable should not spend a critic model call")


class PlanBudgetTests(unittest.TestCase):
    def test_depth_caps_the_budget(self):
        """Each depth is capped, and deeper work gets more room.

        Asserted against the configured ceilings instead of copies of their
        values. The numbers are tunable -- raised once already because four
        steps could not act, observe and answer -- and a test that restates
        them breaks on every tuning while proving nothing extra.
        """
        plan = planner.Plan({"steps": ["s"] * 20})
        for depth in ("direct", "standard", "deep"):
            with self.subTest(depth=depth):
                self.assertLessEqual(plan.execution_budget(depth),
                                     planner.STEP_BUDGETS[depth])
        self.assertLess(plan.execution_budget("direct"),
                        plan.execution_budget("deep"))

    def test_trivial_plans_stay_tiny(self):
        """Tiny, but enough to act, read the receipt and answer.

        This pinned 2, which is one action and one reply -- so a direct turn
        that had to do something and then say what happened was cut off at
        exactly the wrong moment. It created four calendar events and stopped
        with "reached the task execution budget of 2 steps" before confirming
        any of them. The principle (proportional budgets) was right; the floor
        was below the cost of doing one thing properly.
        """
        budget = planner.Plan({"trivial": True}).execution_budget("deep")
        self.assertEqual(budget, planner.TRIVIAL_STEP_BUDGET)
        self.assertGreaterEqual(budget, 4, "act, observe, recover, answer")
        # Still small beside a planned task, which is the actual property.
        self.assertLess(
            budget,
            planner.Plan({"steps": ["a", "b", "c", "d", "e", "f"]})
            .execution_budget("deep"),
            "a trivial turn must stay cheaper than a planned one")

    def test_substantial_text_answer_is_output_only(self):
        plan = planner.Plan({"goal": "build a detailed routine",
                             "deliverable": "a complete routine in the response",
                             "steps": [], "needed_catalogs": [], "trivial": False})
        self.assertTrue(plan.output_only)
        self.assertTrue(plan.trivial)

    def test_advance_can_complete_every_step(self):
        plan = planner.Plan({"steps": ["a", "b", "c"]})
        for _ in range(3):
            plan.advance()
        self.assertTrue(plan.all_steps_done)
        self.assertNotIn("->", plan.header())

    def test_unobservable_criteria_fail_the_quality_gate(self):
        plan = planner.Plan({"goal": "g", "deliverable": "d", "steps": ["a"],
                             "success_criteria": ["Himanshu acknowledges it"]})
        self.assertTrue(any("state of mind" in issue
                            for issue in planner.assess_plan(plan)))


class SandboxTests(unittest.TestCase):
    def test_markdown_fences_are_stripped(self):
        self.assertEqual(codetools._strip_fence("```python\nprint(1)\n```"), "print(1)")

    def test_snippet_output_is_returned(self):
        self.assertIn("5050", codetools.run_sandbox(".", "print(sum(range(101)))"))

    def test_sandbox_cannot_see_the_project(self):
        """Scratch code must not be able to overwrite the running system."""
        listing = codetools.run_sandbox(
            ".", "import os; print(sorted(os.listdir('.')))")
        self.assertIn("snippet.py", listing)
        self.assertNotIn("agent.py", listing)

    def test_failure_surfaces_stderr_and_exit_code(self):
        result = codetools.run_sandbox(".", "raise ValueError('boom')")
        self.assertTrue(result.startswith("[Error]"))
        self.assertIn("boom", result)

    def test_runaway_snippet_is_killed(self):
        result = codetools.run_sandbox(".", "import time; time.sleep(30)", timeout=2)
        self.assertIn("Timed out", result)

    def test_unknown_language_is_refused(self):
        self.assertIn("[Error]", codetools.run_sandbox(".", "x", language="brainfuck"))

    def test_write_code_refuses_to_clobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = codetools.write_code(tmp, "a/b.py", "print(1)")
            self.assertIn("[Success]", first)
            self.assertIn("[Error]", codetools.write_code(tmp, "a/b.py", "print(2)"))
            self.assertIn("[Success]", codetools.write_code(
                tmp, "a/b.py", "print(2)", overwrite=True))


class DesktopSafetyTests(unittest.TestCase):
    def setUp(self):
        import desktop
        self.desktop = desktop

    def test_protected_system_combos_are_identified(self):
        # The pure check, with no dependence on which window is focused.
        for combo in ["ctrl+alt+delete", "alt+f4", "ctrl+shift+esc"]:
            keys = combo.split("+")
            self.assertIsNotNone(self.desktop._protected_combo(keys), combo)

    def test_protected_system_combos_never_execute(self):
        # This used to assert the exact words "Unsupported Action", so it
        # passed or failed depending on which window happened to be in the
        # foreground: with a VANTA window focused the "do not act on my own
        # interface" guard refuses first, for a different and equally correct
        # reason. What matters is that the combo does not run.
        for combo in ["ctrl+alt+delete", "alt+f4", "ctrl+shift+esc"]:
            result = self.desktop.hotkey(combo)
            self.assertTrue(
                "Unsupported Action" in result or "Refused" in result,
                f"{combo} was not refused: {result[:90]}")
            self.assertNotIn("[Success]", result, combo)

    def test_ordinary_shortcuts_are_not_blocked(self):
        self.assertIsNone(self.desktop._protected_combo(["ctrl", "s"]))

    def test_key_aliases_normalise(self):
        self.assertEqual(self.desktop._normalise_keys("Return"), ["enter"])
        self.assertEqual(self.desktop._normalise_keys("ctrl+S"), ["ctrl", "s"])

    def test_winter_own_window_is_identified_by_title(self):
        self.assertTrue(self.desktop.is_winter_own_window(0, "VANTA"))

    def test_editor_named_after_the_project_is_not_winter(self):
        """The project folder is called vanta; that must not lock out the IDE."""
        self.assertFalse(
            self.desktop.is_winter_own_window(0, "vanta - Antigravity IDE"))


class HealthImportTests(unittest.TestCase):
    def _export(self, path, days=10):
        start = datetime.date(2026, 3, 1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0"?>\n<HealthData>\n')
            for day in range(days):
                date = (start + datetime.timedelta(days=day)).isoformat()
                for _ in range(4):
                    handle.write(f'<Record type="HKQuantityTypeIdentifierStepCount" '
                                 f'startDate="{date} 09:00:00 +0530" value="1000"/>\n')
                handle.write(f'<Record type="HKQuantityTypeIdentifierHeartRate" '
                             f'startDate="{date} 09:00:00 +0530" value="60"/>\n')
                handle.write(f'<Record type="HKQuantityTypeIdentifierHeartRate" '
                             f'startDate="{date} 10:00:00 +0530" value="80"/>\n')
                handle.write(
                    f'<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
                    f'value="HKCategoryValueSleepAnalysisAsleepCore" '
                    f'startDate="{date} 01:00:00 +0530" endDate="{date} 07:00:00 +0530"/>\n')
            handle.write("</HealthData>\n")

    def test_steps_sum_and_heart_rate_averages(self):
        import health_import
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.xml")
            self._export(path)
            with open(path, "rb") as handle:
                daily, seen = health_import._parse_apple(handle)
            collapsed = health_import._collapse(daily)
            row = next(iter(collapsed.values()))
            self.assertEqual(row["steps"], 4000)      # accumulated
            self.assertEqual(row["heart_rate"], 70)   # averaged
            self.assertGreater(seen, 0)

    def test_sleep_before_dawn_counts_for_the_previous_night(self):
        import health_import
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.xml")
            self._export(path, days=3)
            with open(path, "rb") as handle:
                daily, _ = health_import._parse_apple(handle)
            collapsed = health_import._collapse(daily)
            nights = [row["sleep_hours"] for row in collapsed.values()
                      if "sleep_hours" in row]
            self.assertTrue(nights)
            self.assertAlmostEqual(nights[0], 6.0, places=1)

    def test_memory_does_not_grow_with_record_count(self):
        """The whole point: a 400 MB export must not be loaded into memory."""
        import health_import
        import tracemalloc
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.xml")
            self._export(path, days=400)
            tracemalloc.start()
            with open(path, "rb") as handle:
                health_import._parse_apple(handle)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(peak, 12 * 1024 * 1024, "parser is holding samples")

    def test_missing_file_is_reported(self):
        import health_import
        self.assertIn("[Error]", health_import.import_health(".", "C:/nope.zip"))


class KnowledgeGapTests(unittest.TestCase):
    def setUp(self):
        import knowledge_gaps
        self.gaps = knowledge_gaps
        self.dir = tempfile.mkdtemp()

    def test_gap_lifecycle_without_storing_a_fact(self):
        self.assertIn("[Success]", self.gaps.note(
            self.dir, "sleep", "How much do you sleep?", why="health has no data"))
        self.assertEqual(len(self.gaps.open_gaps(self.dir)), 1)
        gap = self.gaps.open_gaps(self.dir)[0]
        self.assertIn("[Success]", self.gaps.fill(
            self.dir, gap["id"], "six hours", store_fact=False))
        self.assertEqual(self.gaps.open_gaps(self.dir), [])

    def test_duplicate_topic_is_not_tracked_twice(self):
        self.gaps.note(self.dir, "sleep", "q1")
        self.gaps.note(self.dir, "Sleep", "q2")
        self.assertEqual(len(self.gaps.open_gaps(self.dir)), 1)

    def test_gap_needs_a_question(self):
        self.assertIn("[Error]", self.gaps.note(self.dir, "topic only", ""))

    def test_filling_an_unknown_gap_is_refused(self):
        self.assertIn("[Error]", self.gaps.fill(self.dir, "ghost", "x", store_fact=False))


class ModeSwitchTests(unittest.TestCase):
    def _route(self, target, kind, confidence):
        import cognitive_pipeline as cp
        return cp.CognitiveRoute("switch", target, "respond", kind, "standard",
                                 False, confidence, cp.RetrievalRoute(), "", "")

    def test_working_context_is_offered_not_taken(self):
        import cognitive_pipeline as cp
        # "teach me X" is classified as research, not conversation, and the
        # router is right that tutor mode suits it. It still does not get to
        # make the change: Himanshu asked to be the one who decides, after a
        # portal login was rerouted into an autonomous study session.
        route = self._route("tutor", "research", 0.9)
        self.assertFalse(cp.should_apply_mode_switch(route))
        self.assertTrue(cp.mode_switch_needs_confirmation(route))
        self.assertTrue(cp.should_apply_mode_switch(route, explicit=True))

    def test_low_confidence_does_not_switch(self):
        import cognitive_pipeline as cp
        self.assertFalse(cp.should_apply_mode_switch(
            self._route("tutor", "research", 0.5)))

    def test_restrictive_modes_need_him_to_say_so(self):
        import cognitive_pipeline as cp
        # Work he asked for is never interrupted by a mode question...
        action = self._route("privacy", "action", 0.8)
        self.assertFalse(cp.should_apply_mode_switch(action))
        self.assertFalse(cp.mode_switch_needs_confirmation(action))
        # ...and a restrictive mode is proposed rather than assumed.
        talk = self._route("privacy", "conversation", 0.8)
        self.assertFalse(cp.should_apply_mode_switch(talk))
        self.assertTrue(cp.mode_switch_needs_confirmation(talk))

    def test_nothing_wins_unasked_any_more(self):
        # Emergency used to switch without asking, on the grounds that asking
        # costs the time that matters. That mode is gone, and of the two that
        # remain neither is urgent, so an explicit request is the only way.
        import cognitive_pipeline as cp
        route = self._route("build", "action", 0.95)
        self.assertFalse(cp.should_apply_mode_switch(route))
        self.assertTrue(cp.should_apply_mode_switch(route, explicit=True))

    def test_every_mode_is_routable_and_policed(self):
        import cognitive_pipeline as cp
        import mode_manager as mm
        for mode in mm.WinterMode:
            self.assertIn(mode.value, cp.VALID_MODES)
            self.assertIn(mode, mm.POLICIES)
            self.assertIn(mode, mm.MODE_SPEECH_LIMITS)
            self.assertTrue(mm.POLICIES[mode].tool_policy, f"{mode} has no tool policy")


class LlmTimeoutTests(unittest.TestCase):
    """A stalled provider must not block the turn for ten minutes."""

    def test_every_stage_has_a_bounded_timeout(self):
        import llm_router
        for stage in ["cognitive_router", "planner", "critic", "vision", "routine", None]:
            budget = llm_router.timeout_for(stage)
            self.assertGreater(budget, 0)
            self.assertLessEqual(budget, 180, f"{stage} would stall too long")

    def test_router_is_quicker_than_vision(self):
        import llm_router
        self.assertLess(llm_router.timeout_for("cognitive_router"),
                        llm_router.timeout_for("vision"))

    def test_env_override_is_honoured(self):
        import llm_router
        os.environ["WINTER_LLM_TIMEOUT"] = "12"
        try:
            self.assertEqual(llm_router.timeout_for("planner"), 12.0)
        finally:
            del os.environ["WINTER_LLM_TIMEOUT"]

    def test_timeout_marker_is_distinguishable_from_silence(self):
        import llm_router
        self.assertTrue(llm_router.TIMEOUT_MARKER)
        self.assertNotEqual(llm_router.TIMEOUT_MARKER.strip(), "")


class FindFilesTests(unittest.TestCase):
    def setUp(self):
        import documents
        self.documents = documents
        self.dir = tempfile.mkdtemp()
        for name in ["notes.txt", "report_final.pdf", "data.csv"]:
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        skipped = os.path.join(self.dir, "node_modules")
        os.makedirs(skipped, exist_ok=True)
        with open(os.path.join(skipped, "buried.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")

    def test_bare_word_matches_anywhere_in_the_name(self):
        result = self.documents.find_files("report", root=self.dir)
        self.assertIn("report_final.pdf", result)

    def test_wildcards_are_used_as_given(self):
        result = self.documents.find_files("*.csv", root=self.dir)
        self.assertIn("data.csv", result)
        self.assertNotIn("notes.txt", result)

    def test_noise_directories_are_skipped(self):
        """A search that returns node_modules is a search nobody reads."""
        self.assertNotIn("buried.txt", self.documents.find_files("buried", root=self.dir))

    def test_no_match_explains_what_to_try(self):
        result = self.documents.find_files("zzznothing", root=self.dir)
        self.assertIn("No files matching", result)

    def test_empty_pattern_and_bad_root_are_refused(self):
        self.assertIn("[Error]", self.documents.find_files(""))
        self.assertIn("[Error]", self.documents.find_files("x", root=os.path.join(self.dir, "nope")))


class DurationParsingTests(unittest.TestCase):
    """'remind me in twenty minutes' must not become an hour."""

    def setUp(self):
        import system_tools
        self.tools = system_tools

    def test_compact_forms(self):
        self.assertEqual(self.tools.parse_duration("20m"), 1200)
        self.assertEqual(self.tools.parse_duration("1h30m"), 5400)
        self.assertEqual(self.tools.parse_duration("90s"), 90)
        self.assertEqual(self.tools.parse_duration("2h"), 7200)

    def test_bare_number_is_minutes(self):
        self.assertEqual(self.tools.parse_duration("45"), 2700)

    def test_longest_spoken_phrase_wins(self):
        """'an hour' is a substring of 'an hour and a half'."""
        self.assertEqual(self.tools.parse_duration("an hour and a half"), 5400)
        self.assertEqual(self.tools.parse_duration("an hour"), 3600)
        self.assertEqual(self.tools.parse_duration("half an hour"), 1800)

    def test_unreadable_duration_returns_none(self):
        for text in ["soonish", "later", "", None]:
            self.assertIsNone(self.tools.parse_duration(text))

    def test_timer_beyond_a_day_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("[Error]", self.tools.set_timer(tmp, "30h"))


class KillProcessSafetyTests(unittest.TestCase):
    def setUp(self):
        import system_tools
        self.tools = system_tools

    def test_unconfirmed_kill_is_refused(self):
        result = self.tools.kill_process("notepad")
        self.assertIn("Confirmation Required", result)
        self.assertIn("CLOSE_WINDOW", result)

    def test_empty_target_is_refused(self):
        self.assertIn("[Error]", self.tools.kill_process(""))

    def test_winter_cannot_kill_itself(self):
        result = self.tools.kill_process("python.exe", confirmed=True)
        self.assertIn("Refused", result)
        self.assertNotIn("[Success] Ended", result)


class WeatherTests(unittest.TestCase):
    def test_location_is_never_guessed(self):
        from providers import weather_api
        result = weather_api.get_weather("")
        self.assertIn("[Error]", result)
        self.assertIn("Do not guess", result)

    def test_weather_codes_are_words_not_numbers(self):
        from providers import weather_api
        self.assertEqual(weather_api._describe(95), "thunderstorms")
        self.assertEqual(weather_api._describe(0), "clear")
        self.assertIn("code", weather_api._describe(1234))


class ModePersistenceTests(unittest.TestCase):
    """A mode should not outlive the reason it was entered."""

    def setUp(self):
        import mode_manager
        self.mm = mode_manager
        self.manager = mode_manager.ModeManager()
        now = datetime.datetime.now(datetime.timezone.utc)
        self.now = now.isoformat()
        self.old = (now - datetime.timedelta(hours=9)).isoformat()

    def _resolve(self, mode, when, source):
        return self.manager._unexpired(mode, when, source).value

    def test_an_inferred_mode_does_not_survive_a_restart(self):
        """Build was guessed from the shape of a task, not asked for.

        Privacy and offline used to be exempt from this: he had turned access
        off and a restart must not turn it back on. Both modes are gone, so
        nothing is sticky any more and everything inferred falls back.
        """
        self.assertEqual(self._resolve(self.mm.WinterMode.BUILD, self.now,
                                       "semantic_route"), "assistant")

    def test_a_requested_mode_is_kept(self):
        self.assertEqual(self._resolve(self.mm.WinterMode.BUILD, self.now, "voice"),
                         "build")
        self.assertEqual(self._resolve(self.mm.WinterMode.BUILD, self.now, "text"),
                         "build")

    def test_a_requested_mode_still_goes_stale(self):
        self.assertEqual(self._resolve(self.mm.WinterMode.BUILD, self.old, "voice"),
                         "assistant")

    def test_sleep_stays_active_until_the_user_returns(self):
        self.assertEqual(
            self.mm.STICKY_MODES,
            frozenset({self.mm.WinterMode.SLEEP}))
        self.assertEqual(
            self._resolve(self.mm.WinterMode.SLEEP, self.old, "voice"),
            "sleep")

    def test_unreadable_timestamp_keeps_the_mode(self):
        self.assertEqual(self._resolve(self.mm.WinterMode.BUILD, "not-a-date", "voice"),
                         "build")


class InferenceJsonRepairTests(unittest.TestCase):
    """Models emit almost-JSON; three good proposals were lost to one bare word."""

    def setUp(self):
        import inference
        self.inference = inference

    def test_bare_identifier_value_is_quoted(self):
        parsed = self.inference._extract_json(
            '{"predicate": SAME_CONCEPT_AS, "object": "people"}')
        self.assertEqual(parsed["predicate"], "SAME_CONCEPT_AS")

    def test_python_literals_become_json_literals(self):
        parsed = self.inference._extract_json('{"a": None, "b": True, "c": False}')
        self.assertIsNone(parsed["a"])
        self.assertIs(parsed["b"], True)
        self.assertIs(parsed["c"], False)

    def test_trailing_commas_are_tolerated(self):
        self.assertIsNotNone(self.inference._extract_json('{"proposals": [{"k": 1},]}'))

    def test_valid_json_is_never_altered(self):
        parsed = self.inference._extract_json('{"proposals": [{"object": null}]}')
        self.assertIsNone(parsed["proposals"][0]["object"])

    def test_genuinely_broken_json_still_fails(self):
        self.assertIsNone(self.inference._extract_json('{"proposals": [[['))


class SpeechToTextTests(unittest.TestCase):
    def setUp(self):
        import stt
        self.stt = stt

    def test_whisper_hallucinations_are_discarded(self):
        """Whisper invents these over silence rather than returning nothing."""
        for text in ["you", "Thanks for watching!", "Bye.", "  ", "", None, "..."]:
            self.assertTrue(self.stt._is_noise(text), repr(text))

    def test_real_speech_is_kept(self):
        for text in ["set a timer for ten minutes", "what is my next meeting"]:
            self.assertFalse(self.stt._is_noise(text), text)

    def test_missing_audio_returns_empty(self):
        self.assertEqual(self.stt.transcribe("does_not_exist.wav"), "")
        self.assertEqual(self.stt.transcribe(""), "")

    def test_configuration_is_reported(self):
        self.assertTrue(self.stt.describe())

    def test_no_prompt_is_sent_to_whisper(self):
        """A prompt is echoed back as the transcript when the audio is silent."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "stt.py").read_text(
            encoding="utf-8")
        body = source.split("def _transcribe_online", 1)[1].split("def ", 1)[0]
        self.assertNotIn("prompt=", body)


class NotFoundErrorsTests(unittest.TestCase):
    """A dead-end error invites another guessed path; name the recovery instead."""

    def test_health_import_points_at_find_files(self):
        import health_import
        result = health_import.import_health(".", "C:/nowhere/export.zip")
        self.assertIn("[Error]", result)
        self.assertIn("FIND_FILES", result)

    def test_missing_document_points_at_a_way_to_look(self):
        import documents
        _path, error = documents.resolve(".", "a document that does not exist")
        self.assertIn("FIND_FILES", error)
        self.assertIn("Do not guess", error)

    def test_missing_folder_points_at_find_files(self):
        import documents
        self.assertIn("FIND_FILES", documents.index_folder("C:/nowhere-at-all"))

    def test_sandbox_missing_file_points_at_find_files(self):
        import codetools
        self.assertIn("FIND_FILES", codetools.run_sandbox(".", path="C:/nowhere/x.py"))

    def test_sandbox_accepts_a_path_without_a_body(self):
        """A path makes the tag body unnecessary; requiring both rejected valid calls."""
        import action_schema
        self.assertIsNone(action_schema.validate("RUN_SANDBOX", {"path": "x.py"}))
        self.assertIsNone(action_schema.validate("RUN_SANDBOX", {}, has_body=True))
        self.assertIsNotNone(action_schema.validate("RUN_SANDBOX", {}))


class ListeningFeedbackTests(unittest.TestCase):
    """Holding the key and getting silence back is the worst outcome."""

    def test_every_transcription_outcome_is_answered(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
            encoding="utf-8")
        body = source.split("def process_audio", 1)[1].split("\ndef ", 1)[0]

        # The empty-transcription branch must tell him something.
        empty_branch = body.split("if not text:", 1)[1].split("# --- Voice", 1)[0]
        self.assertIn("speak(", empty_branch)
        self.assertIn("ui_queue.put", empty_branch)

        # So must the STT failure branch.
        error_branch = body.split("except Exception as e:", 1)[1].split("if not text:", 1)[0]
        self.assertIn("speak(", error_branch)

    def test_busy_agent_queues_instead_of_dropping_the_request(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
            encoding="utf-8")
        branch = source.split("if not acquired:", 1)[1].split("try:", 1)[0]
        self.assertIn("_queue_interruption", branch)
        self.assertIn("ui_queue.put", branch)
        self.assertNotIn("ask me again", branch.lower())
        self.assertNotIn("speak(", branch,
                         "busy status belongs on the dashboard, not over the active task")

    def test_clarification_prompt_names_the_real_key(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "agent.py").read_text(
            encoding="utf-8")
        self.assertNotIn('"Hold Ctrl to answer..."', source)


class CoordinateSpaceTests(unittest.TestCase):
    """A screenshot coordinate is not a screen coordinate."""

    def setUp(self):
        import desktop
        import vision
        self.desktop = desktop
        self.vision = vision
        self._scale = vision._send_scale
        self._origin = vision._capture_origin
        # A maximised 1938px window downscaled to the model's 1366px input.
        vision._send_scale = 1366 / 1938
        vision._capture_origin = (100, 50)

    def tearDown(self):
        self.vision._send_scale = self._scale
        self.vision._capture_origin = self._origin

    def test_image_coordinates_are_scaled_and_offset(self):
        x, y = self.desktop.to_screen(400, 300, "image")
        self.assertEqual((x, y), (100 + int(400 / (1366 / 1938)),
                                  50 + int(300 / (1366 / 1938))))

    def test_untransformed_click_would_miss_badly(self):
        """The bug this fixes: over 150px of error on a normal window."""
        x, _y = self.desktop.to_screen(400, 300, "image")
        self.assertGreater(abs(x - 400), 100)

    def test_screen_space_is_left_alone(self):
        self.assertEqual(self.desktop.to_screen(500, 400, "screen"), (500, 400))

    def test_origin_maps_to_the_window_corner(self):
        self.assertEqual(self.desktop.to_screen(0, 0, "image"), (100, 50))

    def test_garbage_coordinates_do_not_raise(self):
        self.assertIsInstance(self.desktop.to_screen("abc", None, "image"), tuple)


class BlenderDiscoveryTests(unittest.TestCase):
    def test_explicit_path_is_validated_not_assumed(self):
        import codetools
        os.environ["BLENDER_EXE"] = r"C:\nowhere\blender.exe"
        try:
            path, error = codetools.find_blender()
            self.assertIsNone(path)
            self.assertIn("does not exist", error)
        finally:
            del os.environ["BLENDER_EXE"]

    def test_missing_blender_explains_how_to_fix_it(self):
        import codetools
        os.environ.pop("BLENDER_EXE", None)
        path, error = codetools.find_blender()
        if path is None:
            self.assertIn("BLENDER_EXE", error)


class ReleaseConsistencyTests(unittest.TestCase):
    """The catalogue, the registry and the dispatcher must agree.

    Every mismatch found in this project has been silent: an action the model
    was told about that policy blocked, or one that was registered with no
    handler and simply did nothing.
    """

    @classmethod
    def setUpClass(cls):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        from pathlib import Path
        src = (Path(root) / "actions.py").read_text(encoding="utf-8")
        agent = (Path(root) / "agent.py").read_text(encoding="utf-8")

        # What can be dispatched, asked of the table that does the
        # dispatching. This used to be six regexes over actions.py, because
        # dispatch was an if/elif ladder and scraping it was the only way to
        # guess -- and a guess is what it was: an earlier version used
        # [A-Z_]+ and so missed every action with a digit in its name, reading
        # BOARD_PLOT3D as unregistered however well it was wired.
        import integrity
        cls.handled = integrity.live_executable_names()
        # Old names mapped onto their replacements before dispatch.
        cls.handled |= set(re.findall(r"'([A-Z0-9_]+)': '[A-Z0-9_]+'", src))
        # A couple of actions are handled in the agent loop itself.
        cls.handled |= set(re.findall(r'type="([A-Z0-9_]+)"', agent))

        catalogs = re.search(r"ACTION_CATALOGS = \{(.*?)\n\}", agent, re.S).group(1)
        # The CREATE_ACTION example in the catalogue demonstrates an action
        # that does not exist yet -- that is the whole point of it -- so its
        # body has to come out before the real names are read, exactly as the
        # dispatcher lifts block bodies out before scanning for tags.
        catalogs = re.sub(r'<ACTION type="CREATE_ACTION"[\s\S]*?</ACTION>', "",
                          catalogs)
        cls.documented = set(re.findall(r'type="([A-Z0-9_]+)"', catalogs))

    def test_nothing_documented_is_blocked_by_policy(self):
        """Telling the model about an action policy refuses wastes every turn."""
        import autonomy
        missing = sorted(self.documented - set(autonomy.KNOWN_ACTIONS))
        self.assertEqual(missing, [], f"documented but not registered: {missing}")

    def test_every_registered_action_can_actually_run(self):
        import autonomy
        ghosts = sorted(set(autonomy.KNOWN_ACTIONS) - self.handled)
        self.assertEqual(ghosts, [], f"registered with no handler: {ghosts}")

    def test_schema_only_describes_real_actions(self):
        import action_schema
        import autonomy
        real = self.handled | set(autonomy.KNOWN_ACTIONS)
        orphans = sorted(set(action_schema.ACTION_SPECS) - real)
        self.assertEqual(orphans, [], f"schema for missing actions: {orphans}")

    def test_retired_id_actions_explain_the_replacement(self):
        import autonomy
        for dead in ("CLICK_ID", "DOUBLE_CLICK_ID", "RIGHT_CLICK_ID"):
            self.assertNotIn(dead, self.documented,
                             f"{dead} is retired but still advertised")

    def test_every_mode_policy_is_complete(self):
        import mode_manager as mm
        for mode in mm.WinterMode:
            policy = mm.POLICIES[mode]
            self.assertTrue(policy.prompt.strip(), f"{mode} has no prompt")
            self.assertTrue(policy.tool_policy.strip(), f"{mode} has no tool policy")
            self.assertIn(mode, mm.MODE_SPEECH_LIMITS)

    def test_mode_blocklists_name_only_real_actions(self):
        import autonomy
        import mode_manager as mm
        known = set(autonomy.KNOWN_ACTIONS)
        for mode in mm.WinterMode:
            unknown = sorted(mm.POLICIES[mode].blocked_actions - known)
            self.assertEqual(unknown, [],
                             f"{mode.value} blocks actions that do not exist: {unknown}")

    def test_every_module_imports(self):
        """A release cannot ship a module that raises on import."""
        for name in ("actions", "desktop", "documents", "codetools", "critic",
                     "inference", "routines", "knowledge_gaps", "health_import",
                     "system_tools", "notifications", "stt", "action_schema",
                     "dataset_tools", "mode_manager", "cognitive_pipeline",
                     "planner", "llm_router", "browser", "ocr_worker"):
            with self.subTest(module=name):
                __import__(name)


class NotificationEscapingTests(unittest.TestCase):
    def test_markup_cannot_break_out_of_the_toast(self):
        import notifications
        escaped = notifications._escape('</text><script>x</script>"@', 200)
        self.assertNotIn("<script>", escaped)
        self.assertNotIn('"@', escaped)


if __name__ == "__main__":
    unittest.main()
