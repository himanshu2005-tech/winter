# -*- coding: utf-8 -*-
"""
unavailability.py -- six different reasons, which used to be one sentence.

VANTA was asked what it could see through the camera. It searched for a camera
action twice, found none, and said it had no access to a live camera feed. The
camera worked. The action was simply missing from the searchable registry, and
"I searched and found nothing" had been allowed to render as "the capability
does not exist" -- which the user reasonably heard as a statement about their
webcam.

That is the whole bug class: a lookup miss, a policy refusal and a dead device
all arrived at the user as the same flat claim of unavailability. They are not
the same, they are not equally the user's problem, and three of them are
VANTA's own fault. So there is no general "unavailable" message here. There are
six causes, each with its own wording, and only one of them is permitted to say
anything about hardware.

    not_shortlisted      the retrieval budget did not include it; it exists
    absent_from_registry it is not declared -- VANTA's own wiring gap
    blocked_by_policy    declared and working, refused by mode or autonomy
    missing_input        the call lacked a required attribute
    handler_failed       the code ran and raised
    provider_failed      the device or service was actually reached and failed

Only PROVIDER_FAILED may mention hardware, and only when carrying a probe that
genuinely attempted the thing. Everything else has to own the failure.
"""


#: The lookup budget did not include it. It exists and may work.
NOT_SHORTLISTED = "not_shortlisted"
#: Not declared anywhere. A wiring gap, not a missing feature.
ABSENT_FROM_REGISTRY = "absent_from_registry"
#: Declared and working; the active mode or autonomy policy refused it.
BLOCKED_BY_POLICY = "blocked_by_policy"
#: The call was missing a required attribute.
MISSING_INPUT = "missing_input"
#: The handler ran and raised.
HANDLER_FAILED = "handler_failed"
#: The device or remote service was actually reached and it failed.
PROVIDER_FAILED = "provider_failed"

CAUSES = (NOT_SHORTLISTED, ABSENT_FROM_REGISTRY, BLOCKED_BY_POLICY,
          MISSING_INPUT, HANDLER_FAILED, PROVIDER_FAILED)

#: The causes that are VANTA's own fault and must never be dressed up as the
#: user's environment being broken.
OURS = frozenset({NOT_SHORTLISTED, ABSENT_FROM_REGISTRY, HANDLER_FAILED})

#: Only this cause has earned the right to describe hardware or a service as
#: unavailable, because only this one has touched it.
MAY_BLAME_HARDWARE = frozenset({PROVIDER_FAILED})


class Unavailable(Exception):
    """A capability could not run, with the reason it could not.

    Carries the cause so that the sentence shown to the user and the sentence
    handed back to the model are both derived from what actually happened
    rather than from whichever string the nearest error path happened to hold.
    """

    def __init__(self, capability, cause, detail="", probe=None):
        if cause not in CAUSES:
            raise ValueError(f"unknown unavailability cause: {cause!r}")
        self.capability = str(capability or "").upper()
        self.cause = cause
        self.detail = str(detail or "").strip()
        #: What was actually attempted, for PROVIDER_FAILED. Without it, a
        #: hardware claim is not permitted.
        self.probe = probe
        super().__init__(self.for_model())

    # -------------------------------------------------------------- #

    def blames_environment(self):
        """Whether this cause is allowed to describe the device as at fault."""
        return self.cause in MAY_BLAME_HARDWARE and self.probe is not None

    def for_model(self):
        """What the model is told. Phrased to direct the next step.

        Each cause implies a different next move, which is the point: a
        shortlist miss should trigger a wider search, while a policy refusal
        must stop the attempt entirely rather than inviting a retry.
        """
        name = self.capability or "that capability"
        tail = f" {self.detail}" if self.detail else ""
        if self.cause == NOT_SHORTLISTED:
            return (f"[Unavailable] {name} exists but was not in this turn's "
                    f"action shortlist. Search the full registry with "
                    f"DISCOVER_ACTIONS before concluding anything.{tail}")
        if self.cause == ABSENT_FROM_REGISTRY:
            return (f"[Unavailable] {name} is not declared in VANTA's "
                    f"capability registry. This is a wiring gap in VANTA, not "
                    f"a missing device or service. Say that the capability is "
                    f"not wired up; do not claim the hardware or account is "
                    f"unavailable.{tail}")
        if self.cause == BLOCKED_BY_POLICY:
            return (f"[Unavailable] {name} is available but refused by the "
                    f"active policy. Do not retry it this turn.{tail}")
        if self.cause == MISSING_INPUT:
            return (f"[Unavailable] {name} was called without a required "
                    f"attribute. Reissue it with the missing value.{tail}")
        if self.cause == HANDLER_FAILED:
            return (f"[Unavailable] {name} failed inside VANTA's own handler. "
                    f"This is a defect in VANTA, not in the device or "
                    f"service.{tail}")
        return (f"[Unavailable] {name} reached the device or service and it "
                f"failed.{tail}")

    def for_user(self):
        """What is said out loud. Short, and honest about whose fault it is."""
        if self.cause == ABSENT_FROM_REGISTRY:
            return ("I don't have that wired up yet — it isn't a problem with "
                    "your hardware.")
        if self.cause == NOT_SHORTLISTED:
            return "I didn't have that to hand. Let me look again."
        if self.cause == BLOCKED_BY_POLICY:
            return self.detail or "That's switched off in the current mode."
        if self.cause == MISSING_INPUT:
            return self.detail or "I was missing something I needed for that."
        if self.cause == HANDLER_FAILED:
            return "That broke on my side."
        return self.detail or "I tried, and it didn't respond."

    def __repr__(self):
        return (f"Unavailable({self.capability!r}, {self.cause!r}, "
                f"detail={self.detail!r})")


def classify_lookup(name, declared_names, shortlisted_names):
    """Why a capability the model wanted was not usable.

    This is the distinction that was missing when the camera was reported as
    inaccessible: absent from the registry and merely absent from this turn's
    shortlist look identical at the call site and mean opposite things.
    """
    upper = str(name or "").upper()
    if upper in {str(n).upper() for n in (shortlisted_names or ())}:
        return None
    if upper in {str(n).upper() for n in (declared_names or ())}:
        return NOT_SHORTLISTED
    return ABSENT_FROM_REGISTRY
