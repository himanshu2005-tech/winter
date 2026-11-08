import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import actions
import autonomy


class FollowupActionTests(unittest.TestCase):
    def run_action(self,text):
        queue=Queue(); actions.execute_actions(text,{},queue,".",origin="user")
        return actions.execute_actions._last_output,actions.execute_actions._direct_user_result

    @patch("providers.google_api.google_connection_status",return_value={"ok":True,"reauthorization_required":False,"message":"Google authorization is valid."})
    def test_connection_health_action(self,status):
        output,result=self.run_action('<ACTION type="CHECK_GOOGLE_CONNECTION" />')
        self.assertIn("Success",output); self.assertIn("valid",result)

    @patch("providers.google_api.sync_contacts",return_value=142)
    def test_explicit_contact_sync_action(self,sync):
        output,result=self.run_action('<ACTION type="SYNC_GOOGLE_CONTACTS" />')
        sync.assert_called_once_with("."); self.assertIn("142",result)

    @patch("providers.google_api.recent_completed_events",return_value=[{"summary":"Exam","start":"yesterday"}])
    def test_recent_events_stay_local(self,recent):
        output,result=self.run_action('<ACTION type="LIST_RECENT_EVENTS" hours_back="48" />')
        self.assertIn("Found 1",output); self.assertIn("Exam",result)

    @patch("followups.status",return_value="Recent event follow-ups:\n- Exam — answered")
    def test_followup_ledger_action(self,status):
        output,result=self.run_action('<ACTION type="LIST_FOLLOWUPS" />')
        self.assertIn("Success",output); self.assertIn("answered",result)

    def test_actions_are_code_owned_and_sync_is_not_proactive(self):
        for action in ("CHECK_GOOGLE_CONNECTION","SYNC_GOOGLE_CONTACTS","LIST_RECENT_EVENTS","LIST_FOLLOWUPS"):
            self.assertIn(action,autonomy.KNOWN_ACTIONS)
        self.assertFalse(autonomy.action_allowed("SYNC_GOOGLE_CONTACTS","proactive")[0])


if __name__=="__main__": unittest.main()