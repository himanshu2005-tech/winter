# -*- coding: utf-8 -*-
"""What an action owes as evidence that it happened.

The defect this exists to stop has recurred in every part of this system, and
every instance has had the same shape: a receipt that asserts instead of
citing.

    ADD_CALENDAR_EVENT returned a bare URL, so the critic could not verify it,
    failed twice, and the model opened the URL and landed on Google Calendar's
    marketing page.

    TYPE_TEXT reported len(text) while a character had been dropped, so the
    receipt was arithmetically true and factually wrong.

    DRAW_ON_SCREEN drew nothing for an unknown shape and reported success.

    SAVE_FILE in its attribute form had no handler at all: it did nothing,
    said nothing, and spent one of Himanshu's approvals doing it.

    OPEN_URL wrote no receipt at all, so the pipeline reported a failure for a
    browser that had opened.

None of those is a bug in doing the work. They are bugs in reporting it, and a
long task made of actions that each might be lying is not a long task anyone
can trust -- which is the whole difference between an assistant for small jobs
and one for real ones.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_bus
import actions


class Queue:
    def __init__(self):
        self.messages = []

    def put(self, item):
        self.messages.append(item)


class ContractsExist(unittest.TestCase):
    def test_the_contracts_name_only_real_actions(self):
        self.assertEqual(actions._CONTRACT_REPORT["unknown"], [],
                         "a contract names an action with no handler, so it "
                         "governs nothing")

    def test_every_verified_action_has_a_contract(self):
        self.assertEqual(
            actions._CONTRACT_REPORT["verified_without_contract"], [],
            "an action is checked against a contract it does not have")

    def test_contracts_were_actually_applied(self):
        self.assertGreater(actions._CONTRACT_REPORT["applied"], 40)

    def test_a_contract_says_what_evidence_looks_like(self):
        """Not "returns a receipt" -- what is in it."""
        for name in ("ADD_CALENDAR_EVENT", "DRIVE_UPLOAD",
                     "WRITE_PROJECT_FILE", "RUN_COMMAND"):
            with self.subTest(action=name):
                contract = action_bus.BUS.contract(name)
                self.assertTrue(contract, f"{name} owes nothing in particular")
                self.assertGreater(len(contract.split()), 3, contract)

    def test_a_family_does_not_share_one_contract(self):
        """Seven Drive actions share a handler and owe different evidence.

        Stored on the registration, whichever name was applied last won, and
        DRIVE_UPLOAD ended up owing "the permission id" because DRIVE_SHARE
        was applied after it.
        """
        upload = action_bus.BUS.contract("DRIVE_UPLOAD")
        share = action_bus.BUS.contract("DRIVE_SHARE")
        self.assertNotEqual(upload, share)
        self.assertIn("byte", upload)
        self.assertIn("permission", share)


class TheSideEffectRatchet(unittest.TestCase):
    """An action that changes the world has to say what it changed.

    A ratchet rather than a demand that all 59 be done at once: the set of
    actions with contracts may only grow. Without this the next action added
    quietly has no contract and nobody notices until its receipt is believed.
    """

    #: Side-effecting actions that still owe a contract. This list may shrink
    #: and must never grow. Each one is an action whose success is currently
    #: taken on trust.
    KNOWN_UNCONTRACTED = {
        "ADD_TASKS", "ARCHIVE_EMAIL", "CONFIRM_INSIGHT", "MARK_EMAIL_READ",
        "SPOTIFY_PLAY", "SPOTIFY_PAUSE", "SPOTIFY_SKIP",
        "MUSIC_PLAY", "MUSIC_PAUSE", "MUSIC_NEXT", "MUSIC_PREVIOUS",
        "CLIPBOARD_WRITE", "CLOSE_WINDOW", "PAUSE_ROUTINE", "RESUME_ROUTINE",
        "UPDATE_PANEL", "SPLIT_DATASET", "CREATE_ACTION", "REMOVE_ACTION",
    }

    def test_no_new_side_effecting_action_arrives_without_a_contract(self):
        on_bus = action_bus.BUS.names()
        uncontracted = {
            name for name in set(actions.SIDE_EFFECT_ACTIONS) & on_bus
            if not action_bus.BUS.contract(name)
        }
        new = sorted(uncontracted - self.KNOWN_UNCONTRACTED)
        self.assertEqual(new, [],
                         f"{len(new)} side-effecting action(s) changed the "
                         f"world with no stated evidence: {new}")

    def test_the_backlog_is_not_silently_growing(self):
        """If one is fixed, this list should shrink with it."""
        on_bus = action_bus.BUS.names()
        uncontracted = {
            name for name in set(actions.SIDE_EFFECT_ACTIONS) & on_bus
            if not action_bus.BUS.contract(name)
        }
        stale = sorted(self.KNOWN_UNCONTRACTED - uncontracted - on_bus)
        self.assertEqual(stale, [],
                         f"these are listed as uncontracted but are not "
                         f"dispatchable at all: {stale}")


class EvidenceIsRequiredNotAssumed(unittest.TestCase):
    """The run-time half: a success that cites nothing is downgraded."""

    def dispatch(self, name, handler_output, **attrs):
        bus = action_bus.ActionBus()
        bus.register(name)(lambda ctx: ctx.report(handler_output))
        bus.apply_contracts({name: "an id and a count"}, {name})
        ctx = action_bus.ActionContext(name, attrs, Queue(), ".")
        bus.dispatch(ctx)
        return ctx.receipt

    def test_a_bare_success_is_downgraded(self):
        receipt = self.dispatch("THING", "[Success] done")
        self.assertTrue(receipt.startswith("[Unverified]"), receipt)
        self.assertIn("not confirmed", receipt)

    def test_it_says_what_to_do_about_it(self):
        receipt = self.dispatch("THING", "[Success] done")
        self.assertIn("check before reporting it as done", receipt)

    def test_a_success_citing_an_id_passes(self):
        receipt = self.dispatch("THING", "[Success] created id=7f3a, 14 rows")
        self.assertTrue(receipt.startswith("[Success]"), receipt)

    def test_a_success_citing_a_path_passes(self):
        receipt = self.dispatch("THING", "[Success] wrote vault/docs/a.md")
        self.assertTrue(receipt.startswith("[Success]"), receipt)

    def test_an_error_is_left_alone(self):
        """A refusal owes no evidence; it is not claiming anything."""
        receipt = self.dispatch("THING", "[Error] nothing to do")
        self.assertEqual(receipt, "[Error] nothing to do")

    def test_an_unverified_action_is_not_downgraded(self):
        """A contract without verification is documentation, by design."""
        bus = action_bus.ActionBus()
        bus.register("DOCUMENTED")(lambda ctx: ctx.report("[Success] done"))
        bus.apply_contracts({"DOCUMENTED": "an id"})      # not verified
        ctx = action_bus.ActionContext("DOCUMENTED", {}, Queue(), ".")
        bus.dispatch(ctx)
        self.assertEqual(ctx.receipt, "[Success] done")

    def test_naming_the_target_can_be_required(self):
        bus = action_bus.ActionBus()
        bus.register("WRITE", verify=(action_bus.must_name("path"),))(
            lambda ctx: ctx.report("[Success] wrote the file"))
        ctx = action_bus.ActionContext("WRITE", {"path": "a/b.txt"},
                                       Queue(), ".")
        bus.dispatch(ctx)
        self.assertTrue(ctx.receipt.startswith("[Unverified]"), ctx.receipt)
        self.assertIn("a/b.txt", ctx.receipt)

    def test_naming_the_target_is_satisfied_by_naming_it(self):
        bus = action_bus.ActionBus()
        bus.register("WRITE", verify=(action_bus.must_name("path"),))(
            lambda ctx: ctx.report("[Success] wrote a/b.txt (12 bytes)"))
        ctx = action_bus.ActionContext("WRITE", {"path": "a/b.txt"},
                                       Queue(), ".")
        bus.dispatch(ctx)
        self.assertTrue(ctx.receipt.startswith("[Success]"))

    def test_an_absent_attribute_is_not_demanded(self):
        bus = action_bus.ActionBus()
        bus.register("WRITE", verify=(action_bus.must_name("path"),))(
            lambda ctx: ctx.report("[Success] did a thing"))
        ctx = action_bus.ActionContext("WRITE", {}, Queue(), ".")
        bus.dispatch(ctx)
        self.assertTrue(ctx.receipt.startswith("[Success]"))


class EveryHandlerWritesAReceipt(unittest.TestCase):
    """The pipeline turns silence into a false failure.

    A handler that reports nothing leaves `_last_output` holding the previous
    action's text, so the loop's own guard fires and tells the model
    "<ACTION> produced no result. Check that its required attributes were
    supplied and spelled correctly" -- about work that may well have happened.
    That is what OPEN_URL did, and the model went looking for a misspelling
    that was never there.
    """

    def test_no_handler_is_silent(self):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "actions.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        functions = {node.name: node for node in tree.body
                     if isinstance(node, ast.FunctionDef)}
        silent = []
        for registration in action_bus.BUS.registrations():
            node = functions.get(registration.handler.__name__)
            if node is None:
                continue
            body = ast.get_source_segment(source, node) or ""
            if not any(mark in body for mark in
                       ("_last_output", "ctx.report", "_record(")):
                silent.append(registration.names[0])
        self.assertEqual(sorted(silent), [],
                         f"{len(silent)} handler(s) report nothing, so the "
                         f"pipeline will call them failures")

    def test_open_url_reports_what_it_did(self):
        """It reported nothing at all, and was called a failure for it."""
        body = action_bus.BUS.handler_source("OPEN_URL")
        self.assertIn("ctx.report", body)

    def test_open_url_does_not_claim_the_page_loaded(self):
        """Opening a browser is not evidence that a page rendered."""
        body = action_bus.BUS.handler_source("OPEN_URL")
        self.assertIn("not confirmed", body)


if __name__ == "__main__":
    unittest.main()
