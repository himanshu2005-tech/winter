# -*- coding: utf-8 -*-
"""Let a test exercise the approval gate without writing to the real store.

Two tests dispatch genuinely gated actions to prove the gate holds -- a
SHUTDOWN and a DRIVE_TRASH. Both mock the provider, so nothing is ever shut
down or trashed, and both assertions are correct. What neither accounted for
is that reaching the gate *is* a side effect: _queue_for_approval writes an
Approval node, so every run left a real pending approval in the live graph.

Seventy-two of them had accumulated, forty-four still marked pending.

That is not merely untidy. A settled "approve" takes the newest pending
record, so for the hour before each one expired, a bare "approve" from
Himanshu -- meaning something else entirely -- could have fired a SHUTDOWN
queued by a test suite. The one-hour expiry is what kept that theoretical
rather than actual, which is a thin margin for a machine shutdown.

So a test that needs to reach the gate wraps the dispatch in `no_approval_writes()`.
The gate still runs, the refusal message is still produced and still asserted;
only the persistence is redirected into a list the test can inspect.
"""

import contextlib
from unittest.mock import patch


@contextlib.contextmanager
def no_approval_writes():
    """Run the real approval gate, but persist nothing.

    Yields the list of writes that were attempted, so a test can assert the
    approval *was* queued rather than merely that a message came back.
    """
    attempted = []

    def fake_execute_write(query, params=()):
        attempted.append((" ".join(str(query).lower().split()), tuple(params)))
        return None

    def fake_query(query, **kwargs):
        lowered = " ".join(str(query).lower().split())
        if "approval" in lowered:
            attempted.append((lowered, kwargs))
            # An empty result means "no existing pending approval", which is
            # the branch that goes on to create one.
            return []
        return []

    with patch("memory.state_db.execute_write", side_effect=fake_execute_write), \
            patch("memory.neo4j_store.store.query", side_effect=fake_query):
        yield attempted


def queued_action_types(attempted):
    """The action types an isolated run tried to queue for approval."""
    found = []
    for query, params in attempted:
        if "insert into approvals" in query and isinstance(params, tuple):
            if len(params) >= 2:
                found.append(params[1])
        elif isinstance(params, dict) and params.get("type"):
            found.append(params["type"])
    return found
