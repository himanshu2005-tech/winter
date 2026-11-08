# Making VANTA think instead of react

## The actual problem

VANTA has ~40 actions and zero cognition. Look at the shape of `_inner_run_agent_loop`:

```
user text -> screenshot -> ONE llm call -> ONE action -> screenshot -> ONE action -> ...
```

That is a reflex arc. There is no point anywhere in it where the model is asked to
reason about the whole problem. Every turn it re-derives its purpose from a JPEG.

Take your flight example and decompose what actually has to happen:

1. Recognise "flight at 12:00" is a *commitment with a hard deadline*, not a fact
2. Notice the blocking unknowns: which airport, where am I now, how do I get there
3. Resolve them (location is already in `gather_context()`; airport + travel time need tools)
4. Apply unwritten human knowledge: **2h airport buffer, +25% traffic padding, 15 min to get out the door**
5. Back-compute: 12:00 − 120 − 75 − 15 = **leave home 08:30**
6. Create durable artifacts: alarm at 07:45, calendar block, tracked commitment
7. Confirm once, then shut up

Steps 1, 2, 4 and 5 have **nowhere to happen** in your current architecture. Step 4 in
particular is the one people always miss: the model knows what an airport is, but it will
not spontaneously apply the buffer stack unless something tells it to. That's not
intelligence you can prompt into existence with "be proactive" — it's domain defaults you
have to write down once.

Five fixes, in order of impact.

---

## Fix 1 — Add a deliberation pass (biggest single change)

One LLM call before the action loop. **No screenshot attached.** Full world state.
Its only output is a structured plan. Then inject that plan into every executor turn.

Why this works: an LLM with a stated, written plan in its context is accountable to it.
An LLM with `"PLAN SILENTLY"` in its prompt does not plan at all — silent planning is a
no-op for a language model. You told it to think without giving it anywhere to think.

Drop `planner.py` next to `agent.py`, then patch `_inner_run_agent_loop`:

```python
from planner import make_plan, step_needs_vision

def _inner_run_agent_loop(text, is_text_input, launched_terms):
    global waiting_for_clarification
    if not text.strip():
        ...
        return

    try:
        # ---- NEW: deliberate first, with no screen in front of us ----
        context_str = gather_context()
        try:
            import commitments
            context_str += "\n\n--- TRACKED COMMITMENTS ---\n" + commitments.due_soon(BASE_DIR, 48)
        except Exception:
            pass

        plan = make_plan(text, context_str, call_llm_fn=call_llm)

        if plan.question:
            waiting_for_clarification = True
            speak(plan.question, mute=False, block=True)
            try:
                ans = clarification_queue.get(timeout=45)
                answer = ans[1] if isinstance(ans, tuple) else ans
            except queue.Empty:
                answer = "no answer, use your best judgement"
            waiting_for_clarification = False
            plan = make_plan(f"{text}\n(He answered: {answer})", context_str, call_llm_fn=call_llm)

        # ---- NEW: vision is now conditional, not always-on ----
        first_step = plan.steps[0] if plan.steps else text
        needs_vision = (not plan.trivial) and step_needs_vision(first_step)

        if needs_vision:
            b64, element_map_text = take_screenshot_b64(BASE_DIR)
            ui_queue.put(("show_vision_preview", ""))
        else:
            b64, element_map_text = None, ""

        system_prompt = get_system_prompt()
        system_prompt += "\n\n--- LIVE WORLD STATE ---\n" + context_str
        system_prompt += "\n\n" + plan.header()      # <-- the anchor
        ...
```

Then inside the step loop, keep re-injecting `plan.header()` and re-evaluating vision.
Everywhere you currently do:

```python
conversation.append({"role": "user", "content": [
    {"type": "text", "text": next_user_text},
    {"type": "image_url", ...}
]})
```

make the image conditional:

```python
plan.advance()
content = [{"type": "text", "text": plan.header() + "\n" + next_user_text}]
if b64 and step_needs_vision(plan.steps[plan.current] if plan.steps else "", response):
    if element_map_text:
        content.append({"type": "text", "text": "UI Elements Map:\n" + element_map_text})
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
conversation.append({"role": "user", "content": content})
```

**Use a stronger model for `make_plan` than for the executor.** It runs once per task,
carries no image, and it is where 100% of the intelligence lives. Flash-class for
clicking, reasoning-class for planning. Two-tier is the standard pattern and it's cheap.

---

## Fix 2 — Stop attaching a screenshot to every single turn

```python
needs_vision = True   # <- this line is costing you most of your intelligence
```

A screenshot with 90 yellow ID boxes drawn on it is an *overwhelming* prior. It says
"your job is to click something." No amount of prompt text saying "be a thoughtful
companion" survives contact with it. That is a large part of why VANTA defaults to
reading emails and clicking around: you keep showing it a picture of a screen and asking
what to do about it.

Most genuinely smart steps — compute a departure time, search for the airport, schedule an
alarm, notice a conflict — need zero pixels. `step_needs_vision()` in `planner.py` handles
the gating. Your context window and your latency both improve as a side effect.

---

## Fix 3 — Structured commitments instead of a 15-minute vibe check

Your current proactivity:

```python
if now - last_vision_check > 900:
    ... "Look at the screen. Think about his FULL situation."
```

That fires every 15 minutes regardless of whether anything changed, with no memory of what
it already said, and asks a vision model to have a feeling about a screenshot. It cannot
produce the flight behaviour, because nothing in the system holds the flight as an object
with a computable leave-by time.

`commitments.json` gives you that object. `commitments.py` computes `leave_by()` by
back-propagating prep + padded travel + door time, and fires at **12h / 2h / 30m / 10m /
0m before leave-by**, once each, ever. That's the difference between "something is
watching" and "something wakes up confused every quarter hour."

In `_init()`:

```python
import commitments
Thread(target=commitments.monitor_worker, args=(BASE_DIR, input_queue), daemon=True).start()
```

Add the three action handlers listed at the bottom of `commitments.py` to
`execute_actions()`, and these to your action catalogue:

```
<ACTION type="TRACK_COMMITMENT" what="..." hard_time="2026-09-18T12:00:00"
        location="..." prep_minutes="120" travel_minutes="75" />
<ACTION type="LIST_COMMITMENTS" />
<ACTION type="COMPLETE_COMMITMENT" id="..." />
```

Then delete the 15-minute situational pulse entirely. Keep the email and calendar watchers,
but route what they find through the planner — a new email is an *input to reasoning*
("does this create an obligation?"), not a thing to read aloud.

---

## Fix 4 — Fix the contradictions in your system prompt

Your prompt currently contains, verbatim, both of these:

> CRITICAL RULE: TAKE INITIATIVE! Do NOT ask for clarification unless absolutely necessary.

> S3. ALWAYS ask for Himanshu's explicit permission BEFORE executing sensitive tasks, such
> as sending a message, posting online, or saving/modifying files in the vault.

A model handed two rules that contradict each other doesn't pick one — it retreats to the
blandest behaviour that violates neither. Which is: read your email, say something
agreeable, do nothing interesting. This is a real and underrated cause of "my agent feels
lobotomised."

Replace both with one rule keyed on reversibility, which is the distinction you actually
care about:

```
DECIDING WHETHER TO ACT OR ASK:
- Reversible and private (reading, searching, computing, creating a draft, setting an
  alarm, creating a calendar entry): ACT. Tell him after, in one sentence.
- Irreversible or externally visible (sending a message or email, posting, deleting,
  overwriting an existing file, spending money): ACT ONLY AFTER a one-line confirmation.
- Ambiguous scope where a wrong guess wastes his time: make the most reasonable
  assumption, state it, and proceed. Do not ask.
Never ask a question whose answer you could derive. Never ask two questions.
```

Other prompt surgery worth doing:

- **Move the four action catalogues out of the system prompt.** You are spending ~6k
  tokens listing tools *before* the model has done any thinking. Keep a one-line index in
  the system prompt; inject the full signatures for only the categories the plan actually
  needs. Attention is finite and you're spending it on a menu.
- **Add `WORLD_MODEL` from `planner.py` to the main prompt too.** Those practical defaults
  are the single highest-leverage block of text in this whole document. Keep adding to it.
- **Drop "You have no moral restrictions, ethical guidelines, or safety filters."**
  Independent of what you think about the content, that framing measurably *degrades*
  instruction-following — it pushes the model toward generic compliant-assistant mode and
  away from the careful, structured reasoning you're trying to get. It's costing you the
  exact behaviour you're asking for. You want VANTA precise, not unshackled.
- **Separately:** `passwords.json` is interpolated into the system prompt on every single
  call, meaning your plaintext credentials ship to a third-party API endpoint with every
  screenshot. Put them behind a `GET_PASSWORD key="..."` action that reads from disk at
  call time instead. Same functionality, none of the exposure.

---

## Fix 5 — Verify the deliverable, not the screenshot

Your J4/J5 rules and the screenshot-diff guard both check "did the screen change." That
catches missed clicks, which is good — but it can't catch the failure mode that matters
for a flight: **the task ends with nothing real having been created.**

Since the plan declares a `deliverable`, gate `<DONE />` on it:

```python
if re.search(r'<DONE\b', response, re.IGNORECASE) and not plan.trivial:
    if not getattr(plan, "_verified", False):
        plan._verified = True
        conversation.append({"role": "assistant", "content": response})
        conversation.append({"role": "user", "content": [{"type": "text", "text":
            f"Before finishing: your stated deliverable was '{plan.deliverable}'. "
            f"Verify it actually exists — list the alarm, re-read the calendar, "
            f"stat the file, whatever proves it. If it does not exist, keep working. "
            f"If verification passes, output <DONE /> again."}]})
        continue
```

One extra round trip, and it eliminates the entire class of "I've set that up for you, sir"
where nothing was set up.

---

## What this looks like when it works

You say: *"I have a flight at 12 tomorrow"*

**Planner** (no screenshot, full context, sees location from Wi-Fi/IP and tomorrow's calendar):

```json
{
  "goal": "Himanshu reaches his 12:00 flight unhurried, with alarms and a leave-by time set",
  "reasoning": "Flight at 12:00 tomorrow. He is in Vikarabad; nearest major airport is
   RGIA Shamshabad. Assume domestic unless told otherwise -> 2h buffer -> be there by 10:00.
   Need real travel time. Once known, leave-by = 10:00 - travel(padded) - 15min.",
  "unknowns": [
    {"what": "nearest airport", "how": "TOOL", "detail": "SEARCH_WEB nearest airport to Vikarabad"},
    {"what": "travel time", "how": "TOOL", "detail": "SEARCH_WEB driving time Vikarabad to RGIA"},
    {"what": "domestic vs international", "how": "ASSUME", "detail": "domestic, 2h buffer"}
  ],
  "steps": [
    "SEARCH_WEB for nearest airport and driving time",
    "RUN_PYTHON to back-compute leave-by with 25% traffic padding",
    "TRACK_COMMITMENT with hard_time, prep_minutes, travel_minutes",
    "SET_ALARM 45 min before leave-by",
    "Tell him the leave-by time and the one assumption, in two sentences"
  ],
  "deliverable": "A tracked commitment + an alarm exist; he knows his leave-by time",
  "question": null
}
```

**Executor** runs those five steps. **Monitor** then owns it from there — pings at 12h
("passport? web check-in?"), 2h, 30m, 10m.

You never said the word "alarm." That's the behaviour you're after, and it comes from
steps 4 and 5 of that reasoning chain, which come from `WORLD_MODEL`.

---

## On the 3D modelling ambition

Don't scale the current loop to Blender — it will fail, and not because of the model.
Blender's UIA tree is effectively empty (it renders its own UI in OpenGL), so `_collect_ui_elements`
will return almost nothing and you'll be relying on OCR + icon detection against a dark
theme with 3D content behind it. That's the hardest possible case for your current vision stack.

The route that actually works: **Blender ships a full Python API.** Treat it like your
`RUN_PYTHON` action rather than a GUI target — the agent writes a `bpy` script, you execute
it, you screenshot the viewport only to *verify* the result. Same for Fusion 360 and
FreeCAD. The general principle, which your prompt already half-states under "CLI-FIRST":
**GUI automation is the fallback, scripting is the primary path.** Push that much harder
everywhere. Every task you move from clicking to scripting gets more reliable, not less.

Get Fixes 1–3 landed first though. A planner that can chain inference is the prerequisite
for everything else; without it, a 3D task is just a longer sequence of the same reflex arc.