"""Unofficial public Indian PIN/post-office lookup provider."""
import re
import threading
import time
from urllib.parse import quote
from providers.http_client import get_json

_BASE = "https://api.postalpincode.in"
_cache = {}
_lock = threading.Lock()


def _fetch(kind, value):
    key = (kind, value.casefold())
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < 86400:
            return hit[1]
    data = get_json(f"{_BASE}/{kind}/{quote(value, safe='')}", timeout=10)
    with _lock:
        _cache[key] = (now, data)
    return data


def lookup(pin="", office=""):
    pin, office = str(pin or "").strip(), str(office or "").strip()
    if pin and not re.fullmatch(r"\d{6}", pin):
        return "[Error] An Indian PIN code must contain exactly six digits."
    if not pin and not office:
        return "[Error] Postal lookup needs a six-digit PIN or post-office name."
    kind, value = ("pincode", pin) if pin else ("postoffice", office)
    try:
        payload = _fetch(kind, value)
    except RuntimeError as exc:
        return f"[Error] Postal lookup service is unavailable: {exc}"
    envelope = payload[0] if isinstance(payload, list) and payload else {}
    matches = envelope.get("PostOffice") or []
    if str(envelope.get("Status", "")).lower() != "success" or not matches:
        return f"No post-office matches found. {envelope.get('Message', '')}".strip()
    lines = [f"{len(matches)} post-office match(es) (unofficial public lookup):"]
    for item in matches[:20]:
        lines.append(
            f"- {item.get('Name', 'Unknown')} — PIN {item.get('Pincode', 'unknown')}; "
            f"{item.get('District', 'unknown district')}, {item.get('State', 'unknown state')}; "
            f"{item.get('DeliveryStatus', 'delivery status unavailable')}; "
            f"{item.get('BranchType', 'branch type unavailable')}"
        )
    if len(matches) > 20:
        lines.append(f"{len(matches) - 20} additional matches omitted.")
    return "\n".join(lines)
