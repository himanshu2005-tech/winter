import os
import threading
import time
from openai import OpenAI
try:
    from groq import Groq
except ImportError:
    Groq = None


def _load_project_env():
    """Load unset router settings independently of agent import order.

    The desktop entrypoint loads .env before importing this module, but tests,
    maintenance commands and future device clients can import the router
    directly. Provider priority must not change merely because of that import
    order. Existing process environment always wins.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as source:
            for line in source:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))
    except OSError:
        pass


_load_project_env()

xai_client = None
openrouter_client = None
airouter_client = None
local_client = None
omniroute_client = None
groq_client = None
gemini_client = None
chatgpt_client = None
# Cloud is the default.  A local provider remains available through /model local
# for offline use, but VANTA should not boot a heavyweight local server unless
# it was explicitly selected.
# The fallback matters more than it looks: it is what runs when .env fails to
# load. It was alibaba/qwen3.8-flash, measured at 266 seconds per call in this
# project, so a missing .env made VANTA look hung rather than misconfigured --
# the hardest kind of fault to diagnose, because nothing reports an error.
# glm-5.3-flash is the configured model and a sane default on its own.
winter_model = os.getenv("WINTER_MODEL", "zai/glm-5.3-flash")
active_provider = os.getenv("WINTER_PROVIDER", "airouter").lower()
_ui_queue = None

# Why the last call produced nothing. An empty string from call_llm is
# ambiguous -- a model with nothing to say and an unreachable gateway look
# identical to the caller -- and on a phone hotspot the difference is the whole
# story. VANTA kept telling Himanshu "the model did not respond" when the real
# answer was that his connection had dropped, which sent him looking at the
# wrong thing.
last_failure = ""
_CALL_STATE = threading.local()
_ROUTER_STATE_LOCK = threading.RLock()


def _note_failure(reason):
    global last_failure
    value = str(reason or "")[:200]
    _CALL_STATE.last_failure = value
    with _ROUTER_STATE_LOCK:
        last_failure = value


_last_truncation = ""


# Every cheap model on this gateway is a reasoning model, and reasoning tokens
# are spent out of the same allowance as the answer. Measured on
# zai/glm-5.3-flash with a one-line prompt: ~1268 reasoning tokens against a
# router budget of 1000, so the router never emitted a character and timed out
# on every boot, while the planner's JSON was cut off mid-string.
#
# reasoning_effort="low" took that to ZERO reasoning tokens and 13 completion
# tokens, returning correct JSON instantly where the same call had timed out.
# These stages want a terse structured answer, not deliberation, so low is the
# right setting for all of them. Set WINTER_REASONING_EFFORT= (empty) to send
# nothing and let each model do as it likes.
_EFFORT_UNSUPPORTED = set()


def reasoning_effort():
    """How hard the model should think before answering, or '' to not say."""
    value = os.getenv("WINTER_REASONING_EFFORT", "low").strip().lower()
    return value if value in {"low", "medium", "high", "minimal"} else ""


def _rejects_effort(exc):
    """Whether this failure is the model refusing reasoning_effort."""
    text = f"{type(exc).__name__} {exc}".lower()
    return ("400" in text or "bad_request" in text or "badrequest" in text) \
        and ("reasoning" in text or "effort" in text or "unsupported" in text
             or "unrecognized" in text or "unknown" in text)


def _note_truncated(length):
    global _last_truncation
    value = (f"the model hit its output budget after {length} "
             f"characters and was cut off mid-answer")
    _CALL_STATE.last_truncation = value
    with _ROUTER_STATE_LOCK:
        _last_truncation = value
    print(f"[Router] Output truncated: {value}. Raise "
          f"WINTER_BUDGET_<STAGE> or use a model that does not spend the "
          f"budget on reasoning.")


def last_truncation():
    """Why the previous answer was incomplete, or '' if it was not."""
    return getattr(_CALL_STATE, "last_truncation", _last_truncation)


def clear_truncation():
    global _last_truncation
    _CALL_STATE.last_truncation = ""
    with _ROUTER_STATE_LOCK:
        _last_truncation = ""


def failure_kind():
    """'network', 'billing', 'timeout' or '' -- what went wrong last."""
    text = getattr(_CALL_STATE, "last_failure", last_failure).lower()
    if not text:
        return ""
    if "connection" in text or "unreachable" in text or "getaddrinfo" in text:
        return "network"
    if "402" in text or "credit" in text or "billing" in text or "quota" in text:
        return "billing"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "other"

def init_clients():
    global xai_client, openrouter_client, airouter_client, local_client, omniroute_client, groq_client, gemini_client, chatgpt_client, winter_model, active_provider
    xai_api_key = os.getenv("XAI_API_KEY", "")
    xai_client  = OpenAI(api_key=xai_api_key, base_url="https://api.x.ai/v1") if xai_api_key else None

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_client  = OpenAI(api_key=openrouter_api_key, base_url="https://openrouter.ai/api/v1") if openrouter_api_key else None

    airouter_api_key = os.getenv("AIROUTER_API_KEY", "")
    airouter_base_url = os.getenv("AIROUTER_BASE_URL", "https://api.airouter.in/v1")
    airouter_client  = OpenAI(api_key=airouter_api_key, base_url=airouter_base_url) if airouter_api_key else None

    omniroute_api_key = os.getenv("OMNIROUTE_API_KEY", "sk-omniroute")
    omniroute_client = OpenAI(api_key=omniroute_api_key, base_url="http://localhost:20128/v1")

    local_client = OpenAI(api_key="sk-no-key", base_url="http://localhost:8080/v1")

    groq_api_key = os.getenv("GROQ_API_KEY", "")
    groq_client  = Groq(api_key=groq_api_key) if (Groq and groq_api_key) else None

    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_client  = OpenAI(api_key=gemini_api_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/") if gemini_api_key else None

    # ChatGPT-plan access uses the supported OAuth flow and Responses API.
    # Authentication and provider priority are separate concerns: having a
    # valid ChatGPT token makes it available as a fallback, but must never
    # silently replace the provider selected by WINTER_PROVIDER.
    chatgpt_client = None
    try:
        from providers import chatgpt_auth
        token = chatgpt_auth.access_token(refresh=True)
        if token:
            chatgpt_client = OpenAI(
                api_key=token, base_url="https://api.openai.com/v1",
                max_retries=0)
    except Exception as exc:
        print(f"[ChatGPT] Connection unavailable: {exc}")

    winter_model = os.getenv("WINTER_MODEL", winter_model)

def set_ui_queue(q):
    global _ui_queue
    _ui_queue = q

def set_active_provider(provider: str, model: str = None):
    global active_provider, winter_model
    active_provider = provider.lower()
    if model:
        winter_model = model

def get_active_provider():
    return active_provider

def get_winter_model():
    return winter_model

# Per-stage model overrides. Planning and criticism benefit from a stronger
# model; routing and execution benefit from a fast one. Each falls back to
# WINTER_MODEL, so leaving these unset preserves current behaviour exactly.
_STAGE_ENV = {
    "planner": "WINTER_MODEL_PLANNER",
    "critic": "WINTER_MODEL_CRITIC",
    "cognitive_router": "WINTER_MODEL_ROUTER",
    "mode_router": "WINTER_MODEL_ROUTER",
    "vision": "WINTER_MODEL_VISION",
}


# The OpenAI client defaults to a 600s read timeout. With stream=True that
# means a provider which stops sending chunks blocks VANTA for ten minutes
# with no output and no error -- indistinguishable from a crash, and the cause
# of turns that simply stopped. Vision turns are the usual trigger because they
# carry a screenshot and two crops.
# These were sized for a fast non-reasoning model. Measured on
# zai/glm-5.3-flash through this gateway: 24-41 seconds to return THIRTEEN
# tokens. A 30s router budget against that is a guaranteed timeout on every
# boot, and no amount of reasoning_effort helps when the floor is 24s.
#
# Raised so a slow model has room, and tunable per stage so the next model
# change is a .env edit rather than a code edit. If a fast model is configured
# these can come back down -- WINTER_TIMEOUT_COGNITIVE_ROUTER=30 and so on.
_STAGE_TIMEOUTS = {
    "inference": 120.0,
    "mode_router": 75.0,
    "cognitive_router": 75.0,
    "routine": 90.0,
    "critic": 90.0,
    "planner": 120.0,
    "vision": 120.0,
}
DEFAULT_TIMEOUT = 90.0

# A distinguishable value so the caller can tell "the model said nothing" from
# "the call never came back".
TIMEOUT_MARKER = "[API Timeout]"


def connection_retries():
    """How many times to retry the last provider before giving up.

    Retrying and falling through to another provider solve the same problem
    two ways, and this router picked the second: max_retries was pinned to 0
    because the chain would take over. With a single provider configured --
    which is the supported setup here -- there is nothing to fall through to,
    so one dropped connection on a phone hotspot ended the turn outright.

    The SDK only retries what is worth retrying: connection errors, 408, 409,
    429 and 5xx. It never retries a 400, so a malformed request still fails
    immediately rather than three times. Connection failures also fail fast,
    in a second or two, so this does not usually multiply the stage timeout.
    Set WINTER_LLM_RETRIES=0 to restore the old behaviour.
    """
    try:
        value = int(os.getenv("WINTER_LLM_RETRIES", "2"))
    except ValueError:
        return 2
    return max(0, min(value, 5))


def timeout_for(task_level):
    try:
        override = float(os.getenv("WINTER_LLM_TIMEOUT", "") or 0)
        if override > 0:
            return override
    except ValueError:
        pass
    stage = task_level or ""
    stage_override = os.getenv(f"WINTER_TIMEOUT_{stage.upper()}", "").strip()
    if stage_override:
        try:
            return max(5.0, min(float(stage_override), 600.0))
        except ValueError:
            pass
    return _STAGE_TIMEOUTS.get(stage, DEFAULT_TIMEOUT)


def model_for(task_level):
    """The model to use for a stage, or the default when unconfigured."""
    env_name = _STAGE_ENV.get(task_level or "")
    if env_name:
        override = os.getenv(env_name, "").strip()
        if override:
            return override
    return winter_model


# Ordered fallbacks, each with the model to use when we get there. A model id
# is provider-specific -- handing Groq an AIRouter model name fails outright --
# so a fallback chain that only swapped clients would trade one failure for
# another. Override any of these in .env.
# Stages whose payload includes images. A model without vision answers these
# with a gateway error however good it is at everything else.
VISION_STAGES = frozenset({"vision"})

# Which providers may be tried when the configured one fails, in order.
# Empty by default: Himanshu asked to stay on AIRouter, and the alternatives
# were doing more harm than good -- a vision payload is far larger than Groq's
# free-tier per-minute allowance, so the "fallback" was a guaranteed 413 that
# cost a round trip to discover. Set WINTER_FALLBACK_PROVIDERS=groq,gemini in
# .env to turn it back on; an AIRouter outage is then survivable rather than
# fatal, which is the trade being made here.
def fallback_providers():
    raw = os.getenv("WINTER_FALLBACK_PROVIDERS", "").strip()
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _role_provider(task_level):
    """Return a code-owned provider pin for the active specialist role.

    Model identifiers are not portable between providers.  A coder role may
    therefore be pinned to AIRouter so an outage is reported honestly instead
    of silently changing both provider and model quality mid-project.
    """
    if task_level in VISION_STAGES:
        return ""
    try:
        import mode_manager
        policy = mode_manager.current_policy()
        if policy.model_env == "WINTER_MODEL_CODER":
            return os.getenv("WINTER_PROVIDER_CODER", "").strip().lower()
    except Exception:
        pass
    return ""


FALLBACK_CHAIN = (
    ("groq", "GROQ_MODEL", "qwen/qwen3.8-27b"),
    ("gemini", "GEMINI_MODEL", "gemini-2.0-flash"),
    ("openrouter", "OPENROUTER_MODEL", "anthropic/claude-3.5-haiku"),
    ("grok", "XAI_MODEL", "grok-2-latest"),
    ("airouter", "WINTER_MODEL", "anthropic/claude-3-haiku"),
)

# Which provider actually answered last, and when we last tried the preferred
# one. Without this, a dead primary costs a failed connection on every single
# call -- unnoticeable in a chat turn, ruinous across a multi-hour research run.
_working_provider = None
_preferred_retry_at = 0.0
PREFERRED_RETRY_SECONDS = 300


def _client_for(provider):
    return {
        "grok": xai_client, "groq": groq_client, "local": local_client,
        "openrouter": openrouter_client, "omniroute": omniroute_client,
        "gemini": gemini_client, "airouter": airouter_client,
        "chatgpt": chatgpt_client,
    }.get(provider, airouter_client)


def _chatgpt_role_model(task_level):
    """The model this stage or mode asks for, if any, before validation.

    A stage is more specific than the mode containing it: a critic override
    should win over "build mode prefers a coding model", because the critic is
    doing a different job inside that mode.
    """
    env_name = _STAGE_ENV.get(task_level or "")
    if env_name:
        provider_env = env_name.replace("WINTER_", "CHATGPT_", 1)
        override = os.getenv(provider_env, "").strip()
        if override:
            return override
    if env_name:
        override = os.getenv(env_name, "").strip()
        if override:
            return override
    if task_level in VISION_STAGES:
        # The stage that carries pixels picks its own model; a coding model
        # has no image support and answered 404 on every screen-aware turn.
        return ""
    try:
        import mode_manager
        policy = mode_manager.current_policy()
        if policy.model_env:
            provider_env = policy.model_env.replace(
                "WINTER_", "CHATGPT_", 1)
            override = os.getenv(provider_env, "").strip()
            if override:
                return override
        return mode_manager.mode_model().strip()
    except Exception:
        return ""


def _model_for_provider(provider, task_level):
    """The stage override if there is one, else this provider's own default."""
    # ChatGPT account models live in a different namespace from AIRouter.
    # Provider-specific selection must happen before legacy stage overrides;
    # otherwise WINTER_MODEL_ROUTER=zai/... gets sent with an OpenAI OAuth
    # token and the Responses endpoint correctly rejects it.
    if provider == "chatgpt":
        from providers import chatgpt_auth
        # A stage or mode may still ask for a different model, provided it is
        # one this account actually has. Returning early for every case made
        # WINTER_MODEL_CODER, _PLANNER, _CRITIC and _ROUTER dead
        # configuration: one model did every job, and a coding mode could not
        # ask for a coding model even when the account offered one.
        wanted = _chatgpt_role_model(task_level)
        if wanted:
            try:
                available = {row["slug"] for row in chatgpt_auth.list_models()}
            except Exception:
                available = set()
            excluded = {
                item.strip().casefold() for item in
                os.getenv("CHATGPT_EXCLUDED_MODELS", "gpt-6-astra").split(",")
                if item.strip()
            }
            # Only an id from this account's own namespace. The early return
            # existed because "zai/glm-5.3-flash" sent with an OpenAI OAuth
            # token is correctly rejected, and that must stay true.
            if wanted in available and wanted.casefold() not in excluded:
                return wanted
        return chatgpt_auth.preferred_model()
    env_name = _STAGE_ENV.get(task_level or "")
    if env_name:
        provider_env = env_name.replace(
            "WINTER_", f"{provider.upper()}_", 1)
        override = os.getenv(provider_env, "").strip()
        if override:
            return override
    if env_name:
        override = os.getenv(env_name, "").strip()
        if override and provider == active_provider:
            return override
    if provider == active_provider:
        # A mode can ask for a different model -- build mode wants a coding
        # model rather than the general one. Only on the preferred provider:
        # handing an AIRouter model id to Groq fails outright, so a fallback
        # must drop back to that provider's own default.
        #
        # Never for the vision stage. A coding model has no image support, so
        # build mode was sending screenshots to alibaba/qwen3-coder and the
        # gateway answered 404 on every screen-aware turn. The stage that
        # carries pixels picks its own model.
        if task_level not in VISION_STAGES:
            try:
                import mode_manager
                policy = mode_manager.current_policy()
                if policy.model_env:
                    provider_env = policy.model_env.replace(
                        "WINTER_", f"{provider.upper()}_", 1)
                    override = os.getenv(provider_env, "").strip()
                    if override:
                        return override
                wanted = mode_manager.mode_model()
                if wanted:
                    return wanted
            except Exception:
                pass
        return winter_model
    for name, env_key, default in FALLBACK_CHAIN:
        if name == provider:
            return os.getenv(env_key, "").strip() or default
    return winter_model


def _candidates(task_level):
    """Providers to try, in order, skipping any without a configured key."""
    order = []
    pinned_provider = _role_provider(task_level)
    if pinned_provider:
        client = _client_for(pinned_provider)
        if client is None:
            return []
        return [(pinned_provider, client,
                 None if pinned_provider == 'chatgpt' else
                 _model_for_provider(pinned_provider, task_level))]
    # Re-probe the preferred provider periodically; an outage is usually
    # temporary and we would rather be on the configured one when it returns.
    with _ROUTER_STATE_LOCK:
        working = _working_provider
        retry_at = _preferred_retry_at
    if working and working != active_provider:
        if time.time() >= retry_at:
            order.append(active_provider)
        order.append(working)
    else:
        order.append(active_provider)

    for name in fallback_providers():
        if name not in order:
            order.append(name)
    # AIRouter and the authenticated ChatGPT account are continuity partners.
    # Their order is always the configured provider first. This is code-owned
    # so an empty optional fallback list cannot leave VANTA mute, while merely
    # authenticating ChatGPT can never promote it over AIRouter.
    if active_provider == "airouter" and "chatgpt" not in order:
        order.append("chatgpt")
    if active_provider == "chatgpt" and "airouter" not in order:
        order.append("airouter")

    seen, result = set(), []
    for provider in order:
        client = _client_for(provider)
        if provider in seen or client is None:
            continue
        seen.add(provider)
        # Account catalog discovery can make a network request. A fallback
        # must not delay or break the healthy primary before it is even tried.
        # Resolve this model inside the guarded attempt only if it is needed.
        result.append((provider, client, None if provider == 'chatgpt' else
                       _model_for_provider(provider, task_level)))
    return result


# Background work -- the research loop and the fact consolidator -- calls this
# from its own threads while a foreground turn is streaming. Both wrote tokens
# to the same console as they arrived, so a reply came out spliced through the
# other's JSON one fragment at a time:
#
#     "confidence": 1<SAY>Sir, I need the URL and login credent.0
#
# Foreground turns still stream live, one chunk at a time under this lock, so
# a background block can only land between chunks rather than inside a word.
# Background callers pass quiet=True and print once, whole, at the end.
_PRINT_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Token accounting.
#
# "It burnt 0.7 dollars in an hour" and "haiku drinks tokens" were both true
# and neither was actionable, because nothing here counted anything. A turn is
# not one call: the mode router, the cognitive router, the planner, the critic
# and every iteration of the execution loop are separate completions, and the
# loop ones each re-send the whole tool catalogue. Without a meter the only way
# to pick a model is by watching the bill, which is a slow and expensive
# feedback loop.
#
# Exact counts are used when the provider reports them. When it does not, the
# figures are estimated from message length and marked as estimates, because a
# rough number attributed to the right stage beats an exact number nobody has.
# --------------------------------------------------------------------------- #
_USAGE_LOCK = threading.RLock()
_USAGE = {}
_USAGE_UNSUPPORTED = set()      # models that reject stream_options


def _blank():
    return {"calls": 0, "prompt": 0, "completion": 0, "images": 0,
            "exact": 0, "estimated": 0}


def estimate_tokens(messages):
    """Roughly what a message list costs, plus how many images it carries.

    Four characters per token is wrong in the third digit and right in the
    first, which is all that is needed to see which stage is expensive.
    """
    chars = 0
    images = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    chars += len(str(part))
                elif part.get("type") == "text":
                    chars += len(str(part.get("text", "")))
                else:
                    images += 1
    return chars // 4, images


def record_usage(stage, prompt_tokens, completion_tokens, images=0, exact=False):
    with _USAGE_LOCK:
        row = _USAGE.setdefault(stage or "turn", _blank())
        row["calls"] += 1
        row["prompt"] += int(prompt_tokens or 0)
        row["completion"] += int(completion_tokens or 0)
        row["images"] += int(images or 0)
        row["exact" if exact else "estimated"] += 1


def usage_totals():
    with _USAGE_LOCK:
        return {stage: dict(row) for stage, row in _USAGE.items()}


def reset_usage():
    with _USAGE_LOCK:
        _USAGE.clear()


def usage_since(before, label="turn"):
    """One line for what has happened since a snapshot of usage_totals().

    A turn is many completions, so the per-turn number is the one that maps
    onto "that question cost this much" -- which is the only figure anyone can
    actually act on while using it.
    """
    before = before or {}
    now = usage_totals()

    def total(rows, key):
        return sum(row[key] for row in rows.values())

    calls = total(now, "calls") - total(before, "calls")
    if calls <= 0:
        return ""
    tokens_in = total(now, "prompt") - total(before, "prompt")
    tokens_out = total(now, "completion") - total(before, "completion")
    stages = []
    for stage, row in sorted(now.items(), key=lambda kv: -kv[1]["prompt"]):
        spent = row["prompt"] - before.get(stage, {}).get("prompt", 0)
        made = row["calls"] - before.get(stage, {}).get("calls", 0)
        if made > 0:
            stages.append(f"{stage} x{made}")
    return (f"[Tokens] {label}: {calls} calls, {tokens_in:,} in, "
            f"{tokens_out:,} out  ({', '.join(stages)})")


def usage_summary(heading="TOKENS THIS SESSION"):
    """A per-stage breakdown, cheapest thing in the world to read."""
    rows = usage_totals()
    if not rows:
        return ""
    order = sorted(rows.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"]))
    width = max(len(name) for name in rows) + 2
    lines = [f"--- {heading} ---",
             f"{'stage':<{width}}{'calls':>6}{'in':>9}{'out':>8}{'img':>5}"]
    total_in = total_out = total_calls = 0
    approximate = False
    for stage, row in order:
        lines.append(f"{stage:<{width}}{row['calls']:>6}{row['prompt']:>9}"
                     f"{row['completion']:>8}{row['images']:>5}")
        total_in += row["prompt"]
        total_out += row["completion"]
        total_calls += row["calls"]
        approximate = approximate or bool(row["estimated"])
    lines.append(f"{'TOTAL':<{width}}{total_calls:>6}{total_in:>9}{total_out:>8}")
    if approximate:
        lines.append("(some figures estimated; the provider did not report usage)")
    return "\n".join(lines)


def _emit(text, end=""):
    if not text:
        return
    with _PRINT_LOCK:
        print(text, end=end, flush=True)


def _stream(client, provider, model, messages, temperature, output_budget,
            call_timeout, quiet=False, on_delta=None):
    """One streaming completion, returned as text. Raises on failure."""

    if provider == "chatgpt":
        return _stream_chatgpt(client, model, messages, output_budget, quiet,
                               on_delta=on_delta)

    def _request(with_effort, with_usage=True):
        kwargs = dict(messages=messages, model=model, temperature=temperature,
                      stream=True, timeout=call_timeout)
        # Streaming responses carry no usage block unless it is asked for.
        # Gateways that do not know the option reject the whole request, so a
        # model that refuses is remembered and never asked again.
        if with_usage and model not in _USAGE_UNSUPPORTED:
            kwargs["stream_options"] = {"include_usage": True}
        if provider == "groq":
            kwargs.update(max_completion_tokens=output_budget, top_p=0.95,
                          stop=None)
        else:
            kwargs["max_tokens"] = output_budget
        if with_effort:
            kwargs["reasoning_effort"] = effort
        return client.chat.completions.create(**kwargs)

    # A model that has already refused the parameter is never asked again --
    # otherwise every single call pays for a round trip to rediscover it.
    effort = reasoning_effort()
    use_effort = bool(effort) and model not in _EFFORT_UNSUPPORTED
    try:
        stream = _request(use_effort)
    except Exception as exc:
        if use_effort and _rejects_effort(exc):
            _EFFORT_UNSUPPORTED.add(model)
            print(f"[Router] {model} does not accept reasoning_effort; "
                  f"continuing without it.")
            stream = _request(False)
        elif "stream_options" in str(exc) and model not in _USAGE_UNSUPPORTED:
            # Counting tokens is never worth losing the call over.
            _USAGE_UNSUPPORTED.add(model)
            print(f"[Router] {model} does not report streamed usage; "
                  f"token figures for it will be estimates.")
            stream = _request(use_effort, with_usage=False)
        else:
            raise

    full_text = ""
    is_reasoning = False
    truncated = False
    reported = {}
    for chunk in stream:
        # The usage block arrives as a final chunk that carries no choices, so
        # it has to be read before the choices guard below skips it.
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            reported = {
                "prompt": getattr(usage, "prompt_tokens", 0) or 0,
                "completion": getattr(usage, "completion_tokens", 0) or 0,
            }
        if not getattr(chunk, "choices", None):
            continue
        # "length" means the budget ran out mid-sentence. Silently returning
        # half a JSON object is how "Could not parse plan JSON" came to mean
        # three different things.
        if getattr(chunk.choices[0], "finish_reason", None) == "length":
            truncated = True
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning", None)
        content = getattr(delta, "content", None)
        if reasoning:
            if not is_reasoning:
                is_reasoning = True
                full_text += "<think>\n"
                if not quiet:
                    _emit("\n\033[90m[Thinking...] ")
            full_text += reasoning
            if not quiet:
                _emit(reasoning)
        if content:
            if is_reasoning:
                is_reasoning = False
                full_text += "\n</think>\n"
                if not quiet:
                    _emit("\033[0m\n")
            full_text += content
            if not quiet:
                _emit(content)
            if on_delta is not None:
                # Content only -- never reasoning. Speaking a model's private
                # deliberation aloud would be both wrong and alarming.
                try:
                    on_delta(content)
                except Exception as exc:
                    print(f"[Router] Delta listener failed: {exc}")
    if is_reasoning:
        full_text += "\n</think>"
        if not quiet:
            _emit("\033[0m", end="\n")
    if not quiet:
        _emit("", end="\n")
    if truncated:
        _note_truncated(len(full_text))
    # Function attributes are process-global. A background research stream
    # could overwrite these numbers between a foreground stream returning and
    # its caller reading them, charging one task for another task's tokens.
    _CALL_STATE.last_usage = reported
    return full_text


def _responses_input(messages):
    """Translate Chat Completions content into supported Responses input."""
    converted = []
    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        parts = []
        for part in content or []:
            if part.get("type") == "text":
                parts.append({"type": "input_text", "text": str(part.get("text", ""))})
            elif part.get("type") == "image_url":
                image = part.get("image_url") or {}
                url = image.get("url") if isinstance(image, dict) else image
                if url:
                    parts.append({"type": "input_image", "image_url": url})
        converted.append({"role": role, "content": parts or [{"type": "input_text", "text": ""}]})
    return converted


def _stream_chatgpt(client, model, messages, output_budget, quiet=False,
                    on_delta=None):
    """Stream a ChatGPT-plan request through the required Responses route."""
    from providers import chatgpt_auth
    token = chatgpt_auth.access_token(refresh=True)
    if not token:
        raise RuntimeError("ChatGPT authentication required")
    # Refresh may have rotated the access token since this client was built.
    client = OpenAI(api_key=token, base_url="https://api.openai.com/v1", max_retries=0)
    full_text, completed, reported = "", False, {}
    with client.responses.create(
            model=model, input=_responses_input(messages), store=False,
            stream=True, max_output_tokens=output_budget) as stream:
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                full_text += delta
                if not quiet:
                    _emit(delta)
                if on_delta is not None:
                    try:
                        on_delta(delta)
                    except Exception as exc:
                        print(f"[Router] Delta listener failed: {exc}")
            elif kind == "response.completed":
                completed = True
                response = getattr(event, "response", None)
                usage = getattr(response, "usage", None)
                if usage:
                    reported = {
                        "prompt": getattr(usage, "input_tokens", 0) or 0,
                        "completion": getattr(usage, "output_tokens", 0) or 0,
                    }
            elif kind in {"response.failed", "response.incomplete"}:
                response = getattr(event, "response", None)
                error = getattr(response, "error", None)
                code = getattr(error, "code", None) or kind
                raise RuntimeError(f"ChatGPT Responses API failed: {code}")
    if not quiet:
        _emit("", end="\n")
    if not completed:
        raise RuntimeError("ChatGPT stream ended before response.completed")
    _CALL_STATE.last_usage = reported
    return full_text


def call_llm(messages, task_level=None, step=None, quiet=False, on_delta=None):
    """Ask a model, falling through to another provider rather than failing.

    The old version picked one client and returned "" if it was missing or
    unreachable. An empty string is indistinguishable from a model that had
    nothing to say, so an outage surfaced as VANTA silently doing nothing --
    and for an unattended research session, as hours of no progress with no
    explanation. Now a dead provider is announced and stepped over.
    """
    global _working_provider, _preferred_retry_at

    prefix = f"[Agent Step {step+1}] " if step is not None else "[Agent] "
    if not quiet:
        _emit(prefix)

    # Structured plans need enough room to close their JSON object; routine
    # executor turns stay compact for latency and cost.
    # A reasoning model spends most of its allowance thinking before it writes
    # anything, and those tokens come out of this budget. glm-5.3-flash used
    # ~1268 reasoning tokens on a small prompt, which is more than the router
    # was ever given -- so the router never emitted a character and timed out
    # on every boot, and the planner's JSON was cut off mid-string. Tunable so
    # a model change does not need a code change.
    _DEFAULT_BUDGETS = {
        "mode_router": 1000, "cognitive_router": 1000,
        "proactive_director": 1200,
        "critic": 1200, "planner": 4000, "vision": 4000,
        # The nightly inference pass borrowed the critic's 1200 and reliably
        # spent all of it reasoning before writing any JSON: "output truncated
        # after 4707 characters" then "no usable JSON returned", on every boot.
        # A whole call, paid for, discarded. It reasons over the entire contact
        # graph, so it needs room to think AND to answer.
        "inference": 4000,
        "memory_extraction": 4000,
    }
    _stage = task_level or ""
    output_budget = _DEFAULT_BUDGETS.get(_stage, 2048)
    _override = os.getenv(f"WINTER_BUDGET_{_stage.upper()}", "").strip()
    if _override:
        try:
            output_budget = max(256, min(int(_override), 32000))
        except ValueError:
            pass

    # Structured stages must not wander; prose may.
    structured = task_level in {"mode_router", "cognitive_router", "planner", "critic", "memory_extraction"}
    call_timeout = timeout_for(task_level)

    candidates = _candidates(task_level)
    if not candidates:
        message = ("[Error] No LLM provider is configured. Set AIROUTER_API_KEY "
                   "or GROQ_API_KEY in .env.")
        print(message)
        if _ui_queue:
            _ui_queue.put(("info", message))
        return ""

    last_error = None
    active_attempt = [None]
    for index, (provider, client, model) in enumerate(candidates):
        if index and callable(getattr(on_delta, 'reset', None)):
            try:
                on_delta.reset()
            except Exception as preview_error:
                print(f'[Router] Output preview reset unavailable: {type(preview_error).__name__}')
        if index and provider != _working_provider:
            # Naming the reason matters: this happens unattended in the middle
            # of long sessions, and a bad key looks exactly like an outage
            # unless the message says which it was.
            reason = str(last_error or "no client configured").splitlines()[0][:140]
            note = (f"[Router] {candidates[index - 1][0]} unavailable "
                    f"({reason}); trying {provider}.")
            print(f"\n{note}")
            if _ui_queue:
                _ui_queue.put(("info", note))
        temperature = 0.1 if structured else (0.6 if provider == "groq" else 0.4)
        # Earlier candidates still fail fast, because another provider is
        # waiting and switching beats waiting. The last one retries, because
        # after it there is nothing left to try.
        is_last = index == len(candidates) - 1
        attempts = connection_retries() if is_last else 0
        from performance_metrics import record as record_timing
        started = time.perf_counter()
        first_content = None
        call_ok = False
        attempt_token = object()
        active_attempt[0] = attempt_token

        def timed_delta(content, token=attempt_token):
            nonlocal first_content
            if active_attempt[0] is not token:
                return
            if content and first_content is None:
                first_content = time.perf_counter() - started
            if on_delta is not None:
                on_delta(content)

        try:
            if model is None:
                model = _model_for_provider(provider, task_level)
            if model != winter_model or provider != active_provider:
                print(f"\n[Router] {task_level or 'turn'} -> {provider}:{model}")
            text = _stream(client.with_options(max_retries=attempts), provider,
                           model, messages, temperature, output_budget,
                           call_timeout, quiet=quiet, on_delta=timed_delta)
            if not str(text or '').strip():
                raise RuntimeError('Provider returned an empty response')
            # Attributed to the stage rather than the model, because the
            # question being answered is which part of a turn is expensive.
            reported = getattr(_CALL_STATE, "last_usage", None) or {}
            guess, images = estimate_tokens(messages)
            record_usage(task_level or "routine",
                         reported.get("prompt") or guess,
                         reported.get("completion") or (len(text) // 4),
                         images=images,
                         exact=bool(reported.get("prompt")))
            _note_failure("")
            with _ROUTER_STATE_LOCK:
                if provider != active_provider:
                    _working_provider = provider
                    _preferred_retry_at = time.time() + PREFERRED_RETRY_SECONDS
                else:
                    _working_provider = None
            call_ok = bool(text)
            return text
        except Exception as exc:
            last_error = exc
            _note_failure(exc)
            timed_out = any(word in type(exc).__name__.lower()
                            for word in ("timeout", "readtimeout"))
            if timed_out and index == len(candidates) - 1:
                # A timeout means the request was accepted then abandoned. The
                # agent loop retries that itself, with a smaller payload.
                message = (f"{TIMEOUT_MARKER} {task_level or 'request'} gave up "
                           f"after {call_timeout:.0f}s.")
                print(f"\n{message}")
                if _ui_queue:
                    _ui_queue.put(("info", message))
                return TIMEOUT_MARKER
            continue
        finally:
            active_attempt[0] = None
            record_timing('model', task_level or 'routine',
                          time.perf_counter() - started, ok=call_ok,
                          first_content=first_content, provider=provider, model=model)

    _note_failure(last_error)
    if len(candidates) == 1:
        message = (f"[API Error] {candidates[0][0]} failed and no fallback is "
                   f"configured: {last_error}. Set WINTER_FALLBACK_PROVIDERS "
                   "in .env to allow another provider.")
    else:
        message = f"[API Error] Every provider failed. Last error: {last_error}."
    print(f"\n{message}")
    if _ui_queue:
        _ui_queue.put(("info", message))
    return ""
