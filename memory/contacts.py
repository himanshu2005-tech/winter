import time
import threading
from memory.graph_store import get_current_edges

_contacts_cache = None
_cache_timestamp = 0
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 300
PHONE_NOT_FOUND = "PHONE_NOT_FOUND"

_FAMILY_PREDICATES = {
    "father": "HAS_FATHER", "pappa": "HAS_FATHER", "dad": "HAS_FATHER",
    "mother": "HAS_MOTHER", "mummy": "HAS_MOTHER", "mom": "HAS_MOTHER",
    "sister": "HAS_SISTER", "akka": "HAS_SISTER",
    "brother": "HAS_BROTHER", "anna": "HAS_BROTHER",
}


def get_all_contacts(force_refresh=False):
    """Return canonical lowercase contact names mapped to phone numbers."""
    global _contacts_cache, _cache_timestamp
    now = time.time()
    with _cache_lock:
        if not force_refresh and _contacts_cache is not None and now - _cache_timestamp < CACHE_TTL_SECONDS:
            return dict(_contacts_cache)
        contacts = {}
        for subject, predicate, object_value in get_current_edges():
            if predicate == "HAS_PHONE":
                contacts[subject.casefold()] = object_value
        _contacts_cache, _cache_timestamp = contacts, now
        return dict(contacts)


def _candidate_contact_names(name, edges):
    """Requested label plus graph-linked legal names, aliases and family roles."""
    requested = str(name or "").strip()
    if not requested:
        return []
    candidates = [requested]
    relation = _FAMILY_PREDICATES.get(requested.casefold())
    if relation:
        candidates.extend(o for s, p, o in edges
                          if s.casefold() == "himanshu" and p == relation)
    changed = True
    while changed:
        changed = False
        known = {value.casefold() for value in candidates}
        for subject, predicate, object_value in edges:
            if predicate != "HAS_ALIAS":
                continue
            if subject.casefold() in known and object_value.casefold() not in known:
                candidates.append(object_value); changed = True
            if object_value.casefold() in known and subject.casefold() not in known:
                candidates.append(subject); changed = True
    return list(dict.fromkeys(candidates))


def resolve_contact_phone(name):
    """Resolve only exact graph-linked labels, then one unambiguous substring."""
    edges = get_current_edges()
    candidates = _candidate_contact_names(name, edges)
    contacts = get_all_contacts()
    for candidate in candidates:
        phone = contacts.get(candidate.casefold())
        if phone:
            return phone
    matches = []
    for candidate in candidates:
        key = candidate.casefold()
        matches.extend((stored, phone) for stored, phone in contacts.items()
                       if key in stored or stored in key)
    unique = list(dict.fromkeys(matches))
    return unique[0][1] if len(unique) == 1 else None

def lookup_contacts(names):
    """Return only the explicitly requested contacts; never dump the address book."""
    results = []
    for raw in names:
        name = " ".join(str(raw or "").split())
        if not name:
            continue
        phone = resolve_contact_phone(name)
        if phone == PHONE_NOT_FOUND:
            results.append({
                "name": name, "phone": None, "found": False,
                "reason": f"I know who {name} is but their phone number is not stored in my graph. Please tell me their number."
            })
        else:
            results.append({"name": name, "phone": phone, "found": bool(phone)})
    return results