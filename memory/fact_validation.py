"""Validation at the graph boundary; content selection remains model-owned."""
import math
import re

SENSITIVITIES = ('public', 'private', 'sensitive', 'restricted')
FACT_CLASSES = {'explicit_user_fact', 'self_reported_pattern',
                'explicit_preference', 'explicit_boundary', 'explicit_goal',
                'external_observation', 'external_fact', 'derived_inference', 'inference'}


def validate_fact(subject, predicate, value, confidence, metadata):
    for name, item in [('subject', subject), ('predicate', predicate), ('object', value)]:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f'{name} must be nonempty text')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,80}', predicate):
        raise ValueError('Invalid fact predicate')
    if isinstance(confidence, bool) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ValueError('Confidence must be finite and between zero and one')
    if metadata.get('sensitivity', 'private') not in SENSITIVITIES:
        raise ValueError('Unknown sensitivity')
    if metadata.get('fact_class', 'explicit_user_fact') not in FACT_CLASSES:
        raise ValueError('Unknown fact class')


def grounded_candidate(candidate, source):
    """A model's confidence alone cannot establish a personal fact."""
    if str(candidate.get('retention', 'durable')).strip().lower() != 'durable':
        return False
    quote = ' '.join(str(candidate.get('quote') or '').split()).casefold()
    source = ' '.join(str(source or '').split()).casefold()
    return bool(quote and quote in source)
