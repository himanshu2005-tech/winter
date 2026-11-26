# -*- coding: utf-8 -*-
"""
action_registry.py -- show the model the dozen actions it needs, not all of them.

Every executor call carried whole catalogues: roughly six to nine thousand
tokens of capability documentation, re-sent on each iteration of the loop, most
of it irrelevant to the request. That cost is paid per call, and a turn is
several calls, so the dominant line in the token bill was a manual the model
already had and mostly did not need.

It also could not grow. VANTA owns 913 key-free public APIs, self-written
actions and more built-ins every week; injecting the catalogue does not scale
past the context window, and "add a capability" eventually meant "make every
turn more expensive".

So the catalogue becomes an index. Each action is parsed once, scored against
the request and the plan, and only the best handful are rendered into the
prompt. Prompt size stops depending on how many capabilities exist -- 2,000
actions cost the same as 20 -- and DISCOVER_ACTIONS is always offered so the
model has a way to reach anything the ranking missed. Retrieval is allowed to
be imperfect precisely because that escape hatch exists.

WHY THE SCORING IS SHAPED THIS WAY
----------------------------------
Names carry most of the signal, and they are compound: WATCH_STATUS and
BROWSER_SNAPSHOT say what they do in their own tokens. So a name is split on
underscores and matched token by token, with stemming loose enough that
"watches" finds WATCH and "email" finds EMAILS. Descriptions are matched too
but weighted lower, because prose repeats common words across unrelated
actions.

An action named outright in the request or plan is not a ranking question --
the planner asked for it by name, so it goes first.
"""

import re
import threading
import math
import hashlib

#: The catalogue whose actions are always offered, however the ranking went.
DISCOVERY_CATALOG = "DISCOVERY"

#: Words that carry no signal in a request.
# Words that match everything and therefore discriminate nothing. The second
# block was added after "where am i" ranked the camera first: the only word
# left in that query after filtering was "am", which the camera's alias phrase
# "what am i holding" happened to contain. A copula is not evidence of intent.
_STOPWORDS = frozenset("""
a an an and are as at be but by can could do does for from get give go has have
how i if in into is it its me my of on only or our out please so that the
their them then there these they this to up us use using want was what when
where which who why will with would you your
am been being did doing done had having he her hers him his isnt its ll
not now re she should ve wasnt we were whats wont youre
also any here just too very
""".split())

# Alias scoring. The first matched word is the strong signal; each further one
# adds less, and after a few they add nothing. Without the taper, breadth of
# alias vocabulary was itself a ranking advantage.
_ALIAS_FIRST_HIT = 17.0
_ALIAS_EXTRA_HIT = 5.0
_ALIAS_HIT_CEILING = 3

# Semantic relevance is scored against the candidate pool, not on an absolute
# scale. Cosine similarities here sit in a narrow band -- right answers around
# 0.25-0.43, wrong ones 0.05-0.25 -- so a flat multiplier could never decide
# anything. Scoring the margin above the pool's typical similarity spreads that
# band out, letting a genuine paraphrase win while an across-the-board weak
# similarity still earns nothing.
_SEMANTIC_MARGIN_WEIGHT = 100.0
_SEMANTIC_BASELINE_QUANTILE = 0.60

_ACTION_START = re.compile(r"<\s*ACTION\b", re.IGNORECASE)
# Catalogues document related actions together as type="A|B|C", so this has to
# accept the alternation and split it afterwards. Requiring a single identifier
# meant the WHOLE entry was skipped: WATCH_PAUSE, WATCH_RESUME and WATCH_CANCEL
# were documented, dispatched, registered and schema-checked -- and invisible
# to the registry. DISCOVER_ACTIONS therefore could not return them, and the
# model searched four times before concluding, reasonably, that there was no
# way to cancel a watch. There was; it simply could not be found.
_TYPE_ATTR = re.compile(
    r'type\s*=\s*["\']([A-Za-z0-9_]+(?:\|[A-Za-z0-9_]+)*)["\']', re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9]+")

_lock = threading.RLock()
_entries = []
_configured_signature = None
_semantic_vectors = {}
_semantic_signature = None
_semantic_warming = False
#: Per-word weight for action-name matches; see _build_name_token_weights.
_name_token_weight = {}

# Capability metadata, not prompt instructions. These are the phrases people
# naturally use when they do not know an action's internal name. Keeping them
# beside the registry means the same aliases improve initial routing and
# DISCOVER_ACTIONS, and adding thousands of actions does not enlarge each
# model prompt. Catalogue prose remains the primary source; aliases only bridge
# vocabulary gaps such as "drive contents" versus LIST_DIRECTORY.
_CAPABILITY_ALIASES = {
    "LIST_DIRECTORY": "browse folder drive contents enumerate files folders what is on disk",
    "FIND_FILES": "locate find search for file filename where is document",
    "READ_FILE": "open inspect view local file contents",
    "GITHUB_READ_REPO": "inspect github repository repo link project",
    "GITHUB_READ_FILE": "inspect github source code file repository",
    "READ_EMAILS": "check inbox mail messages summarize emails",
    "READ_CALENDAR": "schedule agenda appointments events today",
    "LIST_CALENDAR_EVENTS": "schedule agenda appointments events upcoming",
    "ADD_CALENDAR_EVENT": "schedule create meeting appointment block calendar remind reminder timed",
    "UPDATE_CALENDAR_EVENT": "move reschedule change edit correct existing meeting appointment event time calendar",
    "DELETE_CALENDAR_EVENT": "cancel remove delete existing meeting appointment event calendar",
    "LIST_TASKS": "todo reminders pending tasks",
    "ADD_TASK": "remember todo create task reminder",
    "GET_TRAVEL_TIME": "traffic commute eta journey duration directions",
    "SHOW_ROUTE": "map directions route navigation travel",
    "MUSIC_PLAY": "play song playlist audio youtube music",
    "SPOTIFY_PLAY": "play song playlist spotify",
    "SHOW_ON_SCREEN": "display pin checklist note overlay on screen",
    "BROWSER_START": "open launch start browser chrome firefox edge web website online",
    "BROWSER_TABS": "browser tabs pages windows inspect open web sessions",
    "BROWSER_GOTO": "open navigate visit url link website webpage browser forms office microsoft",
    "BROWSER_SNAPSHOT": "inspect read understand find locate scroll browser webpage website form fields buttons links upload resume attachment",
    "BROWSER_CLICK": "click choose browser webpage website button link option create new form",
    "BROWSER_DIALOG": "browser javascript alert confirm prompt popup dialog modal accept dismiss okay cancel",
    "BROWSER_TYPE": "type enter write browser webpage website form question title name email",
    "BROWSER_FILL_FORM": "fill complete browser web form fields inputs name email questionnaire",
    "BROWSER_UPLOAD": "upload attach add resume cv portfolio file document browser web form application",
    "BROWSER_SUBMIT": "submit save create finish browser web form website",
    "LAUNCH_APP": "open launch start run application app program software windows tool calculator notepad terminal settings",
    "RUN_COMMAND": "terminal shell powershell command cli console script automate install build git process network diagnostics cybersecurity security scan system administration",
    "MOVE_FILE": "move relocate transfer file screenshot image artifact desktop folder directory drive downloads documents",
    "DASHBOARD_SHOW_TEXT": "show display render put code text content answer result output snippet dashboard control center",
    "RESULT_WINDOW": "show hide open close toggle display report answer information latex formula equation table chart diagram image photo mesh 3d model animation timeline questionnaire quiz survey explorer sliders flashcards walkthrough code interactive window teach explain derive compare visualise",
    "CONTROL_CENTER": "show display dashboard control center code codes coding panel source",
    "SHOW_CODE_PANEL": "show view display code codes source coding panel editor",
    "RUNTIME_APPLY": "build create generate modify update connect link wire attach model scene architecture blender live executable revision",
    "RUNTIME_STATUS": "inspect examine scene model architecture objects links edges runtime state blender",
    "RUNTIME_RESUME": "resume reopen restore blender scene model executable runtime",
    "RUNTIME_PRESENT": "present presentation model only fullscreen viewport orbit pan zoom blender scene hide interface",
}

_CAPABILITY_DENIAL = re.compile(
    r"\b(?:can(?:not|'t)|unable|do not have|don't have|not available|"
    r"unavailable|not (?:currently )?(?:configured|enabled|accessible)|"
    r"is(?:n(?:'|\u2019)?t| not) (?:currently )?"
    r"(?:available|configured|enabled|accessible)|"
    r"aren(?:'|\u2019)?t (?:currently )?available|no (?:suitable )?"
    r"(?:action|tool|capabilit))\b",
    re.IGNORECASE,
)

# Execution prerequisites and verification companions. These are capability
# dependencies, not utterance rules: regardless of how the user phrases a web
# task, filling a page is unsafe without first opening/navigating and observing
# it, and completion is unverifiable without a final snapshot. Bundles keep a
# coherent workflow from being crowded out by an incidental word such as
# "email" matching READ_EMAILS.
_ACTION_BUNDLES = {
    "BROWSER_GOTO": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT"),
    "BROWSER_CLICK": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
        "BROWSER_CLICK", "BROWSER_DIALOG"),
    "BROWSER_TYPE": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
        "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_PRESS"),
    "BROWSER_FILL_FORM": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
        "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_UPLOAD", "BROWSER_FILL_FORM",
        "BROWSER_SUBMIT"),
    "BROWSER_UPLOAD": (
        "FIND_FILES", "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
        "BROWSER_CLICK", "BROWSER_UPLOAD", "BROWSER_FILL_FORM",
        "BROWSER_SUBMIT"),
    "BROWSER_SUBMIT": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_SNAPSHOT",
        "BROWSER_FILL_FORM", "BROWSER_SUBMIT"),
    "BROWSER_READ": (
        "BROWSER_START", "BROWSER_GOTO", "BROWSER_READ"),
}

# This is execution substrate rather than a domain-specific capability. Keep
# it visible so an ordinary operating-system request can run on the first turn
# when no purpose-built action exists. Visibility is not permission: the
# code-owned command policy still gates high-impact commands.
_ALWAYS_VISIBLE = ("RUN_COMMAND",)


# ------------------------------------------------------------------ #
# Parsing
# ------------------------------------------------------------------ #

def _split_actions(text):
    """Every ACTION tag in a catalogue, with the prose that follows it.

    Written as a scan rather than one regex because a regex for the block form
    will happily swallow a self-closing tag that precedes it: given
    `<ACTION type="READ_EMAILS" />` followed by
    `<ACTION type="SEND_EMAIL">body</ACTION>`, a `<ACTION.*?</ACTION>` pattern
    matches from the first tag to the last close and READ_EMAILS disappears
    from the registry entirely -- an action the model can no longer see.
    """
    found = []
    for match in _ACTION_START.finditer(text):
        start = match.start()
        # Where does this tag end? The first '>' that closes it.
        close = text.find(">", start)
        if close == -1:
            continue
        head = text[start:close + 1]
        name_match = _TYPE_ATTR.search(head)
        if not name_match:
            continue
        names = [part.upper() for part in name_match.group(1).split("|")
                 if part.strip()]
        if not names:
            continue

        if head.rstrip().endswith("/>"):
            body_end = close + 1
        else:
            # Block form. Its own close tag, unless another ACTION opens first
            # -- in which case the tag was never closed and the body is not
            # ours to claim.
            own_close = text.lower().find("</action>", close)
            next_open = _ACTION_START.search(text, close)
            if own_close == -1 or (next_open and next_open.start() < own_close):
                body_end = close + 1
            else:
                body_end = own_close + len("</action>")

        # The explanatory lines that follow, up to the next tag.
        next_tag = _ACTION_START.search(text, body_end)
        tail_end = next_tag.start() if next_tag else len(text)
        tag_text = text[start:body_end].strip()
        notes = text[body_end:tail_end].strip()
        for name in names:
            # One entry each, so every alternative is findable by its own name.
            # They share the documentation, which is right: it was written to
            # describe all of them.
            shown = tag_text
            if len(names) > 1:
                shown = _TYPE_ATTR.sub(f'type="{name}"', tag_text, count=1)
            found.append({"name": name, "text": shown, "notes": notes})
    return found


def configure(catalogs):
    """Index the catalogues. Cheap to call repeatedly; re-parses only on change."""
    global _entries, _configured_signature, _name_token_weight
    catalogs = catalogs or {}
    signature = tuple(sorted((
        name,
        hashlib.sha256(str(text or "").encode("utf-8")).hexdigest(),
    ) for name, text in catalogs.items()))
    with _lock:
        if signature == _configured_signature and _entries:
            return len(_entries)
        # One entry per action, not per mention. A catalogue documents the same
        # action several times to show different forms -- BOARD_ANIMATE appears
        # three times, ADD_CALENDAR_EVENT twice -- and indexing each mention
        # separately meant three identical BOARD_ANIMATE entries filled a
        # twelve-slot budget and pushed BOARD_MATH and BOARD_SECTION out of a
        # teaching prompt. The duplicates were not noise; they were crowding
        # out the actions the turn actually needed.
        # Declared capabilities contribute their retrieval vocabulary the
        # same way the hand-written table does. Merged here rather than at
        # import time so capabilities.py stays free of a registry dependency.
        # Three sources, merged rather than replacing one another: the noun
        # aliases below, the operation vocabulary (the verb half, which was
        # missing entirely), and anything a declared capability brings.
        aliases = dict(_CAPABILITY_ALIASES)
        try:
            import capability_vocab
            for name, words in capability_vocab.vocabulary().items():
                aliases[name] = f"{aliases.get(name, '')} {words}".strip()
        except Exception:
            pass
        try:
            import capabilities
            for name, words in capabilities.alias_overlay().items():
                aliases[name] = f"{aliases.get(name, '')} {words}".strip()
        except Exception:
            pass

        merged = {}
        order = []
        for catalog, text in catalogs.items():
            for found in _split_actions(str(text or "")):
                key = (catalog, found["name"])
                if key in merged:
                    existing = merged[key]
                    # Keep the fullest example and all the documentation.
                    if len(found["text"]) > len(existing["text"]):
                        existing["text"] = found["text"]
                    if found["notes"]:
                        existing["notes"] = (
                            existing["notes"] + "\n" + found["notes"]).strip()
                    continue
                found["catalog"] = catalog
                merged[key] = found
                order.append(key)

        entries = []
        for position, key in enumerate(order):
            found = merged[key]
            # Where it appears in its catalogue. Used only to break ties, and
            # it beats alphabetical: a catalogue is written with the important
            # actions first, so "teach me on the board" was choosing BOARD_HIDE
            # and BOARD_CLEAR over BOARD_WRITE and BOARD_MATH purely because H
            # and C sort before W and M. Document order is the author's own
            # judgement about what matters, which is better than none.
            found["position"] = position
            found["tokens"] = _name_tokens(found["name"])
            # Stopwords are filtered on the query side, so an alias
            # stopword could only ever produce a spurious match -- which is
            # exactly what "am" did for the camera.
            found["alias_tokens"] = sorted(
                _tokenise(aliases.get(found["name"], "")) - _STOPWORDS)
            found["haystack"] = _tokenise(
                f"{found['name']} {found['text']} {found['notes']} "
                f"{aliases.get(found['name'], '')}")
            entries.append(found)
        _name_token_weight = _build_name_token_weights(entries)
        _entries = entries
        _configured_signature = signature
        return len(_entries)


def _semantic_document(entry):
    """Compact capability meaning used by the local semantic retriever."""
    alias = _CAPABILITY_ALIASES.get(entry["name"], "")
    notes = " ".join((entry.get("notes") or "").split())[:500]
    syntax = re.sub(r"[<>'\"=/]", " ", entry.get("text") or "")
    return f"{entry['name'].replace('_', ' ')}. {alias}. {syntax}. {notes}"


def _build_semantic_index(signature, entries):
    global _semantic_vectors, _semantic_signature, _semantic_warming
    try:
        from memory.embeddings import embed_texts
        vectors = []
        documents = [_semantic_document(entry) for entry in entries]
        # Bounded batches avoid a large transient tensor when extension tools
        # eventually push the registry into the thousands.
        for start in range(0, len(documents), 128):
            vectors.extend(embed_texts(documents[start:start + 128]))
        built = {
            (entry["catalog"], entry["name"]): vector
            for entry, vector in zip(entries, vectors)
        }
        with _lock:
            # Discard a stale build if extensions changed while it ran.
            if signature == _configured_signature:
                _semantic_vectors = built
                _semantic_signature = signature
        print(f"[Actions] Semantic chooser ready for {len(built)} capabilities.")
    except Exception as exc:
        print(f"[Actions] Semantic chooser unavailable; lexical routing remains active: {exc}")
    finally:
        with _lock:
            _semantic_warming = False


def warm_semantic_index(background=True):
    """Build the local action embedding index once per registry version."""
    global _semantic_warming
    with _lock:
        signature = _configured_signature
        entries = list(_entries)
        if not entries or _semantic_signature == signature or _semantic_warming:
            return False
        _semantic_warming = True
    if background:
        threading.Thread(
            target=_build_semantic_index, args=(signature, entries), daemon=True,
            name="vanta-action-semantic-index").start()
    else:
        _build_semantic_index(signature, entries)
    return True


def _build_name_token_weights(entries):
    """How much a matched word in an action's name is worth.

    Not all name words are equal evidence. "project" appears in the names of
    eleven capabilities -- LIST_PROJECTS, PROJECT_TREE, OPEN_PROJECT,
    READ_PROJECT_FILE and the rest -- so matching it narrows nothing, while
    "calendar" or "alarm" appears in a handful and is close to decisive.

    Treating them identically is what let a subject noun beat an operation:
    "move the final-year project review to 1 PM" ranked DELETE_PROJECT_FILE
    and PROJECT_TREE above UPDATE_CALENDAR_EVENT, because the sentence happens
    to describe a project while asking to move a meeting. The user's subject
    matter is not a choice of tool.

    So a word's weight falls as the number of capabilities sharing it rises --
    ordinary inverse document frequency, over action names only.
    """
    frequency = {}
    for entry in entries:
        for token in set(entry.get("tokens", ())):
            frequency[token] = frequency.get(token, 0) + 1
    total = max(1, len(entries))
    weights = {}
    for token, count in frequency.items():
        # A token unique to one capability keeps full weight; one shared by a
        # dozen keeps a small fraction. Clamped so that no match is worthless
        # and none is overwhelming.
        weights[token] = max(0.25, min(1.0, math.log(1 + total / count)
                                       / math.log(1 + total)))
    return weights


def _semantic_baseline(similarities):
    """The similarity a candidate has to beat to have said anything.

    Taken from this query's own pool rather than fixed, because the whole
    distribution shifts with the question: a vague "where am i" leaves every
    capability around 0.1, while a specific request pushes the relevant few to
    0.4. A fixed threshold either admits everything in the first case or
    nothing in the second.
    """
    values = sorted(float(value) for value in similarities)
    if not values:
        return 0.0
    index = min(len(values) - 1,
                int(len(values) * _SEMANTIC_BASELINE_QUANTILE))
    baseline = values[index]
    # A baseline at or above the best candidate cancels the very signal it is
    # meant to grade. That happens whenever the pool is small -- with one
    # relevant capability among three, the quantile *is* that capability --
    # and it silently removed the only genuine paraphrase match from the
    # results. When the pool cannot support a quantile, grade against nothing
    # and let raw similarity speak.
    if baseline >= values[-1]:
        return 0.0
    return baseline


def _cosine(left, right):
    # The local embedder already normalizes vectors. Retain the denominator so
    # test doubles and future providers cannot silently break ranking.
    numerator = sum(a * b for a, b in zip(left, right))
    lnorm = math.sqrt(sum(a * a for a in left))
    rnorm = math.sqrt(sum(b * b for b in right))
    return numerator / max(1e-12, lnorm * rnorm)


# ------------------------------------------------------------------ #
# Scoring
# ------------------------------------------------------------------ #

def _name_tokens(name):
    return [part for part in str(name).lower().split("_") if part]


def _tokenise(text):
    return {word.lower() for word in _WORD.findall(str(text or ""))}


def _stem(word):
    """Just enough to let "watches" find WATCH and "emails" find EMAIL."""
    lowered = word.lower()
    for suffix in ("ies", "es", "s"):
        if len(lowered) > 3 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _matches(token, query_tokens, query_stems):
    if token in query_tokens:
        return True
    stem = _stem(token)
    if stem in query_stems or stem in query_tokens:
        return True
    # A query word whose stem is this token: "watches" -> "watch".
    return token in query_stems


def _score(entry, query, query_tokens, query_stems):
    name = entry["name"]
    # Named outright? Then it is not a ranking question.
    if name.lower() in query:
        return 1000.0

    score = 0.0
    tokens = entry["tokens"]
    matched = [token for token in tokens
               if _matches(token, query_tokens, query_stems)]
    if matched:
        # Every part of a compound name matching is a much stronger signal than
        # one part matching, so reward completeness rather than raw count --
        # and weight each word by how few capabilities share it, so a generic
        # subject noun cannot outvote a precise operation word.
        weight = sum(_name_token_weight.get(token, 1.0) for token in matched)
        score += 10.0 * weight + 6.0 * (len(matched) / max(1, len(tokens)))

    # Human vocabulary explicitly attached to the capability is stronger than
    # incidental prose overlap but weaker than naming the action itself. A
    # multi-word intent (folders + files + drive) should beat a tool sharing
    # only one generic noun (FIND_FILES).
    alias_tokens = entry.get("alias_tokens", [])
    alias_hits = sum(1 for token in alias_tokens
                     if _matches(token, query_tokens, query_stems))
    if alias_hits:
        # Aliases are deliberately curated human intent vocabulary.  One
        # matched operation such as "reschedule" or "remind" is stronger than
        # a subject noun that happens to occur in an unrelated action name
        # (an event titled "project review" must not route to project-file
        # tools). Full action names still win outright at 1000 above.
        #
        # Coverage, not count. A flat 18 per matched word rewarded whoever had
        # written the longest alias string, and let one incidental word decide
        # a ranking: "start" put BROWSER_START above ADD_TASK for "remind me
        # at 4:30 to start my new project", although ADD_TASK had the highest
        # semantic similarity of all 243 capabilities for that sentence. The
        # first match carries most of the weight and further ones taper.
        score += _ALIAS_FIRST_HIT + _ALIAS_EXTRA_HIT * min(
            alias_hits - 1, _ALIAS_HIT_CEILING)
        score += 8.0 * (alias_hits / max(1, len(alias_tokens)))

    # Rare words shared with the description. Weighted well below the name,
    # because prose repeats common words across unrelated actions.
    overlap = (entry["haystack"] & query_tokens) - _STOPWORDS
    score += 1.5 * len(overlap)
    # An exact rare token -- a number, an identifier -- is a deliberate match.
    for word in overlap:
        if word.isdigit() or len(word) >= 7:
            score += 4.0
    return score


# ------------------------------------------------------------------ #
# Retrieval
# ------------------------------------------------------------------ #

def _blocked_for(origin):
    try:
        import autonomy
        return autonomy.blocked_actions(str(origin or ""))
    except Exception:
        return frozenset()


def search(base_dir, query, allowed_catalogs=None, limit=12, origin="user",
           include_primitives=True):
    """The actions worth putting in front of the model for this request.

    DISCOVERY is appended rather than ranked: the model must always have a way
    to reach a capability the scoring missed, and that guarantee is what makes
    imperfect ranking acceptable rather than dangerous.
    """
    with _lock:
        entries = list(_entries)
    allowed = set(allowed_catalogs) if allowed_catalogs else None
    blocked = _blocked_for(origin)
    lowered = " ".join(str(query or "").lower().split())
    query_tokens = _tokenise(lowered) - _STOPWORDS
    query_stems = {_stem(word) for word in query_tokens}

    semantic_query = None
    with _lock:
        semantic_ready = (_semantic_signature == _configured_signature
                          and bool(_semantic_vectors))
        semantic_vectors = dict(_semantic_vectors) if semantic_ready else {}
    if semantic_ready:
        try:
            from memory.embeddings import embed_text
            semantic_query = embed_text(lowered)
        except Exception as exc:
            print(f"[Actions] Semantic query degraded to lexical ranking: {exc}")

    # Two passes, because semantic relevance is now judged relative to this
    # query's own pool: the first pass measures, the second ranks. One pass
    # cannot do it, since the baseline is not known until every similarity has
    # been computed.
    candidates, discovery = [], []
    for entry in entries:
        if allowed is not None and entry["catalog"] not in allowed:
            continue
        if entry["name"] in blocked:
            continue
        if entry["catalog"] == DISCOVERY_CATALOG:
            discovery.append(entry)
            continue
        lexical = _score(entry, lowered, query_tokens, query_stems)
        similarity = 0.0
        vector = semantic_vectors.get((entry["catalog"], entry["name"]))
        if semantic_query is not None and vector is not None:
            similarity = max(0.0, _cosine(semantic_query, vector))
        candidates.append((entry, lexical, similarity))

    baseline = _semantic_baseline([sim for _e, _l, sim in candidates])

    scored = []
    for entry, lexical, similarity in candidates:
        # Lexical evidence stays authoritative for explicit names and exact
        # vocabulary. Semantic relevance can rescue genuine paraphrases but a
        # weak generic similarity cannot inject an unrelated capability --
        # which is what the margin does: similarity at or below what most
        # candidates score is worth nothing at all.
        margin = max(0.0, similarity - baseline)
        value = lexical + margin * _SEMANTIC_MARGIN_WEIGHT
        if lexical > 0 or margin > 0:
            entry = dict(entry)
            entry["lexical_score"] = lexical
            entry["semantic_score"] = round(similarity, 4)
            entry["semantic_margin"] = round(margin, 4)
            entry["score"] = round(value, 4)
            scored.append((value, entry))

    scored.sort(key=lambda pair: (-pair[0], pair[1].get("position", 0)))
    wanted = max(0, int(limit or 12))
    ranked = [entry for _value, entry in scored]
    by_name = {entry["name"]: entry for entry in entries
               if entry["name"] not in blocked
               and (allowed is None or entry["catalog"] in allowed)}
    chosen = []
    # Expand only strong leading intents. A low-ranked incidental browser word
    # must not turn an email task into a browser workflow.
    for seed in ranked[:3]:
        bundle = _ACTION_BUNDLES.get(seed["name"])
        if not bundle:
            continue
        for name in bundle:
            companion = by_name.get(name)
            if companion and all(item["name"] != name for item in chosen):
                enriched = dict(companion)
                enriched["selection_reason"] = f"workflow companion of {seed['name']}"
                chosen.append(enriched)
        break
    for entry in ranked:
        if len(chosen) >= wanted:
            break
        if all(item["name"] != entry["name"] for item in chosen):
            chosen.append(entry)
    # Append general primitives rather than ranking them first, so a dedicated
    # calendar/browser/file action still wins whenever one exists.
    for name in (_ALWAYS_VISIBLE if include_primitives else ()):
        primitive = by_name.get(name)
        if (primitive and wanted > 0
                and all(item["name"] != name for item in chosen)):
            enriched = dict(primitive)
            enriched["selection_reason"] = "always-visible execution primitive"
            if len(chosen) >= wanted:
                chosen.pop()
            chosen.append(enriched)
    chosen = chosen[:wanted]
    return chosen + discovery


def search_outcomes(base_dir, outcomes, allowed_catalogs=None, limit=12,
                    origin="user"):
    """Retrieve a bounded, unique tool set across independent outcomes.

    A single blended query is lossy: in "move my review and remind me to start
    the project", the richer project vocabulary can occupy every slot and hide
    the calendar mutation needed by the first outcome.  Search each outcome
    independently and merge round-robin, so every planned result gets a fair
    chance without growing the prompt as the registry grows.
    """
    cleaned = []
    for value in outcomes or []:
        text = " ".join(str(value or "").split())
        if text and text.lower() not in {item.lower() for item in cleaned}:
            cleaned.append(text)
        if len(cleaned) >= 8:
            break
    if not cleaned:
        cleaned = [""]

    wanted = max(1, int(limit or 12))
    per_outcome = max(3, min(6, wanted))
    ranked_sets = []
    discovery = []
    for outcome in cleaned:
        found = search(base_dir, outcome, allowed_catalogs=allowed_catalogs,
                       limit=per_outcome, origin=origin,
                       include_primitives=False)
        ranked_sets.append([
            item for item in found if item["catalog"] != DISCOVERY_CATALOG])
        if not discovery:
            discovery = [item for item in found
                         if item["catalog"] == DISCOVERY_CATALOG]

    chosen, names = [], set()
    depth = 0
    while len(chosen) < wanted:
        added = False
        for ranked in ranked_sets:
            if depth >= len(ranked):
                continue
            entry = ranked[depth]
            if entry["name"] not in names:
                chosen.append(entry)
                names.add(entry["name"])
                added = True
                if len(chosen) >= wanted:
                    break
        if not added and all(depth + 1 >= len(items) for items in ranked_sets):
            break
        depth += 1
    # Add the general shell only after every independent outcome has had a
    # chance to contribute its dedicated tool. Doing this per outcome lets the
    # primitive crowd out a low-ranked but necessary calendar/message action.
    by_name = {entry["name"]: entry for entry in entries_named(
        _ALWAYS_VISIBLE, origin=origin)}
    for name in _ALWAYS_VISIBLE:
        primitive = by_name.get(name)
        if (primitive and all(item["name"] != name for item in chosen)):
            if len(chosen) >= wanted:
                chosen.pop()
            enriched = dict(primitive)
            enriched["selection_reason"] = "always-visible execution primitive"
            chosen.append(enriched)
    return chosen + discovery


def augment_with_resource_queries(entries, base_dir, queries, *,
                                  origin="user", limit=18):
    """Add a bounded capability lane for model-selected resource surfaces.

    Resource requests are not extra outcomes and therefore do not participate
    in outcome verification.  They simply prevent a correct semantic choice
    such as ``browser`` or ``email`` from becoming dead data when an operation
    intent is terse.  The action registry still ranks concrete capabilities,
    dispatch policy still authorizes them, and discovery remains last.
    """
    current = list(entries or ())
    discovery = [item for item in current
                 if item.get("catalog") == DISCOVERY_CATALOG]
    core = [item for item in current
            if item.get("catalog") != DISCOVERY_CATALOG]
    seen = {item.get("name") for item in core}
    ranked_sets = []
    for query in queries or ():
        found = search(
            base_dir, query, allowed_catalogs=None, limit=4, origin=origin,
            include_primitives=False)
        ranked_sets.append([
            item for item in found
            if item.get("catalog") != DISCOVERY_CATALOG])
    wanted = max(len(core), int(limit or 18))
    depth = 0
    while len(core) < wanted and ranked_sets:
        added = False
        for ranked in ranked_sets:
            if depth >= len(ranked):
                continue
            item = ranked[depth]
            name = item.get("name")
            if name and name not in seen:
                enriched = dict(item)
                enriched["selection_reason"] = "model-selected resource surface"
                core.append(enriched)
                seen.add(name)
                added = True
                if len(core) >= wanted:
                    break
        if not added and all(depth + 1 >= len(items) for items in ranked_sets):
            break
        depth += 1
    return core[:wanted] + discovery[:1]


def entries_named(names, origin="user"):
    """Registered prompt entries for canonical action names.

    Outcome selection is stronger evidence than similarity ranking.  This
    lookup lets the caller guarantee that an exact selected action's callable
    syntax is visible even when retrieval did not rank it into the shortlist.
    """
    wanted = [str(name or "").upper() for name in names or () if name]
    blocked = _blocked_for(origin)
    with _lock:
        by_name = {entry["name"].upper(): entry for entry in _entries
                   if entry["name"].upper() not in blocked}
    return [dict(by_name[name]) for name in wanted if name in by_name]


def ensure_named(entries, names, origin="user"):
    """Prepend missing selected actions without duplicating registry rows."""
    current = list(entries or ())
    required = entries_named(names, origin)
    seen = set()
    merged = []
    for entry in required + current:
        name = str(entry.get("name", "")).upper()
        if not name or name in seen:
            continue
        seen.add(name)
        merged.append(entry)
    return merged


def is_capability_denial(text):
    """Whether a response is claiming the required tool is unavailable."""
    return bool(_CAPABILITY_DENIAL.search(str(text or "")))


def discover_outcomes_text(base_dir, outcomes, limit=12, origin="user"):
    """Exact callable syntax for automatic recovery from a false denial."""
    found = search_outcomes(base_dir, outcomes, allowed_catalogs=None,
                            limit=limit, origin=origin)
    found = [item for item in found
             if item["catalog"] != DISCOVERY_CATALOG]
    if not found:
        return "[Success] No matching registered capability was found."
    lines = ["[Success] Runtime capability recovery found:"]
    for entry in found:
        syntax = " ".join((entry.get("text") or "").split())
        summary = " ".join((entry.get("notes") or "").split())[:180]
        lines.append(f"  {entry['name']} ({entry['catalog']})\n"
                     f"    CALL: {syntax}"
                     + (f"\n    USE: {summary}" if summary else ""))
    return "\n".join(lines)


def render(entries):
    """The selected actions as prompt text."""
    if not entries:
        return ""
    lines = ["AVAILABLE ACTIONS (the ones relevant to this request; use "
             "DISCOVER_ACTIONS if you need something not listed):"]
    for entry in entries:
        lines.append("  " + entry["text"])
        from action_schema import input_contract
        contract = input_contract(entry['name'])
        if contract:
            lines.append('    INPUT: ' + contract)
        notes = entry.get("notes") or ""
        if notes:
            for line in notes.splitlines():
                stripped = line.strip()
                if stripped:
                    lines.append("    " + stripped)
    return "\n".join(lines)


def discover_text(base_dir, query, limit=12, origin="user"):
    """DISCOVER_ACTIONS: search the whole surface, not just what was offered."""
    try:
        wanted = max(1, min(int(limit or 12), 40))
    except (TypeError, ValueError):
        wanted = 12
    if not str(query or "").strip():
        return ("[Error] DISCOVER_ACTIONS needs query=\"what you are trying to "
                "do\".")
    found = search(base_dir, query, allowed_catalogs=None, limit=wanted,
                   origin=origin)
    found = [entry for entry in found
             if entry["catalog"] != DISCOVERY_CATALOG]
    if not found:
        return (f"[Success] Nothing in the registry matches {query!r}. "
                f"Either it is not a capability VANTA has, or it needs to be "
                f"written with CREATE_ACTION.")
    lines = [f"[Success] {len(found)} capability(ies) matching {query!r}:"]
    for entry in found:
        # A name is not callable. Return the exact registered syntax so the
        # next model step can execute it instead of searching again or
        # inventing attributes from memory.
        syntax = (entry.get("text") or "").strip()
        summary = " ".join((entry.get("notes") or "").split())[:180]
        from action_schema import input_contract
        contract = input_contract(entry['name'])
        lines.append(f"  {entry['name']} ({entry['catalog']})\n"
                     f"    CALL: {syntax}"
                     + (f"\n    INPUT: {contract}" if contract else '')
                     + (f"\n    USE: {summary}" if summary else ""))
    return "\n".join(lines)


def discovery_signature(base_dir, query, limit=12, origin="user"):
    """Functional identity of a discovery request.

    Two differently worded searches are duplicates when they return the same
    callable capability set. Comparing filler-stripped English cannot prove
    that and both misses paraphrases and merges unrelated requests.
    """
    try:
        wanted = max(1, min(int(limit or 12), 40))
    except (TypeError, ValueError):
        wanted = 12
    found = search(base_dir, query, allowed_catalogs=None, limit=wanted,
                   origin=origin)
    return tuple(sorted(
        str(entry.get("name", "")).upper()
        for entry in found
        if entry.get("catalog") != DISCOVERY_CATALOG and entry.get("name")
    ))


def stats():
    with _lock:
        catalogs = {}
        for entry in _entries:
            catalogs[entry["catalog"]] = catalogs.get(entry["catalog"], 0) + 1
        return {"actions": len(_entries), "catalogs": catalogs}
