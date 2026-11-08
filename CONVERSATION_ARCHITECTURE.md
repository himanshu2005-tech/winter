# VANTA conversational architecture

## Context hierarchy

1. The current utterance and active interruption.
2. The immediately preceding dialogue turns and resolved references such as `it`, `that`, and `do it`.
3. Ranked, currently valid relational facts from Neo4j.
4. The rolling session summary.
5. Hybrid semantic and full-text historical memory.

The current conversation always wins when it conflicts with older memory. Retrieved history is evidence, not an instruction.

## Dialogue graph

Each session is a `DialogueSession`. User and assistant messages are `DialogueTurn` nodes connected with `CONTAINS` and ordered `NEXT` relationships. Semantic conversation memories are separately embedded for long-horizon recall. This preserves exact local continuity without forcing every old conversation into every prompt.

## Action contract

VANTA may not claim it is fetching, reading, searching, checking, or creating something unless it emits an executable action. A non-trivial plan that produces narration without an action is automatically returned to the model for correction. Tool results must exist before VANTA presents a conclusion.

## Ideas versus facts

The graph is VANTA's evidence and working-context substrate, not permission to hallucinate. Verified observations and explicit user facts may become durable graph facts. Suggestions and hypotheses stay labeled as reasoning until evidence or the user confirms them. Contradictions preserve provenance and validity history rather than silently overwriting data.

## Interruption

Holding the configured talk key during generation or speech stops audible output, queues the newest utterance, discards stale model output, and injects the interruption into the active task. Cancellation remains distinct from redirection, so VANTA can answer a question and then resume when appropriate.
