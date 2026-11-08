import queue
import threading
import json
import re
import datetime
import time
import random

from llm_router import call_llm
from memory.graph_store import add_edge, get_current_edges
from memory.entities import get_canonical_name
from memory.neo4j_store import (
    _slug, canonical_predicate, is_unresolved_reference, store,
)

# Background queue for triples extraction
_consolidation_queue = queue.Queue()
_recovery_lock = threading.Lock()
_recovery_done = False

# Preferred vocabulary for common personal predicates.  This is not a closed
# enum: the extractor may introduce a precise uppercase predicate for a new
# kind of explicit fact rather than collapsing meaning into RELATED_TO.
PERSONAL_PREDICATES = {
    "LIVES_IN", "WORKS_AT", "STUDIES_AT", "LIKES", "DISLIKES", 
    "KNOWS", "HAS_GOAL", "IS_WORKING_ON", "HAS_PREFERENCE",
    "RELATED_TO", "OWNS", "EDUCATION_LEVEL", "HAS_CONTACT", "HAS_PHONE",
    "HAS_FATHER", "HAS_MOTHER", "HAS_SISTER", "HAS_BROTHER", "HAS_ALIAS",
    "HAS_PARTNER", "SPEAKS_LANGUAGE", "HAS_PORTAL_LINK", "USES_PORTAL",
    "HAS_COMMITMENT", "HAS_ROUTINE", "HAS_PROCEDURE"
}

# Broader vocabulary for deep research
RESEARCH_PREDICATES = {
    "IS_A", "REQUIRES", "PRODUCES", "USES", "PART_OF", "HAS_PROPERTY",
    "RELATED_TO", "IMPLEMENTS", "OPTIMIZES", "SUPPORTS", "DEPENDS_ON"
}

def _parse_llm_json(text):
    """Read a complete fact array; never extract private reasoning or salvage truncation."""
    text = re.sub(r'<think\b[^>]*>.*?(?:</think\s*>|$)', '', str(text or ''), flags=re.I | re.S)
    def reject_constant(value):
        raise ValueError('Non-finite JSON')
    decoder = json.JSONDecoder(parse_constant=reject_constant)
    candidates, offset = [], 0
    while offset < len(text):
        opening = re.search(r'[\[{]', text[offset:])
        if not opening:
            break
        start = offset + opening.start()
        # Balance complete candidates first. A truncated outer array must not
        # be "repaired" by selecting a valid nested list from its middle.
        stack, quoted, escaped, end = [], False, False, None
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char in '[{':
                stack.append(char)
            elif char in ']}':
                if not stack or (stack.pop(), char) not in {('[', ']'), ('{', '}')}:
                    return None
                if not stack:
                    end = index+1
                    break
        if end is None:
            return None
        try:
            parsed = decoder.decode(text[start:end])
        except (ValueError, TypeError):
            offset = end
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get('facts', parsed.get('triples'))
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            candidates.append(parsed)
        offset = end
    # Multiple answers are ambiguous; keep the durable evidence pending.
    return candidates[0] if len(candidates) == 1 else None


class ExtractionPacer:
    """One worker-wide cooldown, so a backlog cannot amplify a provider failure."""
    def __init__(self, clock=time.monotonic, jitter=random.random):
        self.clock, self.jitter = clock, jitter
        self.failures, self.next_at, self.last_log = 0, 0., -1e9

    def wait(self):
        remaining = self.next_at-self.clock()
        while remaining > 0:
            threading.Event().wait(min(remaining, 30.))
            remaining = self.next_at-self.clock()
        # Memory remains durable while a foreground turn/recording owns the
        # assistant. Never import agent here: that can start a second runtime.
        import idle_gate
        while idle_gate.agent_busy():
            threading.Event().wait(.25)

    def failed(self):
        self.failures += 1
        delay = min(60., 2. ** min(self.failures, 6)) * (.8 + .2*self.jitter())
        self.next_at = self.clock()+delay
        log = self.clock()-self.last_log >= 30
        if log:
            self.last_log = self.clock()
        return delay, log

    def succeeded(self):
        self.failures, self.next_at = 0, 0.


_extraction_pacer = ExtractionPacer()


_LANGUAGE_STATEMENT = re.compile(
    r"\b(?:i\s+(?:can\s+)?speak|my\s+(?:native|primary|first)\s+language\s+is)\s+"
    r"([A-Za-z][A-Za-z -]{1,40}?)(?=\s+(?:so|and|but|because)\b|[,.!?]|$)",
    re.IGNORECASE,
)


def normalize_personal_triple(user_text, subject, predicate, object_value):
    """Keep explicit language claims inside the correct ontology slot.

    This recognizes the shape of a statement, not a hard-coded list of
    languages. It prevents a constrained LLM from turning a language into a
    contact, preference, or generic relationship.
    """
    match = _LANGUAGE_STATEMENT.search(str(user_text or ""))
    if match:
        return subject, "SPEAKS_LANGUAGE", match.group(1).strip()
    return subject, predicate, object_value

def _recover_pending_turns():
    """Resume fact extraction that was interrupted by a restart."""
    recovered = set()
    try:
        for row in store.pending_fact_extractions(limit=200):
            source_id = str(row.get("source_id") or "")
            _consolidation_queue.put((
                str(row.get("content") or ""),
                source_id, "personal",
                int(row.get("attempts") or 0),
            ))
            recovered.add(source_id)
    except Exception as exc:
        # Neo4j health recovery will restore new writes.  A later user turn
        # imports this module again only once, so the ordinary enqueue path is
        # still the primary path; this diagnostic is intentionally one line.
        print(f"[Consolidator] Pending-turn recovery deferred: {exc}")
    return recovered


def recover_pending():
    """Queue interrupted dialogue extractions once per process.

    This is called during normal application startup, not merely after the
    next thing Himanshu says.  An assistant should not require a fresh command
    before it remembers an earlier one.
    """
    global _recovery_done
    if _recovery_done:
        return set()
    with _recovery_lock:
        if _recovery_done:
            return set()
        recovered = _recover_pending_turns()
        _recovery_done = True
        return recovered


def _consolidation_worker():
    while True:
        source_id = ""
        attempt = 0
        try:
            task = _consolidation_queue.get()
            if task is None:
                break
            _extraction_pacer.wait()
                
            if len(task) >= 4:
                user_text, source_id, mode, attempt = task[:4]
            elif len(task) == 3:
                user_text, source_id, mode = task
            else:
                user_text, source_id = task
                mode = "personal"

            # Claim the durable DialogueTurn when this task came from a live
            # user turn.  External/research sources do not have one and remain
            # supported for backwards compatibility.
            claimed = store.claim_fact_extraction(source_id) if mode != "research" else None
            if claimed:
                user_text = str(claimed.get("content") or user_text)
                attempt = int(claimed.get("attempts") or attempt)
                
            if mode == "research":
                prompt = (
                    "You are an exact knowledge graph extraction engine for domain research.\n"
                    "Extract factual, high-confidence relationships from the following text.\n"
                    "Format as a JSON array of objects with 'subject', 'predicate', 'object', and 'confidence' (0.0 to 1.0).\n"
                    f"Allowed predicates: {', '.join(RESEARCH_PREDICATES)}\n"
                    "Example output: [{\"subject\": \"Machine Learning\", \"predicate\": \"REQUIRES\", \"object\": \"Data\", \"confidence\": 1.0}]\n"
                    "Return ONLY valid JSON. If no facts are present, return []."
                )
                allowed_set = RESEARCH_PREDICATES
            else:
                now = datetime.datetime.now().astimezone()
                prompt = (
                    "You are an exact fact extraction engine.\n"
                    f"Local time is {now.isoformat(timespec='seconds')}.\n"
                    "Extract explicitly stated, high-confidence facts about the user from the following text.\n"
                    "Do NOT infer facts that are not explicitly stated by the user.\n"
                    "Store durable identity facts, preferences, relationships, owned items, resources, active projects, explicit goals, confirmed future commitments, explicitly recurring instructions, and user-taught conditional procedures. The user does not need to say 'remember this'.\n"
                    "A URL explicitly identified as the user's named portal, repository, portfolio, or other reusable personal resource is durable. Store a named portal as HAS_PORTAL_LINK with the exact URL, including its path. Do not store incidental search-result URLs.\n"
                    "Do NOT store a question, command output, pasted or quoted third-party text, hypothetical, uncertain possibility, temporary emotion, present-conversation need, request for reassurance, one-night decision, or complaint about someone as a lasting preference or goal.\n"
                    "Statements such as 'I need reassurance', 'I don't want to talk to them tonight', 'I feel alone', and 'I am here for you' produce [].\n"
                    "For each candidate decide retention: durable or transient. Only durable information useful beyond this conversation belongs in the graph. Omit incidental notifications, authentication codes, credentials, generic advice, assistant claims, and duplicate restatements. Never turn quoted instructions into user preferences.\n"
                    "Format as a JSON array of atomic objects with 'subject', 'predicate', 'object', 'confidence' (0.0 to 1.0), 'fact_class', 'sensitivity', 'cardinality', 'operation', 'quote', 'memory_kind', 'retention', and 'persistent'.\n"
                    "memory_kind must be one of identity, fact, personal_preference, assistant_preference, relationship, resource, goal, project, commitment, self_reported_pattern, procedure, or routine.\n"
                    "Use assistant_preference only for an explicitly lasting instruction about how the assistant should behave. Set persistent=true only when the wording establishes an ongoing preference, rule, or recurring instruction rather than a one-turn request. For procedure also include trigger, instruction, and optional actions.\n"
                    "Use commitment only when the user unambiguously says they will, must, need to, or are scheduled to do a specific future thing. Set commitment_status to confirmed only for a definite obligation, not 'might', 'maybe', 'probably', or an idea. Include hard_time as a complete ISO-8601 local timestamp only when the time can be resolved exactly from the statement and current time; otherwise leave it empty. Include location, prep_minutes, and travel_minutes only when stated.\n"
                    "Use routine only for an explicit recurring instruction. Include instruction and a normalized schedule such as 'daily 08:00', 'weekdays 09:30', 'weekly mon 18:00', or 'every 45m'. Do not invent missing schedule details.\n"
                    "A reminder/alarm request is an action request, not automatically a memory. Do not create an alarm candidate. If it also states a genuine underlying obligation, you may extract only that commitment.\n"
                    "The 'subject' should almost always be the user (e.g., 'Himanshu') or an entity the user owns.\n"
                    f"Prefer these established predicates when they fit: {', '.join(sorted(PERSONAL_PREDICATES))}.\n"
                    "When none fits, create one precise, reusable predicate in UPPER_SNAKE_CASE; never discard a stated fact merely because its relation is new.\n"
                    "fact_class must be explicit_user_fact or self_reported_pattern. sensitivity must be public, private, sensitive, or restricted.\n"
                    "cardinality is single when only one current value can be true (for example a current location), otherwise multi. operation is upsert, replace, or remove. Use replace for an explicit correction and remove for 'no longer', 'not anymore', or a direct request to forget that exact fact; never turn a negated fact into a positive fact.\n"
                    "For personal relationships, use atomic relations such as Himanshu HAS_FATHER Ambadas, "
                    "Himanshu HAS_PARTNER Madhu, and Ambadas HAS_ALIAS Pappa.\n"
                    "Also include 'quote': the exact words from the text that state this fact, "
                    "copied verbatim and never paraphrased. It is what lets Himanshu check the claim later.\n"
                    "Example output: [{\"subject\": \"Himanshu\", \"predicate\": \"LIKES\", \"object\": \"Coffee\", \"confidence\": 1.0, \"quote\": \"i love coffee\"}]\n"
                    "Return ONLY valid JSON. If no explicit facts are present, return []."
                )
                allowed_set = None
            
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text}
            ]
            
            # Runs on the consolidator thread while a turn may be streaming.
            response_text = call_llm(messages, task_level="memory_extraction", quiet=True)
            
            triples = _parse_llm_json(response_text)
            if triples is None:
                raise ValueError("fact extractor returned invalid JSON")
            
            unverified = []

            for t in triples:
                if not isinstance(t, dict): continue

                if mode != "research":
                    from memory.retention import normalize_candidate
                    t = normalize_candidate(t, user_text)
                    if t is None:
                        continue
                    from memory.fact_validation import grounded_candidate
                    if not grounded_candidate(t, user_text):
                        continue
                
                subj = t.get("subject", "").strip()
                pred = t.get("predicate", "").strip().upper()
                obj = str(t.get("object", "")).strip()
                conf = float(t.get("confidence", 1.0))

                if mode != "research":
                    subj, pred, obj = normalize_personal_triple(
                        user_text, subj, pred, obj)

                # Validate against the canonical vocabulary, not the model's
                # surface synonym. Previously PORTAL_URL was rejected before
                # add_edge had a chance to canonicalize it.
                pred = canonical_predicate(pred)
                
                if not subj or not obj or not pred:
                    continue
                if is_unresolved_reference(subj) or is_unresolved_reference(obj):
                    print("[Consolidator] Rejected unresolved reference")
                    continue
                    
                if allowed_set is not None and pred not in allowed_set:
                    pred = "RELATED_TO"
                elif not re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", pred):
                    pred = "RELATED_TO"
                    
                # Add to the graph (supersession logic handled automatically).
                # Only keep a quote the text actually contains; a paraphrase
                # presented as a quotation is worse than no evidence at all.
                quote = str(t.get("quote", "") or "").strip()
                if quote and ' '.join(quote.split()).casefold() not in ' '.join(str(user_text or "").split()).casefold():
                    quote = ""
                if mode == "research":
                    # Curiosity, retrieval and deduction use one evidence
                    # class. Earlier research writes used ``external_fact``;
                    # readers retain that legacy alias, while every new write
                    # uses the canonical name.
                    fact_class, sensitivity = "external_observation", "public"
                    operation, single_valued = "upsert", False
                    evidence_type, authority = "researched_source", 50
                else:
                    fact_class = str(
                        t.get("fact_class") or "explicit_user_fact").lower()
                    if fact_class not in {
                            "explicit_user_fact", "self_reported_pattern"}:
                        fact_class = "explicit_user_fact"
                    sensitivity = str(t.get("sensitivity") or "private").lower()
                    if sensitivity not in {
                            "public", "private", "sensitive", "restricted"}:
                        sensitivity = "private"
                    operation = str(t.get("operation") or "upsert").lower()
                    single_valued = (
                        str(t.get("cardinality") or "").lower() == "single")
                    evidence_type = "user_stated"
                    authority = 100 if fact_class == "explicit_user_fact" else 90
                if operation == "remove":
                    removed = store.invalidate_fact(
                        subj, pred, obj,
                        reason=f"explicit user correction ({source_id})")
                    result = f"Retired {len(removed)} matching fact(s)"
                    verified = not any(
                        (_slug(edge_s), canonical_predicate(edge_p), _slug(edge_o))
                        == (_slug(subj), canonical_predicate(pred), _slug(obj))
                        for edge_s, edge_p, edge_o in get_current_edges(subj))
                    if not verified:
                        unverified.append(f"remove {subj} -{pred}-> {obj}")
                    continue
                result = add_edge(
                    subj, pred, obj, confidence=conf, source_id=source_id,
                    evidence_quote=quote or user_text[:400],
                    evidence_ref=source_id, fact_class=fact_class,
                    evidence_type=evidence_type, authority=authority,
                    sensitivity=sensitivity, review_status="accepted",
                    single_valued=single_valued or operation == "replace",
                    correction_reason=("explicit user correction"
                                       if operation == "replace" else None))
                # Compare the way the store keys things, not the way the model
                # typed them. An entity keeps the name it was first stored
                # under, so the second mention of "Transformer models" reads
                # back as "transformer models" and a case-sensitive compare
                # called a perfectly good write a failure. Predicates go
                # through aliasing on the way in, so they are canonicalised
                # here too.
                want = (_slug(subj), canonical_predicate(pred), _slug(obj))
                verified = any(
                    (_slug(edge_s), canonical_predicate(edge_p), _slug(edge_o)) == want
                    for edge_s, edge_p, edge_o in get_current_edges(subj))
                if not verified:
                    # One unverifiable triple is not a reason to throw away the
                    # rest of the batch. This used to raise, which abandoned
                    # every remaining fact in the extraction and skipped
                    # task_done() with it.
                    unverified.append(f"{subj} -{pred}-> {obj}")
                    continue
                print(f"[Consolidator] Verified fact: class={fact_class}; sensitivity={sensitivity}")

                if mode != "research":
                    from memory.retention import apply_operational_memory
                    retained = apply_operational_memory(t, source_id)
                    if retained:
                        print(f"[Consolidator] Operational memory: {retained}")

            if unverified:
                raise ValueError(f'{len(unverified)} fact write(s) could not be verified')

            if mode != "research" and source_id:
                store.finish_fact_extraction(source_id, status="complete")
            _extraction_pacer.succeeded()

        except Exception as e:
            delay, log_failure = _extraction_pacer.failed()
            if log_failure:
                print(f"[Consolidator] Background extraction failed: {type(e).__name__}: {e}. "
                      f"Cooling down for {delay:.0f}s; pending memory remains durable.")
            if source_id:
                try:
                    store.finish_fact_extraction(
                        source_id, status="retry", error=str(e))
                except Exception:
                    pass
            # Transient model/graph failures must not silently lose a stated
            # fact. Retry a few times in-process; after that the durable turn
            # remains marked retry and is recovered on the next start.
            if source_id and attempt < 3:
                retry_task = (user_text, source_id, mode, attempt + 1)
                timer = threading.Timer(
                    min(30.0, 2.0 ** max(0, attempt)),
                    lambda task=retry_task: _consolidation_queue.put(task))
                timer.daemon = True
                timer.start()
        finally:
            # Always, or a queue.join() waits on a task that already ended.
            _consolidation_queue.task_done()

# Start the background worker thread
threading.Thread(target=_consolidation_worker, daemon=True, name="vanta-consolidator").start()

def should_consolidate_personal(user_text):
    """Whether a user utterance is a candidate for durable fact extraction."""
    text = " ".join(str(user_text or "").split())
    if not text:
        return False
    # A question can contain a true-looking clause without asserting it. Let
    # historical retrieval answer provenance questions; never manufacture a
    # new autobiographical fact from the wording of the question itself.
    if re.match(
        r"^(?:how|why|what|when|where|who|which|do|does|did|can|could|would|"
        r"should|is|are|was|were|have|has|remember)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    # Present-tense distress and one-off wishes belong to the dialogue episode,
    # not to Himanshu's durable identity.  The extractor has the same rule, but
    # this cheap gate also prevents a model mistake from ever reaching a write.
    transient_state = re.search(
        r"\b(?:right now|at the moment|for now|tonight|just today)\b|"
        r"\bi (?:am|feel|felt|was) (?:heartbroken|hurt|upset|angry|sad|lonely|"
        r"overwhelmed|anxious|scared|tired|lost|confused|stressed)\b",
        text,
        re.IGNORECASE,
    )
    if transient_state:
        return False
    durable_markers = re.compile(
        r"\b(i (?:am|live|work|study|like|love|dislike|prefer|own|know|want)|"
        r"my (?:goal|preference|project|job|college|email|phone|contact|birthday|father|mother|sister|brother|family|partner|girlfriend|boyfriend|gf|bf)|"
        r"(?:[A-Za-z][A-Za-z .'-]{0,60}) is my (?:partner|girlfriend|boyfriend|gf|bf)|"
        r"i (?:call|speak)|"
        r"remember (?:that|this)|from now on)\b", re.IGNORECASE
    )
    return bool(durable_markers.search(text))


def queue_for_consolidation(user_text, source_id="user_stated", mode="personal"):
    """Queue every substantive user turn for model-based fact extraction.

    ``should_consolidate_personal`` remains a cheap advisory signal for callers
    and tests, but it is deliberately not a loss boundary.  The extraction
    model sees the full turn and returns [] for commands, questions and
    transient conversation; unexpected wording can no longer make a real fact
    disappear before the model has evaluated it.
    """
    text = " ".join(str(user_text or "").split())
    if not text:
        return False
    recovered = recover_pending()
    if str(source_id) in recovered:
        return True
    _consolidation_queue.put((text, source_id, mode, 0))
    return True
