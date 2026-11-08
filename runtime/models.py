from __future__ import annotations
from dataclasses import dataclass,field
from enum import Enum
from typing import Any,Dict,Optional
import time,uuid

class RuntimeState(str,Enum):
    IDLE="idle"; LISTENING="listening"; THINKING="thinking"; ACTING="acting"
    SPEAKING="speaking"; PAUSED="paused"; CANCELLED="cancelled"; FAILED="failed"

class EventKind(str,Enum):
    USER_INPUT="user_input"; MODEL_OUTPUT="model_output"; ACTION_REQUESTED="action_requested"
    ACTION_COMPLETED="action_completed"; INTERRUPT="interrupt"; CANCEL="cancel"
    PAUSE="pause"; RESUME="resume"; COMPLETE="complete"; ERROR="error"

class ActionStatus(str,Enum):
    SUCCEEDED="succeeded"; FAILED="failed"; BLOCKED="blocked"; PENDING_APPROVAL="pending_approval"; INTERRUPTED="interrupted"

class EvidenceKind(str,Enum):
    VERIFIED_FACT="verified_fact"; USER_MEMORY="user_memory"; OBSERVATION="observation"
    SUGGESTION="suggestion"; HYPOTHESIS="hypothesis"

@dataclass(frozen=True)
class EvidenceItem:
    content:str
    kind:EvidenceKind
    source_id:str
    confidence:float=1.0
    confirmed:bool=False

    def may_become_fact(self):
        return self.kind in {EvidenceKind.VERIFIED_FACT,EvidenceKind.USER_MEMORY} and self.confirmed

@dataclass(frozen=True)
class Event:
    kind:EventKind
    payload:Dict[str,Any]=field(default_factory=dict)
    id:str=field(default_factory=lambda:uuid.uuid4().hex)
    timestamp:float=field(default_factory=time.time)

@dataclass
class ActionOutcome:
    action_type:str
    status:ActionStatus
    summary:str
    data:Optional[Any]=None
    error:Optional[str]=None
    retryable:bool=False
    verified:bool=False
    handle_id:Optional[str]=None

    @property
    def succeeded(self): return self.status is ActionStatus.SUCCEEDED

    def model_text(self):
        parts=[f"Action {self.action_type}: {self.status.value}.",self.summary]
        if self.error: parts.append(f"Error: {self.error}")
        if self.handle_id: parts.append(f"Private result handle: {self.handle_id}")
        return "\n".join(part for part in parts if part)
