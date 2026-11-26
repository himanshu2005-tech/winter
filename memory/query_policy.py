"""Deterministic retrieval routing. Current context must beat old memory."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalPolicy:
    include_facts: bool = True
    include_semantic: bool = True
    include_conversations: bool = False
    fact_limit: int = 10
    semantic_limit: int = 3
    reason: str = "general"


def policy_for(query):
    text=" ".join(str(query or "").lower().split())
    if re.search(r"\b(on (?:my|the) screen|what(?:'s| is) open|active window|right now|currently visible)\b",text):
        return RetrievalPolicy(False,False,False,0,0,"live_state_only")
    if re.fullmatch(r"(?:yes[, ]*)?(?:do|start|continue|send|make|open|save|use|try)\s*(?:it|that|this)?[.!]?",text):
        return RetrievalPolicy(True,False,False,8,0,"immediate_reference")
    if re.search(r"\b(earlier|previously|last time|we discussed|our conversation|conversation history)\b",text):
        return RetrievalPolicy(True,True,True,12,4,"explicit_history")
    if re.search(r"\b(about me|my preferences?|what do you know about me|remember about me|my style|my goals?)\b",text):
        return RetrievalPolicy(True,True,True,20,5,"personal_memory")
    return RetrievalPolicy(True,True,False,10,3,"general")


def requires_memory_recall(query):
    """Whether the user is explicitly asking VANTA to use remembered context.

    This is a retrieval reliability contract, not an ontology or a fact rule:
    the model still decides what the words mean and which returned evidence is
    relevant.  It merely prevents a router omission from making durable memory
    invisible on an explicit recall request.
    """
    text = " ".join(str(query or "").casefold().split())
    return bool(re.search(
        r"\b(?:remember|remembered|told you|gave you|shared with you|"
        r"saved|stored|from (?:before|earlier|last time)|previously|"
        r"what do you know about|what did i (?:say|tell)|"
        r"do you (?:know|have|remember) my|"
        r"what(?:'s| is| are) my|who is my|where is my)\b",
        text,
    ))


def requires_personal_grounding(query):
    """Whether a request depends on a user-owned resource or personal fact.

    This is deliberately about grammatical ownership rather than particular
    products or portals.  A semantic router may decide that an old memory is
    unnecessary, but it may not make the address of "my portal", "my flight",
    or "my project" invisible and force the executor to search the public web.
    Live visual questions remain governed by the current-screen policy.
    """
    text = " ".join(str(query or "").casefold().split())
    if policy_for(text).reason == "live_state_only":
        return False
    return bool(re.search(
        r"\b(?:my|our)\s+(?!screen\b|active window\b|mouse\b|keyboard\b)"
        r"[a-z0-9][a-z0-9_' -]{1,80}",
        text,
    ))
