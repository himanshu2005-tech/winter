"""Bounded, content-free timings for model calls and action execution."""
from collections import deque
import threading
import math

_lock = threading.Lock()
_samples = deque(maxlen=512)


def record(kind, name, elapsed, *, ok, first_content=None, provider='', model=''):
    sample = dict(kind=str(kind), name=str(name), ok=bool(ok),
                  elapsed_ms=round(max(0, elapsed) * 1000, 2))
    if first_content is not None:
        sample['first_content_ms'] = round(max(0, first_content) * 1000, 2)
    if provider:
        sample['provider'] = str(provider)
    if model:
        sample['model'] = str(model)
    with _lock:
        _samples.append(sample)


def snapshot():
    with _lock:
        return [dict(row) for row in _samples]


def reset():
    with _lock:
        _samples.clear()


def summary():
    groups = {}
    for row in snapshot():
        bucket = groups.setdefault((row['kind'], row['name']), [])
        bucket.append(row)
    result = []
    for (kind, name), rows in sorted(groups.items()):
        times = sorted(row['elapsed_ms'] for row in rows)
        result.append(dict(kind=kind, name=name, count=len(rows),
                           failures=sum(not row['ok'] for row in rows),
                           mean_ms=round(sum(times) / len(times), 2),
                           p95_ms=times[max(0, math.ceil(len(times) * .95) - 1)]))
    return result
