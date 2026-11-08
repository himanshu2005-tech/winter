# -*- coding: utf-8 -*-
"""
deduction.py -- conclusions that follow from facts nobody put side by side.

The inference engine looks for patterns about him: what he seems to prefer,
how he seems to work. It reads facts one at a time and proposes what they
suggest. What it never did was chain them -- notice that A is tied to B, that
B is tied to C, and that together those say something about A and C that
neither fact says alone.

    Nikhila  -HAS_ALIAS->  Akka  <-POSSIBLY_SAME_PERSON_AS-  nikhila usa

Each edge is in the graph. The conclusion -- that two contact entries are
very probably one person -- is in none of them.

WHO DOES THE REASONING
----------------------
The model does. Code finds the chains, and code checks the answer; the step in
the middle -- does anything actually follow? -- is judgment, and writing it as
rules would be a thousand rules for a graph that will contain different things
next month.

THE THREE THINGS CODE DOES, AND WHY EACH ONE IS THERE
-----------------------------------------------------
*It refuses to pivot on a hub.* Most of the graph is a star around Himanshu:
647 of the 2-hop paths in it pass through him, and every one of those says
only "two things are both his". A path is only worth reasoning over when it
turns on something that is not everyone's neighbour -- nor on a literal, since
two people sharing a phone number's format is not a relationship.

*It checks the conclusion is grounded.* The model is asked which edges the
conclusion rests on, and the answer is verified: those edges must exist in
the path, there must be at least two of them (a conclusion from one edge is
just that edge restated), and an endpoint of the conclusion must be something
the path actually contains. A deduction that mentions things the evidence
does not is a guess dressed as logic.

*It never promotes its own output.* A conclusion lands as a pending
inference like any other, with the path as its rationale, and gets the same
corroboration gate as everything the inference engine proposes. Confidence is
capped below the auto-promotion line, so a deduction is always something he
can confirm or dismiss and never something VANTA simply starts believing --
and the facts it reasons from exclude derived inferences entirely, for the
reason inference.py gives: an engine that reasons over its own conclusions
becomes certain of things nobody said.
"""

import datetime
import hashlib
import json
import os
import re
import tempfile
import threading

import idle_gate

JOB = "deduction"
_LOCK = threading.RLock()
_FILENAME = os.path.join("vault", "memory", "deduction_ledger.json")

# --- limits ---------------------------------------------------------- #
#: A node with more than this many edges is a hub, not a pivot.
HUB_DEGREE = 20
MAX_HOPS = 3
#: Paths shown to the model in one pass. One call, bounded input.
MAX_PATHS = 12
#: Conclusions kept from one pass.
MAX_CONCLUSIONS = 6
#: Paths that pass through the same pivot, so one busy node cannot be the
#: whole pass.
MAX_PER_PIVOT = 3
#: Hours before a path may be put to the model again, and how many times in
#: all. Re-asking is how a conclusion is corroborated; asking forever about a
#: path that never yields one is waste.
COOLDOWN_HOURS = 20.0
MAX_CHECKS = 3
#: Hard ceiling on what the traversal will examine, whatever the graph holds.
MAX_EXPANSIONS = 60_000

#: The ceiling on a deduction's confidence, below idle auto-promotion's
#: threshold: a deduction is always something to confirm, never something
#: simply believed.
CONFIDENCE_CAP = 0.85
#: And lower still when nothing in the path was said by him.
OBSERVED_ONLY_CAP = 0.60

#: Classes of fact a deduction may reason from. Derived inferences are
#: deliberately absent.
SOURCE_CLASSES = ("explicit_user_fact", "explicit_preference",
                  "explicit_goal", "explicit_boundary",
                  "self_reported_pattern", "external_observation",
                  # Backward compatibility for research facts written before
                  # external_observation became the canonical class.
                  "external_fact")
STATED_CLASSES = frozenset(SOURCE_CLASSES) - {
    "external_observation", "external_fact"}

#: Entity types that are values rather than things.
_LITERAL_TYPES = frozenset({"ProfileValue", "Email"})
_LITERAL_SHAPE = re.compile(
    r"^\+?[\d\s().-]{7,}$|@|^https?://", re.IGNORECASE)


# ------------------------------------------------------------------ #
# The graph, as plain data
# ------------------------------------------------------------------ #
class Edge:
    __slots__ = ("subject", "subject_key", "predicate", "object",
                 "object_key", "confidence", "fact_class", "source")

    def __init__(self, row):
        self.subject = str(row.get("s") or "")
        self.subject_key = str(row.get("sk") or self.subject.lower())
        self.predicate = str(row.get("p") or "")
        self.object = str(row.get("o") or "")
        self.object_key = str(row.get("ok") or self.object.lower())
        self.confidence = float(row.get("c") if row.get("c") is not None else 1.0)
        self.fact_class = str(row.get("fc") or "explicit_user_fact")
        self.source = str(row.get("src") or "")

    def key(self):
        return (self.subject_key, self.predicate, self.object_key)

    def text(self):
        return f"({self.subject}) -{self.predicate}-> ({self.object})"

    def stated(self):
        return self.fact_class in STATED_CLASSES


class Graph:
    """Nodes and edges with an undirected index, for finding chains."""

    def __init__(self, rows, types=None):
        self.edges = []
        self.names = {}
        self.types = dict(types or {})
        self.incident = {}
        seen = set()
        for row in rows:
            edge = Edge(row)
            if not edge.subject_key or not edge.object_key:
                continue
            if edge.subject_key == edge.object_key:
                continue
            if edge.key() in seen:
                continue
            seen.add(edge.key())
            self.edges.append(edge)
            self.names.setdefault(edge.subject_key, edge.subject)
            self.names.setdefault(edge.object_key, edge.object)
            if row.get("st"):
                self.types.setdefault(edge.subject_key, row["st"])
            if row.get("ot"):
                self.types.setdefault(edge.object_key, row["ot"])
            self.incident.setdefault(edge.subject_key, []).append(edge)
            self.incident.setdefault(edge.object_key, []).append(edge)

    def degree(self, key):
        return len(self.incident.get(key, ()))

    def is_literal(self, key):
        if self.types.get(key) in _LITERAL_TYPES:
            return True
        return bool(_LITERAL_SHAPE.search(self.names.get(key, "")))

    def is_hub(self, key, hub_degree=HUB_DEGREE):
        return self.degree(key) > hub_degree

    def can_pivot(self, key, hub_degree=HUB_DEGREE):
        return not self.is_literal(key) and not self.is_hub(key, hub_degree)

    def linked(self, a, b):
        return any(edge.subject_key == b or edge.object_key == b
                   for edge in self.incident.get(a, ()))


def other_end(edge, key):
    return edge.object_key if edge.subject_key == key else edge.subject_key


class Path:
    """A chain of edges, with the nodes between them in order."""

    __slots__ = ("nodes", "edges")

    def __init__(self, nodes, edges):
        self.nodes = tuple(nodes)
        self.edges = tuple(edges)

    def key(self):
        """Stable across direction and order, for the ledger."""
        triples = sorted("|".join(edge.key()) for edge in self.edges)
        return hashlib.sha1("\n".join(triples).encode()).hexdigest()[:16]

    def pivots(self):
        return self.nodes[1:-1]

    def stated_edges(self):
        return [edge for edge in self.edges if edge.stated()]


def enumerate_paths(graph, hub_degree=HUB_DEGREE, max_hops=MAX_HOPS,
                    max_expansions=MAX_EXPANSIONS):
    """Every chain of two or three edges that turns on ordinary nodes.

    Endpoints may be anything that is not a literal; pivots must also not be
    hubs. Deduplicated across direction, and bounded by a hard expansion cap
    so a graph that grows ten times larger cannot turn a pass into an hour.
    """
    found, seen, budget = [], set(), [max_expansions]

    def extend(nodes, edges):
        if budget[0] <= 0:
            return
        tail = nodes[-1]
        for edge in graph.incident.get(tail, ()):
            budget[0] -= 1
            if budget[0] <= 0:
                return
            if any(edge is used for used in edges):
                continue
            nxt = other_end(edge, tail)
            if nxt in nodes or graph.is_literal(nxt):
                continue
            chain_nodes, chain_edges = nodes + [nxt], edges + [edge]
            if len(chain_edges) >= 2:
                signature = tuple(sorted("|".join(e.key())
                                         for e in chain_edges))
                if signature not in seen:
                    seen.add(signature)
                    found.append(Path(chain_nodes, chain_edges))
            if (len(chain_edges) < max_hops
                    and graph.can_pivot(nxt, hub_degree)):
                extend(chain_nodes, chain_edges)

    for start in list(graph.incident):
        if budget[0] <= 0:
            break
        if graph.is_literal(start) or graph.is_hub(start, hub_degree):
            continue
        if not graph.can_pivot(start, hub_degree):
            continue
        # The first step may leave from any non-hub node; what follows must
        # pivot on one too, which `extend` enforces.
        extend([start], [])
    return [path for path in found
            if all(graph.can_pivot(node, hub_degree)
                   for node in path.pivots())]


# ------------------------------------------------------------------ #
# Choosing which chains are worth the model's attention
# ------------------------------------------------------------------ #
def score(path, graph):
    """How likely a chain is to say something nobody wrote down.

    Cheap and structural. The model decides whether anything follows; this
    only decides which chains are worth asking about, because asking about
    all of them is a call per chain.
    """
    predicates = [edge.predicate for edge in path.edges]
    if len(set(predicates)) == 1 and predicates[0] == "RELATED_TO":
        return 0.0           # "related to" chains tell you nothing
    value = 1.0
    value += 0.6 * (len(set(predicates)) - 1)           # variety
    value += 0.8 * sum(1 for edge in path.edges if edge.stated())
    # Two things already directly connected add nothing by being connected
    # again through a third.
    if graph.linked(path.nodes[0], path.nodes[-1]):
        value -= 1.2
    value += 0.4 * min(e.confidence for e in path.edges)
    if len(path.edges) > 2:
        # Each extra hop is another place the reasoning can slip, and three
        # distinct predicates earn more in variety than a hop costs here --
        # so without a real penalty a rambling three-edge chain outranks a
        # tight two-edge one that says the same thing more soundly.
        value -= 1.8
    return max(0.0, value)


def pick(paths, graph, ledger, limit=MAX_PATHS, now=None):
    """The chains to put to the model, best first, with diversity."""
    ranked = sorted(((score(path, graph), path) for path in paths),
                    key=lambda pair: -pair[0])
    chosen, per_pivot = [], {}
    for value, path in ranked:
        if value <= 0 or len(chosen) >= limit:
            break
        if not ledger_allows(ledger, path, now):
            continue
        if any(per_pivot.get(node, 0) >= MAX_PER_PIVOT for node in path.pivots()):
            continue
        for node in path.pivots():
            per_pivot[node] = per_pivot.get(node, 0) + 1
        chosen.append(path)
    return chosen


# ------------------------------------------------------------------ #
# What has already been asked
# ------------------------------------------------------------------ #
def _path(base_dir):
    path = os.path.join(base_dir, _FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def load_ledger(base_dir):
    try:
        with open(_path(base_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_ledger(base_dir, ledger):
    target = _path(base_dir)
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


def ledger_allows(ledger, path, now=None):
    entry = ledger.get(path.key())
    if not entry:
        return True
    if int(entry.get("checks", 0)) >= MAX_CHECKS and not entry.get("concluded"):
        return False       # asked enough times; nothing follows from it
    try:
        age = (now or datetime.datetime.now()) - datetime.datetime.fromisoformat(
            entry["at"])
    except (KeyError, ValueError):
        return True
    return age >= datetime.timedelta(hours=COOLDOWN_HOURS)


def note_checked(ledger, path, concluded, now=None):
    entry = ledger.setdefault(path.key(), {"checks": 0})
    entry["checks"] = int(entry.get("checks", 0)) + 1
    entry["at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    entry["concluded"] = bool(entry.get("concluded") or concluded)


# ------------------------------------------------------------------ #
# Asking
# ------------------------------------------------------------------ #
_SYSTEM = ("You reason carefully over a knowledge graph and output only JSON. "
           "You conclude only what the evidence forces.")

_PROMPT = """\
Below are chains of facts from a personal knowledge graph. Each chain links
things through shared nodes. For each chain, decide whether a NEW conclusion
follows from the edges taken TOGETHER -- something none of the edges says on
its own.

Conclude only when the edges force it or make it very much more likely than
not. Do not restate an edge. Do not conclude from a single edge. Do not guess
what is plausible. For most chains the correct answer is to conclude nothing,
and an empty list is a good answer.

Do not conclude something that only swaps a name for one of its aliases:
"he has a mother called Anitha" and "Anitha is also called Mummy" do not make
"he has a mother called Mummy" a new finding. A conclusion has to add a
relationship that was not already there under another name.

Never infer health conditions, beliefs, finances, relationship quality or any
other sensitive trait about a person.

CHAINS
{chains}

Return exactly this JSON and nothing else:
{{"conclusions": [
  {{"chain": 1,
    "edges": [1, 2],
    "subject": "an entity from the chain",
    "predicate": "RELATION_NAME_IN_CAPS",
    "object": "an entity from the chain, or a short new description",
    "statement": "one plain sentence saying what follows",
    "confidence": 0.0}}
]}}

"edges" lists the edge numbers within that chain that the conclusion rests on;
at least two. At most {limit} conclusions in total.
"""


def render(paths):
    """The chains, numbered, with who said each edge."""
    blocks = []
    for number, path in enumerate(paths, 1):
        lines = [f"CHAIN {number}:"]
        for index, edge in enumerate(path.edges, 1):
            who = "he said" if edge.stated() else "observed elsewhere"
            lines.append(f"  e{index}: {edge.text()}  "
                         f"[{who}, confidence {edge.confidence:.2f}]")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_PREDICATE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")

#: A relation is a name, not a sentence. "POSSIBLY_SAME_PERSON_AS" is four
#: words and a relation; "IS_PROBABLY_THE_SAME_PERSON" is a claim wearing a
#: predicate's clothes, and storing it makes the graph unqueryable.
MAX_PREDICATE_WORDS = 4


def parse(raw, paths):
    """Conclusions the model offered, kept only where they are grounded."""
    import inference
    data = inference._extract_json(raw) or {}
    kept = []
    for item in (data.get("conclusions") or [])[:MAX_CONCLUSIONS * 2]:
        if not isinstance(item, dict):
            continue
        try:
            chain = int(item.get("chain"))
        except (TypeError, ValueError):
            continue
        if not 1 <= chain <= len(paths):
            continue
        path = paths[chain - 1]
        cited = set()
        for value in item.get("edges") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= len(path.edges):
                cited.add(number)
        # A conclusion from one edge is that edge restated.
        if len(cited) < 2:
            continue
        subject = " ".join(str(item.get("subject") or "").split())
        obj = " ".join(str(item.get("object") or "").split())
        predicate = str(item.get("predicate") or "").strip().upper().replace(
            " ", "_")
        statement = " ".join(str(item.get("statement") or "").split())[:300]
        if not (subject and obj and statement and _PREDICATE.match(predicate)):
            continue
        if len([w for w in predicate.split("_") if w]) > MAX_PREDICATE_WORDS:
            continue
        if len(subject) > 80 or len(obj) > 120 or subject.lower() == obj.lower():
            continue
        # Grounded: an endpoint has to be something the chain contains.
        in_chain = {path_name.lower() for path_name in
                    (n for edge in path.edges for n in (edge.subject, edge.object))}
        if subject.lower() not in in_chain and obj.lower() not in in_chain:
            continue
        # Not just an existing edge under another name.
        if any(edge.subject.lower() == subject.lower()
               and edge.object.lower() == obj.lower() for edge in path.edges):
            continue
        try:
            stated_confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            stated_confidence = 0.5
        # The same conclusion reached from two chains is one conclusion. The
        # first dry run against the real graph produced "I HAS_FATHER Pappa"
        # twice, which would have counted as corroboration of itself.
        signature = (subject.lower(), predicate, obj.lower())
        if any((k["subject"].lower(), k["predicate"], k["object"].lower())
               == signature for k in kept):
            continue
        kept.append({
            "path": path, "cited": sorted(cited), "subject": subject,
            "predicate": predicate, "object": obj, "statement": statement,
            "confidence": cap_confidence(
                path, [path.edges[n - 1] for n in cited], stated_confidence),
        })
        if len(kept) >= MAX_CONCLUSIONS:
            break
    return kept


def cap_confidence(path, cited_edges, stated):
    """Never higher than the weakest edge, the model's own doubt, or the cap."""
    ceiling = (CONFIDENCE_CAP if any(edge.stated() for edge in cited_edges)
               else OBSERVED_ONLY_CAP)
    weakest = min(edge.confidence for edge in cited_edges)
    return round(max(0.05, min(ceiling, weakest, max(0.0, min(1.0, stated)))), 2)


def rationale(conclusion):
    path = conclusion["path"]
    cited = "; ".join(path.edges[n - 1].text() for n in conclusion["cited"])
    return f"Deduced: {conclusion['statement']} From: {cited}"[:600]


# ------------------------------------------------------------------ #
# A pass
# ------------------------------------------------------------------ #
_SQL = """
MATCH (s:Entity)-[r:FACT]->(o:Entity)
WHERE r.valid_to IS NULL
  AND coalesce(r.fact_class,'explicit_user_fact') IN $classes
RETURN s.key AS sk, s.name AS s, coalesce(s.entity_type,'Entity') AS st,
       r.predicate AS p, o.key AS ok, o.name AS o,
       coalesce(o.entity_type,'Entity') AS ot,
       coalesce(r.confidence,1.0) AS c,
       coalesce(r.fact_class,'explicit_user_fact') AS fc,
       coalesce(r.source_id,'') AS src
LIMIT 8000
"""


def load_graph(store):
    rows = store.query(_SQL, classes=list(SOURCE_CLASSES))
    return Graph(rows)


def run_pass(base_dir, call_llm, store=None, now=None):
    """One pass: find chains, ask, check, propose. Returns what was proposed."""
    if store is None:
        from memory.neo4j_store import store as graph
        store = graph
    run = idle_gate.Run(base_dir, JOB, max_calls=2, max_seconds=180.0,
                        daily_calls=12)
    if not run.may_continue():
        return []
    try:
        graph = load_graph(store)
    except Exception as exc:
        print(f"[Deduction] Could not read the graph: {exc}")
        return []

    ledger = load_ledger(base_dir)
    paths = pick(enumerate_paths(graph), graph, ledger, now=now)
    if not paths:
        print("[Deduction] No chains worth reasoning over right now.")
        return []

    prompt = _PROMPT.format(chains=render(paths), limit=MAX_CONCLUSIONS)
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt}]
    try:
        run.charge(1)
        try:
            raw = call_llm(messages, task_level="inference")
        except TypeError:
            raw = call_llm(messages)
    except Exception as exc:
        print(f"[Deduction] Model call failed: {exc}")
        return []

    conclusions = parse(raw, paths)
    concluded_paths = {conclusion["path"].key() for conclusion in conclusions}
    for path in paths:
        note_checked(ledger, path, path.key() in concluded_paths, now)
    save_ledger(base_dir, ledger)

    recorded = []
    for conclusion in conclusions:
        try:
            written = store.propose_inference(
                conclusion["subject"], conclusion["predicate"],
                conclusion["object"], rationale(conclusion),
                confidence=conclusion["confidence"],
                source_id=f"deduction:{conclusion['path'].key()}")
        except Exception as exc:
            print(f"[Deduction] Could not record a conclusion: {exc}")
            continue
        if written:
            recorded.append(written)
            print(f"[Deduction] Concluded ({conclusion['confidence']:.2f}): "
                  f"{written}")
    if not recorded:
        print(f"[Deduction] Considered {len(paths)} chain(s); nothing "
              f"followed that was not already known.")
    return recorded
