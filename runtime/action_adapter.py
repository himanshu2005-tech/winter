from __future__ import annotations
import re
from runtime.models import ActionOutcome,ActionStatus

def _status_from_output(raw):
    text=(raw or "").lstrip()
    if text.startswith("[Approval Required]"): return ActionStatus.PENDING_APPROVAL
    if text.startswith("[Interrupted]"): return ActionStatus.INTERRUPTED
    if text.startswith(("[Authorization Policy]","[Autonomy Policy]","[Mode Policy]",
                        "[Foreground Observer Mode]","[Confirmation Required]",
                        "[Unsupported Action]")):
        return ActionStatus.BLOCKED
    if text.startswith(("[Error]","[Action Error]","Error:")): return ActionStatus.FAILED
    return ActionStatus.SUCCEEDED

def execute_legacy(dispatcher,response,element_map,ui_queue,base_dir,**kwargs):
    dispatcher(response,element_map,ui_queue,base_dir,**kwargs)
    results=list(getattr(dispatcher,"_results",[]) or [])
    blocked=str(getattr(dispatcher,"_blocked_reason","") or "")
    raw=str(getattr(dispatcher,"_last_output","") or "")

    if not results:
        status=ActionStatus.BLOCKED if blocked else _status_from_output(raw)
        summary=blocked or raw or "No executable action was parsed."
        # An action queued for approval or refused outright produces no
        # receipt, and this said "UNKNOWN" -- so the outcome read "approval
        # pending on UNKNOWN" when the response plainly named SEND_EMAIL.
        # Whatever consumes this then cannot say what is waiting on Himanshu.
        named=re.search(r"""<ACTION\s+type=["']([A-Z_0-9]+)""", response or "")
        return ActionOutcome(named.group(1) if named else "UNKNOWN",
                             status,summary,
                             error=summary if status is not ActionStatus.SUCCEEDED else None,
                             retryable=status is ActionStatus.FAILED,
                             verified=status is ActionStatus.SUCCEEDED)

    # Per-action receipts, so a turn with several actions reports exactly which
    # one failed instead of collapsing into one ambiguous blob.
    lines=[]
    for item in results:
        state="OK" if item.get("ok") else "FAILED"
        lines.append(f"[{state}] {item.get('type')}: {item.get('output','')}")
    summary="\n".join(lines)

    failed=[item for item in results if not item.get("ok")]
    if blocked:
        status=ActionStatus.BLOCKED
    elif not failed:
        status=ActionStatus.SUCCEEDED
    else:
        status=_status_from_output(failed[0].get("output",""))
        if status is ActionStatus.SUCCEEDED:
            status=ActionStatus.FAILED

    action_type=results[0].get("type","UNKNOWN") if len(results)==1 else "MULTI"
    handle=None
    found=re.search(r'Handle:\s*([A-Za-z0-9_-]+)',summary)
    if found: handle=found.group(1)
    return ActionOutcome(action_type,status,summary,
                         data=results,
                         error=(blocked or (failed[0].get("output") if failed else None)),
                         retryable=status is ActionStatus.FAILED,
                         verified=not failed and status is ActionStatus.SUCCEEDED,
                         handle_id=handle)
