# Approval handoff and report evidence

The supplied log records an actual button approval, then a clarification
timeout and an incorrect model statement that no approval was given. The
settlement worker had to acquire the agent lock while the agent held it
waiting on a different clarification queue. Approval was durable, but its
execution could not proceed until that task eventually released the lock.

Approved payloads now enter a process-local handoff queue after the existing
pending-record settlement and payload-integrity checks. An idle worker drains
it under the normal action lock. An active task already owning that lock can
drain the same queue between steps or while waiting for clarification. Atomic
dequeue plus the shared action lock prevents both paths from executing a job.
Buttons, speech, typed input, Telegram and gestures retain the same settlement
entry point and code-owned authorization rules.

Clarification waits poll the control handoff with bounded waits. Executed
approvals return actual receipts into the active conversation instead of
becoming a synthetic “no response”. Approval is not proof of execution: a
failed or empty result stays failed. Abort marks queued jobs cancelled so a
later turn cannot revive them merely by clearing a global cancellation flag.
No code can undo an external action that has already finished.

The other failure was a report/file mismatch. A complete REPORT was withheld
for review, but the receipt-only critic could see only a short narration and
search results. It invented a disk artifact requirement and requested Word,
then Markdown files to “prove” the report. Reports now supply code-compiled
content evidence (source, counts and hash), distinct from factual verification
and external mutation receipts. The critic receives the original request and
complete report excerpt, and must not demand a file unless the user requested
an export. Saving the same prose is never factual corroboration.

The basic Mermaid `timeline` grammar in the supplied report now renders as
an SVG instead of falling back to code. Existing unsupported grammars retain
their readable fallback; arbitrary Mermaid/HTML scripts are not executed.

Tests reproduce an ASK owner holding the lock, approvals through the shared
entry point, a waiting fallback worker, late decisions, cancellations and
exactly-once execution using synthetic payloads and mocked stores/executors.
They never run the supplied shell command or replay production approvals.
