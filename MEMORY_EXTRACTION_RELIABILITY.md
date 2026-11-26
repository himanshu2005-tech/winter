# Memory extraction and startup reports

The supplied startup log showed Neo4j becoming ready, followed by repeated
`fact extractor returned invalid JSON` failures while recovering 14 pending
turns. The failure was in extraction, not evidence of a database outage.

The model router returns reasoning inside `<think>` followed by final output.
The old greedy bracket regex could combine reasoning examples/empty arrays
with the final JSON array, making a valid answer fail to parse. Extraction now
excludes reasoning, balances complete JSON documents and accepts a single
array of fact objects (or an explicit `facts`/`triples` wrapper). An unfinished,
ambiguous or malformed answer remains a failure; it is never converted to
`[]`, and no missing facts are invented.

Memory uses the existing configured models through the `memory_extraction`
stage, with structured temperature and a 4000-token default allowance.
`WINTER_BUDGET_MEMORY_EXTRACTION` can override that allowance. Its usage is
attributed separately from foreground `routine` calls.

A worker-wide exponential cooldown with jitter (capped at 60 seconds) prevents
an invalid-output backlog from generating rapid retries. Repeated diagnostics
are throttled to 30 seconds. Each retry callback binds its own source tuple.
Before starting a new extraction, the worker yields while the registered
foreground-turn/recording probe is busy. It does not import or start another
agent process, and normal environmental silence is not part of this gate.

Existing DialogueTurn evidence and pending work stay in Neo4j. Invalid output
and graph writes that cannot be verified remain retryable; only completed,
verified work is marked complete. This change does not migrate or delete data.
Existing bounded in-process retries and startup recovery remain in place.

The startup briefing was also excluded by the user-only report visibility
condition even when the model supplied REPORT. The gate now admits the named
startup briefing in addition to explicit user reports. Ordinary proactive
events still do not open report windows.

Tests use synthetic turns, mocked providers and stores, and isolated network
guards. They do not replay the user's pending production turns. Restarting
VANTA resumes its normal durable recovery with the corrected parser.
