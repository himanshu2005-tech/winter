"""Lossless, governed graph ingestion for every email VANTA observes.

Triage controls interruption, never persistence. Email content is external
evidence and can inform answers, but it cannot authorize an action.
"""
from __future__ import annotations

import hashlib
from email.utils import parseaddr
import re
from typing import Iterable, Mapping

from memory.graph_store import add_edge
from memory.neo4j_store import _slug, store

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _hash(*values):
    canonical = "\n".join(str(value or "") for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _addresses(message):
    text = " ".join(str(message.get(key) or "") for key in (
        "sender", "to", "cc", "reply_to", "subject", "snippet"))
    return list(dict.fromkeys(value.casefold() for value in _EMAIL.findall(text)))


def remember_emails(messages: Iterable[Mapping], verdict: Mapping | None = None):
    """Store every observed email and all bounded metadata, idempotently.

    A changed observation creates another immutable evidence snapshot rather
    than overwriting the old one. The stable Email entity points to every
    snapshot, while current labels/unread state may advance on the entity.
    """
    verdict = verdict or {}
    important_ids = {str(value) for value in verdict.get("important_ids", [])}
    severity = _clean(verdict.get("severity"), 20).lower() or "normal"
    reason = _clean(verdict.get("reason"), 240)
    stored = 0
    for message in messages or []:
        message_id = _clean(message.get("id"), 180)
        if not message_id:
            continue
        sender = _clean(message.get("sender"), 300) or "Unknown sender"
        subject = _clean(message.get("subject"), 500) or "No subject"
        snippet = _clean(message.get("snippet"), 4000)
        to = _clean(message.get("to"), 1000)
        cc = _clean(message.get("cc"), 1000)
        reply_to = _clean(message.get("reply_to"), 300)
        sent_at = _clean(message.get("date"), 160)
        thread_id = _clean(message.get("thread_id"), 180)
        labels = sorted({_clean(value, 100) for value in message.get("labels", []) if value})
        unread = bool(message.get("unread", "UNREAD" in labels))
        source_id = f"gmail:{message_id}"
        content_hash = _hash(message_id, sender, to, cc, reply_to, subject,
                             snippet, sent_at, thread_id)
        evidence_id = f"email-evidence:{_hash(source_id)[:20]}:{content_hash[:16]}"
        entity_name = f"Email: {subject} — from {sender} [Gmail ID: {message_id}]"[:500]
        entity_key = _slug(entity_name)
        important = message_id in important_ids

        # Insert-only evidence. A second observation with identical content is
        # a no-op; different content receives a new hash-addressed snapshot.
        store.query(
            """
            MERGE (e:EmailEvidence {id:$evidence_id})
            ON CREATE SET e.source_id=$source_id,e.gmail_message_id=$message_id,
              e.content_hash=$content_hash,e.sender=$sender,e.to=$to,e.cc=$cc,
              e.reply_to=$reply_to,e.subject=$subject,e.snippet=$snippet,
              e.sent_at=$sent_at,e.thread_id=$thread_id,e.labels=$labels,
              e.unread=$unread,e.important=$important,e.severity=$severity,
              e.triage_reason=$reason,e.immutable=true,
              e.semantic_retrievable=false,e.observed_at=datetime()
            MERGE (mail:Entity {key:$entity_key})
            ON CREATE SET mail.created_at=datetime(),mail.entity_type='Email'
            SET mail.name=$entity_name,mail.norm_key=$entity_key,
              mail.gmail_message_id=$message_id,mail.source_id=$source_id,
              mail.sender=$sender,mail.subject=$subject,mail.sent_at=$sent_at,
              mail.thread_id=$thread_id,mail.labels=$labels,mail.unread=$unread,
              mail.important=coalesce(mail.important,false) OR $important,
              mail.last_seen=datetime(),mail.updated_at=datetime()
            MERGE (mail)-[:SUPPORTED_BY]->(e)
            """,
            evidence_id=evidence_id, source_id=source_id, message_id=message_id,
            content_hash=content_hash, sender=sender, to=to, cc=cc,
            reply_to=reply_to, subject=subject, snippet=snippet,
            sent_at=sent_at, thread_id=thread_id, labels=labels, unread=unread,
            important=important, severity=severity, reason=reason,
            entity_key=entity_key, entity_name=entity_name,
        )
        add_edge(
            "Himanshu", "RECEIVED_EMAIL", entity_name, confidence=1.0,
            source_id=source_id, fact_id="email-received:" + _hash(source_id)[:24],
            fact_class="external_observation", evidence_type="email",
            authority=70, sensitivity="private", autonomy_eligible=False,
            autonomy_scope="advisory", confirmation_required=False,
            review_status="accepted", claim_key=f"himanshu:RECEIVED_EMAIL:{source_id}",
            evidence_ref=evidence_id,
        )
        for address in _addresses(message):
            add_edge(
                entity_name, "MENTIONS_EMAIL_ADDRESS", address, confidence=1.0,
                source_id=source_id,
                fact_id="email-address:" + _hash(source_id, address)[:24],
                fact_class="external_observation", evidence_type="email",
                authority=70, sensitivity="private", autonomy_eligible=False,
                autonomy_scope="advisory", confirmation_required=False,
                review_status="accepted",
                claim_key=f"{entity_key}:MENTIONS_EMAIL_ADDRESS:{address}",
                evidence_ref=evidence_id,
            )
        sender_name, sender_address = parseaddr(sender)
        if sender_name and sender_address:
            add_edge(
                sender_name, "PRIMARY_EMAIL", sender_address.casefold(),
                confidence=0.95, source_id=source_id,
                fact_id="sender-address:" + _hash(source_id, sender_name, sender_address)[:24],
                fact_class="external_observation", evidence_type="email_header",
                authority=75, sensitivity="private", autonomy_eligible=False,
                autonomy_scope="advisory", confirmation_required=False,
                review_status="accepted",
                claim_key=f"{sender_name.casefold()}:PRIMARY_EMAIL:{sender_address.casefold()}",
                evidence_ref=evidence_id,
            )
        stored += 1
    if stored:
        print(f"[Email Memory] Stored/updated {stored} email(s), including all observed addresses and metadata.")
    return stored


def remember_important(messages: Iterable[Mapping], verdict: Mapping | None):
    """Backward-compatible entry point; storage is intentionally no longer filtered."""
    return remember_emails(messages, verdict)
