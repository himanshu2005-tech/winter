# -*- coding: utf-8 -*-
"""Semantic source inference for visual requests.

The model may phrase the same request in hundreds of ways, but the source is
not an arbitrary tool choice: physical-world evidence comes from the camera;
digital-interface evidence comes from the screen.  This module classifies the
evidence category rather than matching complete user sentences.

The default is deliberately CAMERA.  Himanshu uses an unqualified question
such as "what can you see?" to mean VANTA's physical view.  Screen access must
be grounded by current-turn digital evidence (screen, window, selected text,
cursor, browser tab, and so on); an old mention of Chrome must never poison a
new camera question.
"""

from dataclasses import dataclass
import re


CAMERA = "camera"
SCREEN = "screen"


@dataclass(frozen=True)
class PerceptionDecision:
    source: str
    visual_request: bool
    confidence: float
    evidence: tuple = ()


# These are concepts, not memorised commands.  Multi-word anchors represent
# relations whose individual words are too broad ("in front of me", "on this
# page").  The scored groups let one incidental word lose to several coherent
# pieces of evidence.
_CAMERA_ANCHORS = (
    (r"\b(?:camera|webcam|cam|lens)\b", 6, "camera device"),
    (r"\b(?:in\s+front\s+of\s+(?:me|you)|behind\s+me|beside\s+me)\b",
     5, "physical relation"),
    (r"\b(?:holding|wearing|my\s+hand|on\s+my\s+desk|in\s+the\s+room)\b",
     5, "physical scene"),
    (r"\b(?:face|person|people|room|surroundings|physical(?:ly)?|object)\b",
     3, "physical entity"),
)

_SCREEN_ANCHORS = (
    (r"\b(?:screen|monitor|desktop|display)\b", 6, "display device"),
    (r"\b(?:highlighted|selected|selection|"
     r"under\s+(?:(?:the|my)\s+)?cursor)\b",
     7, "digital selection"),
    (r"\b(?:browser|webpage|web\s+page|tab|window|dialog|popup|terminal|"
     r"editor|ide|taskbar|toolbar|address\s+bar|user\s+interface|ui)\b",
     5, "interface element"),
    (r"\b(?:screenshot|screen\s*shot|image\.(?:png|jpe?g|webp)|"
     r"diagram\s+on\s+(?:the\s+)?screen)\b", 6, "digital artifact"),
    (r"\b(?:button|menu|icon|checkbox|field)\b", 3, "interface control"),
    (r"\b(?:on\s+(?:my|the|this)\s+(?:screen|page|window|tab)|"
     r"shown\s+on\s+screen)\b", 7, "digital relation"),
)

_VISUAL_ACT = re.compile(
    r"\b(?:see|look|view|show|read|explain|identify|recognise|recognize|"
    r"describe|inspect|analyse|analyze|check|watch|visible|holding|wearing)\b",
    re.IGNORECASE,
)
_VISUAL_QUESTION = re.compile(
    r"\b(?:what|who)\b[^?.!]{0,35}\b(?:see|visible|showing|shown|"
    r"highlighted|selected|screen|camera|window|tab)\b"
    r"|\b(?:what|who)\s+(?:is|are|'s)\s+(?:this|that|there)\b"
    r"|\bcan\s+you\s+(?:see|look|identify|read|recognise|recognize)\b",
    re.IGNORECASE,
)
_TELL_VISUAL = re.compile(
    r"\btell\s+(?:me\s+)?(?:what|who)\b[^?.!]{0,40}"
    r"\b(?:see|visible|showing|shown|there)\b",
    re.IGNORECASE,
)


def _score(text, anchors):
    score = 0
    evidence = []
    for pattern, weight, label in anchors:
        if re.search(pattern, text, re.IGNORECASE):
            score += weight
            evidence.append(label)
    return score, evidence


def infer(text, history=None):
    """Return the visual source implied by the current utterance.

    ``history`` remains in the signature for callers, but source selection is
    intentionally current-turn scoped.  Background UI words from an earlier
    exchange caused "what can you see?" to capture Chrome instead of the
    webcam.  A user who means the screen supplies digital evidence now; all
    other visual observations default to the camera.
    """
    del history
    utterance = " ".join(str(text or "").split())
    camera_score, camera_evidence = _score(utterance, _CAMERA_ANCHORS)
    screen_score, screen_evidence = _score(utterance, _SCREEN_ANCHORS)

    has_source_evidence = bool(camera_score or screen_score)
    generic_visual = bool(
        _VISUAL_QUESTION.search(utterance)
        or _TELL_VISUAL.search(utterance)
        or re.search(r"\blook\s+at\s+this\b", utterance, re.IGNORECASE)
    )
    visual_request = bool(
        generic_visual
        or (has_source_evidence and (
            _VISUAL_ACT.search(utterance)
            or re.search(r"\b(?:what|who|which|where)\b",
                         utterance, re.IGNORECASE)))
    )

    if screen_score > camera_score:
        return PerceptionDecision(
            SCREEN, visual_request, min(1.0, 0.55 + screen_score / 20.0),
            tuple(screen_evidence))
    if camera_score > screen_score:
        return PerceptionDecision(
            CAMERA, visual_request, min(1.0, 0.55 + camera_score / 20.0),
            tuple(camera_evidence))

    # Equal explicit evidence is rare (for example, "use the screen light so
    # the camera can see me"). The physical observation is still the requested
    # result; the display is merely an aid. Source-free visual language has
    # the same documented default.
    evidence = tuple(camera_evidence + screen_evidence)
    return PerceptionDecision(CAMERA, visual_request, 0.65, evidence)


def is_visual_request(text):
    return infer(text).visual_request
