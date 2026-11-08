"""Small bounded HTTP helper shared by public-data providers."""
import random
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests


def get_json(url, *, params=None, timeout=10, attempts=3):
    attempts = max(1, min(5, int(attempts)))
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('HTTP timeout must be a positive finite number')
    timeout = min(timeout, 30)
    last = 'unknown error'
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            status = response.status_code
            retryable = status in {408, 429} or status >= 500
            if status >= 400:
                last = f'HTTP {status}'
                if not retryable:
                    # An invalid key or missing resource is not a transient
                    # outage. Repeating it only adds latency.
                    raise RuntimeError(f'service request failed: {last}')
                retry_after = str(response.headers.get('Retry-After', '')).strip()
                delay = 0.0
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            stamp = parsedate_to_datetime(retry_after)
                            if stamp.tzinfo is None:
                                stamp = stamp.replace(tzinfo=timezone.utc)
                            delay = (stamp - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            delay = 0.0
                delay = max(0.0, delay) if math.isfinite(delay) else 0.0
                if delay > 10:
                    raise RuntimeError(f'service request deferred: {last}; retry after {delay:.0f}s')
                if attempt + 1 < attempts:
                    time.sleep(delay or (0.5 * (2 ** attempt) + random.random() * 0.25))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
            # Exception strings can embed an authenticated URL, including API
            # keys in query parameters. Return diagnostic classes, not URLs.
            last = type(exc).__name__
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.25)
    raise RuntimeError(f"service request failed after {attempts} attempts: {last}")
