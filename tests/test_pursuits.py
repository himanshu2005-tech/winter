# -*- coding: utf-8 -*-
"""Work that outlives a turn.

VANTA could hold facts, deadlines, conditions and background runs. It had
nowhere to put the ordinary shape of real work: an objective pursued over
days, made of steps, some done and some not, with something in the way. "Find
me a flat", "apply to these twenty roles", "get through the syllabus" are none
of a deadline, a condition, a fact or a precedent, so every conversation
started over and the only record was the transcript.

The tests that matter most here are the ones about evidence. A to-do list that
accepts "done" on trust would launder a single unverified action into days of
apparent progress -- and this system's recurring defect is exactly actions
claiming success they had not earned. A step closes against a receipt or it
does not close.
"""

import json
import os
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pursuits


class Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name

    def start(self, title="find a flat", why="", steps=()):
        receipt = pursuits.start(self.base, title, why, steps)
        self.pursuit = pursuits.listing(self.base)[0]
        return receipt

    def only(self):
        return pursuits.listing(self.base, include_closed=True)[0]


class StartingOne(Base):
    def test_it_is_recorded_with_an_id(self):
        receipt = self.start("find a flat in Hyderabad", "moving in January")
        self.assertIn("[Success]", receipt)
        self.assertIn(self.pursuit["id"], receipt)
        self.assertEqual(self.pursuit["title"], "find a flat in Hyderabad")
        self.assertEqual(self.pursuit["why"], "moving in January")
        self.assertEqual(self.pursuit["state"], pursuits.ACTIVE)

    def test_it_survives_the_process(self):
        """The whole point: the next conversation can find it."""
        self.start("learn Rust")
        again = pursuits.listing(self.base)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["title"], "learn Rust")

    def test_steps_can_be_given_at_the_start(self):
        self.start("ship the release", steps=["write the notes", "tag it"])
        self.assertEqual(len(self.pursuit["steps"]), 2)

    def test_a_pursuit_needs_a_title(self):
        for empty in ("", "   ", None):
            with self.subTest(title=empty):
                with self.assertRaises(pursuits.PursuitError):
                    pursuits.start(self.base, empty)

    def test_starting_the_same_thing_twice_returns_the_first(self):
        """Asked twice across two conversations, which is the normal case."""
        self.start("find a flat")
        receipt = pursuits.start(self.base, "Find A Flat")
        self.assertEqual(len(pursuits.listing(self.base)), 1)
        self.assertIn("Already pursuing", receipt)

    def test_a_closed_pursuit_does_not_block_a_new_one(self):
        """The same goal can come round again."""
        self.start("find a flat")
        pursuits.close(self.base, self.pursuit["id"], "signed a lease")
        pursuits.start(self.base, "find a flat")
        self.assertEqual(len(pursuits.listing(self.base)), 1)
        self.assertEqual(
            len(pursuits.listing(self.base, include_closed=True)), 2)

    def test_there_is_a_ceiling_on_open_pursuits(self):
        """Twelve open pursuits is a list nobody is actually pursuing."""
        for number in range(pursuits.MAX_OPEN_PURSUITS):
            pursuits.start(self.base, f"thing number {number}")
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.start(self.base, "one too many")
        self.assertIn("already open", str(caught.exception))

    def test_the_file_is_written_atomically(self):
        """A half-written file loses every pursuit at once."""
        self.start("a thing")
        with open(os.path.join(self.base, pursuits._FILENAME),
                  encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), list)

    def test_a_corrupt_file_does_not_crash_the_reader(self):
        path = os.path.join(self.base, pursuits._FILENAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(pursuits.listing(self.base), [])


class Planning(Base):
    def setUp(self):
        super().setUp()
        self.start("apply to twenty roles")

    def test_steps_are_added(self):
        receipt = pursuits.plan(self.base, self.pursuit["id"],
                                ["build the shortlist", "tailor the CV"])
        self.assertIn("Added 2 step(s)", receipt)
        self.assertEqual(len(self.only()["steps"]), 2)

    def test_steps_start_pending(self):
        pursuits.plan(self.base, self.pursuit["id"], ["do the thing"])
        self.assertEqual(self.only()["steps"][0]["state"],
                         pursuits.STEP_PENDING)

    def test_a_duplicate_step_is_not_added_twice(self):
        """Re-planning is normal; duplicating the plan is not."""
        pursuits.plan(self.base, self.pursuit["id"], ["build the shortlist"])
        pursuits.plan(self.base, self.pursuit["id"], ["Build The Shortlist"])
        self.assertEqual(len(self.only()["steps"]), 1)

    def test_empty_lines_are_ignored(self):
        pursuits.plan(self.base, self.pursuit["id"], ["a step", "", "   "])
        self.assertEqual(len(self.only()["steps"]), 1)

    def test_no_steps_at_all_is_refused(self):
        with self.assertRaises(pursuits.PursuitError):
            pursuits.plan(self.base, self.pursuit["id"], [])

    def test_an_unknown_pursuit_says_what_is_open(self):
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.plan(self.base, "no-such-thing", ["a step"])
        self.assertIn("apply to twenty roles", str(caught.exception))

    def test_there_is_a_ceiling_on_steps(self):
        pursuits.plan(self.base, self.pursuit["id"],
                      [f"step {n}" for n in range(pursuits.MAX_STEPS)])
        with self.assertRaises(pursuits.PursuitError):
            pursuits.plan(self.base, self.pursuit["id"], ["one more"])


class AStepClosesAgainstEvidence(Base):
    """The rule this exists for.

    Every false-success defect in this system has been a receipt that asserted
    rather than cited. A pursuit that accepted "done" would carry one of them
    across days instead of one turn, and nothing later would find out.
    """

    def setUp(self):
        super().setUp()
        self.start("apply to twenty roles")
        pursuits.plan(self.base, self.pursuit["id"],
                      ["build the shortlist", "tailor the CV", "send five"])

    def test_a_receipt_closes_a_step(self):
        receipt = pursuits.advance(self.base, self.pursuit["id"], "1",
                                   receipt="vault/docs/shortlist.md, 20 rows")
        self.assertIn("[Success]", receipt)
        step = self.only()["steps"][0]
        self.assertEqual(step["state"], pursuits.STEP_DONE)
        self.assertIn("shortlist.md", step["receipt"])

    def test_no_receipt_means_no_closing(self):
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.advance(self.base, self.pursuit["id"], "1")
        self.assertIn("evidence", str(caught.exception))
        self.assertEqual(self.only()["steps"][0]["state"],
                         pursuits.STEP_PENDING)

    def test_the_refusal_says_what_would_satisfy_it(self):
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.advance(self.base, self.pursuit["id"], "1")
        message = str(caught.exception)
        self.assertIn("receipt=", message)
        self.assertIn("blocked=", message)

    def test_whitespace_is_not_evidence(self):
        with self.assertRaises(pursuits.PursuitError):
            pursuits.advance(self.base, self.pursuit["id"], "1", receipt="   ")

    def test_a_step_can_be_referred_to_by_its_number(self):
        pursuits.advance(self.base, self.pursuit["id"], "2", receipt="cv.pdf")
        self.assertEqual(self.only()["steps"][1]["state"], pursuits.STEP_DONE)

    def test_a_step_can_be_referred_to_by_its_text(self):
        pursuits.advance(self.base, self.pursuit["id"], "tailor",
                         receipt="cv.pdf v3")
        self.assertEqual(self.only()["steps"][1]["state"], pursuits.STEP_DONE)

    def test_an_ambiguous_reference_is_refused(self):
        """Two steps match "the"; guessing would close the wrong one."""
        pursuits.plan(self.base, self.pursuit["id"], ["review the offers"])
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.advance(self.base, self.pursuit["id"], "the",
                             receipt="done")
        self.assertIn("not a step", str(caught.exception))

    def test_an_unknown_step_lists_the_real_ones(self):
        with self.assertRaises(pursuits.PursuitError) as caught:
            pursuits.advance(self.base, self.pursuit["id"], "99",
                             receipt="x")
        self.assertIn("build the shortlist", str(caught.exception))


class BlockedIsAState(Base):
    """Long work fails by stalling silently, not by erroring."""

    def setUp(self):
        super().setUp()
        self.start("apply to twenty roles")
        pursuits.plan(self.base, self.pursuit["id"], ["one", "two"])

    def test_a_blocker_is_recorded_with_its_reason(self):
        pursuits.advance(self.base, self.pursuit["id"], "1",
                         blocked="needs his 2026 CV")
        step = self.only()["steps"][0]
        self.assertEqual(step["state"], pursuits.STEP_BLOCKED)
        self.assertIn("2026 CV", step["reason"])

    def test_the_pursuit_itself_becomes_blocked(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", blocked="waiting")
        self.assertEqual(self.only()["state"], pursuits.BLOCKED)

    def test_clearing_the_last_blocker_unblocks_the_pursuit(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", blocked="waiting")
        pursuits.advance(self.base, self.pursuit["id"], "1",
                         receipt="got it: cv-2026.pdf")
        self.assertEqual(self.only()["state"], pursuits.ACTIVE)

    def test_clearing_one_of_two_blockers_does_not(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", blocked="a")
        pursuits.advance(self.base, self.pursuit["id"], "2", blocked="b")
        pursuits.advance(self.base, self.pursuit["id"], "1", receipt="fixed a")
        self.assertEqual(self.only()["state"], pursuits.BLOCKED)

    def test_a_blocker_beats_a_receipt_given_at_the_same_time(self):
        """Both arguments at once means something is wrong; the safe reading
        is that it did not finish."""
        pursuits.advance(self.base, self.pursuit["id"], "1",
                         receipt="sort of done", blocked="actually stuck")
        self.assertEqual(self.only()["steps"][0]["state"],
                         pursuits.STEP_BLOCKED)


class ProgressIsCounted(Base):
    def setUp(self):
        super().setUp()
        self.start("ship it")
        pursuits.plan(self.base, self.pursuit["id"], ["a", "b", "c", "d"])

    def test_counts_match_the_steps(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", receipt="x1")
        pursuits.advance(self.base, self.pursuit["id"], "2", blocked="stuck")
        tally = pursuits.counts(self.only())
        self.assertEqual(tally["total"], 4)
        self.assertEqual(tally[pursuits.STEP_DONE], 1)
        self.assertEqual(tally[pursuits.STEP_BLOCKED], 1)
        self.assertEqual(tally[pursuits.STEP_PENDING], 2)

    def test_the_progress_line_is_derived_not_written(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", receipt="x1")
        self.assertIn("1/4 done", pursuits.progress_line(self.only()))

    def test_no_steps_says_so_rather_than_claiming_zero_percent(self):
        pursuits.start(self.base, "something vague")
        vague = [row for row in pursuits.listing(self.base)
                 if row["title"] == "something vague"][0]
        self.assertIn("No steps planned", pursuits.progress_line(vague))

    def test_the_next_step_skips_what_is_done_and_blocked(self):
        pursuits.advance(self.base, self.pursuit["id"], "1", receipt="x1")
        pursuits.advance(self.base, self.pursuit["id"], "2", blocked="stuck")
        self.assertEqual(pursuits.next_step(self.only())["what"], "c")

    def test_nothing_left_means_no_next_step(self):
        for number in range(1, 5):
            pursuits.advance(self.base, self.pursuit["id"], str(number),
                             receipt=f"done {number}")
        self.assertIsNone(pursuits.next_step(self.only()))


class Closing(Base):
    def setUp(self):
        super().setUp()
        self.start("find a flat")
        pursuits.plan(self.base, self.pursuit["id"], ["look", "view", "sign"])

    def test_it_closes_with_an_outcome(self):
        receipt = pursuits.close(self.base, self.pursuit["id"],
                                 "signed the Kondapur lease")
        self.assertIn("[Success]", receipt)
        self.assertEqual(self.only()["state"], pursuits.DONE)
        self.assertIn("Kondapur", self.only()["outcome"])

    def test_unfinished_steps_are_mentioned_not_hidden(self):
        """Usually right, occasionally a mistake, and only he can tell."""
        receipt = pursuits.close(self.base, self.pursuit["id"], "found one")
        self.assertIn("3 step(s) were still pending", receipt)

    def test_it_can_be_abandoned(self):
        pursuits.close(self.base, self.pursuit["id"], "not moving after all",
                       pursuits.ABANDONED)
        self.assertEqual(self.only()["state"], pursuits.ABANDONED)

    def test_it_can_be_paused(self):
        pursuits.close(self.base, self.pursuit["id"], "after exams",
                       pursuits.PAUSED)
        self.assertEqual(self.only()["state"], pursuits.PAUSED)

    def test_a_closed_pursuit_leaves_the_open_list(self):
        pursuits.close(self.base, self.pursuit["id"], "done")
        self.assertEqual(pursuits.listing(self.base), [])

    def test_a_nonsense_state_is_refused(self):
        with self.assertRaises(pursuits.PursuitError):
            pursuits.close(self.base, self.pursuit["id"], "x", "sideways")


class WhatTheModelIsTold(Base):
    def test_nothing_in_flight_adds_nothing_to_the_prompt(self):
        self.assertEqual(pursuits.prompt_block(self.base), "")

    def test_an_open_pursuit_appears_with_its_next_step(self):
        self.start("find a flat")
        pursuits.plan(self.base, self.pursuit["id"], ["shortlist areas"])
        block = pursuits.prompt_block(self.base)
        self.assertIn("find a flat", block)
        self.assertIn("shortlist areas", block)

    def test_a_blocker_appears(self):
        self.start("apply to roles")
        pursuits.plan(self.base, self.pursuit["id"], ["tailor the CV"])
        pursuits.advance(self.base, self.pursuit["id"], "1",
                         blocked="needs his 2026 CV")
        self.assertIn("2026 CV", pursuits.prompt_block(self.base))

    def test_it_says_that_a_plan_is_not_a_permission(self):
        """Same rule as memory and workflows, stated where it is read."""
        self.start("a thing")
        self.assertIn("not permissions", pursuits.prompt_block(self.base))

    def test_it_is_bounded(self):
        for number in range(10):
            pursuits.start(self.base, f"pursuit number {number}")
        block = pursuits.prompt_block(self.base, max_pursuits=3)
        self.assertLessEqual(
            len([line for line in block.split("\n") if line.startswith("- ")]),
            3)

    def test_closed_pursuits_are_not_carried(self):
        self.start("done thing")
        pursuits.close(self.base, self.pursuit["id"], "finished")
        self.assertEqual(pursuits.prompt_block(self.base), "")


class NoticingWhenWorkStops(Base):
    """Writing work down only helps if something notices it stalling."""

    def test_a_fresh_pursuit_is_not_stale(self):
        self.start("a thing")
        self.assertEqual(pursuits.stale(self.base), [])

    def test_an_untouched_pursuit_becomes_stale(self):
        self.start("a thing")
        rows = pursuits._load(self.base)
        rows[0]["updated"] = "2020-01-01T00:00:00"
        pursuits._save(self.base, rows)
        self.assertEqual(len(pursuits.stale(self.base)), 1)

    def test_a_closed_pursuit_is_never_stale(self):
        self.start("a thing")
        pursuits.close(self.base, self.pursuit["id"], "done")
        rows = pursuits._load(self.base)
        rows[0]["updated"] = "2020-01-01T00:00:00"
        pursuits._save(self.base, rows)
        self.assertEqual(pursuits.stale(self.base), [])

    def test_a_stalled_pursuit_becomes_a_follow_up_candidate(self):
        """Something has to notice. The follow-up loop already asks about
        finished events; work that has stopped moving belongs there too."""
        import followups
        self.start("find a flat")
        pursuits.plan(self.base, self.pursuit["id"], ["shortlist areas"])
        rows = pursuits._load(self.base)
        rows[0]["updated"] = "2020-01-01T00:00:00"
        pursuits._save(self.base, rows)
        with patch.object(followups, "_load", return_value=[]):
            candidates = followups.collect_candidates(self.base)
        mine = [row for row in candidates if row["source"] == "pursuit"]
        self.assertEqual(len(mine), 1, candidates)
        self.assertEqual(mine[0]["title"], "find a flat")
        self.assertIn("shortlist areas", mine[0]["description"])

    def test_a_moving_pursuit_is_not_asked_about(self):
        import followups
        self.start("find a flat")
        with patch.object(followups, "_load", return_value=[]):
            candidates = followups.collect_candidates(self.base)
        self.assertEqual([row for row in candidates
                          if row["source"] == "pursuit"], [])

    def test_an_unreadable_date_does_not_crash_the_check(self):
        self.start("a thing")
        rows = pursuits._load(self.base)
        rows[0]["updated"] = "not a date"
        pursuits._save(self.base, rows)
        self.assertEqual(pursuits.stale(self.base), [])


class TheCapabilitiesAreWired(unittest.TestCase):
    """Six layers have to agree or the action is invisible.

    Declared in capabilities.py, which derives five of them, plus a handler on
    the action bus for the sixth. This is the check that the derivation
    actually happened -- the CAPTURE_CAMERA failure was exactly this, a
    capability authorized and classified and absent from the catalogue.
    """

    NAMES = ("START_PURSUIT", "LIST_PURSUITS", "PURSUIT_STATUS",
             "PLAN_PURSUIT", "ADVANCE_PURSUIT", "CLOSE_PURSUIT")

    @classmethod
    def setUpClass(cls):
        import action_registry
        import agent
        import capabilities
        catalogs = dict(agent.ACTION_CATALOGS)
        catalogs.update(capabilities.catalog_overlay())
        action_registry.configure(catalogs)
        action_registry.warm_semantic_index(background=False)

    def test_all_six_layers_agree(self):
        import action_bus
        import action_schema
        import agent
        import autonomy
        import capabilities
        catalogue = "\n".join(agent.ACTION_CATALOGS.values())
        for name in self.NAMES:
            with self.subTest(action=name):
                self.assertIsNotNone(capabilities.get(name), "not declared")
                self.assertIn(name, autonomy.KNOWN_ACTIONS, "not authorized")
                self.assertIn(name, action_schema.ACTION_SPECS, "no schema")
                self.assertIn(name, catalogue, "not in the catalogue")
                self.assertTrue(action_bus.BUS.handles(name), "no handler")

    def test_each_handler_resolves_to_something_real(self):
        import capabilities
        for name in self.NAMES:
            with self.subTest(action=name):
                self.assertTrue(callable(capabilities.get(name).resolve_handler()))

    def test_they_are_reachable_from_plain_speech(self):
        import action_registry
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wanted = {
            "i want to start looking for a flat, it'll take weeks":
                {"START_PURSUIT"},
            "what am i in the middle of": {"LIST_PURSUITS"},
            "how far along is the flat search": {"PURSUIT_STATUS",
                                                 "LIST_PURSUITS"},
            "that step is done now": {"ADVANCE_PURSUIT"},
        }
        for phrase, expected in wanted.items():
            with self.subTest(phrase=phrase):
                found = {row["name"] for row in action_registry.search_outcomes(
                    base, [phrase], None, 12, "user")}
                self.assertTrue(
                    found & expected,
                    f"{phrase!r} reached none of {sorted(expected)}; "
                    f"got {sorted(found)[:8]}")

    def test_a_plan_is_not_a_permission(self):
        """A pursuit must not be a way to pre-authorize its own steps."""
        import capabilities
        for name in self.NAMES:
            with self.subTest(action=name):
                self.assertEqual(capabilities.get(name).origins[0], "user")

    def test_the_prompt_is_wired_in(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("pursuits.prompt_block", source,
                      "nothing tells the model about work already in flight")


class ThroughTheDispatcher(unittest.TestCase):
    """End to end, the way a reply actually arrives."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.base = self._dir.name
        self.queue = queue.Queue()

    def run_tag(self, tag):
        import actions
        actions.execute_actions(tag, {}, self.queue, self.base, origin="user")
        return actions.execute_actions._last_output

    def test_a_pursuit_can_be_started_and_planned(self):
        self.run_tag('<ACTION type="START_PURSUIT" title="find a flat" />')
        rows = pursuits.listing(self.base)
        self.assertEqual(len(rows), 1)
        receipt = self.run_tag(
            f'<ACTION type="PLAN_PURSUIT" id="{rows[0]["id"]}">\n'
            f'shortlist areas\nbook viewings\n</ACTION>')
        self.assertIn("Added 2 step(s)", receipt)

    def test_the_plan_body_is_not_squeezed_into_an_attribute(self):
        """Newlines in an XML attribute are not newlines."""
        import actions
        self.assertIn("PLAN_PURSUIT", actions.BLOCK_BODY_ACTIONS)

    def test_planning_with_no_body_says_what_is_missing(self):
        self.run_tag('<ACTION type="START_PURSUIT" title="a thing" />')
        rows = pursuits.listing(self.base)
        receipt = self.run_tag(
            f'<ACTION type="PLAN_PURSUIT" id="{rows[0]["id"]}" />')
        self.assertIn("tag body", receipt)

    def test_a_step_cannot_be_closed_without_evidence(self):
        self.run_tag('<ACTION type="START_PURSUIT" title="a thing" />')
        rows = pursuits.listing(self.base)
        self.run_tag(f'<ACTION type="PLAN_PURSUIT" id="{rows[0]["id"]}">\n'
                     f'do the thing\n</ACTION>')
        receipt = self.run_tag(
            f'<ACTION type="ADVANCE_PURSUIT" id="{rows[0]["id"]}" step="1" />')
        self.assertIn("[Error]", receipt)
        self.assertIn("evidence", receipt)

    def test_an_unknown_pursuit_is_an_error_not_a_crash(self):
        receipt = self.run_tag(
            '<ACTION type="PURSUIT_STATUS" id="nothing-like-this" />')
        self.assertIn("[Error]", receipt)

    def test_listing_works_with_nothing_in_flight(self):
        receipt = self.run_tag('<ACTION type="LIST_PURSUITS" />')
        self.assertIn("[Success]", receipt)
        self.assertIn("Nothing is being pursued", receipt)

    def test_a_proactive_turn_may_read_but_not_write(self):
        """A background turn must not start or advance work by itself."""
        import actions
        for name, tag in (
                ("START_PURSUIT",
                 '<ACTION type="START_PURSUIT" title="sneaky" />'),
                ("PLAN_PURSUIT",
                 '<ACTION type="PLAN_PURSUIT" id="x">a step</ACTION>')):
            with self.subTest(action=name):
                actions.execute_actions(tag, {}, self.queue, self.base,
                                        origin="proactive")
                self.assertEqual(pursuits.listing(self.base), [],
                                 f"{name} ran on a background turn")


if __name__ == "__main__":
    unittest.main()
