# -*- coding: utf-8 -*-
"""Google People (Contacts) provider for VANTA.

Reads work on the original ``contacts.readonly`` grant. Writes need the wider
``contacts`` scope; a token issued before that scope was requested gets a 403
from Google, which is turned here into a plain instruction to reauthorize
rather than a stack trace. The provider never decides whether a change is
authorized -- that is ``autonomy.py``'s job -- and, like the Drive provider,
mutates by resource name so two people with the same name cannot be confused.
"""

from __future__ import annotations

import re

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from providers.google_api import get_credentials

READ_FIELDS = ("names,phoneNumbers,emailAddresses,organizations,birthdays,"
               "addresses,biographies,urls")
SEARCH_FIELDS = "names,phoneNumbers,emailAddresses,organizations"

_RESOURCE = re.compile(r"^people/c?\d+$")


class ContactsWriteNotGranted(RuntimeError):
    """The stored grant is read-only for contacts."""


def _service(base_dir):
    return build("people", "v1", credentials=get_credentials(base_dir),
                 cache_discovery=False)


def _is_scope_error(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = str(exc).lower()
    return status == 403 and ("insufficient" in detail or "scope" in detail
                              or "permission" in detail)


def _guarded(call):
    """Run a People write, translating a read-only grant into guidance."""
    try:
        return call()
    except HttpError as exc:
        if _is_scope_error(exc):
            raise ContactsWriteNotGranted(
                "Contacts access is read-only on this Google login. Reauthorize "
                "Google (REAUTHORIZE_GOOGLE) to grant permission to change "
                "contacts, then try again.") from exc
        raise


def _first(rows, key="value"):
    for row in rows or []:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def summarize(person):
    """A flat, speakable record from a People API person resource."""
    names = person.get("names") or []
    primary = next((n for n in names if (n.get("metadata") or {}).get("primary")),
                   names[0] if names else {})
    birthday = ""
    for entry in person.get("birthdays") or []:
        date = entry.get("date") or {}
        if date.get("month") and date.get("day"):
            birthday = (f"{date['day']:02d}/{date['month']:02d}"
                        + (f"/{date['year']}" if date.get("year") else ""))
            break
    organization = (person.get("organizations") or [{}])[0]
    return {
        "resource_name": person.get("resourceName", ""),
        "name": (primary.get("displayName") or "").strip(),
        "phones": [(p.get("canonicalForm") or p.get("value") or "").strip()
                   for p in person.get("phoneNumbers") or []
                   if (p.get("value") or p.get("canonicalForm"))],
        "emails": [(e.get("value") or "").strip()
                   for e in person.get("emailAddresses") or [] if e.get("value")],
        "organization": (organization.get("name") or "").strip(),
        "title": (organization.get("title") or "").strip(),
        "birthday": birthday,
        "notes": _first(person.get("biographies")),
    }


def format_person(row, detail=False):
    parts = [row["name"] or "(no name)"]
    if row.get("organization"):
        parts.append(row["organization"] + (f", {row['title']}" if row.get("title") else ""))
    if row.get("phones"):
        parts.append("phone " + ", ".join(row["phones"][:3]))
    if row.get("emails"):
        parts.append("email " + ", ".join(row["emails"][:3]))
    if detail and row.get("birthday"):
        parts.append("birthday " + row["birthday"])
    if detail and row.get("notes"):
        parts.append("notes: " + row["notes"][:200])
    return f"- {' | '.join(parts)} [id={row['resource_name']}]"


def search_people(base_dir, query, limit=10):
    """Find people by name, email or number, including 'other contacts'.

    'Other contacts' are the addresses Google auto-saves from mail; they are
    only visible with the other-contacts scope, so they are best-effort.
    """
    query = str(query or "").strip()
    if not query:
        return {"people": [], "text": "[Error] PEOPLE_SEARCH needs a query."}
    limit = max(1, min(int(limit or 10), 30))
    service = _service(base_dir)
    found = {}
    # The search index is built lazily: Google documents that the first
    # request can return nothing, so a warm-up call is sent and ignored.
    try:
        service.people().searchContacts(query="", readMask=SEARCH_FIELDS).execute()
    except HttpError:
        pass
    response = service.people().searchContacts(
        query=query, pageSize=limit, readMask=SEARCH_FIELDS).execute()
    for result in response.get("results") or []:
        person = result.get("person") or {}
        row = summarize(person)
        found[row["resource_name"]] = row
    try:
        other = service.otherContacts().search(
            query=query, pageSize=limit,
            readMask="names,emailAddresses,phoneNumbers").execute()
        for result in other.get("results") or []:
            row = summarize(result.get("person") or {})
            row["other"] = True
            found.setdefault(row["resource_name"], row)
    except HttpError:
        pass  # scope not granted; saved contacts are still returned
    people = list(found.values())[:limit]
    if not people:
        return {"people": [], "text": f"No contacts found for '{query}'."}
    lines = [f"Contacts matching '{query}' ({len(people)}):"]
    lines += [format_person(row) + (" (auto-saved from mail)" if row.get("other") else "")
              for row in people]
    return {"people": people, "text": "\n".join(lines)}


def get_person(base_dir, resource_name):
    name = _normalize_resource(resource_name)
    person = _service(base_dir).people().get(
        resourceName=name, personFields=READ_FIELDS).execute()
    row = summarize(person)
    return {"person": row, "text": "Contact:\n" + format_person(row, detail=True)}


def _normalize_resource(value):
    value = str(value or "").strip()
    if re.fullmatch(r"c?\d+", value):
        value = "people/" + value
    if not _RESOURCE.match(value):
        raise ValueError(
            "A contact id looks like people/c123456789 -- get it from "
            "PEOPLE_SEARCH rather than guessing.")
    return value


def _body(name="", phone="", email="", organization="", title="", notes="",
          birthday=""):
    body = {}
    if name:
        parts = str(name).strip().split(None, 1)
        body["names"] = [{"givenName": parts[0],
                          **({"familyName": parts[1]} if len(parts) > 1 else {})}]
    if phone:
        body["phoneNumbers"] = [{"value": str(phone).strip()}]
    if email:
        body["emailAddresses"] = [{"value": str(email).strip()}]
    if organization or title:
        body["organizations"] = [{"name": organization or "", "title": title or ""}]
    if notes:
        body["biographies"] = [{"value": str(notes), "contentType": "TEXT_PLAIN"}]
    if birthday:
        date = _parse_birthday(birthday)
        if date:
            body["birthdays"] = [{"date": date}]
    return body


def _parse_birthday(text):
    """'1999-03-14', '14 March' or '14/03' -> a People API date."""
    text = str(text).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return {"year": int(match[1]), "month": int(match[2]), "day": int(match[3])}
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if match:
        return {"day": int(match[1]), "month": int(match[2])}
    try:
        from dateutil import parser
        parsed = parser.parse(text, default=__import__("datetime").datetime(2000, 1, 1))
        has_year = bool(re.search(r"\b\d{4}\b", text))
        out = {"month": parsed.month, "day": parsed.day}
        if has_year:
            out["year"] = parsed.year
        return out
    except (ValueError, OverflowError, ImportError):
        return None


def create_person(base_dir, name, phone="", email="", organization="",
                  title="", notes="", birthday=""):
    if not str(name or "").strip():
        raise ValueError("A contact needs a name.")
    body = _body(name, phone, email, organization, title, notes, birthday)
    created = _guarded(lambda: _service(base_dir).people().createContact(
        body=body, personFields=SEARCH_FIELDS).execute())
    row = summarize(created)
    return (f"[Success] Created contact {row['name']} [id={row['resource_name']}]"
            + (f" with phone {row['phones'][0]}" if row["phones"] else "")
            + (f" and email {row['emails'][0]}" if row["emails"] else "") + ".")


def update_person(base_dir, resource_name, name="", phone="", email="",
                  organization="", title="", notes="", birthday=""):
    """Change fields on one contact. Phone/email are ADDED, not replaced, so a
    contact with two numbers does not lose one when a third is mentioned."""
    resource = _normalize_resource(resource_name)
    service = _service(base_dir)
    current = service.people().get(
        resourceName=resource, personFields=READ_FIELDS).execute()
    body = _body(name, phone, email, organization, title, notes, birthday)
    if not body:
        raise ValueError("Nothing to change: give a name, phone, email, "
                         "organization, title, notes or birthday.")
    mask = []
    for field, key in (("names", "names"), ("organizations", "organizations"),
                       ("biographies", "biographies"), ("birthdays", "birthdays")):
        if key in body:
            mask.append(field)
    for field in ("phoneNumbers", "emailAddresses"):
        if field in body:
            existing = current.get(field) or []
            new_value = body[field][0]["value"]
            if not any((e.get("value") or "").replace(" ", "") == new_value.replace(" ", "")
                       for e in existing):
                body[field] = [{"value": e.get("value")} for e in existing] + body[field]
            else:
                body.pop(field)
                continue
            mask.append(field)
    if not mask:
        return f"[Success] {resource} already has that information; nothing changed."
    body["etag"] = current.get("etag")
    updated = _guarded(lambda: service.people().updateContact(
        resourceName=resource, body=body, updatePersonFields=",".join(mask),
        personFields=SEARCH_FIELDS).execute())
    row = summarize(updated)
    return (f"[Success] Updated {row['name']} [id={row['resource_name']}]: "
            f"changed {', '.join(mask)}.")


def delete_person(base_dir, resource_name):
    resource = _normalize_resource(resource_name)
    service = _service(base_dir)
    current = service.people().get(
        resourceName=resource, personFields="names").execute()
    label = summarize(current)["name"] or resource
    _guarded(lambda: service.people().deleteContact(resourceName=resource).execute())
    return f"[Success] Deleted contact {label} [id={resource}]."
