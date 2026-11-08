"""Small, deterministic conversational intents that must never invoke tools."""
import re


_ACKNOWLEDGEMENT = re.compile(
    r"(?:okay[ ,]+|ok[ ,]+|alright[ ,]+|perfect[ ,]+|got it[ ,]+)?"
    r"(?:thanks|thank you)(?:[ ,]+(?:vanta|sir))?[.!]*",
    re.IGNORECASE,
)


def is_social_acknowledgement(text):
    """True only for a standalone thanks, never for thanks plus a command."""
    cleaned=" ".join(str(text or "").split())
    return bool(_ACKNOWLEDGEMENT.fullmatch(cleaned))


def acknowledgement_reply(_text):
    return "You're welcome, sir. I'm here when you need me."
