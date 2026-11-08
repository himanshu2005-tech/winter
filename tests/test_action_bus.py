# -*- coding: utf-8 -*-
"""Dispatch is a table, and the table is the thing that can be checked.

The 116-branch ladder this replaced had one property that caused real
failures: nothing could enumerate it. A capability could be authorized,
schema-checked, mode-classified and documented, and still have no rung -- the
CAPTURE_CAMERA failure exactly. You could not ask "what can be dispatched?"
without reading three and a half thousand lines, so nobody asked, and the
answer drifted.

A table can be asked. Most of this file is that question in different forms:
every authorized name reaches a handler or is accounted for, no name reaches
two, the controls that used to sit between rungs still refuse what they
refused, and an unknown name is still refused rather than falling through.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_bus
import actions
import autonomy


class Queue:
    def __init__(self):
        self.messages = []

    def put(self, item):
        self.messages.append(item)


def context(name, **attrs):
    return action_bus.ActionContext(
        name, attrs, Queue(), os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))),
        legacy=actions.execute_actions)


class TheTableIsComplete(unittest.TestCase):
    """What the ladder could never answer."""

    def setUp(self):
        self.bus = action_bus.BUS

    def test_something_is_registered(self):
        self.assertGreater(len(self.bus.names()), 200,
                           "the handlers did not register; dispatch would "
                           "refuse every action")

    def test_every_authorized_action_is_dispatchable_or_accounted_for(self):
        """The CAPTURE_CAMERA failure, as a standing check.

        An action that is authorized but unreachable is the worst of both: the
        model is told it exists and nothing happens when it asks.
        """
        import capabilities
        elsewhere = set()
        # Handled by the pass that runs after the main loop.
        elsewhere |= set(actions.POST_LOOP_ACTIONS)
        elsewhere |= set(actions.DEFERRED_ACTIONS)
        elsewhere |= set(actions.RETIRED_ACTIONS)
        # Older spellings normalised to a live name before dispatch.
        elsewhere |= {"MOUSE_MOVE", "MOUSE_DRAG", "DOUBLECLICK", "RIGHTCLICK"}
        # Declared capabilities that run inside the agent loop, not here.
        elsewhere |= {name for name, cap in capabilities.CAPABILITIES.items()
                      if cap.runs_in != capabilities.RUNS_IN_DISPATCHER}
        # ADD_ALARM is a spelling of SET_ALARM, which is post-loop.
        elsewhere |= {"ADD_ALARM"}

        unreachable = sorted(set(autonomy.KNOWN_ACTIONS)
                             - self.bus.names() - elsewhere)
        self.assertEqual(unreachable, [],
                         f"{len(unreachable)} authorized action(s) have no "
                         f"handler anywhere")

    def test_nothing_on_the_bus_is_unauthorized(self):
        """Registering a handler must never be a way to grant permission."""
        ungoverned = sorted(self.bus.names() - set(autonomy.KNOWN_ACTIONS))
        self.assertEqual(ungoverned, [],
                         "a handler exists for an action the authorization "
                         "set does not know about")

    def test_no_name_has_two_handlers(self):
        """In the ladder the second rung for a name was dead code."""
        seen = {}
        for registration in self.bus.registrations():
            for name in registration.names:
                self.assertNotIn(name, seen,
                                 f"{name} is registered twice "
                                 f"({seen.get(name)} and {registration.family})")
                seen[name] = registration.family

    def test_a_duplicate_registration_is_refused_outright(self):
        bus = action_bus.ActionBus()
        bus.register("THING")(lambda ctx: None)
        with self.assertRaises(ValueError):
            bus.register("THING")(lambda ctx: None)

    def test_a_registration_needs_a_name(self):
        with self.assertRaises(ValueError):
            action_bus.ActionBus().register()(lambda ctx: None)

    def test_every_name_resolves_to_a_callable(self):
        for name in sorted(self.bus.names()):
            with self.subTest(action=name):
                self.assertTrue(callable(self.bus.resolve(name).handler))

    def test_the_table_can_describe_itself(self):
        """The property the ladder lacked: it can be asked."""
        text = self.bus.describe("PROJECT_TREE")
        self.assertIn("PROJECT_TREE", text)
        self.assertIn("open_project_required", text,
                      "describe() does not mention the controls in force")

    def test_it_can_name_what_it_cannot_dispatch(self):
        self.assertIn("no handler", self.bus.describe("NOT_A_REAL_ACTION"))


class AnUnknownNameIsRefused(unittest.TestCase):
    """Default-deny as a table entry rather than a final else."""

    def test_it_does_not_raise(self):
        ctx = context("DEFINITELY_NOT_AN_ACTION")
        self.assertEqual(action_bus.BUS.dispatch(ctx), action_bus.FLOW_NEXT)

    def test_it_reports_why(self):
        ctx = context("DEFINITELY_NOT_AN_ACTION")
        action_bus.BUS.dispatch(ctx)
        self.assertIn("DEFINITELY_NOT_AN_ACTION", ctx.receipt)
        self.assertNotIn("[Success]", ctx.receipt)

    def test_it_never_claims_the_hardware_is_at_fault(self):
        """unavailability's rule: only a real probe may blame a device."""
        ctx = context("DEFINITELY_NOT_AN_ACTION")
        action_bus.BUS.dispatch(ctx)
        for word in ("broken", "no camera", "hardware failure"):
            self.assertNotIn(word, ctx.receipt.lower())


class ControlsRefuseWhatTheyRefusedBefore(unittest.TestCase):
    """The three guards that used to sit between rungs of an inner ladder.

    They read as dispatch and were invisible as policy. They are declared as
    preconditions now, and these tests exist because a mechanical extraction
    could have dropped one -- which for two of them would have let a
    background turn write standing instructions.
    """

    def _refusal(self, name, origin="proactive", **attrs):
        ctx = action_bus.ActionContext(
            name, attrs, Queue(), os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))),
            origin=origin, legacy=actions.execute_actions)
        action_bus.BUS.dispatch(ctx)
        return ctx.receipt

    def test_standing_instructions_are_user_only(self):
        for name in ("SET_PERSONA", "ADD_DIRECTIVE", "REMOVE_DIRECTIVE"):
            with self.subTest(action=name):
                receipt = self._refusal(name, text="be brief")
                self.assertIn("[Error]", receipt)
                self.assertIn("Himanshu said", receipt)

    def test_reading_the_directives_is_not_gated(self):
        """The original guard exempted it by name; the exemption survives."""
        registration = action_bus.BUS.resolve("LIST_DIRECTIVES")
        self.assertEqual(registration.preconditions, (),
                         "listing directives became user-only, which the "
                         "original guard explicitly did not do")

    def test_workflows_are_user_only(self):
        for name in ("CREATE_WORKFLOW", "DEACTIVATE_WORKFLOW"):
            with self.subTest(action=name):
                self.assertIn("Himanshu initiated", self._refusal(name, id="x"))

    def test_reading_workflows_is_not_gated(self):
        for name in ("LIST_WORKFLOWS", "READ_WORKFLOW"):
            with self.subTest(action=name):
                self.assertEqual(
                    action_bus.BUS.resolve(name).preconditions, ())

    def test_project_file_actions_need_an_open_project(self):
        for name in ("PROJECT_TREE", "READ_PROJECT_FILE", "WRITE_PROJECT_FILE",
                     "EDIT_PROJECT_FILE", "DELETE_PROJECT_FILE",
                     "SEARCH_PROJECT", "FIND_PROJECT_FILES", "RUN_IN_PROJECT"):
            with self.subTest(action=name):
                self.assertEqual(
                    len(action_bus.BUS.resolve(name).preconditions), 1,
                    f"{name} lost its open-project precondition")

    def test_opening_and_listing_projects_do_not_need_one(self):
        """They are how you get a project open in the first place."""
        for name in ("LIST_PROJECTS", "CREATE_PROJECT", "OPEN_PROJECT"):
            with self.subTest(action=name):
                self.assertEqual(
                    action_bus.BUS.resolve(name).preconditions, (),
                    f"{name} cannot require what it exists to provide")

    def test_a_precondition_stops_the_handler_running(self):
        bus = action_bus.ActionBus()
        ran = []
        bus.register("GATED", requires=(action_bus.user_only(),))(
            lambda ctx: ran.append(1))
        ctx = action_bus.ActionContext("GATED", {}, Queue(), ".",
                                       origin="proactive")
        bus.dispatch(ctx)
        self.assertEqual(ran, [], "the precondition did not stop it")
        self.assertIn("Himanshu", ctx.receipt)

    def test_a_satisfied_precondition_lets_it_through(self):
        bus = action_bus.ActionBus()
        ran = []
        bus.register("GATED", requires=(action_bus.user_only(),))(
            lambda ctx: ran.append(1))
        bus.dispatch(action_bus.ActionContext("GATED", {}, Queue(), ".",
                                              origin="user"))
        self.assertEqual(ran, [1])


class FlowSignals(unittest.TestCase):
    """What `continue` and `break` against the old loop became."""

    def test_the_default_is_to_carry_on(self):
        bus = action_bus.ActionBus()
        bus.register("PLAIN")(lambda ctx: None)
        self.assertEqual(
            bus.dispatch(action_bus.ActionContext("PLAIN", {}, Queue(), ".")),
            action_bus.FLOW_NEXT)

    def test_a_handler_may_say_it_recorded_its_own_receipt(self):
        bus = action_bus.ActionBus()
        bus.register("OWN")(lambda ctx: action_bus.FLOW_RECORDED)
        self.assertEqual(
            bus.dispatch(action_bus.ActionContext("OWN", {}, Queue(), ".")),
            action_bus.FLOW_RECORDED)

    def test_a_returned_value_that_is_not_a_signal_is_ignored(self):
        """A handler that returns its receipt by accident must not be read as
        a flow instruction."""
        bus = action_bus.ActionBus()
        bus.register("ODD")(lambda ctx: "[Success] done")
        self.assertEqual(
            bus.dispatch(action_bus.ActionContext("ODD", {}, Queue(), ".")),
            action_bus.FLOW_NEXT)

    def test_the_dispatcher_honours_both_signals(self):
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "actions.py"), encoding="utf-8").read()
        self.assertIn("FLOW_RECORDED", source)
        self.assertIn("FLOW_STOP_BATCH", source)


class TheContext(unittest.TestCase):
    def test_a_receipt_writes_through_to_the_legacy_field(self):
        """The pipeline, the critic and the recovery sweep all read it."""
        ctx = context("ANY")
        ctx.report("[Success] it happened")
        self.assertEqual(actions.execute_actions._last_output,
                         "[Success] it happened")

    def test_attributes_are_read_with_whitespace_stripped(self):
        ctx = context("ANY", path="  file.txt  ")
        self.assertEqual(ctx.attr("path"), "file.txt")

    def test_a_missing_attribute_has_a_default(self):
        self.assertEqual(context("ANY").attr("nope", "fallback"), "fallback")

    def test_flags_read_the_spellings_the_model_actually_writes(self):
        for raw in ("true", "True", "yes", "1", "on"):
            with self.subTest(value=raw):
                self.assertTrue(context("ANY", overwrite=raw).flag("overwrite"))
        for raw in ("false", "no", "0", ""):
            with self.subTest(value=raw):
                self.assertFalse(context("ANY", overwrite=raw).flag("overwrite"))

    def test_the_step_line_is_clipped_for_the_notch(self):
        ctx = context("ANY")
        ctx.step("x" * 500)
        self.assertLessEqual(len(ctx.ui_queue.messages[0][1]), 60)

    def test_it_does_not_mutate_the_attributes_it_was_given(self):
        given = {"path": "a.txt"}
        ctx = action_bus.ActionContext("ANY", given, Queue(), ".")
        ctx.attrs["path"] = "b.txt"
        self.assertEqual(given["path"], "a.txt")


class PipelineControlsAreData(unittest.TestCase):
    """They were literal comparisons written into the loop."""

    def test_a_retired_name_explains_the_replacement(self):
        message = actions.RETIRED_ACTIONS["CLICK_ID"]
        self.assertIn("CLICK_SCREEN_TEXT", message)
        self.assertIn("{action_type}", message)

    def test_file_writes_are_deferred_not_dispatched(self):
        for name in ("SAVE_FILE", "WRITE_FILE"):
            with self.subTest(action=name):
                self.assertIn(name, actions.DEFERRED_ACTIONS)
                self.assertFalse(action_bus.BUS.handles(name),
                                 "both the loop and the post-loop pass would "
                                 "run it, which double-wrote the file")

    def test_starting_media_is_once_per_task(self):
        self.assertIn("MUSIC_PLAY", actions.ONCE_PER_TASK_ACTIONS)

    def test_waiting_is_not_followed_by_another_wait(self):
        self.assertIn("WAIT", actions.NO_SETTLE_DELAY)


class AddingAnActionTakesOneDecorator(unittest.TestCase):
    """The requirement: more actions are coming, and adding one must not mean
    editing a ladder."""

    def test_a_new_handler_is_reachable_immediately(self):
        bus = action_bus.ActionBus()

        @bus.register("BREW_TEA", family="kitchen",
                      receipt="the cup, and how strong it is")
        def _brew(ctx):
            ctx.report("[Success] one cup, strong")

        ctx = action_bus.ActionContext("BREW_TEA", {}, Queue(), ".")
        bus.dispatch(ctx)
        self.assertEqual(ctx.receipt, "[Success] one cup, strong")
        self.assertEqual(bus.resolve("BREW_TEA").family, "kitchen")

    def test_the_name_is_matched_case_insensitively(self):
        """The model writes <Action type="brew_tea"> as readily as capitals."""
        bus = action_bus.ActionBus()
        bus.register("brew_tea")(lambda ctx: ctx.report("ok"))
        self.assertTrue(bus.handles("BREW_TEA"))

    def test_one_handler_may_serve_several_names(self):
        bus = action_bus.ActionBus()
        bus.register("A", "B", "C", family="letters")(lambda ctx: None)
        self.assertEqual(len(bus.names()), 3)
        self.assertEqual(len(bus.registrations()), 1)


if __name__ == "__main__":
    unittest.main()
