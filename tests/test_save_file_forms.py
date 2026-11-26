# -*- coding: utf-8 -*-
"""SAVE_FILE has to accept the way the model actually writes it.

Himanshu asked for a job search written into a Word document. The research
was done, the content was composed, and then nothing was saved -- twelve model
calls and 69,000 tokens ending in VANTA stating, correctly, that it could not
produce a receipt.

The content had been offered as an attribute:

    <ACTION type="SAVE_FILE" path="...docx" content="# AI/ML and Software..." />

Only the block form has a handler:

    <ACTION type="SAVE_FILE" path="...">...content...</ACTION>

So the attribute form fell through the whole dispatch chain and returned
"SAVE_FILE produced no result. Check that its required attributes were
supplied and spelled correctly" -- which is actively misleading, because the
attributes were spelled correctly and were perfectly reasonable.

It then got worse in a way that wasted the approval. SAVE_FILE needs consent,
so the attribute form queued, Himanshu approved it by message, and the replay
rebuilt the action from its stored payload:

    fake_response = f"<ACTION {attrs} />" if body is None else ...

`content` is not `body`, so body was None, so the replay produced the same
unhandled self-closing form and failed again -- consuming a real approval on
an action that could never run.

Both forms are reasonable. Both now work.
"""

import os
import queue
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_schema
import actions

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dispatch(tag):
    """Run one tag with approval granted, and return its receipts."""
    actions.execute_actions._results = []
    with patch.object(actions, "_policy_confirmation_required",
                      return_value=False):
        actions.execute_actions(tag, {}, queue.Queue(), BASE_DIR,
                                origin="user", turn_id="save-forms")
    return list(getattr(actions.execute_actions, "_results", []))


class BothFormsWrite(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(BASE_DIR, "vault", "temp",
                                 "vanta_save_form_test.txt")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_the_block_form_still_works(self):
        receipts = dispatch(
            f'<ACTION type="SAVE_FILE" path="{self.path}">hello from the '
            f'body</ACTION>')
        self.assertTrue(receipts)
        self.assertTrue(os.path.exists(self.path), receipts)
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("hello from the body", handle.read())

    def test_the_content_attribute_form_writes_too(self):
        """The form that silently did nothing."""
        receipts = dispatch(
            f'<ACTION type="SAVE_FILE" path="{self.path}" '
            f'content="hello from the attribute" />')
        self.assertTrue(receipts, "no receipt at all")
        self.assertTrue(receipts[0]["ok"], receipts[0]["output"])
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("hello from the attribute", handle.read())

    def test_write_file_accepts_the_attribute_too(self):
        receipts = dispatch(
            f'<ACTION type="WRITE_FILE" path="{self.path}" '
            f'content="written by attribute" />')
        self.assertTrue(receipts[0]["ok"], receipts[0]["output"])
        self.assertTrue(os.path.exists(self.path))

    def test_multiline_content_survives(self):
        """The real content was a markdown document with blank lines."""
        body = "# Heading\\n\\n- one\\n- two\\n"
        receipts = dispatch(
            f'<ACTION type="SAVE_FILE" path="{self.path}" content="{body}" />')
        self.assertTrue(receipts[0]["ok"], receipts[0]["output"])
        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("Heading", written)
        self.assertIn("one", written)

    def test_no_content_at_all_is_reported_not_silent(self):
        receipts = dispatch(f'<ACTION type="SAVE_FILE" path="{self.path}" />')
        self.assertTrue(receipts, "no receipt at all")
        self.assertFalse(receipts[0]["ok"])
        self.assertIn("content", receipts[0]["output"].lower())

    def test_no_path_is_reported(self):
        receipts = dispatch('<ACTION type="SAVE_FILE" content="orphan" />')
        self.assertTrue(receipts)
        self.assertFalse(receipts[0]["ok"])


class TheSchemaDescribesIt(unittest.TestCase):
    """65 actions have no schema; this was one, so nothing caught the form."""

    def test_save_file_has_a_schema(self):
        self.assertIsNotNone(action_schema.ACTION_SPECS.get("SAVE_FILE"))

    def test_write_file_has_a_schema(self):
        self.assertIsNotNone(action_schema.ACTION_SPECS.get("WRITE_FILE"))

    def test_the_schema_accepts_content_as_an_attribute(self):
        self.assertFalsy = self.assertFalse
        self.assertFalse(action_schema.validate(
            "SAVE_FILE", {"path": "x.txt", "content": "hi"}))

    def test_the_schema_accepts_a_body_instead(self):
        self.assertFalse(action_schema.validate(
            "SAVE_FILE", {"path": "x.txt"}, has_body=True))

    def test_the_schema_rejects_neither(self):
        problem = action_schema.validate("SAVE_FILE", {"path": "x.txt"})
        self.assertTrue(problem)
        self.assertIn("content", problem.lower())


class AnApprovedReplayUsesTheSameForm(unittest.TestCase):
    """The replay rebuilt a form that had no handler, wasting the approval."""

    def test_a_content_payload_replays_as_something_dispatchable(self):
        import agent
        payload = {"path": "C:/tmp/x.docx", "content": "hello"}
        rebuilt = agent.rebuild_approved_action("SAVE_FILE", dict(payload))
        self.assertIn("SAVE_FILE", rebuilt)
        # Either form is fine; silence is not. What matters is that the
        # content travels with it.
        self.assertIn("hello", rebuilt)

    def test_a_body_payload_still_replays_as_a_block(self):
        import agent
        rebuilt = agent.rebuild_approved_action(
            "SAVE_FILE", {"path": "C:/tmp/x.docx"}, body="from body")
        self.assertIn("from body", rebuilt)
        self.assertIn("</ACTION>", rebuilt)


if __name__ == "__main__":
    unittest.main()
