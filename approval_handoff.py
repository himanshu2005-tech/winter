"""Hand approved work to the existing action-lock owner, not a competing executor."""
import queue
from dataclasses import dataclass


@dataclass
class ApprovedWork:
    response: str
    action_type: str
    approval_id: str
    outcome_id: str = ''
    cancelled: bool = False


class ApprovalHandoff:
    """Taking a job is atomic; execution must still hold the runtime action lock."""
    def __init__(self):
        self.jobs = queue.Queue()
        self.notices = queue.Queue()

    def submit(self, response, action_type, approval_id, outcome_id=''):
        work = ApprovedWork(response, action_type, approval_id, outcome_id)
        self.jobs.put(work)
        return work

    def drain(self, execute, notify=True, cancel=None):
        results = []
        while True:
            try:
                work = self.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                if work.cancelled:
                    result = cancel(work) if cancel else {'type': work.action_type, 'ok': False, 'output': '[Cancelled] Approved action cancelled before execution.'}
                else:
                    result = execute(work, notify)
                results.append(result)
            except Exception as exc:
                results.append({'type': work.action_type, 'ok': False,
                                'approval_id': work.approval_id,
                                'output': f'[Unverified] Approved execution raised {type(exc).__name__}; completion is unknown.'})
            finally:
                self.jobs.task_done()
        return results

    def cancel_pending(self):
        # Retain cancellation receipts for the owner to consume, but never run
        # these jobs even if a later turn clears the global cancel flag.
        with self.jobs.mutex:
            for work in self.jobs.queue:
                work.cancelled = True

    def notice(self, action_type, verdict):
        self.notices.put({'type': action_type, 'ok': False, 'output': f'[Approval] {verdict}; action was not executed.'})

    def take_notices(self):
        values = []
        while True:
            try:
                values.append(self.notices.get_nowait())
            except queue.Empty:
                return values


def wait_for_answer(answers, pump, timeout, clock, cancelled=lambda: False):
    """Wake on actual execution/decision or a normal clarification answer."""
    deadline = clock()+timeout
    while not cancelled():
        receipts = pump()
        if receipts:
            return 'approval_result', receipts
        remaining = deadline-clock()
        if remaining <= 0:
            raise queue.Empty
        try:
            return answers.get(timeout=min(.2, remaining))
        except queue.Empty:
            continue
    return 'cancelled', []
