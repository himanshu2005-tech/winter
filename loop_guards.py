"""Deterministic circuit breakers for VANTA's agent loop.

These guards do not choose tools or decide intent. They only stop two forms of
non-progress that should never require model judgement: discovering the same
capability set repeatedly, and retrying an action after the same deterministic
failure has already occurred twice in one turn.
"""

from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True)
class DiscoveryDecision:
    execute: bool
    grant_extension: bool
    duplicate: bool = False


class DiscoveryGuard:
    """Deduplicate discovery by what it finds, not how the query was worded."""

    def __init__(self, max_extensions=2):
        self.max_extensions = max(0, int(max_extensions))
        self._seen = set()

    def check(self, signature):
        signature = tuple(signature or ())
        if signature in self._seen:
            return DiscoveryDecision(False, False, True)
        self._seen.add(signature)
        grant = len(self._seen) <= self.max_extensions
        return DiscoveryDecision(True, grant, False)


def _failure_signature(output):
    """Stable part of an action failure, without volatile IDs or addresses."""
    first_line = str(output or "").strip().splitlines()[0][:300].casefold()
    first_line = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b|\b0x[0-9a-f]+\b", "<id>",
        first_line,
    )
    return re.sub(r"\s+", " ", first_line)


_ACTION_HEADER = re.compile(
    r'<\s*ACTION\s+((?:"[^"]*"|\'[^\']*\'|[^>"\'])+?)>',
    re.IGNORECASE | re.DOTALL,
)
_TYPE = re.compile(r'\btype\s*=\s*["\']([A-Za-z0-9_]+)["\']',
                   re.IGNORECASE)


def _proposal_attempts(response_text):
    """Return ordered (action, payload hash) pairs from a model response.

    A circuit breaker is allowed to stop an *identical attempt*, not an entire
    capability.  The old guard keyed only on action name and error text, so two
    malformed Blender calls permanently blocked the corrected third call; two
    bad PowerShell commands even blocked a later ``echo test``.  The full tag,
    including a block body's code, is the functional identity needed here.
    """
    text = str(response_text or "")
    found = []
    for match in _ACTION_HEADER.finditer(text):
        attrs = match.group(1)
        type_match = _TYPE.search(attrs)
        if not type_match:
            continue
        action = type_match.group(1).upper()
        end = match.end()
        if not attrs.rstrip().endswith("/"):
            close = re.search(r'</\s*ACTION\s*>', text[end:], re.IGNORECASE)
            if close:
                end += close.end()
        material = re.sub(r"\s+", " ", text[match.start():end].strip())
        digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()
        found.append((action, digest))
    return found


class FailureCircuitBreaker:
    """Stop a deterministic action crash from becoming an agent retry loop."""

    def __init__(self, threshold=2):
        self.threshold = max(1, int(threshold))
        self._counts = {}

    def record(self, receipts, proposal_text=""):
        attempts = {}
        for action, digest in _proposal_attempts(proposal_text):
            attempts.setdefault(action, []).append(digest)
        for item in receipts or ():
            if not isinstance(item, dict) or item.get("ok"):
                continue
            action = str(item.get("type", "") or "").strip().upper()
            signature = _failure_signature(item.get("output", ""))
            if not action or not signature:
                continue
            choices = attempts.get(action) or []
            attempt = choices.pop(0) if choices else ""
            key = (action, signature, attempt)
            self._counts[key] = self._counts.get(key, 0) + 1

    def reason_for(self, proposal):
        proposed_attempts = _proposal_attempts(proposal) if isinstance(proposal, str) else []
        wanted = ({action for action, _ in proposed_attempts}
                  if proposed_attempts else
                  {str(name or "").strip().upper() for name in proposal or ()})
        wanted_attempts = set(proposed_attempts)
        for (action, signature, attempt), count in self._counts.items():
            same_attempt = (not proposed_attempts or not attempt
                            or (action, attempt) in wanted_attempts)
            if action in wanted and same_attempt and count >= self.threshold:
                return (
                    f"{action} has already failed {count} times in this turn "
                    f"with the same error: {signature}. Do not run it again. "
                    "Use a genuinely different capability if one exists; "
                    "otherwise report the blocker and finish."
                )
        return ""
