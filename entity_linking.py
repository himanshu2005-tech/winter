# -*- coding: utf-8 -*-
"""
entity_linking.py -- joining what his separate worlds say about the same thing.

The graph held islands. 142 contacts. Mail from companies and services. A
calendar. Tasks. Projects on disk. Each is a faithful record of one part of his
life, and none of them knew the others existed:

    calendar   "Apply for Chennai gate pass -- before Sunday's departure"
    task       "Check final PDF paper"
    email      "Interview with Deloitte -- Thursday"
    project    "Mavo"

An email that is about a project he is building, a task that is the same
obligation as a calendar entry, a sender who is a person he already knows:
every one of those relationships was real and invisible. Nothing could reason
across them, because nothing connected them.

WHAT THE DATA SUPPORTS
----------------------
Honestly less than the obvious design assumes. The contact sync asked Google
for names and phone numbers only, and the address book answers: 165 contacts
with phones, 2 with an email address, 2 distinct "organizations" -- both junk.
Linking "a company in an email to the contacts who work there" needs data that
is not there. What is there is text: every silo mentions the others by name. So
this links by what things are called, which scales as the silos fill up and
needs nothing the address book lacks.

TWO KINDS OF LINK, KEPT APART
-----------------------------
*A mention is an observation.* If an email's subject contains the name of a
project he has, that is a fact about the email, not a belief about him. It is
stored as an `external_observation` -- the class the inference engine does not
read -- with the matched text as its evidence.

*"These are the same thing" is a judgment.* Two items sharing some words is a
candidate, not an answer: "Interview with Deloitte" and "Deloitte job alert"
share a name and are different matters. Those go to the model, and what it
decides lands as a pending inference with a reason, subject to the same
corroboration gate as everything else.

WHAT IT WILL NOT DO
-------------------
It does not merge entities, edit one, or delete anything. The only nodes it
creates are brand new ones for items, keyed so they cannot collide with an
existing entity -- `upsert_entity` overwrites a node's type and aliases, and a
contact reset by a linking pass would be a quiet corruption nobody noticed for
weeks. It does not guess at generic names: a name that appears in a quarter of
everything is a word, not an identity.
"""

import datetime
import hashlib
import json
import os
import re
import tempfile
import threading

import idle_gate

JOB = "linking"
_LOCK = threading.RLock()
_FILENAME = os.path.join("vault", "memory", "linking_ledger.json")

# --- limits ---------------------------------------------------------- #
MAX_ITEMS = 150
MAX_NEW_LINKS = 40
MAX_PAIRS_ASKED = 12
#: A name that appears in more than this share of items is a word, not a
#: name -- measured against the items actually being linked, so it adapts to
#: what he has rather than to a list of words someone thought of.
GENERIC_SHARE = 0.25
MIN_NAME_LENGTH = 4
#: Distinctive shared words needed before two items become a candidate pair.
MIN_SHARED_WEIGHT = 3.2
#: A graph node with more edges than this is a hub, never a link target.
HUB_DEGREE = 20

SILO_EVENT, SILO_TASK = "event", "task"
SILO_EMAIL, SILO_PROJECT = "email", "project"

#: Providers whose address says nothing about an organisation.
_FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "yahoo.in", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "rediffmail.com"})

_WORD = re.compile(r"[a-z0-9][a-z0-9'+#.-]*[a-z0-9]|[a-z0-9]", re.IGNORECASE)
_STOP = frozenset("""
    the a an and or of to for in on at by with from your you our this that
    these those is are was were be been it its as into about over under after
    before re fwd has have had will would can could should not no yes do does
    did new get got set one two all any more most some than then them they
    their there here out up down off via per
""".split())


def normalise(text):
    return " ".join(_WORD.findall(str(text or "").lower()))


def tokens(text):
    """Distinctive words of a text: lowercase, not a stop word, 3+ letters."""
    return [word for word in _WORD.findall(str(text or "").lower())
            if len(word) >= 3 and word not in _STOP and not word.isdigit()]


# ------------------------------------------------------------------ #
# Things to link
# ------------------------------------------------------------------ #
class Item:
    """One thing from one silo, as text."""

    __slots__ = ("silo", "ident", "title", "body", "when", "extra")

    def __init__(self, silo, ident, title, body="", when="", extra=None):
        self.silo = silo
        self.ident = str(ident)
        self.title = " ".join(str(title or "").split())[:200]
        self.body = " ".join(str(body or "").split())[:600]
        self.when = str(when or "")
        self.extra = dict(extra or {})

    def text(self):
        return f"{self.title} {self.body}".strip()

    def fingerprint(self):
        raw = f"{self.silo}|{self.ident}|{self.title}|{self.body[:200]}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:14]

    def node_name(self):
        """A name that cannot collide with a person, project or concept."""
        label = {SILO_EVENT: "Event", SILO_TASK: "Task", SILO_EMAIL: "Email",
                 SILO_PROJECT: "Project"}[self.silo]
        return f"{label}: {self.title[:90]}"

    def entity_type(self):
        return {SILO_EVENT: "CalendarEvent", SILO_TASK: "Task",
                SILO_EMAIL: "EmailMessage", SILO_PROJECT: "Project"}[self.silo]


class Target:
    """Something an item can refer to: a person, a project, a concept."""

    __slots__ = ("name", "kind", "pattern")

    def __init__(self, name, kind):
        self.name = " ".join(str(name or "").split())
        self.kind = kind
        words = [re.escape(part) for part in normalise(self.name).split()]
        # Whole words only, with flexible spacing between them: "Mavo" must
        # not match "Mavolution", while "final pdf" matches "final-pdf" and
        # "final_pdf". Strict is the right default for something that writes
        # edges -- a missed link costs nothing, a wrong one has to be found.
        self.pattern = re.compile(
            r"(?<![a-z0-9])" + r"[\s\-_]+".join(words) + r"(?![a-z0-9])",
            re.IGNORECASE) if words else None

    def key(self):
        return normalise(self.name)

    def strong(self):
        """Whether a match on this name is unambiguous enough to write.

        A full name, or a project's name, means one thing. A single word does
        not: the real address book has a contact called "Chennai", so a
        calendar entry about the city was linked to a person. Whether "Rahul"
        in an email subject is the Rahul he knows is a judgment about
        context, and judgments go to the model -- not to a list of words that
        happen to be places.
        """
        words = self.key().split()
        if self.kind == "project" or len(words) >= 2:
            return True
        return self.kind == "concept" and len(words[0]) >= 9


def usable_name(name):
    """Whether a name is specific enough to look for in other people's text."""
    text = normalise(name)
    if len(text) < MIN_NAME_LENGTH:
        return False
    if _LITERALISH.search(str(name or "")):
        return False
    return any(len(word) >= 3 and word not in _STOP for word in text.split())


_LITERALISH = re.compile(r"@|^\+?[\d\s().-]{7,}$|^https?://", re.IGNORECASE)


def build_targets(persons=(), concepts=(), projects=()):
    seen, targets = set(), []
    for kind, names in (("person", persons), ("project", projects),
                        ("concept", concepts)):
        for name in names:
            if not usable_name(name):
                continue
            target = Target(name, kind)
            if target.pattern is None or target.key() in seen:
                continue
            seen.add(target.key())
            targets.append(target)
    return targets


# ------------------------------------------------------------------ #
# Mentions
# ------------------------------------------------------------------ #
def find_mentions(items, targets, generic_share=GENERIC_SHARE):
    """Which items name which targets, minus the names that name everything.

    Returns [(item, target, quote)]. A target that matches more than
    `generic_share` of the items is dropped, judged against the items given:
    "Graph" appears in a third of a student's mail and identifies nothing.
    """
    hits = []
    per_target = {}
    for item in items:
        text = item.text()
        for target in targets:
            match = target.pattern.search(text)
            if not match:
                continue
            if target.kind == "person" and item.silo == SILO_PROJECT:
                continue
            if item.silo == SILO_PROJECT and normalise(item.title) == target.key():
                continue          # a project does not refer to itself
            hits.append((item, target, match.group(0)))
            per_target.setdefault(target.key(), set()).add(item.ident)
    if len(items) >= 8:
        limit = max(3, int(len(items) * generic_share))
        hits = [hit for hit in hits if len(per_target[hit[1].key()]) <= limit]
    return hits


# ------------------------------------------------------------------ #
# Candidate pairs
# ------------------------------------------------------------------ #
def candidate_pairs(items, min_weight=MIN_SHARED_WEIGHT, limit=MAX_PAIRS_ASKED):
    """Pairs of items from different silos that share distinctive words.

    Weighted by how rare each shared word is across the items, so two items
    both containing "report" score nothing and two both containing "Deloitte"
    score a lot. Never pairs within one silo: two emails about the same
    sender are a thread, not a link between worlds.
    """
    from math import log
    docs = [(item, set(tokens(item.text()))) for item in items]
    frequency = {}
    for _item, words in docs:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1
    total = max(1, len(docs))
    pairs = []
    for index, (first, first_words) in enumerate(docs):
        for second, second_words in docs[index + 1:]:
            if first.silo == second.silo:
                continue
            shared = first_words & second_words
            if not shared:
                continue
            weight = sum(log(1 + total / frequency[word]) for word in shared)
            if weight >= min_weight and (len(shared) >= 2 or weight >= 4.5):
                pairs.append((weight, first, second, sorted(shared)))
    pairs.sort(key=lambda row: -row[0])
    return pairs[:limit]


# ------------------------------------------------------------------ #
# Adapters. Each is bounded, optional and never allowed to raise.
# ------------------------------------------------------------------ #
def load_persons(store, hub_degree=HUB_DEGREE):
    """People he knows. Never the account owner, who is every email's
    recipient: "Himanshu" matched most of the inbox and meant nothing."""
    try:
        rows = store.query(
            "MATCH (p:Entity) WHERE p.entity_type IN ['Person','FamilyAlias'] "
            "OPTIONAL MATCH (p)-[r:FACT]-() WHERE r.valid_to IS NULL "
            "WITH p, count(r) AS degree WHERE degree <= $hub "
            "RETURN p.name AS name, coalesce(p.aliases,[]) AS aliases "
            "LIMIT 1000", hub=int(hub_degree))
    except Exception:
        return []
    names = []
    for row in rows:
        names.append(row["name"])
        names.extend(row.get("aliases") or [])
    return names


def load_concepts(store, hub_degree=HUB_DEGREE):
    """Concept-like entities: not people, not values, not hubs."""
    try:
        rows = store.query(
            "MATCH (e:Entity)-[r:FACT]-() WHERE r.valid_to IS NULL "
            "AND NOT coalesce(e.entity_type,'Entity') IN "
            "['Person','FamilyAlias','ProfileValue','Email','CalendarEvent',"
            "'Task','EmailMessage','Project'] "
            "WITH e, count(r) AS degree WHERE degree >= 2 AND degree <= $hub "
            "RETURN e.name AS name LIMIT 600", hub=int(hub_degree))
    except Exception:
        return []
    return [row["name"] for row in rows]


def load_emails(store, limit=60):
    try:
        rows = store.query(
            "MATCH (e:EmailEvidence) RETURN e.id AS id, e.subject AS subject, "
            "e.sender AS sender, e.snippet AS snippet, e.sent_at AS sent "
            "ORDER BY coalesce(e.sent_at,'') DESC LIMIT $limit",
            limit=int(limit))
    except Exception:
        return []
    items = []
    for row in rows:
        sender = str(row.get("sender") or "")
        display = re.sub(r"<.*?>", "", sender).strip().strip('"')
        items.append(Item(SILO_EMAIL, row.get("id") or row.get("subject"),
                          row.get("subject"), row.get("snippet"),
                          row.get("sent"), {"sender": sender,
                                            "display": display}))
    return items


def load_calendar(base_dir):
    try:
        import schedule
        events = schedule._calendar_events(base_dir, days_ahead=21)
    except Exception:
        return []
    return [Item(SILO_EVENT, f"{event['start']}|{event['what']}",
                 event["what"], "", event["start"])
            for event in events or []]


def load_tasks(base_dir):
    try:
        from providers.google_api import get_credentials
        from googleapiclient.discovery import build
        service = build("tasks", "v1", credentials=get_credentials(base_dir))
        items = []
        for tasklist in service.tasklists().list(maxResults=20).execute().get(
                "items", []):
            for task in service.tasks().list(
                    tasklist=tasklist["id"], showCompleted=False,
                    maxResults=60).execute().get("items", []):
                items.append(Item(SILO_TASK, task.get("id"),
                                  task.get("title"), task.get("notes"),
                                  task.get("due")))
        return items
    except Exception:
        return []


def load_projects(base_dir):
    try:
        import projects
    except Exception:
        return []
    items = []
    for row in projects.listing(base_dir)[:12]:
        body = row.get("description", "")
        try:
            text, _meta = projects.read_file(base_dir, row["name"], "README.md",
                                             max_bytes=1500)
            body = f"{body} {text}"
        except Exception:
            pass
        items.append(Item(SILO_PROJECT, row["name"], row["name"], body,
                          row.get("modified", "")))
    return items


# ------------------------------------------------------------------ #
# What has been done
# ------------------------------------------------------------------ #
def _ledger_path(base_dir):
    path = os.path.join(base_dir, _FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def load_ledger(base_dir):
    try:
        with open(_ledger_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_ledger(base_dir, ledger):
    target = _ledger_path(base_dir)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(target), delete=False,
        suffix=".tmp")
    try:
        json.dump(ledger, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)


# ------------------------------------------------------------------ #
# Writing, without touching anything that exists
# ------------------------------------------------------------------ #
def ensure_item_entity(store, item):
    """Create the node for an item if it is new. Never alters an existing one.

    Not `upsert_entity`: that SETs name, type and aliases, so calling it on a
    node that already exists -- a contact whose name happens to match -- would
    quietly reset it. `ON CREATE` only writes a node that was not there.
    """
    from memory.neo4j_store import _slug, _norm_key
    name = item.node_name()
    store.query(
        "MERGE (e:Entity {key:$key}) "
        "ON CREATE SET e.name=$name, e.entity_type=$type, "
        "e.norm_key=$norm, e.created_at=datetime(), e.origin=$origin",
        key=_slug(name), name=name, type=item.entity_type(),
        norm=_norm_key(name), origin=f"linking:{item.silo}")
    return name


def write_mention(store, item, target, quote):
    """An item names a target. An observation, stored as one."""
    name = ensure_item_entity(store, item)
    result = store.add_fact(
        name, "REFERS_TO", target.name, confidence=0.9,
        source_id=f"linking:{item.silo}:{item.ident}"[:120],
        fact_class="external_observation", evidence_type="cross_silo_match",
        authority=40, sensitivity="private", review_status="accepted",
        evidence_quote=f"{item.silo} text contains {quote!r}")
    return not str(result).startswith("[Rejected]")


# ------------------------------------------------------------------ #
# Asking the model about pairs
# ------------------------------------------------------------------ #
_SYSTEM = ("You judge whether things from someone's digital life concern "
           "the same matter. You output only JSON.")

_PROMPT = """\
Below are questions about items from different parts of one person's life:
calendar, tasks, email, projects, and the people in his address book. For each,
decide:

  same      -- they are about the very same thing, obligation or person
  related   -- they are about connected things (one is part of the other, or
               depends on it, or is a step toward it)
  unrelated -- sharing a word or a name does not make them the same matter

Be strict. "Interview with Deloitte" and "Deloitte job alert" are unrelated:
same company, different matters. A contact called "Chennai" is not the city of
Chennai. A first name in an email subject is only that contact if the context
makes it likely. Most questions are unrelated.

{questions}

Return exactly this JSON and nothing else:
{{"pairs": [{{"pair": 1, "verdict": "same|related|unrelated",
             "reason": "one short sentence", "confidence": 0.0}}]}}
"""


class Question:
    """One thing for the model to judge: two items, or an item and a name."""

    __slots__ = ("kind", "weight", "item", "other", "target", "detail")

    def __init__(self, kind, item, other=None, target=None, weight=0.0,
                 detail=""):
        self.kind = kind            # "items" or "mention"
        self.item = item
        self.other = other
        self.target = target
        self.weight = weight
        self.detail = detail

    def render(self):
        first = (f"  A [{self.item.silo}] {self.item.title}"
                 + (f" -- {self.item.body[:120]}" if self.item.body else ""))
        if self.kind == "items":
            second = (f"  B [{self.other.silo}] {self.other.title}"
                      + (f" -- {self.other.body[:120]}" if self.other.body
                         else ""))
        else:
            what = {"person": "a person in his address book",
                    "concept": "a topic in his knowledge graph",
                    "project": "one of his projects"}[self.target.kind]
            second = f"  B [{what}] {self.target.name}"
        return f"{first}\n{second}\n  ({self.detail})"


def questions_from(pairs, weak_mentions):
    """Item-pairs and ambiguous mentions, as one list, strongest first."""
    questions = [Question("items", first, other=second, weight=weight,
                          detail="share: " + ", ".join(shared[:6]))
                 for weight, first, second, shared in pairs]
    questions += [Question("mention", item, target=target, weight=1.0,
                           detail=f"the text contains {quote!r}")
                  for item, target, quote in weak_mentions]
    questions.sort(key=lambda q: -q.weight)
    return questions[:MAX_PAIRS_ASKED]


def render_questions(questions):
    return "\n\n".join(f"PAIR {number}:\n{question.render()}"
                       for number, question in enumerate(questions, 1))


VERDICTS = {"same": "SAME_CONCEPT_AS", "related": "RELATED_TO"}
#: Capped below the auto-promotion line: a judgement between two things is
#: always something he can see and dismiss, never something simply believed.
VERDICT_CAP = 0.88


def parse_verdicts(raw, questions):
    import inference
    data = inference._extract_json(raw) or {}
    verdicts = []
    for item in data.get("pairs") or []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("pair"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= len(questions):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.6
        question = questions[number - 1]
        predicate = VERDICTS[verdict]
        # An item that is the same as a person IS a reference to them; saying
        # "same concept as" about an email and a human would be nonsense.
        if question.kind == "mention" and verdict == "same":
            predicate = "REFERS_TO"
        verdicts.append({
            "question": question, "verdict": verdict, "predicate": predicate,
            "reason": " ".join(str(item.get("reason") or "").split())[:240],
            "confidence": round(max(0.3, min(VERDICT_CAP, confidence)), 2),
        })
    return verdicts


# ------------------------------------------------------------------ #
# A pass
# ------------------------------------------------------------------ #
def gather(store, base_dir):
    items = []
    for loader in (lambda: load_projects(base_dir),
                   lambda: load_calendar(base_dir),
                   lambda: load_tasks(base_dir),
                   lambda: load_emails(store)):
        try:
            items.extend(loader())
        except Exception as exc:
            print(f"[Linking] A source was unavailable: {exc}")
    return items[:MAX_ITEMS]


def run_pass(base_dir, call_llm=None, store=None, items=None, targets=None):
    """Link what is new. Returns {'mentions': n, 'proposed': n}."""
    if store is None:
        from memory.neo4j_store import store as graph
        store = graph
    run = idle_gate.Run(base_dir, JOB, max_calls=2, max_seconds=120.0,
                        daily_calls=12)
    if items is None:
        items = gather(store, base_dir)
    if not items:
        return {"mentions": 0, "proposed": 0}

    ledger = load_ledger(base_dir)
    fresh = [item for item in items
             if ledger.get(f"{item.silo}:{item.ident}") != item.fingerprint()]
    if not fresh:
        return {"mentions": 0, "proposed": 0}

    if targets is None:
        project_names = [item.title for item in items
                         if item.silo == SILO_PROJECT]
        targets = build_targets(load_persons(store), load_concepts(store),
                                project_names)

    mentions, weak = 0, []
    for item, target, quote in find_mentions(items, targets):
        if item not in fresh:
            continue
        if not target.strong():
            weak.append((item, target, quote))
            continue
        if mentions >= MAX_NEW_LINKS:
            continue
        try:
            if write_mention(store, item, target, quote):
                mentions += 1
        except Exception as exc:
            print(f"[Linking] Could not record {item.title!r} -> "
                  f"{target.name!r}: {exc}")

    proposed = 0
    pairs = [row for row in candidate_pairs(items)
             if row[1] in fresh or row[2] in fresh]
    questions = questions_from(pairs, weak)
    if questions and call_llm is not None and run.may_continue():
        messages = [{"role": "system", "content": _SYSTEM},
                    {"role": "user",
                     "content": _PROMPT.format(
                         questions=render_questions(questions))}]
        try:
            run.charge(1)
            try:
                raw = call_llm(messages, task_level="inference")
            except TypeError:
                raw = call_llm(messages)
        except Exception as exc:
            print(f"[Linking] Model call failed: {exc}")
            raw = ""
        for verdict in parse_verdicts(raw, questions):
            question = verdict["question"]
            try:
                subject = ensure_item_entity(store, question.item)
                if question.kind == "items":
                    obj = ensure_item_entity(store, question.other)
                else:
                    obj = question.target.name
                written = store.propose_inference(
                    subject, verdict["predicate"], obj,
                    f"Cross-silo: {verdict['reason']}",
                    confidence=verdict["confidence"],
                    source_id=f"linking:{question.item.ident}"[:120])
                if written:
                    proposed += 1
                    print(f"[Linking] Proposed: {written}")
            except Exception as exc:
                print(f"[Linking] Could not record a judgment: {exc}")

    for item in fresh:
        ledger[f"{item.silo}:{item.ident}"] = item.fingerprint()
    save_ledger(base_dir, ledger)
    if mentions or proposed:
        print(f"[Linking] {mentions} mention(s) recorded, {proposed} "
              f"link(s) proposed across {len(fresh)} new item(s).")
    return {"mentions": mentions, "proposed": proposed}
