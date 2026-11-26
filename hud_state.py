# -*- coding: utf-8 -*-
"""
hud_state.py -- what is on the screen overlay, as data rather than widgets.

VANTA had three ways to put something on screen and none of them fit "show me a
todo while I study and tick it off as I go":

* the blackboard takes the whole display, which is right for teaching a subject
  and wrong for a list that sits beside the work;
* Control Center panels live inside a window you have to open, so they are
  invisible exactly when they are wanted;
* the notch shows one line of status.

The missing surface is a small persistent card that stays visible over whatever
he is doing and can be changed mid-task -- a checklist whose items get ticked
while he works, a figure to glance at, a count that goes down.

This file is that card's state and nothing else. The widgets live in hud.py.
The split is deliberate: the interesting failures here are logical -- ticking
the wrong item, losing the list on restart, a model sending the same item
twice -- and keeping them out of the rendering code means they can be tested
without a screen, which is the only way they get tested at all.

MATCHING AN ITEM BY WHAT HE CALLED IT
-------------------------------------
"I finished the derivatives one" has to tick an item called "Revise
derivatives and the chain rule". Requiring an exact string or an index would
push the work back onto him, which is the opposite of the point, so matching is
by overlap: unambiguous overlap ticks, ambiguity refuses and says which ones
matched rather than guessing between them.
"""

import json
import os
import threading
import time

MAX_CARDS = 4
MAX_ITEMS = 24
MAX_TEXT = 160

_lock = threading.RLock()
_cards = {}
_order = []


def store_path(base_dir):
    return os.path.join(base_dir, "vault", "system", "hud.json")


def _clean(value, limit=MAX_TEXT):
    text = " ".join(str(value or "").split())
    return text[:limit]


# Filler words must not decide which line gets ticked.
#
# Length alone was the filter, and "the" is three characters: asked to tick
# "the review", the overlap scored "Read the RL review slides" at two ("the"
# plus "review") against one for the actual review items, so a filler word
# chose the wrong line and did it confidently. Short words are still dropped;
# these are dropped regardless of length.
_FILLER = frozenset("""
the and for with that this from into onto are was were has have had been being
all any its out off per via not nor but yet than then thus also both each few
more most other some such only own same too very can will just don now
done finish finished complete completed completing tick ticked mark marked
did doing made make making got get getting
""".split())


def _tokens(text):
    return {word for word in _clean(text).lower().replace("-", " ").split()
            if len(word) > 2 and word not in _FILLER}


# ------------------------------------------------------------------ #
# Reading and writing the card set
# ------------------------------------------------------------------ #

def show(base_dir, title, items, card_id=None, kind="checklist", payload=None):
    """Put a card on screen, replacing one with the same id.

    Returns the card. Replacing by id rather than appending matters: a model
    asked to "update the list" will send the whole list again, and three
    near-identical study checklists stacked on screen is worse than none.
    """
    with _lock:
        identifier = _clean(card_id or title or "card", 60).lower() or "card"
        entries = []
        for raw in list(items or [])[:MAX_ITEMS]:
            if isinstance(raw, dict):
                text = _clean(raw.get("text") or raw.get("label"))
                done = bool(raw.get("done"))
            else:
                text = _clean(raw)
                done = False
            if text:
                entries.append({"text": text, "done": done})
        card = {
            "id": identifier,
            "title": _clean(title, 80),
            "kind": str(kind or "checklist"),
            "items": entries,
            "updated": time.time(),
        }
        # A route card carries geometry rather than a list of lines. Kept as an
        # opaque payload so this file stays about cards and ticking, and knows
        # nothing about GeoJSON.
        if payload is not None:
            card["payload"] = payload
        _cards[identifier] = card
        if identifier in _order:
            _order.remove(identifier)
        _order.append(identifier)
        # Oldest goes first. A screen with four cards on it is already busy.
        while len(_order) > MAX_CARDS:
            _cards.pop(_order.pop(0), None)
        _save(base_dir)
        return dict(card)


def cards():
    with _lock:
        return [dict(_cards[key], items=list(_cards[key]["items"]))
                for key in _order if key in _cards]


def card(card_id):
    """One card by id, or None."""
    with _lock:
        found = _cards.get(_clean(card_id, 60).lower())
        return dict(found, items=list(found["items"])) if found else None


def find_item(card, phrase):
    """Which item he means, or a list of the ones that are ambiguous.

    Returns (index, candidates). index is None when nothing or too much
    matched, and candidates says what it was torn between -- so the reply can
    name them instead of silently ticking the wrong line.
    """
    wanted = _tokens(phrase)
    if not wanted:
        return None, []
    exact = [i for i, item in enumerate(card["items"])
             if _clean(item["text"]).lower() == _clean(phrase).lower()]
    if len(exact) == 1:
        return exact[0], []

    scored = []
    for index, item in enumerate(card["items"]):
        overlap = wanted & _tokens(item["text"])
        if overlap:
            scored.append((len(overlap), index))
    if not scored:
        return None, []
    scored.sort(reverse=True)
    best = scored[0][0]
    tied = [index for score, index in scored if score == best]
    if len(tied) == 1:
        return tied[0], []
    return None, [card["items"][index]["text"] for index in tied]


def check(base_dir, phrase, done=True, card_id=None):
    """Tick (or untick) the item he just named. Returns a receipt string."""
    with _lock:
        if not _order:
            return "[Error] Nothing is on screen to tick."
        targets = ([_cards[card_id.lower()]]
                   if card_id and card_id.lower() in _cards
                   else [_cards[key] for key in reversed(_order)])

        # Scored across every card at once, not card by card.
        #
        # Taking the first card with a unique match meant an ambiguous newest
        # card fell through to an older one -- and "the review", torn between
        # two review items, silently ticked "Read the RL review slides" on a
        # different list. Falling back to a less likely card to find a unique
        # answer is exactly the wrong-line failure this is meant to prevent.
        wanted = _tokens(phrase)
        exact = _clean(phrase).lower()
        scored = []
        for card in targets:
            for index, item in enumerate(card["items"]):
                text = _clean(item["text"]).lower()
                if text == exact:
                    scored.append((10_000, card, index, item))
                    continue
                overlap = wanted & _tokens(item["text"])
                if overlap:
                    scored.append((len(overlap), card, index, item))
        if not scored:
            return f"[Error] Nothing on screen matches {_clean(phrase)!r}."

        best = max(row[0] for row in scored)
        tied = [row for row in scored if row[0] == best]
        if len(tied) > 1:
            # Guessing between two plausible items is how the wrong thing gets
            # marked done and the real one forgotten.
            names = "; ".join(repr(row[3]["text"]) for row in tied[:4])
            return ("[Error] That could be any of these, so nothing was "
                    f"ticked: {names}. Say which one.")

        _score, card, index, item = tied[0]
        if item["done"] == done:
            state = "already ticked" if done else "not ticked anyway"
            return f"[Success] {item['text']!r} was {state}."
        item["done"] = done
        card["updated"] = time.time()
        _save(base_dir)
        remaining = sum(1 for one in card["items"] if not one["done"])
        return (f"[Success] Ticked {item['text']!r}. "
                f"{remaining} left on {card['title']!r}.")


def hide(base_dir, card_id=None):
    with _lock:
        if card_id:
            key = _clean(card_id, 60).lower()
            if _cards.pop(key, None) is None:
                return f"[Error] No card called {key!r} is on screen."
            if key in _order:
                _order.remove(key)
            _save(base_dir)
            return f"[Success] Removed {key!r} from the screen."
        _cards.clear()
        _order.clear()
        _save(base_dir)
        return "[Success] Screen cleared."


def summary():
    """One line for the model, so it knows what he can already see."""
    with _lock:
        if not _order:
            return ""
        lines = ["--- ON HIS SCREEN ---"]
        for key in _order:
            card = _cards[key]
            if card["items"]:
                done = sum(1 for item in card["items"] if item["done"])
                lines.append(f"{card['title']} ({done}/{len(card['items'])} done)")
                for item in card["items"]:
                    mark = "x" if item["done"] else " "
                    lines.append(f"  [{mark}] {item['text']}")
            elif card.get("kind") == "route":
                try:
                    import route_render
                    caption = route_render.describe(card.get("payload") or {})
                except Exception:
                    caption = ""
                lines.append(f"{card['title']} (a route is drawn on screen"
                             + (f": {caption}" if caption else "") + ")")
            else:
                lines.append(card["title"])
        lines.append("He can see this now, so refer to it rather than "
                     "repeating it, and tick items as he reports them.")
        return "\n".join(lines)


# ------------------------------------------------------------------ #
# Persistence -- a study list should survive a restart
# ------------------------------------------------------------------ #

def _save(base_dir):
    if not base_dir:
        return
    path = store_path(base_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"order": _order, "cards": _cards}, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[HUD] Could not save: {exc}")
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load(base_dir):
    """Restore what was on screen. A half-done list is worth keeping."""
    global _cards, _order
    try:
        with open(store_path(base_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    with _lock:
        cards_in = data.get("cards") if isinstance(data, dict) else None
        order_in = data.get("order") if isinstance(data, dict) else None
        if not isinstance(cards_in, dict) or not isinstance(order_in, list):
            return 0
        # A route is a moment, not a to-do list: it described a journey that
        # was being discussed, and showing it at the next launch put an old
        # map on screen before anything had been asked. Checklists persist; a
        # route card does not.
        _cards = {key: value for key, value in cards_in.items()
                  if isinstance(value, dict) and value.get("kind") != "route"}
        _order = [key for key in order_in if key in _cards]
        return len(_order)


def reset():
    """For tests. Does not touch the stored file."""
    global _cards, _order
    with _lock:
        _cards, _order = {}, []
