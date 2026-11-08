import ast,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime import ConversationRuntime,Event,EventKind,RuntimeState,EvidenceItem,EvidenceKind,PrivacyPolicy
from runtime.action_adapter import execute_legacy
from runtime.models import ActionStatus

class FakeStore:
    def __init__(self): self.saved=[]; self.latest=None
    def save_checkpoint(self,*args): self.saved.append(args)
    def latest_checkpoint(self,session_id=None): return self.latest

def test_transitions_and_checkpoints():
    store=FakeStore(); runtime=ConversationRuntime(store,"s1")
    runtime.dispatch(Event(EventKind.USER_INPUT,{"text":"do it","turn_id":"t1","task_id":"task1"}))
    assert runtime.state is RuntimeState.THINKING
    runtime.dispatch(Event(EventKind.ACTION_REQUESTED,{})); assert runtime.state is RuntimeState.ACTING
    runtime.interrupt("wait"); assert runtime.state is RuntimeState.PAUSED
    runtime.dispatch(Event(EventKind.ACTION_COMPLETED,{})); assert runtime.state is RuntimeState.THINKING
    runtime.dispatch(Event(EventKind.MODEL_OUTPUT,{})); runtime.dispatch(Event(EventKind.COMPLETE,{}))
    assert runtime.state is RuntimeState.IDLE and len(store.saved)>=6

def test_invalid_transition_and_recovery():
    store=FakeStore(); runtime=ConversationRuntime(store,"s1")
    try: runtime.dispatch(Event(EventKind.ACTION_COMPLETED,{})); assert False
    except RuntimeError: pass
    store.latest={"task_id":"x","state":"acting","payload_json":json.dumps({"turn_id":"t","context":{"a":1}})}
    assert runtime.restore_latest() and runtime.state is RuntimeState.PAUSED

def test_late_action_completion_does_not_revive_cancelled_task():
    store=FakeStore(); runtime=ConversationRuntime(store,"s1")
    runtime.dispatch(Event(EventKind.USER_INPUT,{"text":"do it"}))
    runtime.dispatch(Event(EventKind.ACTION_REQUESTED,{}))
    runtime.cancel("abort")
    runtime.dispatch(Event(EventKind.ACTION_COMPLETED,{"context":{"receipt":"released inputs"}}))
    assert runtime.state is RuntimeState.CANCELLED
    assert runtime.context["receipt"]=="released inputs"

def test_privacy_and_evidence():
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/"config.json"; path.write_text('{"privacy":{"email":{"dashboard":true}}}',encoding="utf-8")
        policy=PrivacyPolicy(path)
        assert policy.allows("email","dashboard") and not policy.allows("email","airouter")
    hypothesis=EvidenceItem("maybe useful",EvidenceKind.HYPOTHESIS,"model",.4,False)
    fact=EvidenceItem("confirmed",EvidenceKind.USER_MEMORY,"user",1,True)
    assert not hypothesis.may_become_fact() and fact.may_become_fact()

def test_typed_legacy_adapter():
    def dispatcher(*args,**kwargs):
        dispatcher._last_output="[Approval Required] queued"
        dispatcher._blocked_reason=None
    outcome=execute_legacy(dispatcher,'<ACTION type="SEND_EMAIL" />',{},None,'.')
    assert outcome.action_type=="SEND_EMAIL" and outcome.status is ActionStatus.PENDING_APPROVAL

def test_agent_runtime_state_is_bound_for_every_turn():
    """A user action must not depend on an import in the proactive branch."""
    source=(Path(__file__).resolve().parents[1]/"agent.py").read_text(
        encoding="utf-8",errors="replace")
    tree=ast.parse(source)
    function=next(node for node in tree.body
                  if isinstance(node,ast.FunctionDef)
                  and node.name=="_inner_run_agent_loop")
    unconditional=[]
    for node in function.body:
        if isinstance(node,ast.ImportFrom) and node.module=="runtime":
            unconditional.extend(alias.name for alias in node.names)
        if isinstance(node,(ast.If,ast.Try,ast.For,ast.While,ast.With)):
            break
    assert "RuntimeState" in unconditional

def run():
    test_transitions_and_checkpoints(); test_invalid_transition_and_recovery()
    test_privacy_and_evidence(); test_typed_legacy_adapter()
    print("Conversation runtime checks passed")

if __name__=="__main__": run()
