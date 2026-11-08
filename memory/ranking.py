"""Query-dependent ranking of bounded graph candidates without another model."""
import math
import re
from collections import Counter

from memory.neo4j_store import fact_query_tokens


def _terms(value):
    return set(fact_query_tokens(str(value or '').replace('_', ' '), limit=100))


def rank_facts(rows, query, limit):
    """Prefer distinctive query matches, then evidence quality; dedupe claims.

    Connector noise cannot win just by mentioning a common word or being
    synced many times. Exact identifiers and URL paths remain untouched.
    Database scores are deliberately not reused across separate query lanes.
    """
    unique = {}
    for row in rows:
        key = tuple(str(row.get(k) or '').strip().casefold()
                    for k in ('subject', 'predicate', 'object'))
        existing = unique.get(key)
        if existing is None or float(row.get('authority') or 50) > float(existing.get('authority') or 50):
            unique[key] = row
    candidates = list(unique.values())
    terms = _terms(query)
    fields = [(_terms(row.get('subject')), _terms(row.get('predicate')),
               _terms(row.get('object'))) for row in candidates]
    frequencies = Counter(word for fs in fields for word in set.union(*fs))
    ranked = []
    has_relevant_candidates = any(terms & set.union(*fs) for fs in fields)
    for row, (subject, predicate, obj) in zip(candidates, fields):
        matched = terms & (subject | predicate | obj)
        if has_relevant_candidates and not matched:
            continue
        relevance = sum(
            math.log(1 + (len(candidates) + 1) / (1 + frequencies[t]))
            * (1.4 if t in predicate else 1.2 if t in obj else 1.0)
            for t in matched)
        authority = min(100, max(0, float(row.get('authority') or 50))) / 100
        confidence = min(1, max(0, float(row.get('confidence') or 0.5)))
        explicit = str(row.get('fact_class') or 'explicit_user_fact') in {
            'explicit_user_fact', 'explicit_preference', 'self_reported_pattern'}
        score = relevance + .35 * authority + .15 * confidence + .25 * explicit
        ranked.append((score, row))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in ranked[:max(0, int(limit))]]
