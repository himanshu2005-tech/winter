# Useful cognitive and background work

VANTA has one main assistant/execution orchestrator. The cognitive router,
planner, executor, critic, proactive director, researcher and curiosity worker
have specialised roles. Some run in background threads and make separate model
calls, sharing the existing memory and action system. This is not an independent
multi-agent delegation network, and no such network is added by these changes.

The cognitive router selects evidence, planning depth, mode and operation
intents. Prepared research is retrieved for the current request before planning,
then passed to execution once, with its summary and source links. This does not
make an extra model call. Generic conversation does not automatically receive
unrelated research. The model still decides how the evidence helps the task.

Curiosity studies public topics connected to active work while the idle/battery
and spending gates permit it. Topic selection now sees the next steps and
blockers on open pursuits. Failed graph writes are not reported as learned
knowledge; rejected facts are not counted. A short retry cooldown applies to
unsuccessful work, rather than treating it as a completed two-week research topic.

Only findings actually included in a prompt with a usable model response consume
their prompt-exposure allowance. Exposures do not mean the user was shown the
result. Relevant prepared evidence remains retrievable after that allowance,
so it is not forgotten merely because it was offered before. Graph knowledge
retains its existing low-authority external-observation classification.

The proactive director can see prepared findings alongside current resources
and pursuits. It asks for a concrete user benefit and an observable objective;
queued work still passes through ordinary planning, policy and receipt checks.
The local director state records queued versus quiet or suppressed work, never
claims that placing an objective on a queue completed it.

Recent objectives are deduplicated even if their explanation changes or other
objectives run between them. Priority escalation can bypass the cooldown once.
Resource-change bursts are coalesced under the existing minimum review interval.
Background model adapters are invoked once; an internal TypeError cannot cause
an unbudgeted second model call.

These changes preserve permissions and do not start new research sessions or
write personal data into the production graph during development tests.
