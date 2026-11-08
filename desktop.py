# -*- coding: utf-8 -*-
"""
desktop.py — native Windows control for VANTA.

Desktop mutation was disabled after input landed in the wrong application and
browser retries opened duplicate windows. The fix is not to click more
carefully; it is to stop clicking blind. Three things make that possible here:

1. TARGET BY NAME, NOT PIXELS. `uiautomation` finds a control by its accessible
   name and clicks its real centre. A coordinate is a guess about where
   something was when a screenshot was taken; a control reference is the thing
   itself, and it fails loudly instead of clicking whatever moved into that
   spot.

2. VERIFY FOCUS BEFORE TYPING. Keystrokes go wherever focus happens to be. Every
   text-entry call can assert which window it expects, and refuses if the
   foreground window changed. This is what actually caused the original damage.

3. NEVER ACT ON PROTECTED WINDOWS. VANTA's own UI and anything that looks like
   a credential prompt are refused outright, so it cannot type into its own
   dashboard or into a password box.

Everything returns a string starting with "[Error]" on failure, matching the
action contract in actions.py.
"""

import re
import time
import json
import os
import shutil
import math

import pyautogui

import vision
from vision import get_virtual_screen_bounds

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

_SENSITIVE_HINTS = (
    "password", "passcode", "pin", "cvv", "otp", "secret", "credential",
    "card number", "security code",
)


def _uia():
    import uiautomation
    return uiautomation


def foreground_window():
    """(hwnd, title) of the window currently in front."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        return hwnd, win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return 0, ""


def is_winter_own_window(hwnd, title=""):
    """VANTA's actual UI, by handle and window class.

    Deliberately narrower than vision.is_protected_window, which also hides
    editors and anything with 'vanta' in the title so screenshots never leak
    source code. That breadth is right for capture and wrong for input: it
    would refuse to control Himanshu's own editor because the project folder
    happens to be named vanta.
    """
    try:
        if hwnd and hwnd in getattr(vision, "_WINTER_HWNDS", set()):
            return True
        import win32gui
        if hwnd and win32gui.GetClassName(hwnd).lower().startswith(("tk", "toplevel")):
            return True
    except Exception:
        pass
    return (title or "").strip().upper() == "VANTA"


def _guard(expect_window=""):
    """Refuse to act on VANTA itself or a window we were not aimed at."""
    hwnd, title = foreground_window()
    if is_winter_own_window(hwnd, title):
        return (
            f"[Error] Refused: the foreground window ('{title or 'unknown'}') is "
            "VANTA's own interface."
        )
    if expect_window:
        if expect_window.strip().casefold() not in title.casefold():
            return (
                f"[Error] Refused: expected to act on a window matching "
                f"'{expect_window}' but the foreground window is '{title}'. "
                "Focus the right window first, then retry."
            )
    return None


def _clamp(x, y):
    vx, vy, vw, vh = get_virtual_screen_bounds()
    return max(vx, min(int(x), vx + vw - 1)), max(vy, min(int(y), vy + vh - 1))


def to_screen(x, y, space="image"):
    """Map a coordinate the model read off a screenshot onto the real screen.

    Screenshots are cropped to the active window and then downscaled to fit the
    model's input width, so a point in that image is in neither screen
    coordinates nor native window coordinates. Clicking the raw number was out
    by the scale factor plus the window origin — on a maximised 1938px window
    downscaled to 1366px that is roughly 158px horizontally and 116px
    vertically, which lands on a different control entirely.

        screen = capture_origin + image_coordinate / send_scale

    `space="screen"` skips the transform, for coordinates that already came
    from the desktop itself (a window rectangle, a previous INSPECT_POINT).
    """
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return _clamp(0, 0)
    if str(space).lower().startswith("screen"):
        return _clamp(x, y)

    import vision
    scale = vision.get_send_scale() or 1.0
    origin_x, origin_y = vision.get_capture_origin()
    return _clamp(origin_x + x / scale, origin_y + y / scale)


def describe_point(x, y):
    """What is actually under a screen coordinate."""
    try:
        auto = _uia()
        control = auto.ControlFromPoint(int(x), int(y))
        if control is None:
            return "nothing identifiable"
        name = (control.Name or "").strip()
        kind = control.ControlTypeName.replace("Control", "")
        return f"{kind}" + (f" '{name[:60]}'" if name else " (unnamed)")
    except Exception:
        return "nothing identifiable"


# ------------------------------------------------------------------ #
# Pointer
# ------------------------------------------------------------------ #

def click(x, y, button="left", clicks=1, expect_window="", space="image"):
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    sx, sy = to_screen(x, y, space)
    # Report what was actually under the cursor. A click that lands on the
    # wrong control otherwise reports success and the model believes it.
    target = describe_point(sx, sy)
    pyautogui.click(x=sx, y=sy, button=button, clicks=int(clicks))
    time.sleep(0.2)
    _, title = foreground_window()
    return (f"[Success] {button} click x{clicks} at screen ({sx}, {sy}) "
            f"[from {space} ({x}, {y})]. Hit: {target}. Foreground now: '{title}'.")


def move_mouse(x, y, space="image"):
    sx, sy = to_screen(x, y, space)
    pyautogui.moveTo(sx, sy)
    return f"[Success] Pointer at screen ({sx}, {sy}). Under it: {describe_point(sx, sy)}."


def inspect_point(x, y, space="image"):
    """Look before clicking. The way to hit a small target reliably."""
    sx, sy = to_screen(x, y, space)
    from PIL import ImageGrab
    try:
        pixel = ImageGrab.grab(bbox=(sx, sy, sx + 1, sy + 1)).getpixel((0, 0))
        colour = "#%02x%02x%02x" % pixel[:3]
    except Exception:
        colour = "unknown"
    return (f"At screen ({sx}, {sy}) [from {space} ({x}, {y})]: "
            f"{describe_point(sx, sy)}, pixel {colour}.")


def _visual_signature():
    """Return a small in-memory image of the foreground window."""
    try:
        from PIL import ImageGrab
        import win32gui
        hwnd, title = foreground_window()
        if not hwnd or win32gui.IsIconic(hwnd):
            return None, title
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return None, title
        frame = ImageGrab.grab(
            bbox=(left, top, right, bottom), all_screens=True)
        frame = frame.convert("L").resize((96, 54))
        return bytes(frame.tobytes()), title
    except Exception:
        return None, foreground_window()[1]


def _signature_change(before, after):
    if not before or not after or len(before) != len(after):
        return None
    return sum(abs(a - b) for a, b in zip(before, after)) / (
        255.0 * len(before)) * 100.0


def drag(x1, y1, x2, y2, duration=0.4, expect_window="", space="image",
         button="left", modifiers="", interrupt_event=None):
    """Perform a guarded, interruptible pointer gesture.

    Beyond ordinary left drags this supports custom-rendered canvases:
    Blender-style middle-button orbit, shift+middle pan, ctrl+middle zoom,
    and right-button strokes. Held inputs are always released on Abort.
    """
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    start_x, start_y = to_screen(x1, y1, space)
    end_x, end_y = to_screen(x2, y2, space)
    button = str(button or "left").strip().lower()
    if button not in {"left", "middle", "right"}:
        return "[Error] DRAG button must be left, middle, or right."
    held = []
    aliases = {"control": "ctrl", "option": "alt", "windows": "win"}
    for item in re.split(r"[+,\s]+", str(modifiers or "").lower()):
        item = aliases.get(item, item)
        if item and item in {"ctrl", "shift", "alt", "win"} and item not in held:
            held.append(item)
    try:
        seconds = max(0.05, min(float(duration), 10.0))
    except (TypeError, ValueError):
        seconds = 0.4

    before, before_title = _visual_signature()
    cancelled = False
    try:
        pyautogui.moveTo(start_x, start_y)
        for key in held:
            pyautogui.keyDown(key)
        pyautogui.mouseDown(button=button)
        # Time slices keep long canvas gestures responsive to Abort.
        steps = max(2, min(120, int(math.ceil(seconds / 0.02))))
        started = time.monotonic()
        for index in range(1, steps + 1):
            if interrupt_event is not None and interrupt_event.is_set():
                cancelled = True
                break
            ratio = index / steps
            pyautogui.moveTo(
                round(start_x + (end_x - start_x) * ratio),
                round(start_y + (end_y - start_y) * ratio),
                duration=0)
            remaining = started + seconds * ratio - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.03))
    finally:
        # Never leave an application with a logically stuck key or button.
        try:
            pyautogui.mouseUp(button=button)
        finally:
            for key in reversed(held):
                pyautogui.keyUp(key)

    if cancelled:
        return (f"[Cancelled] Released {button} button and modifiers during "
                f"the gesture at {pyautogui.position()}.")
    time.sleep(0.15)
    after, after_title = _visual_signature()
    changed = _signature_change(before, after)
    modifier_text = "+".join(held) + "+" if held else ""
    evidence = (f" Foreground: '{after_title or before_title}'."
                if changed is None else
                f" Foreground: '{after_title or before_title}'; visual change "
                f"{changed:.2f}%.")
    if changed is not None and changed < 0.02:
        return (f"[Warning] Sent {modifier_text}{button} drag from "
                f"({start_x}, {start_y}) to ({end_x}, {end_y}), but no "
                f"measurable visual change followed.{evidence}")
    return (f"[Success] {modifier_text}{button} drag from ({start_x}, "
            f"{start_y}) to ({end_x}, {end_y}).{evidence}")


def scroll(amount=-500, x=None, y=None, space="image"):
    if x is not None and y is not None:
        mx, my = to_screen(x, y, space)
        pyautogui.moveTo(mx, my)
    pyautogui.scroll(int(amount))
    return f"[Success] Scrolled {int(amount)}."


# ------------------------------------------------------------------ #
# Control targeting (preferred over coordinates)
# ------------------------------------------------------------------ #

def _walk(root, depth, limit=1200):
    """Breadth-first walk of the UI tree, bounded so a deep app cannot hang us."""
    seen = 0
    queue = [(root, 0)]
    while queue and seen < limit:
        node, level = queue.pop(0)
        if level > depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        for child in children:
            seen += 1
            yield child
            queue.append((child, level + 1))


def find_control(name, control_type="", scope="foreground", depth=12):
    """Locate a control by visible name. Returns (control, error)."""
    auto = _uia()
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None, "[Error] A control name is required."
    try:
        root = auto.GetForegroundControl() if scope == "foreground" else auto.GetRootControl()
    except Exception as exc:
        return None, f"[Error] Could not read the UI tree: {exc}"

    exact, partial = [], []
    for node in _walk(root, depth):
        try:
            label = (node.Name or "").strip()
            if not label:
                continue
            if control_type and control_type.lower() not in node.ControlTypeName.lower():
                continue
            if not node.IsEnabled:
                continue
            low = label.casefold()
            if low == wanted:
                exact.append((node, label))
            elif wanted in low:
                partial.append((node, label))
        except Exception:
            continue
        if len(exact) >= 3:
            break

    matches = exact or partial
    if len({node.ControlTypeName for node, _ in exact}) > 1 and not control_type:
        # "Replace" in Notepad is an edit box, a split button, a button and a
        # label at once. Picking whichever the tree lists first clicked the
        # edit box when the button was meant -- a wrong target reported as a
        # success. Say which kinds exist and make the caller choose.
        kinds = ", ".join(sorted({node.ControlTypeName.replace("Control", "")
                                  for node, _ in exact}))
        return None, (
            f"[Error] '{name}' names more than one kind of control here ({kinds}). "
            "Say which with control_type, e.g. control_type=\"Button\".")
    if not matches:
        return None, (
            f"[Error] No enabled control named '{name}' in the foreground window. "
            "Use LIST_CONTROLS to see what is actually available."
        )
    if len(matches) > 1 and not exact:
        names = ", ".join(sorted({label for _, label in matches[:6]}))
        return None, (
            f"[Error] '{name}' is ambiguous — it matches: {names}. "
            "Use the exact name."
        )
    return matches[0][0], None


def active_browser_url():
    """Read the foreground browser address without focusing or typing."""
    hwnd, title = foreground_window()
    if not hwnd or is_winter_own_window(hwnd, title):
        return "[Error] Your browser is not the foreground window."
    try:
        root = _uia().GetForegroundControl()
    except Exception as exc:
        return f"[Error] Could not inspect the foreground window: {exc}"
    hints = ("address and search bar", "address bar",
             "search or enter web address", "omnibox", "url bar")
    candidates = []
    for node in _walk(root, depth=8, limit=500):
        try:
            if "edit" not in (node.ControlTypeName or "").lower():
                continue
            name = (node.Name or "").strip().casefold()
            pattern = node.GetValuePattern()
            value = str(pattern.Value or "").strip() if pattern else ""
            looks_named = any(hint in name for hint in hints)
            looks_url = re.match(r"^(?:https?://|www\.)", value, re.I)
            if looks_named or looks_url:
                candidates.append((0 if looks_url else 1, value))
        except Exception:
            continue
    from urllib.parse import urlparse
    for _, value in sorted(candidates):
        if not value:
            continue
        candidate = value if re.match(r"^https?://", value, re.I) else "https://" + value
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
    return ("[Error] I could not read an exact URL from the active window. "
            "Copy the product link and ask me to watch that link.")


def click_control(name, control_type="", expect_window=""):
    """Click a control by its name rather than a remembered coordinate."""
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    control, error = find_control(name, control_type)
    if error:
        return error
    try:
        label = control.Name
        try:
            control.SetFocus()
        except Exception:
            pass
        control.Click(simulateMove=False)
        time.sleep(0.2)
        _, title = foreground_window()
        return f"[Success] Clicked '{label}'. Foreground now: '{title}'."
    except Exception as exc:
        return f"[Error] Could not click '{name}': {exc}"


def list_controls(limit=40, control_type=""):
    """What is actually clickable right now, by name."""
    auto = _uia()
    try:
        root = auto.GetForegroundControl()
    except Exception as exc:
        return f"[Error] Could not read the UI tree: {exc}"
    _, title = foreground_window()
    rows, seen = [], set()
    for node in _walk(root, depth=12):
        try:
            label = (node.Name or "").strip()
            kind = node.ControlTypeName.replace("Control", "")
            if not label or not node.IsEnabled:
                continue
            if control_type and control_type.lower() not in kind.lower():
                continue
            key = (label, kind)
            if key in seen:
                continue
            seen.add(key)
            rows.append(f"- [{kind}] {label}")
        except Exception:
            continue
        if len(rows) >= int(limit):
            break
    if not rows:
        return f"No named controls found in '{title}'."
    return f"Controls in '{title}':\n" + "\n".join(rows)


# ------------------------------------------------------------------ #
# Keyboard
# ------------------------------------------------------------------ #

def _focused_value():
    """The text the focused control currently holds, or None if unreadable.

    Used to check what typing actually achieved. Many controls -- a terminal,
    a canvas, a custom-drawn editor -- expose no value pattern, and None is
    the honest answer there: the typing happened but cannot be confirmed,
    which is different from confirming it worked.
    """
    try:
        node = _uia().GetFocusedControl()
        if node is None:
            return None
        return str(node.GetValuePattern().Value)
    except Exception:
        return None


def _focused_field_is_sensitive():
    try:
        auto = _uia()
        node = auto.GetFocusedControl()
        if node is None:
            return False, ""
        label = (node.Name or "")
        try:
            if node.GetValuePattern() and getattr(node, "IsPassword", False):
                return True, label
        except Exception:
            pass
        if "password" in node.ControlTypeName.lower():
            return True, label
        low = label.casefold()
        if any(hint in low for hint in _SENSITIVE_HINTS):
            return True, label
    except Exception:
        return False, ""
    return False, ""


def type_text(text, expect_window="", into=""):
    """Type into the focused field, or into a named field first."""
    if not text:
        return "[Error] Nothing to type."
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    if into:
        control, error = find_control(into, "Edit")
        if error:
            control, error = find_control(into)
        if error:
            return error
        try:
            control.SetFocus()
            time.sleep(0.1)
        except Exception as exc:
            return f"[Error] Could not focus '{into}': {exc}"

    sensitive, label = _focused_field_is_sensitive()
    if sensitive:
        return (
            f"[Error] Refused: the focused field ('{label or 'unnamed'}') looks like a "
            "password or other credential input. Himanshu types those himself."
        )
    wanted = str(text)
    before = _focused_value()
    pyautogui.typewrite(wanted, interval=0.01)
    _, title = foreground_window()

    # Read the field back. The receipt used to be len(text), which reports
    # what was asked for and therefore cannot be wrong -- so when a keystroke
    # was dropped ("desktop" arriving as "esktop") it still said success, and
    # the next step built on text that was quietly corrupt. Measured over
    # twelve runs the loss is intermittent, which is exactly why the claim has
    # to be checked rather than assumed.
    after = _focused_value()
    if after is None:
        return (f"[Success] Typed {len(wanted)} characters into '{title}' "
                f"(unverified: this field does not expose its text).")
    landed = after
    if before and landed.startswith(before):
        landed = landed[len(before):]
    if wanted in after or landed == wanted:
        return f"[Success] Typed {len(wanted)} characters into '{title}'."
    return (f"[Error] Typing did not land correctly in '{title}'. "
            f"Asked to type {wanted!r} but the field now reads {landed!r}. "
            f"Clear the field and try again, or use SET_FIELD, which writes "
            f"the value directly instead of depending on keystrokes.")


def set_value(name, value, expect_window=""):
    """Set a field directly instead of typing into it.

    Typing depends on focus landing where you think it will and on nothing
    stealing it mid-string. Setting the value through the control's own
    pattern is atomic, survives autocomplete, and either works or reports
    that it could not.
    """
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    control, error = find_control(name, "Edit")
    if error:
        control, error = find_control(name)
    if error:
        return error
    label = control.Name
    low = (label or "").casefold()
    if any(hint in low for hint in _SENSITIVE_HINTS):
        return (
            f"[Error] Refused: '{label}' looks like a credential field. "
            "Himanshu fills those himself."
        )
    wanted = str(value)

    def visible_value():
        """What the field reads as on a fresh look, the way READ_FORM sees it.

        Checking the control object we already hold is not good enough. On the
        Save As dialog, SetValue on the File name edit updates that edit's own
        ValuePattern -- it reads back perfectly -- while the dialog goes on to
        save under the name it had before. Re-finding the control is what
        shows the value the dialog will actually use.
        """
        fresh, problem = find_control(name, "Edit")
        if problem:
            fresh, problem = find_control(name)
        if problem or fresh is None:
            return None
        try:
            return str(fresh.GetValuePattern().Value)
        except Exception:
            return None

    def took():
        seen = visible_value()
        return seen is not None and seen.strip() == wanted.strip()

    # Typing first. The original reasoning here was that setting a value is
    # atomic while typing depends on focus -- true in general, and wrong for
    # the case that matters most. A file dialog opens with the name field
    # already focused, and typing is the only thing that reaches it; SetValue
    # silently does not, however convincingly it reports otherwise.
    typed_error = None
    try:
        control.SetFocus()
        time.sleep(0.12)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite(wanted, interval=0.01)
        time.sleep(0.25)
        if took():
            return f"[Success] Typed '{wanted[:60]}' into '{label}'."
    except Exception as exc:
        typed_error = exc

    # Then the control's own pattern, for anything that cannot take focus.
    try:
        control.GetValuePattern().SetValue(wanted)
        time.sleep(0.2)
        if took():
            return f"[Success] Set '{label}' to '{wanted[:60]}'."
    except Exception as exc:
        if typed_error is not None:
            return f"[Error] Could not set '{label}': {exc}"

    seen = visible_value()
    if seen is None:
        return (f"[Warning] Sent '{wanted[:60]}' to '{label}', but the field "
                "exposes no readable value, so this is unconfirmed. Check with "
                "READ_FORM before relying on it.")
    return (f"[Error] '{label}' still reads '{seen[:60]}' after trying to set "
            f"it to '{wanted[:60]}'. The value did not take.")


def toggle(name, checked=True, expect_window=""):
    """Tick or untick a checkbox / select a radio button."""
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    control, error = find_control(name, "Check")
    if error:
        control, error = find_control(name, "Radio")
    if error:
        control, error = find_control(name)
    if error:
        return error
    label = control.Name
    try:
        try:
            pattern = control.GetTogglePattern()
            state = pattern.ToggleState  # 0 off, 1 on
            if bool(state) != bool(checked):
                pattern.Toggle()
            return f"[Success] '{label}' is now {'checked' if checked else 'unchecked'}."
        except Exception:
            control.GetSelectionItemPattern().Select()
            return f"[Success] Selected '{label}'."
    except Exception as exc:
        return f"[Error] Could not toggle '{label}': {exc}"


def select_option(name, value, expect_window=""):
    """Choose an item in a combo box or list."""
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    control, error = find_control(name, "Combo")
    if error:
        control, error = find_control(name, "List")
    if error:
        control, error = find_control(name)
    if error:
        return error
    label = control.Name
    try:
        try:
            control.GetExpandCollapsePattern().Expand()
            time.sleep(0.25)
        except Exception:
            pass
        wanted = str(value).strip().casefold()
        for item in _walk(control, depth=4, limit=300):
            try:
                if (item.Name or "").strip().casefold() == wanted:
                    item.GetSelectionItemPattern().Select()
                    return f"[Success] Selected '{item.Name}' in '{label}'."
            except Exception:
                continue
        try:
            control.GetValuePattern().SetValue(str(value))
            return f"[Success] Set '{label}' to '{value}'."
        except Exception:
            pass
        return f"[Error] '{value}' is not an option in '{label}'. Use LIST_CONTROLS to see the choices."
    except Exception as exc:
        return f"[Error] Could not use '{label}': {exc}"


def read_form(limit=40):
    """Every field in the foreground window with its current value."""
    auto = _uia()
    try:
        root = auto.GetForegroundControl()
    except Exception as exc:
        return f"[Error] Could not read the UI tree: {exc}"
    _, title = foreground_window()
    rows = []
    for node in _walk(root, depth=14, limit=2500):
        try:
            kind = node.ControlTypeName.replace("Control", "")
            if kind not in ("Edit", "ComboBox", "CheckBox", "RadioButton", "List",
                            "Slider", "Document", "Spinner"):
                continue
            label = (node.Name or "").strip() or "(unnamed)"
            state = ""
            try:
                state = f" = '{node.GetValuePattern().Value[:60]}'"
            except Exception:
                try:
                    state = " = checked" if node.GetTogglePattern().ToggleState else " = unchecked"
                except Exception:
                    state = ""
            rows.append(f"- [{kind}] {label}{state}")
        except Exception:
            continue
        if len(rows) >= int(limit):
            break
    if not rows:
        return f"No form fields found in '{title}'."
    return f"Fields in '{title}':\n" + "\n".join(rows)


def fill_form(pairs, expect_window=""):
    """Fill several native fields in one go. pairs: list of (name, value, kind)."""
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    done, failed = [], []
    for name, value, kind in pairs:
        if kind == "check":
            result = toggle(name, str(value).lower() not in {"false", "0", "no"})
        elif kind == "select":
            result = select_option(name, value)
        else:
            result = set_value(name, value)
        (failed if result.startswith("[Error]") else done).append(
            f"{name}" if not result.startswith("[Error]") else f"{name} ({result[8:90]})")
    parts = []
    if done:
        parts.append(f"[Success] Filled: {', '.join(done)}.")
    if failed:
        parts.append(f"Could not fill: {'; '.join(failed)}.")
    return " ".join(parts) or "[Error] No fields were given."


# Combinations VANTA must never synthesize. Ctrl+Alt+Delete is a Secure
# Attention Sequence that Windows will not deliver from a normal process
# anyway, so sending it is at best a no-op and at worst confusing; Alt+F4 kills
# the foreground app and can discard unsaved work that CLOSE_WINDOW would have
# prompted about.
_PROTECTED_COMBOS = {
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "shift", "esc"}),
    frozenset({"ctrl", "shift", "escape"}),
    # This is VANTA's own emergency shutdown chord. Letting a model synthesize
    # it bypasses the SHUTDOWN action's approval gate and can terminate the
    # assistant while it is merely trying browser keyboard shortcuts.
    frozenset({"ctrl", "alt", "f12"}),
}


#: VANTA's own Ctrl+Alt+<letter> chords: O (Owner Override), L (Lockdown),
#: P (Public Space), E (end protocol). The keyboard listener cannot tell a
#: synthesised keystroke from a real one, so a model that pressed Ctrl+Alt+O
#: would confirm itself as the owner and lift the guest-session limits. Any
#: combination holding Ctrl and Alt with one of these letters is refused,
#: whatever other modifiers ride along.
_OWN_CHORD_LETTERS = frozenset({"o", "l", "p", "e"})


def _protected_combo(keys):
    held = frozenset(keys)
    own_chord = {"ctrl", "alt"} <= held and bool(held & _OWN_CHORD_LETTERS)
    if own_chord or held in _PROTECTED_COMBOS:
        pressed = "+".join(keys)
        return (
            f"[Unsupported Action] {pressed} is a protected system combination and "
            "was not sent. Use CLOSE_WINDOW to close an application, LOCK_SCREEN to "
            "lock, or LIST_PROCESSES to inspect what is running."
        )
    return None


_KEY_ALIASES = {
    "return": "enter", "esc": "escape", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown", "ins": "insert",
}


def _normalise_keys(spec):
    parts = [p.strip().lower() for p in re.split(r"[+\s,]+", str(spec or "")) if p.strip()]
    return [_KEY_ALIASES.get(p, p) for p in parts]


def press_key(key, times=1, expect_window=""):
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    keys = _normalise_keys(key)
    if not keys:
        return "[Error] No key given."
    protected = _protected_combo(keys)
    if protected:
        return protected
    for _ in range(max(1, int(times))):
        pyautogui.press(keys[0] if len(keys) == 1 else keys)
    return f"[Success] Pressed {'+'.join(keys)} x{max(1, int(times))}."


def hotkey(combo, expect_window=""):
    blocked = _guard(expect_window)
    if blocked:
        return blocked
    keys = _normalise_keys(combo)
    if len(keys) < 2:
        return "[Error] A hotkey needs at least two keys, for example 'ctrl+s'."
    protected = _protected_combo(keys)
    if protected:
        return protected
    pyautogui.hotkey(*keys)
    time.sleep(0.15)
    return f"[Success] Sent {'+'.join(keys)}."


# ------------------------------------------------------------------ #
# Windows
# ------------------------------------------------------------------ #

def list_windows(limit=25):
    import win32gui
    rows = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or is_winter_own_window(hwnd, title):
            return
        rows.append((hwnd, title))

    win32gui.EnumWindows(collect, None)
    if not rows:
        return "No open windows found."
    _, active = foreground_window()
    lines = [f"Open windows (active: '{active}'):"]
    for hwnd, title in rows[:int(limit)]:
        lines.append(f"- [{hwnd}] {title}")
    return "\n".join(lines)


def _find_window(title_fragment):
    import win32gui
    wanted = str(title_fragment or "").strip().casefold()
    found = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and wanted in title.casefold() and not is_winter_own_window(hwnd, title):
            found.append((hwnd, title))

    win32gui.EnumWindows(collect, None)
    return found


def focus_window(title_fragment):
    import win32con
    import win32gui
    matches = _find_window(title_fragment)
    if not matches:
        return f"[Error] No visible window matching '{title_fragment}'. Use LIST_WINDOWS."
    hwnd, title = matches[0]
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # A foreground change from a background thread is refused by Windows
        # unless the calling thread is attached to the target's input queue.
        try:
            import win32process
            import win32api
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            current_tid = win32api.GetCurrentThreadId()
            if target_tid and target_tid != current_tid:
                win32process.AttachThreadInput(current_tid, target_tid, True)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    win32process.AttachThreadInput(current_tid, target_tid, False)
            else:
                win32gui.SetForegroundWindow(hwnd)
        except Exception:
            win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        _, now = foreground_window()
        if title.casefold() not in now.casefold():
            return f"[Error] Windows refused to bring '{title}' forward; it is still '{now}'."
        return f"[Success] Focused '{title}'."
    except Exception as exc:
        return f"[Error] Could not focus '{title_fragment}': {exc}"


def window_state(title_fragment, state="minimize"):
    import win32con
    import win32gui
    matches = _find_window(title_fragment)
    if not matches:
        return f"[Error] No visible window matching '{title_fragment}'."
    hwnd, title = matches[0]
    flags = {
        "minimize": win32con.SW_MINIMIZE,
        "maximize": win32con.SW_MAXIMIZE,
        "restore": win32con.SW_RESTORE,
    }
    if state not in flags:
        return f"[Error] Unknown window state '{state}'."
    win32gui.ShowWindow(hwnd, flags[state])
    return f"[Success] {state.capitalize()}d '{title}'."


def close_window(title_fragment):
    import win32con
    import win32gui
    matches = _find_window(title_fragment)
    if not matches:
        return f"[Error] No visible window matching '{title_fragment}'."
    hwnd, title = matches[0]
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return f"[Success] Asked '{title}' to close. Unsaved work will prompt in the app itself."


# ------------------------------------------------------------------ #
# Machine controls
# ------------------------------------------------------------------ #

def media_key(action):
    keys = {
        "play": "playpause", "pause": "playpause", "playpause": "playpause",
        "next": "nexttrack", "previous": "prevtrack", "prev": "prevtrack",
        "stop": "stop",
    }
    key = keys.get(str(action or "").lower())
    if not key:
        return f"[Error] Unknown media action '{action}'."
    pyautogui.press(key)
    return f"[Success] Media: {action}."


def volume(direction="up", steps=5):
    key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}.get(
        str(direction or "").lower()
    )
    if not key:
        return f"[Error] Unknown volume direction '{direction}'."
    for _ in range(1 if key == "volumemute" else max(1, int(steps))):
        pyautogui.press(key)
    return f"[Success] Volume {direction}."


def set_brightness(percent):
    """Laptop panel brightness over WMI."""
    try:
        level = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        return "[Error] Brightness must be a number between 0 and 100."
    import subprocess
    script = (
        "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1,{level})"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return f"[Error] Could not set brightness: {detail[0] if detail else 'WMI refused'}"
    return f"[Success] Brightness set to {level}%."


def clipboard_read():
    import pyperclip
    text = pyperclip.paste() or ""
    if not text.strip():
        return "The clipboard is empty."
    return f"Clipboard contents:\n{text[:4000]}"


def clipboard_write(text):
    import pyperclip
    pyperclip.copy(str(text))
    return f"[Success] Copied {len(str(text))} characters to the clipboard."


def lock_screen():
    import ctypes
    ctypes.windll.user32.LockWorkStation()
    return "[Success] Workstation locked."


def launch(target):
    """Start an app, document, or URL and report observed state honestly.

    Windows application names are not generally executables (``Calculator`` is
    a Start-menu entry, for example), so resolve all installed Start apps rather
    than maintaining a brittle list of special cases.
    """
    target = str(target or "").strip()
    if not target:
        return "[Error] Nothing to launch."
    import subprocess

    before_hwnd, before_title = foreground_window()
    method = ""
    try:
        is_url = bool(re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I))
        is_path = os.path.exists(os.path.expandvars(os.path.expanduser(target)))
        executable = shutil.which(target) or shutil.which(target + ".exe")

        if is_url or is_path:
            os.startfile(os.path.expandvars(os.path.expanduser(target)))
            method = "Windows association"
        elif executable:
            subprocess.Popen(
                [executable], shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            method = "executable"
        else:
            # Get-StartApps is the authoritative installed-app catalogue. JSON
            # avoids trying to parse localized, column-aligned PowerShell text.
            probe = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            apps = json.loads(probe.stdout or "[]") if probe.returncode == 0 else []
            if isinstance(apps, dict):
                apps = [apps]
            wanted = re.sub(r"[^a-z0-9]", "", target.casefold())
            ranked = []
            for app in apps:
                name = str(app.get("Name", ""))
                folded = re.sub(r"[^a-z0-9]", "", name.casefold())
                if wanted and (wanted == folded or wanted in folded or folded in wanted):
                    rank = (0 if wanted == folded else 1, abs(len(folded) - len(wanted)), name)
                    ranked.append((rank, app))
            if not ranked:
                return (f"[Error] Could not find an installed application named '{target}'. "
                        "Use LIST_PROCESSES or provide an executable/path.")
            app = min(ranked, key=lambda item: item[0])[1]
            subprocess.Popen(
                ["explorer.exe", "shell:AppsFolder\\" + str(app["AppID"])],
                shell=False,
            )
            method = f"Start app '{app['Name']}'"

        # Some apps take several seconds to create/focus a window. Observe
        # rather than sleeping once and claiming success regardless.
        deadline = time.monotonic() + 6.0
        hwnd, title = before_hwnd, before_title
        while time.monotonic() < deadline:
            time.sleep(0.25)
            hwnd, title = foreground_window()
            if hwnd and (hwnd != before_hwnd or title != before_title):
                return (f"[Success] Opened '{target}' using {method}. "
                        f"Foreground now: '{title}'.")
        return (f"[Warning] Windows accepted the request to open '{target}' using {method}, "
                "but no new foreground window was observed within 6 seconds.")
    except Exception as exc:
        return f"[Error] Could not launch '{target}': {exc}"


def list_processes(limit=20, name_filter=""):
    import psutil
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if name_filter and name_filter.lower() not in name.lower():
                continue
            mem = (info.get("memory_info").rss / (1024 * 1024)) if info.get("memory_info") else 0
            rows.append((mem, info.get("pid"), name))
        except Exception:
            continue
    rows.sort(reverse=True)
    if not rows:
        return "No matching processes."
    lines = ["Processes by memory:"]
    for mem, pid, name in rows[:int(limit)]:
        lines.append(f"- [{pid}] {name} — {mem:.0f} MB")
    return "\n".join(lines)
