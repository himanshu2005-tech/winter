import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import followups


class FollowupTests(unittest.TestCase):
    def test_model_selects_meaningful_event_and_ledger_deduplicates(self):
        candidates=[
            {"id":"exam1","source":"calendar","external_id":"g1","title":"Final exam",
             "start":"2026-09-28T09:00:00+05:30","end":"2026-09-28T12:00:00+05:30",
             "description":"","location":"College"},
            {"id":"meal1","source":"calendar","external_id":"g2","title":"Lunch",
             "start":"2026-09-28T13:00:00+05:30","end":"2026-09-28T14:00:00+05:30",
             "description":"","location":""},
        ]
        def model(_messages,task_level=None):
            return '{"ask":true,"id":"exam1","question":"How did your final exam go?","reason":"Meaningful evaluation"}'
        choice=followups.choose_candidate(candidates,model)
        self.assertEqual(choice["id"],"exam1")
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(followups.mark_asked(folder,choice,datetime.datetime(2026,9,29,9)))
            self.assertFalse(followups.mark_asked(folder,choice,datetime.datetime(2026,9,29,10)))
            self.assertIn("awaiting answer",followups.status(folder))

    def test_model_can_choose_silence(self):
        candidate={"id":"meal","source":"calendar","external_id":"x","title":"Lunch",
                   "start":"x","end":"x","description":"","location":""}
        result=followups.choose_candidate([candidate],lambda *_args,**_kwargs:'{"ask":false,"id":"","question":"","reason":"routine"}')
        self.assertIsNone(result)

    def test_answer_attaches_to_exact_event_and_requires_graph_readback(self):
        candidate={"id":"exam1","source":"calendar","external_id":"g1","title":"Final exam",
                   "start":"2026-09-28T09:00:00+05:30","end":"2026-09-28T12:00:00+05:30",
                   "description":"","location":"","question":"How did your final exam go?","reason":"evaluation"}
        with tempfile.TemporaryDirectory() as folder:
            asked=datetime.datetime(2026,9,29,9)
            followups.mark_asked(folder,candidate,asked)
            with patch("memory.graph_store.add_edge") as add_edge, \
                 patch("memory.graph_store.get_current_edges",return_value=[("Final exam","HAS_OUTCOME","It went well")]):
                self.assertTrue(followups.record_answer_if_pending(folder,"It went well",source_id="turn-1",now=asked+datetime.timedelta(minutes=2)))
                add_edge.assert_called_once()
            self.assertIn("answered",followups.status(folder))
            self.assertFalse(followups.record_answer_if_pending(folder,"duplicate",now=asked+datetime.timedelta(minutes=3)))

    def test_unrelated_command_is_not_stored_as_event_outcome(self):
        candidate={"id":"exam2","source":"calendar","external_id":"g2","title":"Exam",
                   "start":"x","end":"x","description":"","location":"",
                   "question":"How did it go?","reason":"evaluation"}
        with tempfile.TemporaryDirectory() as folder:
            now=datetime.datetime(2026,9,29,9)
            followups.mark_asked(folder,candidate,now)
            self.assertFalse(followups.record_answer_if_pending(
                folder,"Open Chrome and search internships",now=now+datetime.timedelta(minutes=1)))
            self.assertIn("awaiting answer",followups.status(folder))
    def test_invalid_model_candidate_is_rejected(self):
        row={"id":"known","source":"calendar","external_id":"x","title":"Exam","start":"x","end":"x","description":"","location":""}
        model=lambda *_args,**_kwargs:'{"ask":true,"id":"invented","question":"How did it go?","reason":"x"}'
        self.assertIsNone(followups.choose_candidate([row],model))


if __name__=="__main__": unittest.main()