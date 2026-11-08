"""Code-owned escalation protocols for exceptional situations."""
from dataclasses import dataclass
from enum import Enum
import threading
import time

class Protocol(str, Enum):
    MEDICAL_EMERGENCY = "medical_emergency"
    PERSONAL_SAFETY = "personal_safety"
    CYBER_INCIDENT = "cyber_incident"
    FINANCIAL_FRAUD = "financial_fraud"
    DATA_RECOVERY = "data_recovery"
    CRISIS_SUPPORT = "crisis_support"
    OWNER_OVERRIDE = "owner_override"
    LOCKDOWN = "lockdown"
    PUBLIC_SPACE = "public_space"

@dataclass(frozen=True)
class ProtocolSpec:
    label: str
    purpose: str
    immediate: str
    playbook: tuple[str, ...]
    identity_policy: str = "normal"
    duration_minutes: int = 0

SPECS = {
    Protocol.MEDICAL_EMERGENCY: ProtocolSpec(
        "Medical Emergency", "accident, serious injury, or acute symptoms",
        "If there is immediate danger or serious injury, call 112 now.",
        ("Establish immediate danger, location, consciousness, and breathing.",
         "Give one safe next step at a time; do not overload the user.",
         "Prepare emergency-contact actions, but obey normal authorization.",
         "Keep the incident active until safety or handoff is confirmed.")),
    Protocol.PERSONAL_SAFETY: ProtocolSpec(
        "Personal Safety", "threat, violence, fire, stalking, or being stranded",
        "Move to a safer place and call 112 if danger is immediate.",
        ("Establish location and whether the threat is immediate.",
         "Prioritize moving to safety over collecting information.",
         "Prepare trusted-contact or emergency actions under normal authorization.",
         "Preserve useful details without exposing the user's location broadly.")),
    Protocol.CYBER_INCIDENT: ProtocolSpec(
        "Cyber Incident", "compromised account, stolen device, or active intrusion",
        "Contain access, preserve evidence, and avoid destroying data.",
        ("Identify affected accounts, devices, and the earliest known event.",
         "Contain the smallest affected scope without destroying evidence.",
         "Prioritize credential/session recovery from a trusted device.",
         "Record verified actions and unresolved exposure for follow-up.")),
    Protocol.FINANCIAL_FRAUD: ProtocolSpec(
        "Financial Fraud", "unauthorized payment, scam, or identity misuse",
        "Contact the financial provider through its verified channel.",
        ("Identify the institution, transaction, time, and current exposure.",
         "Use verified provider channels; never trust contact data from the scam.",
         "Preserve receipts, identifiers, and communication as evidence.",
         "Track disputes, freezes, reports, and promised follow-ups.")),
    Protocol.DATA_RECOVERY: ProtocolSpec(
        "Data Recovery", "data loss, corruption, ransomware, or destructive failure",
        "Stop writes to the affected storage and preserve recoverable evidence.",
        ("Identify the affected system and stop avoidable writes.",
         "Establish backups and recovery points before modifying anything.",
         "Prefer reversible imaging/copying before repair attempts.",
         "Verify restored data independently before declaring recovery.")),
    Protocol.CRISIS_SUPPORT: ProtocolSpec(
        "Crisis Support", "severe distress or risk of self-harm",
        "Stay with a trusted person and contact emergency help if danger is immediate.",
        ("Ask directly and calmly whether there is immediate danger.",
         "Keep language brief, present, and non-judgmental.",
         "Prioritize a trusted person and local emergency support.",
         "Remain engaged until safety or a human handoff is confirmed.")),
    Protocol.OWNER_OVERRIDE: ProtocolSpec(
        "Owner Override",
        "darkness, walking, or another situation where the camera cannot verify Himanshu",
        "Personal conversation may continue without a face match for a limited time.",
        ("Answer the current conversation normally without requesting a face check.",
         "Keep replies on the channel where Himanshu contacted VANTA.",
         "Do not treat this protocol as approval for any consequential action.",
         "Expire automatically and return to identity-aware delivery."),
        identity_policy="owner_override", duration_minutes=30),
    Protocol.LOCKDOWN: ProtocolSpec(
        "Lockdown", "an unattended or potentially exposed computer",
        "Protect the local machine and keep personal information off its speakers.",
        ("Lock the Windows session when that action is available.",
         "Suppress personal local speech and display.",
         "Route important updates to Himanshu's authenticated remote channel.",
         "Continue alarms and monitors without local private disclosure."),
        identity_policy="remote_only"),
    Protocol.PUBLIC_SPACE: ProtocolSpec(
        "Public Space", "using VANTA around other people",
        "Keep conversation useful while withholding private details from bystanders.",
        ("Allow general questions and harmless local assistance.",
         "Move personal details and long results to Telegram.",
         "Do not read messages, contacts, health, calendar, or financial data aloud.",
         "End the protocol when Himanshu explicitly asks."),
        identity_policy="private_remote"),
}
_lock = threading.RLock()
_pending = None
_active = None
_active_until = 0.0

def _expire_locked():
    global _active, _active_until
    if _active is not None and _active_until and time.time() >= _active_until:
        _active = None
        _active_until = 0.0

def active_protocol():
    with _lock:
        _expire_locked()
        return _active

def identity_bypass_active():
    return active_protocol() == Protocol.OWNER_OVERRIDE

def local_disclosure_blocked():
    return active_protocol() in {Protocol.LOCKDOWN, Protocol.PUBLIC_SPACE}

# ---------------------------------------------------------------------------
# Short ways in. A protocol used to need the model to notice a situation,
# recommend it, and then be answered "yes" -- three steps and two model calls,
# in exactly the circumstances (a dark room, a stranger at the desk) where
# there is no time for them. These are the direct routes: a key chord, or a few
# typed or spoken words, matched on the WHOLE utterance so that a sentence which
# merely mentions a lockdown activates nothing.
# ---------------------------------------------------------------------------
HOTKEYS = {
    # Ctrl+Alt+<letter>. Windows virtual-key codes, since a held Ctrl turns the
    # letter into a control character in the key event.
    0x4F: ("activate", Protocol.OWNER_OVERRIDE),   # O -- "it's me"
    0x4C: ("activate", Protocol.LOCKDOWN),         # L
    0x50: ("activate", Protocol.PUBLIC_SPACE),     # P
    0x45: ("end", None),                           # E
}

_PHRASES = {
    "owner override": Protocol.OWNER_OVERRIDE, "owner mode": Protocol.OWNER_OVERRIDE,
    "owner": Protocol.OWNER_OVERRIDE, "its me": Protocol.OWNER_OVERRIDE,
    "this is me": Protocol.OWNER_OVERRIDE, "i am here": Protocol.OWNER_OVERRIDE,
    "im here": Protocol.OWNER_OVERRIDE, "i am himanshu": Protocol.OWNER_OVERRIDE,
    "lockdown": Protocol.LOCKDOWN, "lock down": Protocol.LOCKDOWN,
    "lock it down": Protocol.LOCKDOWN,
    "public": Protocol.PUBLIC_SPACE, "public mode": Protocol.PUBLIC_SPACE,
    "public space": Protocol.PUBLIC_SPACE,
}
_END_PHRASES = {"end", "end protocol", "stop protocol", "protocol off",
                "deactivate protocol", "protocol end", "all clear"}


def parse_command(text):
    """('activate', Protocol) / ('end', None) for a whole-utterance command."""
    import re
    value = str(text or "").casefold().replace("\u2019", "").replace("'", "")
    slash = value.strip().startswith("/")
    value = re.sub(r"[^a-z\s]", " ", value)
    value = " ".join(value.split())
    for prefix in ("protocol ", "activate ", "vanta "):
        if value.startswith(prefix) and value[len(prefix):] in {
                *_PHRASES, *_END_PHRASES}:
            value = value[len(prefix):]
    if value in _END_PHRASES:
        if value == "end" and not slash:
            return None
        return ("end", None)
    if value in _PHRASES:
        # A bare "owner" or "public" is only a command with a slash; as plain
        # words they are too easily part of a conversation.
        if value in {"owner", "public"} and not slash:
            return None
        return ("activate", _PHRASES[value])
    return None


def activate(protocol, minutes=None):
    """Start a protocol directly. Returns the confirmation line."""
    global _pending, _active, _active_until
    item = Protocol(protocol)
    spec = SPECS[item]
    with _lock:
        _active, _pending = item, None
        span = spec.duration_minutes if minutes is None else minutes
        _active_until = time.time() + span * 60 if span else 0.0
    expiry = f" for {span} minutes" if span else " until you end it"
    return (f"{spec.label} Protocol on{expiry}. Normal action permissions "
            f"still apply. Ctrl+Alt+E ends it.")


def run_command(command):
    """Apply a parsed command; return the message to show."""
    kind, protocol = command
    if kind == "end":
        old = deactivate()
        return (f"{SPECS[old].label} Protocol ended." if old is not None
                else "No protocol was active.")
    return activate(protocol)


def recommend(protocol, severity="serious", reason=""):
    global _pending
    item = Protocol(protocol)
    with _lock:
        _expire_locked()
        if _active == item or _pending == item:
            return ""
        _pending = item
    spec = SPECS[item]
    lead = f"{spec.immediate} " if severity == "critical" else ""
    return f"{lead}Activate {spec.label} Protocol?"

def answer(text):
    """Return (handled, message). Consent never approves protocol actions."""
    global _pending, _active, _active_until
    value = " ".join(str(text or "").casefold().split())
    with _lock:
        _expire_locked()
        pending = _pending
        if _active is not None and value in {
                "end protocol", "stop protocol", "deactivate protocol",
                "protocol off"}:
            label = SPECS[_active].label
            _active = None
            _active_until = 0.0
            return True, f"{label} Protocol ended."
        if pending is None:
            return False, ""
        if value in {"yes", "yes activate it", "activate it", "do it", "proceed"}:
            _active, _pending = pending, None
            spec = SPECS[_active]
            _active_until = (time.time() + spec.duration_minutes * 60
                             if spec.duration_minutes else 0.0)
            expiry = (f" for {spec.duration_minutes} minutes"
                      if spec.duration_minutes else "")
            return True, (f"{spec.label} Protocol activated{expiry}. "
                          "Normal action permissions still apply.")
        if value in {"no", "no thanks", "do not", "don't", "cancel"}:
            _pending = None
            return True, "Protocol activation cancelled."
    return False, ""

def context():
    active = active_protocol()
    if active is None:
        return ""
    spec = SPECS[active]
    steps = " ".join(
        f"{index}. {step}" for index, step in enumerate(spec.playbook, 1))
    return (
        f"ACTIVE PROTOCOL: {spec.label}. Purpose: {spec.purpose}. Prioritize "
        "immediate safety and ask only questions that change the safe next step. "
        f"PLAYBOOK: {steps} Execute only the steps relevant to the live situation; "
        "verify results and keep unresolved steps visible. "
        f"IDENTITY POLICY: {spec.identity_policy}. "
        "This protocol never grants action permission: calls, messages, purchases, "
        "deletion, credential changes, and disclosures still use code-owned approvals.")

def deactivate():
    global _active, _active_until
    with _lock:
        old, _active, _active_until = _active, None, 0.0
    return old
