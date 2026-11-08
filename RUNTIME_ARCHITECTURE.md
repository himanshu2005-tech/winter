# VANTA runtime architecture

VANTA now places an event-driven conversational runtime between `agent.py` and the existing providers.

## State machine

Tasks move through validated states: `idle`, `listening`, `thinking`, `acting`, `speaking`, `paused`, `cancelled`, and `failed`. Invalid transitions raise immediately rather than silently corrupting the conversation. User input, model output, action requests/results, interruptions, cancellation, pause/resume, completion, and failure are typed events.

## Typed action boundary

`runtime/action_adapter.py` translates the legacy dispatcher into `ActionOutcome`: action type, status, summary, error, retryability, verification, and private handle. The conversation loop no longer reads the legacy `_last_output` global directly. Providers can now be migrated behind this interface one at a time.

## Recovery

Every state-changing event is checkpointed as a `TaskCheckpoint` in Neo4j. If VANTA restarts while thinking, acting, or speaking, the task restores as paused instead of pretending it completed. A new user turn supersedes and checkpoints unfinished work explicitly.

## Interruption

Barge-in and cancellation are runtime events. Speech still stops immediately; the active task and latest interruption are checkpointed before the model is redirected.

## Evidence

Evidence is typed as verified fact, user memory, observation, suggestion, or hypothesis. Only confirmed facts and user memories qualify for durable fact promotion. Suggestions and hypotheses must remain distinguishable from knowledge.

## Inference budget

VANTA uses one executor completion for ordinary conversation, one-step actions,
and screen questions. A separate cognitive-router completion is reserved for
mode changes, safety/protocol decisions, explicitly deep work, long ambiguous
requests, and multi-step planning. Direct personal turns still retrieve a
small, ranked graph/context bundle, so saving inference calls never disables
durable knowledge. Standard and deep turns retain planner and receipt critic
stages; direct turns do not pay for either stage.

The model provider is independent from memory. Switching between ChatGPT-plan,
AIRouter, and local inference must not copy or discard graph knowledge. Provider
failover changes computation only; Neo4j remains the governed source of durable
facts and conversation evidence.

## Privacy

Per-data-class destinations are configured in `vault/config.json`. Email and contacts deny AIRouter, dashboard, and speech disclosure by default. Screen, calendar, and memory have independent controls. Screen capture is checked before pixels are attached to an AIRouter request.

## Compatibility

The existing Google, browser, alarm, Spotify, file, and safety implementations remain intact behind the adapter. This avoids a high-risk all-at-once provider rewrite while giving new providers a stable typed interface.
