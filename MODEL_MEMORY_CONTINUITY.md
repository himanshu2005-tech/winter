# Stronger ChatGPT fallback and memory continuity

The running configuration now pins CHATGPT_MODEL=gpt-6.1-sol. The authenticated
account's model catalog includes that exact id, and a minimal live Responses
request returned the requested answer. WINTER_PROVIDER=airouter and
VANTA_PREFER_CHATGPT=0 are preserved. Existing AIRouter and coding-provider
choices, credentials, endpoints and reasoning settings are unchanged.

The supplied log retrieved 47 graph facts, then treated building personal
knowledge as a recap. A quoted "remember that" example also tripped a generic
false-progress detector and unnecessarily pushed the turn into action
discovery. The guard now ignores quoted examples and requires a first-person
memory promise. Real claims to perform work still require actions and receipts.
The assistant contract guides knowledge-building conversations to retrieve
what is already known and ask one useful unresolved question, without requiring
the user to say a magic remember phrase or inventing missing personal facts.

Previously session_context clipped user text to 1,500 characters before storing
it as DialogueTurn evidence. It now preserves the full message in the existing
graph evidence node, while a bounded head/tail excerpt supplies live prompt
context. Long assistant answers retain their final question in follow-up
context. Delivery records the complete user-facing answer, not only its spoken
digest; completion no longer concatenates abandoned attempts and action bodies
into the conversation. This does not import a bootstrap profile or reconstruct
information already lost by earlier truncation.

Ordinary fact extraction still distinguishes durable user facts, observations,
and inferred hypotheses, and still uses existing provenance and privacy gates.
No production facts were invented, deleted, promoted or imported by this change.
Full evidence retention does not guarantee perfect extraction or recall; the
model and retrieval policy still choose relevant information.

Tests cover provider separation, stronger-model selection, full evidence,
bounded context, end-of-message continuity, quoted-memory examples and final
answer retention. Test fixtures are synthetic and graph writes are mocked.
The live model smoke request contained only a minimal response instruction, not
the user's graph or personal details. Restart VANTA to use the new settings.
