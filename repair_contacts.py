# -*- coding: utf-8 -*-
"""
repair_contacts.py — re-retire stale contact facts.

A regression in add_fact matched FACT edges on subject/predicate/object without
checking whether they were still valid, then wrote valid_to=NULL onto them. Any
edge the contact sync re-asserted was therefore brought back to life, including
contacts that had been superseded long ago. The write path is fixed; this
repairs the data it already touched.

The repair is deterministic rather than guesswork: retire every contact-sync
edge, then re-run the sync, which re-asserts exactly the contacts Google
actually has right now. Anything Google no longer knows about stays retired.
Nothing is deleted -- retired edges keep their history.

    python repair_contacts.py           # report only, changes nothing
    python repair_contacts.py --apply   # retire stale, then re-sync
"""

import argparse
import os
import sys

from memory.neo4j_store import store

CONTACT_PREDICATES = ["HAS_CONTACT", "HAS_PHONE"]


def say(text=""):
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(str(text).encode(encoding, errors="replace").decode(encoding))


def counts():
    live = store.query(
        """
        MATCH ()-[r:FACT]->() WHERE r.valid_to IS NULL AND r.predicate IN $preds
        RETURN r.predicate AS predicate, count(*) AS n ORDER BY predicate
        """,
        preds=CONTACT_PREDICATES,
    )
    retired = store.query(
        """
        MATCH ()-[r:FACT]->() WHERE r.valid_to IS NOT NULL AND r.predicate IN $preds
        RETURN count(*) AS n
        """,
        preds=CONTACT_PREDICATES,
    )[0]["n"]
    return live, retired


def main():
    parser = argparse.ArgumentParser(description="Re-retire stale contact facts.")
    parser.add_argument("--apply", action="store_true",
                        help="perform the repair (default is a dry run)")
    args = parser.parse_args()

    try:
        store.ensure_schema()
        if store.health().get("status") != "ok":
            say("Neo4j is not healthy.")
            return 1
    except Exception as exc:
        say(f"Could not reach Neo4j: {exc}")
        return 1

    live, retired = counts()
    total_live = sum(row["n"] for row in live)
    say("Contact facts currently live:")
    for row in live:
        say(f"  {row['n']:5d}  {row['predicate']}")
    say(f"  {total_live:5d}  total live")
    say(f"  {retired:5d}  already retired")
    say("")
    say("Google Contacts holds roughly 142 contacts, so a healthy graph should")
    say("show about 284 live contact facts (one HAS_CONTACT + one HAS_PHONE each).")

    if not args.apply:
        say("")
        say("Dry run. Re-run with --apply to retire these and re-sync from Google.")
        return 0

    say("")
    say("Retiring all contact-sync facts...")
    result = store.query(
        """
        MATCH ()-[r:FACT]->() WHERE r.valid_to IS NULL AND r.predicate IN $preds
        SET r.valid_to = toString(datetime()),
            r.correction_reason = 'stale contact retired by repair_contacts'
        RETURN count(r) AS n
        """,
        preds=CONTACT_PREDICATES,
    )
    say(f"  retired {result[0]['n'] if result else 0}")

    say("Re-syncing from Google Contacts...")
    try:
        from providers.google_api import sync_contacts
        synced = sync_contacts(os.path.dirname(os.path.abspath(__file__)))
        say(f"  re-asserted {synced} contacts")
    except Exception as exc:
        say(f"  SYNC FAILED: {exc}")
        say("  Contact facts are retired but not restored. Run VANTA (it syncs")
        say("  on boot) or re-run this script once Google access works again.")
        return 1

    live, retired = counts()
    say("")
    say("After repair:")
    for row in live:
        say(f"  {row['n']:5d}  {row['predicate']}")
    say(f"  {sum(row['n'] for row in live):5d}  total live")
    say(f"  {retired:5d}  retired (kept as history)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
