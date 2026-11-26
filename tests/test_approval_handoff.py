"""Reproduce button approval while ASK owns the agent lock, with no real actions."""
import json
import queue
import threading
import time
from unittest import mock

import pytest

from approval_handoff import ApprovalHandoff, wait_for_answer


def test_lock_owner_can_complete_work_while_fallback_waits_without_double_execution():
    handoff, lock, calls = ApprovalHandoff(), threading.Lock(), []
    def execute(work, notify):
        calls.append(work.approval_id)
        return {'type':work.action_type,'ok':True,'output':'[Success] Synthetic action completed.'}
    def fallback():
        with lock:
            handoff.drain(execute)
    with lock:
        handoff.submit('synthetic', 'RUN_COMMAND', 'approval-1')
        thread = threading.Thread(target=fallback)
        thread.start()
        answer = wait_for_answer(queue.Queue(), lambda: handoff.drain(execute, notify=False), 1, time.monotonic)
        assert answer[0]=='approval_result' and answer[1][0]['ok']
        assert calls == ['approval-1']
    thread.join(timeout=2)
    assert not thread.is_alive() and calls==['approval-1']


def test_late_control_decision_interrupts_question_wait():
    handoff, ready = ApprovalHandoff(), threading.Event()
    def pump():
        ready.set()
        return handoff.take_notices()
    def decide():
        ready.wait(1)
        handoff.notice('SEND_EMAIL', 'rejected')
    thread=threading.Thread(target=decide);thread.start()
    answer=wait_for_answer(queue.Queue(),pump,2,time.monotonic)
    thread.join(timeout=1)
    assert answer[0]=='approval_result' and answer[1][0]['ok'] is False


def test_cancel_pending_jobs_never_executes_and_failures_are_not_success():
    handoff=ApprovalHandoff()
    handoff.submit('synthetic','RUN_COMMAND','approval-1')
    handoff.cancel_pending()
    execute=mock.Mock()
    assert handoff.drain(execute)[0]['ok'] is False
    execute.assert_not_called()
    handoff.submit('synthetic','RUN_COMMAND','approval-2')
    execute.side_effect=RuntimeError('synthetic failure')
    assert handoff.drain(execute)[0]['output'].startswith('[Unverified]')
    assert handoff.jobs.unfinished_tasks==0


def test_ordinary_answers_and_timeouts_remain_distinct():
    answers=queue.Queue();answers.put(('text','The blue one'))
    assert wait_for_answer(answers,lambda:[],1,time.monotonic)==('text','The blue one')
    with pytest.raises(queue.Empty):
        wait_for_answer(queue.Queue(),lambda:[],0,time.monotonic)
    assert wait_for_answer(queue.Queue(),lambda:[],1,time.monotonic,cancelled=lambda:True)[0]=='cancelled'


@pytest.mark.parametrize('phrase,source,pinned', [
    ('approve','button',True), ('approved','voice',False),
    ('approved','text',False), ('[Telegram]: approved','telegram',False)])
def test_real_settlement_executes_under_ask_owner_and_resumes_from_receipt(monkeypatch, phrase, source, pinned):
    import agent
    import actions
    import memory.state_db
    from memory.neo4j_store import store
    handoff=ApprovalHandoff();lock=threading.Lock();threads=[];calls=[]
    monkeypatch.setattr(agent,'_approval_handoff',handoff)
    monkeypatch.setattr(agent,'_agent_lock',lock)
    monkeypatch.setattr(agent,'ui_queue',queue.Queue())
    monkeypatch.setattr(agent,'speak',mock.Mock())
    monkeypatch.setattr(agent,'pending_approval_exists',lambda:True)
    monkeypatch.setattr(memory.state_db,'execute_read',lambda *a,**k:[('approval-test','RUN_COMMAND',json.dumps({'cmd':'SYNTHETIC_NEVER_RUN'}))])
    writes=mock.Mock();monkeypatch.setattr(memory.state_db,'execute_write',writes)
    graph=mock.Mock(return_value=[]);monkeypatch.setattr(store,'query',graph)
    def thread_factory(*args,**kwargs):
        thread=threading.Thread(*args,**kwargs);threads.append(thread);return thread
    monkeypatch.setattr(agent,'Thread',thread_factory)
    def synthetic(*args,**kwargs):
        calls.append(args[0])
        synthetic._last_output='RUN_COMMAND: [Success] Synthetic command completed.'
        synthetic._results=[{'type':'RUN_COMMAND','ok':True,'output':'[Success] Synthetic command completed.'}]
    monkeypatch.setattr(actions,'execute_actions',synthetic)
    with lock:
        assert agent.handle_approval_input(phrase,approval_id='approval-test' if pinned else '',source=source)
        answer=wait_for_answer(queue.Queue(),lambda:agent._drain_approved_actions(lock_owned=True),1,time.monotonic)
        assert answer[0]=='approval_result' and answer[1][0]['ok'] is True
        assert len(calls)==1 and 'SYNTHETIC_NEVER_RUN' in calls[0]
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(calls)==1
    assert any("a.status='executed'" in c.args[0] for c in graph.call_args_list)


def test_empty_approved_action_result_is_not_reported_as_success(monkeypatch):
    import agent,actions
    import approval_grants
    from memory.neo4j_store import store
    monkeypatch.setattr(agent,'_agent_lock',threading.Lock())
    monkeypatch.setattr(agent,'ui_queue',queue.Queue())
    monkeypatch.setattr(store,'query',mock.Mock())
    monkeypatch.setattr(approval_grants,'grant',mock.Mock())
    monkeypatch.setattr(actions,'execute_actions',mock.Mock())
    result=agent._run_approved_action('<ACTION type="RUN_COMMAND"/>','RUN_COMMAND','synthetic-id',report_completion=False)
    assert result['ok'] is False
