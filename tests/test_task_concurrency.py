
import os
import sys

# Every other test file carries this. Without it these four only passed
# under a runner that happened to put the project root on sys.path, so
# `python tests/test_x.py` failed with ModuleNotFoundError -- a suite that
# depends on how it is invoked is not a suite you can trust.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect
import queue
import unittest
import threading
from unittest.mock import patch


class TaskConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import agent
        self.agent = agent
        while True:
            try:
                agent.interruption_queue.get_nowait()
            except queue.Empty:
                break
        with agent._interruption_lock:
            agent._interruption_keys.clear()
        agent._barge_in_event.clear()
        with agent._proactive_lock:
            agent._proactive_backlog.clear()
            agent._proactive_keys.clear()

    def test_distinct_interruptions_are_fifo_and_never_concatenated(self):
        self.agent._queue_interruption("play music", "voice")
        self.agent._queue_interruption("read my mail", "text")
        self.assertEqual(self.agent._take_interruption(),
                         ("voice", "play music"))
        self.assertEqual(self.agent._take_interruption(),
                         ("text", "read my mail"))

    def test_duplicate_pending_request_is_stored_once(self):
        self.agent._queue_interruption("Play   music", "voice")
        self.agent._queue_interruption("play music", "text")
        self.assertEqual(self.agent.interruption_queue.qsize(), 1)

    def test_barge_event_stays_set_while_more_work_is_queued(self):
        self.agent._queue_interruption("first", "voice")
        self.agent._queue_interruption("second", "voice")
        self.agent._take_interruption()
        self.assertTrue(self.agent._barge_in_event.is_set())
        self.agent._take_interruption()
        self.assertFalse(self.agent._barge_in_event.is_set())

    def test_proactive_events_are_deduplicated(self):
        self.assertTrue(self.agent._defer_proactive("check weather"))
        self.assertFalse(self.agent._defer_proactive("  check   weather "))
        self.assertEqual(len(self.agent._proactive_backlog), 1)

    def test_approved_actions_share_the_agent_transaction_lock(self):
        source = inspect.getsource(self.agent._run_approved_action)
        self.assertIn("with _agent_lock", source)

    def test_busy_user_task_is_queued_not_dropped(self):
        source = inspect.getsource(self.agent.run_agent_loop)
        busy = source[source.index("if not acquired:"):]
        self.assertIn("_queue_interruption", busy)
        self.assertNotIn("ask me again", busy.lower())

    def test_completion_boundary_request_becomes_a_fresh_fifo_task(self):
        source = inspect.getsource(self.agent._inner_run_agent_loop)
        marker = source.index("Request arrived at completion; queued as a fresh task")
        tail = source[marker:marker + 500]
        self.assertIn("_queue_interruption(interruption_text, source)", tail)
        self.assertNotIn("_barge_in_prompt(interruption_text, plan)", tail)

    def test_action_discovery_gets_bounded_budget_replacement(self):
        source = inspect.getsource(self.agent._inner_run_agent_loop)
        self.assertIn("max_discovery_extensions = 2", source)
        self.assertIn("discovery_extensions += 1", source)
        self.assertIn("execution_budget + discovery_extensions", source)

    def test_turn_context_begins_only_after_execution_slot_is_owned(self):
        source = inspect.getsource(self.agent.run_agent_loop)
        self.assertLess(source.index("if not acquired:"),
                        source.index("turn_context.begin("))
        self.assertLess(source.index("turn_context.begin("),
                        source.index("usage_before ="))

    def test_router_call_metadata_is_thread_local(self):
        import llm_router
        seen = {}

        def worker(name, failure, usage):
            llm_router._note_failure(failure)
            llm_router._CALL_STATE.last_usage = {"prompt": usage}
            seen[name] = (llm_router.failure_kind(),
                          llm_router._CALL_STATE.last_usage["prompt"])

        first = threading.Thread(target=worker,
                                 args=("foreground", "timeout", 10))
        second = threading.Thread(target=worker,
                                  args=("research", "billing credit", 99))
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(seen["foreground"], ("timeout", 10))
        self.assertEqual(seen["research"], ("billing", 99))

    def test_deliberate_shutdown_closes_browser_before_hard_exit(self):
        source = inspect.getsource(self.agent._graceful_shutdown)
        self.assertIn("browser.close(all_tabs=True)", source)
        self.assertLess(source.index("browser.close(all_tabs=True)"),
                        source.index("os._exit(0)"))
        self.assertIn("done.wait(timeout=4.0)", source)

    def test_abort_clears_every_ephemeral_task_queue_atomically(self):
        old_input = self.agent.input_queue
        old_clarification = self.agent.clarification_queue
        self.agent.input_queue = queue.Queue()
        self.agent.clarification_queue = queue.Queue()
        self.agent._queue_interruption("second user task", "voice")
        self.agent._defer_proactive("background review")
        self.agent.input_queue.put(("text", "third user task"))
        # Security/control events are not work and must survive Abort.
        self.agent.input_queue.put(("approval", "approve", "a-1"))
        acquired = self.agent._agent_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with patch.object(self.agent, "_get_conversation_runtime") as runtime:
                self.assertTrue(self.agent._cancel_active_task("test"))
            runtime.return_value.cancel.assert_called_once_with()
            self.assertTrue(self.agent.cancel_flag)
            self.assertTrue(self.agent.interruption_queue.empty())
            self.assertEqual(self.agent._proactive_backlog, [])
            self.assertEqual(self.agent._proactive_keys, set())
            self.assertEqual(
                self.agent.input_queue.get_nowait(),
                ("approval", "approve", "a-1"))
            with self.assertRaises(queue.Empty):
                self.agent.input_queue.get_nowait()
        finally:
            self.agent._agent_lock.release()
            self.agent.input_queue = old_input
            self.agent.clarification_queue = old_clarification
            self.agent.cancel_flag = False
            self.agent._barge_in_event.clear()


if __name__ == "__main__":
    unittest.main()
