from __future__ import annotations
from collections import deque
import json,threading,uuid
from runtime.models import Event,EventKind,RuntimeState

TRANSITIONS={
    RuntimeState.IDLE:{EventKind.USER_INPUT:RuntimeState.THINKING,EventKind.RESUME:RuntimeState.THINKING},
    RuntimeState.LISTENING:{EventKind.USER_INPUT:RuntimeState.THINKING,EventKind.CANCEL:RuntimeState.CANCELLED},
    RuntimeState.THINKING:{EventKind.MODEL_OUTPUT:RuntimeState.SPEAKING,EventKind.ACTION_REQUESTED:RuntimeState.ACTING,EventKind.INTERRUPT:RuntimeState.THINKING,EventKind.CANCEL:RuntimeState.CANCELLED,EventKind.ERROR:RuntimeState.FAILED},
    RuntimeState.ACTING:{EventKind.ACTION_COMPLETED:RuntimeState.THINKING,EventKind.INTERRUPT:RuntimeState.PAUSED,EventKind.PAUSE:RuntimeState.PAUSED,EventKind.CANCEL:RuntimeState.CANCELLED,EventKind.ERROR:RuntimeState.FAILED},
    RuntimeState.SPEAKING:{EventKind.INTERRUPT:RuntimeState.THINKING,EventKind.COMPLETE:RuntimeState.IDLE,EventKind.ACTION_REQUESTED:RuntimeState.ACTING,EventKind.CANCEL:RuntimeState.CANCELLED},
    RuntimeState.PAUSED:{EventKind.RESUME:RuntimeState.THINKING,EventKind.USER_INPUT:RuntimeState.THINKING,EventKind.ACTION_COMPLETED:RuntimeState.THINKING,EventKind.CANCEL:RuntimeState.CANCELLED},
    # An action may finish unwinding after Abort. Its late completion is
    # evidence, not permission to revive the cancelled task.
    RuntimeState.CANCELLED:{EventKind.USER_INPUT:RuntimeState.THINKING,EventKind.COMPLETE:RuntimeState.IDLE,EventKind.ACTION_COMPLETED:RuntimeState.CANCELLED},
    RuntimeState.FAILED:{EventKind.USER_INPUT:RuntimeState.THINKING,EventKind.RESUME:RuntimeState.THINKING,EventKind.COMPLETE:RuntimeState.IDLE},
}

class ConversationRuntime:
    def __init__(self,store,session_id,ui_queue=None,event_limit=200):
        self.store=store; self.session_id=session_id; self.ui_queue=ui_queue
        self.state=RuntimeState.IDLE; self.task_id=None; self.turn_id=None
        self.events=deque(maxlen=event_limit); self.context={}; self._lock=threading.RLock()

    def dispatch(self,event):
        if not isinstance(event,Event): raise TypeError("dispatch requires Event")
        with self._lock:
            next_state=TRANSITIONS.get(self.state,{}).get(event.kind)
            if next_state is None:
                raise RuntimeError(f"Invalid runtime transition: {self.state.value} + {event.kind.value}")
            if event.kind is EventKind.USER_INPUT:
                self.task_id=event.payload.get("task_id") or uuid.uuid4().hex
                self.turn_id=event.payload.get("turn_id")
            self.state=next_state; self.events.append(event)
            self.context.update(event.payload.get("context",{}))
            self.checkpoint(event.kind.value,event.payload)
            if self.ui_queue:
                self.ui_queue.put((self.state.value,event.payload.get("message",event.payload.get("text",""))))
            return self.state

    def checkpoint(self,reason,payload=None):
        if not self.task_id: return
        snapshot={"reason":reason,"turn_id":self.turn_id,"context":self.context,"event_ids":[event.id for event in self.events],"payload":payload or {}}
        self.store.save_checkpoint(f"{self.task_id}:latest",self.task_id,self.session_id,self.state.value,snapshot)

    def restore_latest(self):
        row=self.store.latest_checkpoint(self.session_id)
        if not row: return False
        payload=json.loads(row.get("payload_json") or "{}")
        self.task_id=row.get("task_id"); self.turn_id=payload.get("turn_id")
        self.context=payload.get("context") or {}; self.state=RuntimeState(row.get("state","paused"))
        if self.state in {RuntimeState.ACTING,RuntimeState.THINKING,RuntimeState.SPEAKING}:
            self.state=RuntimeState.PAUSED
        return True

    def interrupt(self,text):
        return self.dispatch(Event(EventKind.INTERRUPT,{"text":text,"message":text,"context":{"latest_interruption":text}}))

    def cancel(self,reason="user_cancelled"):
        return self.dispatch(Event(EventKind.CANCEL,{"message":reason}))
