# Graph lifecycle hardening

The existing Entity–FACT–Entity and Memory architecture is preserved. No
production nodes, facts, profiles or evidence were deleted during this work.

## Saving information

The model chooses what is useful beyond the current conversation. Personal
extraction now requests a durable/transient retention decision, and the writer
requires an actual supporting quote. Missing, invented or transient evidence
cannot become an automatically extracted personal fact. Existing extraction
outputs without a retention field remain compatible if grounded in a quote.
This proves provenance, not semantic entailment; model extraction can still err.

The graph boundary validates nonempty entities, canonical predicates, finite
confidence, sensitivity and fact classes before any entity write. Established
bootstrap classes remain supported. Repeating a fact cannot lower its privacy
classification. Stronger evidence updates its trust classification; weak
connector observations cannot supersede higher-authority facts.

## Correcting and forgetting

CORRECT_FACT uses one managed Neo4j transaction for retirement, replacement and
supersession links. Failed replacement rolls back retirement. Restricted facts
stay restricted when corrected. An explicit stale old-object selector fails,
and ambiguous multi-valued corrections require an exact target. Broad retirement
without a predicate or object is rejected at the store boundary, not just UI.

FORGET_FACT closes validity and retains history. It is **not secure erasure**:
original dialogue, source documents and history remain available for audit.
No general-purpose node deletion was added; person nodes can also own face,
contact, evidence and profile relationships. Reimporting original source data
can reintroduce a retired claim; global cross-source erasure/tombstones are a
separate lifecycle decision and are not claimed here.

## Retrieval

Existing expiry, inference and sensitivity filters remain in place. Rejected,
suspended and withdrawn claims are excluded even when a caller requests pending
patterns. Legacy facts without sensitivity continue to default to private.

The existing proposal worker still records repeat sightings as auto_accepted.
Retrieval now treats that label as unconfirmed, because repeated model outputs
are not independent corroboration. Such hypotheses are available for relevant
pattern-exploration requests, not ordinary user-fact context. A normal accepted
or reviewed status is needed to enter the reviewed-inference section.

## Maintenance and import

The legacy dedupe command now retires only edges with matching non-audit
metadata; it never deletes FACT relationships. Source, privacy, review status,
claim identity and unknown metadata differences prevent consolidation. Its
default remains dry-run and console output omits raw personal claim values.

Bootstrap commit recomputes the reviewed manifest hash and validates ImportRun
identity before opening a writer. A defensive copy prevents transaction retries
from mutating the caller's reviewed manifest. No bootstrap import was run.

## Verification

Read-only production audit on 2026-10-31: 2,176 entities; 3,628 total FACT edges;
2,697 current facts; zero current duplicate groups; zero missing fact identities;
zero orphan entities; zero rejected/suspended/withdrawn current facts. Five
legacy edges lack sensitivity metadata and remain subject to private defaults.

Neo4j EXPLAIN compiled the modified add, retirement and duplicate-maintenance
queries without executing writes. Automated tests isolate providers and the
production graph. Transaction failure tests exercise the application callback
with a transactional fake; no destructive production rollback trial was run.

See reports/graph-lifecycle-tests.xml and reports/graph-core-tests.xml for test
results. Restart VANTA to load the changes.

## Contact-sync regression correction

The last privacy-guard edit originally introduced SQL-style `NOT IN`, which
Cypher rejected. This was made after the earlier EXPLAIN check. The corrected
guard uses `NOT (value IN [...])`. Run `python scripts/check_graph_queries.py`
to compile the final add, retirement and maintenance statements against the
configured Neo4j server. That command uses EXPLAIN and executes no graph writes.

Contact sync now stops a batch on syntax, authentication or connection failure,
keeps contact names out of diagnostic errors, skips dependent detail writes
when graph access is blocked, and reports only completed phone-contact writes
as successful. Pending contacts are retried by the next sync. Existing graph
records are not cleared or replaced as part of recovery.
