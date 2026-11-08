import queue
import tkinter as tk
import tkinter.font as tkfont
from tkinter import Canvas
import customtkinter as ctk
import math
import colorsys
import os
import time
import webbrowser
import window_identity

# This must run before the first Tk root is created.  Otherwise Windows has
# already grouped the native result window under pythonw.exe and changing its
# bitmap later cannot fully repair the taskbar identity.
window_identity.configure_process()

# Initialize CustomTkinter settings
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# State constants
STATE_IDLE      = "idle"
STATE_LOADING   = "loading"
STATE_LISTENING = "listening"
STATE_THINKING  = "thinking"
STATE_SPEAKING  = "speaking"

# ------------------------------------------------------------------ #
# Palette, type scale and the identity mark now live outside this file:
# theme.py so the orb and the Control Center cannot drift apart, and
# winter_mark.py so every surface that draws the mark draws the same one.
# The old names are re-exported because the overlays, blackboard and
# annotation canvas below all still reach for them.
# ------------------------------------------------------------------ #
import winter_mark
from approval_prompt import ApprovalPromptMixin
from hud import HudMixin
from blackboard import BlackboardMixin
from control_center import ControlCenterMixin, user_activity_text
from workbench import WorkbenchMixin
from rest_screen import RestScreenMixin
from theme import (MAGIC, SURFACE, SURFACE_DEEP, SEPARATOR,
                   BG_WINDOW, BG_CARD, BG_CONTROL, BORDER_SOFT, BLUE, GREEN,
                   LABEL_PRIMARY, LABEL_SECONDARY, LABEL_TERTIARY,
                   ACCENTS, STATE_LABELS)
from theme import blend as _blend, dim as _dim


def display_worthy_text(value):
    """True only for content worth interrupting the desktop to display."""
    from result_presenter import display_worthy
    return display_worthy(value)


def rich_blocks_have_content(blocks):
    """Reject empty structured envelopes before they create a blank window."""
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or raw.get("kind") or "text").lower()
        if kind in {"text", "markdown", "code", "math", "formula", "latex"}:
            if display_worthy_text(raw.get("content") or raw.get("text")
                                   or raw.get("latex") or raw.get("code")):
                return True
        elif kind == "table" and (raw.get("headers") or raw.get("rows")):
            return True
        elif kind == "diagram" and (raw.get("nodes") or raw.get("edges")
                                     or raw.get("source") or raw.get("text")):
            return True
        elif kind == "chart" and (raw.get("text") or raw.get("series")
                                   or raw.get("values")):
            return True
        elif kind == "image" and (raw.get("path") or raw.get("src")):
            return True
        elif kind in {"scene3d", "model3d"} and raw.get("objects"):
            return True
        elif kind == "animation" and raw.get("frames"):
            return True
        elif kind == "questionnaire" and raw.get("questions"):
            return True
        elif kind == "explorer" and (raw.get("curves") or raw.get("shapes")):
            return True
        elif kind == "lesson" and raw.get("sections"):
            return True
        elif kind == "walkthrough" and raw.get("steps"):
            return True
        elif kind == "actions" and raw.get("items"):
            return True
    return False


def centered_face_preview(source, normalized_box=(), size=226):
    """Return a square preview centered on a detected face when available."""
    from PIL import Image, ImageOps
    source = source.convert("RGB")
    width, height = source.size
    try:
        x1, y1, x2, y2 = [float(value) for value in normalized_box]
        valid = (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return ImageOps.fit(source, (size, size), method=Image.Resampling.LANCZOS,
                            centering=(.5, .45))

    face_w = (x2 - x1) * width
    face_h = (y2 - y1) * height
    # Include hair and shoulders rather than zooming to a passport crop. The
    # slight downward offset keeps the whole head visually centred.
    side = min(min(width, height), max(face_w, face_h) * 2.35)
    side = max(side, min(width, height) * .38)
    center_x = ((x1 + x2) * .5) * width
    center_y = ((y1 + y2) * .5) * height + face_h * .14
    left = max(0.0, min(width - side, center_x - side * .5))
    top = max(0.0, min(height - side, center_y - side * .5))
    crop = source.crop((int(left), int(top), int(left + side), int(top + side)))
    return crop.resize((size, size), Image.Resampling.LANCZOS)


def physical_screen_geometry(window, width, height, left=0, top=0):
    """Return CTk geometry that occupies exactly the requested physical area.

    ``CTkToplevel.geometry`` multiplies width and height by Windows' display
    scaling.  Passing the physical screen dimensions directly therefore made
    a 1920x1080 identity gate 2400x1350 at 125% scaling; the card was correctly
    centered in that oversized window, but visibly far down and to the right.
    Positions are not scaled by CustomTkinter, so only dimensions are divided.
    """
    try:
        scaling = float(window._get_window_scaling())
    except (AttributeError, TypeError, ValueError):
        scaling = 1.0
    if not math.isfinite(scaling) or scaling <= 0:
        scaling = 1.0
    logical_width = max(1, round(float(width) / scaling))
    logical_height = max(1, round(float(height) / scaling))
    return (f"{logical_width}x{logical_height}"
            f"{int(left):+d}{int(top):+d}")


def _pick_family(*candidates):
    try:
        available = set(tkfont.families())
    except Exception:
        return candidates[-1]
    for c in candidates:
        if c in available:
            return c
    return candidates[-1]


class WinterUI(ControlCenterMixin, BlackboardMixin, WorkbenchMixin,
               RestScreenMixin, ApprovalPromptMixin, HudMixin):
    def __init__(self, ui_queue, input_queue):
        self._q = ui_queue
        self._in_q = input_queue
        self._state = STATE_IDLE
        self._prev_rendered_state = None
        self._t = 0.0
        self._clicks = []
        self.is_inputting = False
        self._last_info = ""
        self._status_detail = ""
        self._step_line = ""
        self._step_line_at = 0.0
        self._active_mode_label = "ASSISTANT"
        self._notch_question = ""
        self._notch_answer = ""
        self._notch_answer_until = 0.0
        self._conversation_until = 0.0
        self._conversation_was_active = False
        self._light_win = None
        self._annot_win = None
        self._annot_canvas = None
        self._identity_gate_win = None
        self._identity_gate_photo = None
        self._identity_gate_canvas = None
        self._identity_gate_percent = None
        self._identity_gate_message = None
        self._identity_gate_title = None
        self._board_win = None
        self._board_canvas = None
        self._board_y = 0
        self._widget_win = None
        self._widget_id = ""

        self._build_border_window()

        # Font family resolved once: real SF Pro if the person happens
        # to have it installed, otherwise Segoe UI Variable (ships with
        # Win11 and is the closest metric/weight match available),
        # otherwise plain Segoe UI.
        self.FONT_DISPLAY = _pick_family("SF Pro Display", "Segoe UI Variable Display", "Segoe UI")
        self.FONT_TEXT    = _pick_family("SF Pro Text", "Segoe UI Variable Text", "Segoe UI")
        self.FONT_MONO    = _pick_family("SF Mono", "Cascadia Code", "Consolas")

        self._build_notch()
        self._build_input_window()
        self._build_info_window()
        # self._build_vision_window() # Removed per user request
        self._build_dashboard_window()

        self._poll()
        self._animate()

        # Register all VANTA windows with vision.py so they're
        # reliably excluded from screenshot capture.
        self.root.after(600, self._register_winter_hwnds)

    def _build_identity_gate(self):
        """A blank, blocking lifecycle surface with an ephemeral face preview."""
        required = ("_identity_gate_canvas", "_identity_gate_percent",
                    "_identity_gate_message", "_identity_gate_title")
        current = self._identity_gate_win
        if current is not None:
            try:
                if current.winfo_exists() and all(
                        getattr(self, name, None) is not None
                        for name in required):
                    return
            except Exception:
                pass
            # A constructor can fail after creating the Toplevel but before
            # creating its children.  Never cache that half-built surface:
            # every later frame would otherwise fail on the missing widgets.
            try:
                current.destroy()
            except Exception:
                pass

        self._identity_gate_win = None
        self._identity_gate_canvas = None
        self._identity_gate_percent = None
        self._identity_gate_message = None
        self._identity_gate_title = None

        win = ctk.CTkToplevel(self.root)
        try:
            # On Windows/Tk, -fullscreen cannot be enabled on an
            # override-redirect window. Use Tk's own logical screen size so
            # CustomTkinter DPI scaling does not enlarge the window and push
            # its supposedly centred content to the right.
            win.withdraw()
            gate_bg = "#000000"
            gate_surface = "#000000"
            win.configure(fg_color=gate_bg)
            width = win.winfo_screenwidth()
            height = win.winfo_screenheight()
            win.geometry(physical_screen_geometry(win, width, height))
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            window_identity.hide_from_taskbar(win, self.root)

            # One uninterrupted surface.  The old grey card and the black
            # square around the circular video made a simple identity check
            # look like three windows stacked on top of one another.
            stage = ctk.CTkFrame(
                win, width=340, height=410, corner_radius=0,
                fg_color="transparent", border_width=0)
            stage.place(relx=.5, rely=.5, anchor="center")
            stage.pack_propagate(False)
            title = ctk.CTkLabel(
                stage, text="Verifying",
                font=ctk.CTkFont(family=self.FONT_DISPLAY, size=18,
                                 weight="bold"),
                text_color="#f5f5f7")
            title.pack(pady=(2, 16))
            canvas = tk.Canvas(
                stage, width=268, height=268, bg=gate_bg,
                highlightthickness=0, bd=0)
            canvas.pack()
            canvas.create_oval(
                3, 3, 265, 265, fill=gate_bg, outline="#111111", width=1,
                tags="placeholder-halo")
            canvas.create_oval(
                12, 12, 256, 256, fill=gate_surface,
                outline="#2c2c2e", width=2,
                tags="placeholder")
            percent = ctk.CTkLabel(
                stage, text="0%", font=ctk.CTkFont(
                    family=self.FONT_DISPLAY, size=16, weight="bold"),
                text_color="#f5f5f7")
            percent.pack(pady=(8, 0))
            message = ctk.CTkLabel(
                stage, text="Center your face",
                font=ctk.CTkFont(family=self.FONT_TEXT, size=12),
                text_color=LABEL_SECONDARY)
            message.pack(pady=(0, 8))
            ctk.CTkButton(
                stage, text="Cancel", width=76, height=28, corner_radius=14,
                fg_color="transparent", hover_color="#111111",
                border_width=0, text_color=LABEL_SECONDARY,
                command=self._abort_identity_gate
            ).pack()

            # Publish references only after every widget exists.  Queue
            # handlers can therefore never observe a partially built gate.
            self._identity_gate_win = win
            self._identity_gate_title = title
            self._identity_gate_canvas = canvas
            self._identity_gate_percent = percent
            self._identity_gate_message = message
        except Exception:
            try:
                win.destroy()
            except Exception:
                pass
            self._identity_gate_win = None
            raise

    def _abort_identity_gate(self):
        try:
            import authentication_gate
            authentication_gate.abort()
        except Exception as exc:
            print(f"[UI] Could not abort identity gate: {exc}")

    def _show_identity_gate(self, payload=None):
        self._build_identity_gate()
        self._identity_gate_title.configure(text="Verifying")
        self._identity_gate_percent.configure(text="0%")
        self._identity_gate_message.configure(text="Center your face")
        self._identity_gate_win.deiconify()
        self._identity_gate_win.lift()
        self._identity_gate_win.focus_force()

    def _update_identity_gate(self, payload):
        self._build_identity_gate()
        payload = payload or {}
        encoded = payload.get("image")
        if encoded:
            try:
                import base64
                from io import BytesIO
                from PIL import Image, ImageDraw, ImageTk
                source = Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
                image = centered_face_preview(
                    source, payload.get("box") or (), size=244)
                mask = Image.new("L", (244, 244), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 243, 243), fill=255)
                # Transparent outside the mask: an opaque black RGBA base was
                # the square visible behind the circular preview.
                composed = Image.new("RGBA", (244, 244), (0, 0, 0, 0))
                composed.paste(image.convert("RGBA"), (0, 0), mask)
                photo = ImageTk.PhotoImage(composed)
                self._identity_gate_photo = photo
                canvas = self._identity_gate_canvas
                canvas.delete("all")
                canvas.create_image(134, 134, image=photo)
                colour = GREEN if payload.get("matched") else BLUE
                canvas.create_oval(
                    12, 12, 256, 256, outline="#2c2c2e", width=2)
                progress = max(0, min(100, int(payload.get("percent") or 0)))
                canvas.create_arc(
                    12, 12, 256, 256, start=90, extent=-(progress * 3.6),
                    style="arc", outline=colour, width=4)
                canvas.create_oval(
                    7, 7, 261, 261, outline="#111111", width=1)
            except Exception as exc:
                print(f"[UI] Identity preview failed: {type(exc).__name__}.")
        self._identity_gate_percent.configure(
            text=f"{int(payload.get('percent') or 0)}%")
        self._identity_gate_title.configure(
            text="Verified" if payload.get("matched") else "Verifying")
        self._identity_gate_message.configure(
            text=str(payload.get("message") or "Align your face"))

    def _hide_identity_gate(self):
        if self._identity_gate_win is not None:
            self._identity_gate_win.withdraw()

    def _show_light(self, spec):
        """Fullscreen wash of colour — a softbox for video calls, or a dimmer.

        Kept click-through so it never traps the pointer: a full-screen window
        that swallows input would be indistinguishable from a frozen machine.
        """
        colour = str(spec.get("color", "#ffffff") or "#ffffff")
        try:
            opacity = float(spec.get("opacity", 1.0))
        except (TypeError, ValueError):
            opacity = 1.0
        opacity = max(0.05, min(1.0, opacity))

        if self._light_win is None or not self._light_win.winfo_exists():
            win = ctk.CTkToplevel(self.root)
            win.withdraw()
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            window_identity.hide_from_taskbar(win, self.root)
            self._light_win = win
        else:
            win = self._light_win

        win.configure(fg_color=colour)
        try:
            win.attributes("-alpha", opacity)
        except Exception:
            pass
        try:
            from vision import get_virtual_screen_bounds
            left, top, width, height = get_virtual_screen_bounds()
        except Exception:
            left, top = 0, 0
            width, height = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{width}x{height}{left:+d}{top:+d}")
        win.deiconify()
        win.lift()
        try:
            # Exclude from capture so VANTA never screenshots its own wash,
            # and make it click-through so the desktop stays usable.
            import win32con
            import win32gui
            hwnd = win32gui.GetParent(win.winfo_id()) or win.winfo_id()
            styles = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE,
                styles | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT,
            )
            from vision import register_winter_hwnd
            register_winter_hwnd(hwnd)
        except Exception as exc:
            print(f"[Light] Could not make the overlay click-through: {exc}")

    def _hide_light(self):
        if self._light_win is not None and self._light_win.winfo_exists():
            self._light_win.withdraw()

    # ---------------------------------------------------------------- #
    # BLACKBOARD
    #
    # A teaching surface rather than a chat log. Content flows down the
    # board and scrolls up when it reaches the bottom, the way a lecture
    # board actually fills, so a follow-up question continues the lesson
    # instead of replacing it. Everything it can draw lives in blackboard.py.
    # ---------------------------------------------------------------- #

    def _ensure_annotation_canvas(self):
        """A transparent, click-through canvas covering the whole screen.

        Colour-key transparency is what makes this work: the window is filled
        with a key colour Windows renders as fully transparent, so only the
        drawn strokes are visible. A normal alpha window would wash out
        everything underneath, which is the opposite of pointing at it.
        """
        import tkinter as tk
        if getattr(self, "_annot_win", None) is not None and self._annot_win.winfo_exists():
            return self._annot_canvas

        key = "#010203"  # a colour nothing real will use
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        win.configure(bg=key)
        try:
            from vision import get_virtual_screen_bounds
            left, top, width, height = get_virtual_screen_bounds()
        except Exception:
            left, top = 0, 0
            width, height = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{width}x{height}{left:+d}{top:+d}")
        try:
            win.attributes("-transparentcolor", key)
        except Exception as exc:
            print(f"[Annotate] Colour-key transparency unavailable: {exc}")
        canvas = tk.Canvas(win, bg=key, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        try:
            import win32con
            import win32gui
            hwnd = win32gui.GetParent(win.winfo_id()) or win.winfo_id()
            styles = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE,
                styles | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT,
            )
            # Re-applying WS_EX_LAYERED through SetWindowLong discards the
            # layer attributes Tk had just set for -transparentcolor, so the
            # window became transparent everywhere instead of only on the key
            # colour: annotations were drawn correctly and rendered nothing.
            # The key has to be restored afterwards, as 0x00BBGGRR.
            win32gui.SetLayeredWindowAttributes(hwnd, 0x00030201, 0, 0x00000001)
            from vision import register_winter_hwnd
            register_winter_hwnd(hwnd)
        except Exception as exc:
            print(f"[Annotate] Could not make annotations click-through: {exc}")
        win.deiconify()
        self._annot_win = win
        self._annot_canvas = canvas
        return canvas

    def _screen_scale(self):
        """Tk's coordinate space divided by the real screen, cached.

        OCR and screen grabs measure physical pixels; Tk draws in whatever
        space it was given, which on a scaled display is smaller unless the
        process declared DPI awareness before Tk started. Annotations were
        landing a quarter of the screen away from what they named, because
        one side of the calculation was 1920 wide and the other 1536.
        """
        cached = getattr(self, "_screen_scale_cache", None)
        if cached is not None:
            return cached
        scale = 1.0
        try:
            import ctypes

            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            # A device context is a handle, not an int. Left to guess, ctypes
            # truncates it and GetDC(0) raises "int too long to convert".
            user32.GetDC.restype = ctypes.c_void_p
            user32.GetDC.argtypes = [ctypes.c_void_p]
            user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
            device = user32.GetDC(None)
            # DESKTOPHORZRES reports true pixels whatever the process knows
            # about DPI, which is exactly the number needed here.
            physical = gdi32.GetDeviceCaps(device, 118)
            user32.ReleaseDC(None, device)
            logical = self.root.winfo_screenwidth()
            if physical > 0 and logical > 0:
                scale = logical / physical
        except Exception as exc:
            print(f"[Annotate] Could not measure the display scale: {exc}")
        self._screen_scale_cache = scale
        if abs(scale - 1.0) > 0.01:
            print(f"[Annotate] Display is scaled; annotation coordinates "
                  f"multiplied by {scale:.3f}.")
        return scale

    def _draw_annotation(self, spec):
        canvas = self._ensure_annotation_canvas()
        # Normalised through the same table the dispatcher refuses against, so
        # the two cannot disagree about what is drawable. The chain below has
        # no fallback branch: an unknown name draws nothing, and the label it
        # still renders makes that look like a half-working feature.
        try:
            import action_schema
            shape = action_schema.normalise_shape(spec.get("shape")) or "box"
        except Exception:
            shape = str(spec.get("shape", "box") or "box").lower()
        colour = str(spec.get("color", "#ff3b30"))
        width = int(spec.get("width", 4))
        scale = self._screen_scale()

        def place(key):
            try:
                return int(float(spec.get(key, 0)) * scale)
            except (TypeError, ValueError):
                return 0

        x1, y1 = place("x1"), place("y1")
        x2, y2 = place("x2"), place("y2")
        label = str(spec.get("label", "") or "")

        if shape == "box":
            canvas.create_rectangle(x1, y1, x2, y2, outline=colour, width=width)
        elif shape in ("circle", "ellipse"):
            canvas.create_oval(x1, y1, x2, y2, outline=colour, width=width)
        elif shape == "arrow":
            canvas.create_line(x1, y1, x2, y2, fill=colour, width=width,
                               arrow="last", arrowshape=(18, 22, 7))
        elif shape == "line":
            canvas.create_line(x1, y1, x2, y2, fill=colour, width=width)
        elif shape == "highlight":
            canvas.create_rectangle(x1, y1, x2, y2, outline="", fill=colour,
                                    stipple="gray25")
        elif shape == "underline":
            bottom = max(y1, y2)
            canvas.create_line(min(x1, x2), bottom, max(x1, x2), bottom,
                               fill=colour, width=max(3, width))
        elif shape == "number":
            # A numbered badge, for walking through something in order. The
            # point of a walkthrough is the sequence, and an unnumbered ring
            # around each step does not carry one.
            radius = max(16, width * 5)
            canvas.create_oval(x1 - radius, y1 - radius, x1 + radius, y1 + radius,
                               fill=colour, outline="#ffffff", width=2)
            canvas.create_text(x1, y1, text=str(spec.get("n", "1")),
                               fill="#ffffff",
                               font=(self.FONT_DISPLAY, int(radius), "bold"))
        elif shape == "callout":
            # A label placed clear of the thing it names, with a line back to
            # it: writing on top of what you are pointing at hides it.
            canvas.create_line(x1, y1, x2, y2, fill=colour, width=max(2, width - 1))
            canvas.create_oval(x1 - 6, y1 - 6, x1 + 6, y1 + 6, fill=colour,
                               outline="")
        if label:
            if shape == "callout":
                text_x, text_y, anchor = x2, y2, "w" if x2 >= x1 else "e"
            else:
                text_x, text_y, anchor = min(x1, x2), max(14, min(y1, y2) - 12), "sw"
            # Drawn twice: a dark copy underneath so the label stays readable
            # over a white PDF page as well as a dark slide.
            canvas.create_text(text_x + 2, text_y + 1, text=label, fill="#000000",
                               font=(self.FONT_TEXT, 14, "bold"), anchor=anchor)
            canvas.create_text(text_x, text_y, text=label, fill=colour,
                               font=(self.FONT_TEXT, 14, "bold"), anchor=anchor)
        self._annot_win.deiconify()
        self._annot_win.lift()

    def _draw_scene(self, scene):
        """Render a complete drawing from bounded, declarative primitives."""
        canvas = self._ensure_annotation_canvas()
        if scene.get("clear", True):
            canvas.delete("all")
        try:
            self._annot_win.update_idletasks()
        except Exception:
            pass
        width = max(1, canvas.winfo_width() or self.root.winfo_screenwidth())
        height = max(1, canvas.winfo_height() or self.root.winfo_screenheight())

        def x(value):
            return float(value or 0) * width / 1000.0

        def y(value):
            return float(value or 0) * height / 1000.0

        background = str(scene.get("background", "") or "").strip()
        if background and background.lower() != "transparent":
            canvas.create_rectangle(0, 0, width, height, fill=background, outline="")

        for item in scene.get("items", [])[:250]:
            kind = str(item.get("type", "")).lower()
            colour = str(item.get("stroke", item.get("color", "#ffffff")))
            fill = str(item.get("fill", "") or "")
            line_width = max(1, min(30, int(float(item.get("width", 4) or 4))))
            if kind in {"line", "arrow"}:
                canvas.create_line(x(item.get("x1")), y(item.get("y1")),
                                   x(item.get("x2")), y(item.get("y2")),
                                   fill=colour, width=line_width,
                                   arrow="last" if kind == "arrow" else "none",
                                   smooth=bool(item.get("smooth", False)))
            elif kind == "rectangle":
                canvas.create_rectangle(x(item.get("x1")), y(item.get("y1")),
                                        x(item.get("x2")), y(item.get("y2")),
                                        outline=colour, fill=fill, width=line_width)
            elif kind == "oval":
                canvas.create_oval(x(item.get("x1")), y(item.get("y1")),
                                   x(item.get("x2")), y(item.get("y2")),
                                   outline=colour, fill=fill, width=line_width)
            elif kind == "polygon":
                coords = []
                for point in item.get("points", [])[:100]:
                    if isinstance(point, (list, tuple)) and len(point) >= 2:
                        coords.extend((x(point[0]), y(point[1])))
                if len(coords) >= 6:
                    canvas.create_polygon(*coords, outline=colour, fill=fill,
                                          width=line_width)
            elif kind == "arc":
                canvas.create_arc(x(item.get("x1")), y(item.get("y1")),
                                  x(item.get("x2")), y(item.get("y2")),
                                  start=float(item.get("start", 0) or 0),
                                  extent=float(item.get("extent", 180) or 180),
                                  style=str(item.get("style", "arc")),
                                  outline=colour, fill=fill, width=line_width)
            elif kind == "text":
                size = max(8, min(96, int(float(item.get("size", 24) or 24))))
                canvas.create_text(x(item.get("x")), y(item.get("y")),
                                   text=str(item.get("text", ""))[:500],
                                   fill=colour,
                                   font=(self.FONT_TEXT, size,
                                         "bold" if item.get("bold") else "normal"),
                                   anchor=str(item.get("anchor", "center")))
        self._annot_win.deiconify()
        self._annot_win.lift()

    def _clear_annotations(self):
        if getattr(self, "_annot_win", None) is not None and self._annot_win.winfo_exists():
            self._annot_canvas.delete("all")
            self._annot_win.withdraw()

    def _register_winter_hwnds(self):
        """Tell vision.py which HWNDs are ours, so it never captures them."""
        try:
            import ctypes
            from vision import register_winter_hwnd

            def _get_root_hwnd(tk_widget):
                child = tk_widget.winfo_id()
                # GA_ROOT = 2
                root = ctypes.windll.user32.GetAncestor(child, 2)
                return root or child

            for win in [self.root, self.pill_win, self.input_win,
                        self.info_win, self.info_restore_win,
                        self.dashboard_win]:
                try:
                    hwnd = _get_root_hwnd(win)
                    register_winter_hwnd(hwnd)
                    print(f"[UI] Registered VANTA HWND: {hwnd} ({win.winfo_id()})")
                except Exception as e:
                    print(f"[UI] Could not register HWND for {win}: {e}")

            # Also apply NOACTIVATE to the vision window now
            # (Vision window removed)
            pass
        except Exception as e:
            print(f"[UI] _register_winter_hwnds failed: {e}")


    # ------------------------------------------------------------ #
    # Vision Preview window -- shows annotated screenshot to user
    # ------------------------------------------------------------ #
    def _build_vision_window(self):
        PREVIEW_W, PREVIEW_H = 360, 220
        x = self.SW - PREVIEW_W - 16
        y = self.SH - PREVIEW_H - 60  # above taskbar

        self.vision_win = ctk.CTkToplevel(self.root, fg_color=SURFACE)
        self.vision_win.withdraw()
        self.vision_win.overrideredirect(True)
        self.vision_win.attributes('-topmost', True)
        window_identity.hide_from_taskbar(self.vision_win, self.root)
        self.vision_win.geometry(f"{PREVIEW_W}x{PREVIEW_H}+{x}+{y}")

        # Header bar
        header = ctk.CTkFrame(self.vision_win, fg_color="#1a1a1c", height=26, corner_radius=0)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        ctk.CTkLabel(
            header, text="Vision",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11, weight="bold"),
            text_color=LABEL_TERTIARY
        ).pack(side=tk.LEFT, padx=12, pady=4)

        ctk.CTkButton(
            header, text="X", width=24, height=18, corner_radius=8,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11),
            fg_color="#1a1a1c", hover_color="#3a1010",
            text_color=LABEL_TERTIARY,
            command=self.vision_win.withdraw
        ).pack(side=tk.RIGHT, padx=8, pady=4)

        # Canvas to draw the image
        self.vision_canvas = tk.Canvas(
            self.vision_win, bg=SURFACE_DEEP, highlightthickness=0,
            width=PREVIEW_W, height=PREVIEW_H - 26
        )
        self.vision_canvas.pack(fill=tk.BOTH, expand=True)
        self._vision_photo = None
        self._vision_win_w = PREVIEW_W
        self._vision_win_h = PREVIEW_H - 26

        self.vision_win.withdraw()


    # ------------------------------------------------------------ #
    # Dashboard Window -- borderless, toggleable VANTA overlay
    # ------------------------------------------------------------ #
    def _append_control_text(self, widget, text):
        if not hasattr(self, widget):
            return
        target = getattr(self, widget)
        target.configure(state="normal")
        target.insert(tk.END, text)
        target.see(tk.END)
        target.configure(state="disabled")

    def _send_control_command(self, event=None, command=None):
        text = command
        if text is None and isinstance(event, str):
            text = event
        if text is None and hasattr(self, "control_command"):
            text = self.control_command.get().strip()
            self.control_command.delete(0, tk.END)
        text = str(text or "").strip()
        if not text:
            return
        self._append_control_text("chat_history", f"YOU\n{text}\n\n")
        self._in_q.put(("text", text))
        self._show_page("Chat")

    def _refresh_files(self):
        self.file_path_label.configure(text="Vault / loading...")
        def worker():
            try:
                from control_center_files import list_vault_directory
                self._q.put(("file_scan_result", list_vault_directory(self._vault_root, self._vault_current)))
            except Exception as exc:
                self._q.put(("file_preview_error", f"Vault navigation failed: {exc}"))
        import threading
        threading.Thread(target=worker, daemon=True, name="vanta-vault-browser").start()

    def _apply_file_rows(self, payload):
        payload = payload or {}
        self._vault_current = payload.get("current", ".")
        self._file_rows = list(payload.get("entries", []))
        self.file_list.delete(0, tk.END)
        for row in self._file_rows:
            prefix = "[UP] " if row.get("kind") == "parent" else "[DIR] " if row.get("kind") == "folder" else "      "
            self.file_list.insert(tk.END, prefix + row.get("name", ""))
        shown = "Vault" if self._vault_current in ("", ".") else f"Vault / {self._vault_current}"
        self.file_path_label.configure(text=shown)

    def _has_unsaved_vault_changes(self):
        return bool(getattr(self, "_vault_dirty", False))

    def _open_selected_file(self, _event=None):
        selection = self.file_list.curselection()
        if not selection:
            return
        row = self._file_rows[int(selection[0])]
        if row.get("kind") in {"parent", "folder"}:
            if self._has_unsaved_vault_changes():
                self._show_file_error("Save or revert changes before navigating.")
                return
            self._vault_current = row.get("relative", ".")
            self._refresh_files()
            return
        try:
            from control_center_files import read_vault_file
            loaded = read_vault_file(self._vault_root, row["relative"])
            self._vault_file, self._vault_original, self._vault_hash = loaded["relative"], loaded["content"], loaded["hash"]
            self._vault_dirty = False
            self.file_title.configure(text=self._vault_file)
            self.file_editor.delete("0.0", tk.END)
            self.file_editor.insert("0.0", loaded["content"])
            self._update_vault_diff()
        except Exception as exc:
            self._show_file_error(str(exc))

    def _vault_go_up(self):
        if self._has_unsaved_vault_changes():
            self._show_file_error("Save or revert changes before navigating.")
            return
        if self._vault_current not in ("", "."):
            self._vault_current = os.path.dirname(self._vault_current) or "."
            self._refresh_files()

    def _update_vault_diff(self):
        if not hasattr(self, "file_diff"):
            return
        edited = self.file_editor.get("0.0", "end-1c") if self._vault_file else ""
        from control_center_files import unified_diff
        lines = unified_diff(self._vault_original, edited, self._vault_file or "file") if self._vault_file else []
        self._vault_dirty = bool(lines)
        self.file_diff.configure(state="normal")
        self.file_diff.delete("1.0", tk.END)
        if not lines:
            self.file_diff.insert(tk.END, "No unsaved changes.\n", "context")
        for line in lines:
            tag = "header" if line.startswith(("---", "+++", "@@")) else "added" if line.startswith("+") else "removed" if line.startswith("-") else "context"
            self.file_diff.insert(tk.END, line + "\n", tag)
        self.file_diff.configure(state="disabled")
    def _save_vault_file(self):
        if not self._vault_file:
            return self._show_file_error("Select a Vault text file first.")
        try:
            from control_center_files import save_vault_file
            edited = self.file_editor.get("0.0", "end-1c")
            result = save_vault_file(self._vault_root, self._vault_file, edited, self._vault_hash)
            self._vault_original, self._vault_hash, self._vault_dirty = edited, result["hash"], False
            self.file_title.configure(text=f"{self._vault_file} - saved")
            self._update_vault_diff()
            note = f" | backup: {result['backup']}" if result.get("backup") else ""
            self._append_control_text("activity_text", f"Vault saved: {self._vault_file}{note}\n\n")
        except Exception as exc:
            self._show_file_error(f"Save failed: {exc}")

    def _revert_vault_file(self):
        if not self._vault_file:
            return
        self.file_editor.delete("0.0", tk.END)
        self.file_editor.insert("0.0", self._vault_original)
        self._vault_dirty = False
        self.file_title.configure(text=self._vault_file)
        self._update_vault_diff()

    def _reload_vault_file(self):
        if not self._vault_file:
            return self._show_file_error("Select a Vault text file first.")
        if self._has_unsaved_vault_changes():
            return self._show_file_error("Save or revert local changes before reloading.")
        try:
            from control_center_files import read_vault_file
            loaded = read_vault_file(self._vault_root, self._vault_file)
            previous = self._vault_original
            self._vault_hash = loaded["hash"]
            self.file_editor.delete("0.0", tk.END)
            self.file_editor.insert("0.0", loaded["content"])
            self._vault_original = previous
            self._update_vault_diff()
            suffix = "external changes ready for review" if self._vault_dirty else "unchanged"
            self.file_title.configure(text=f"{self._vault_file} - {suffix}")
        except Exception as exc:
            self._show_file_error(f"Reload failed: {exc}")

    def _send_vault_command(self, _event=None):
        instruction = self.file_ai_command.get().strip()
        if not instruction:
            return
        if not self._vault_file:
            return self._show_file_error("Select the Vault file VANTA should edit.")
        if self._has_unsaved_vault_changes():
            return self._show_file_error("Save or revert local changes before asking VANTA to edit.")
        absolute = os.path.join(str(self._vault_root), self._vault_file)
        task = (
            f"Edit exactly this VANTA Vault file: {absolute}\nUser instruction: {instruction}\n"
            "Read it first, preserve unrelated content and formatting, make only the requested changes, "
            "use the normal authorized file action, verify by reading it back, and report concisely. "
            "Do not edit any other path."
        )
        self.file_ai_command.delete(0, tk.END)
        self._append_control_text("chat_history", f"YOU - VAULT EDIT\n{instruction}\nFile: {self._vault_file}\n\n")
        self._append_control_text("activity_text", f"Vault edit requested: {self._vault_file}\n\n")
        self._in_q.put(("text", task))
        self._show_page("Chat")
        for delay in (3000, 8000, 15000):
            self.root.after(delay, self._reload_vault_file_if_clean)

    def _reload_vault_file_if_clean(self):
        if self._vault_file and not self._has_unsaved_vault_changes():
            self._reload_vault_file()

    def _show_file_error(self, message):
        if hasattr(self, "file_title"):
            self.file_title.configure(text=str(message))
        self._append_control_text("activity_text", f"Vault: {message}\n\n")
    def _refresh_graph(self):
        if getattr(self, "_graph_loading", False):
            return
        self._graph_loading = True
        query = self.graph_search.get().strip() if hasattr(self, "graph_search") else ""
        if hasattr(self, "graph_status"):
            self.graph_status.configure(text="Loading Neo4j graph...")
        def worker():
            try:
                from graph_explorer import load_graph_snapshot
                self._q.put(("graph_result", load_graph_snapshot(query, limit=70)))
            except Exception as exc:
                self._q.put(("graph_error", f"Graph unavailable: {exc}"))
        import threading
        threading.Thread(target=worker, daemon=True, name="vanta-graph-explorer").start()

    def _apply_graph_snapshot(self, snapshot):
        self._graph_loading = False
        self._graph_snapshot = snapshot or {"nodes": [], "edges": []}
        node_count = len(self._graph_snapshot.get("nodes", []))
        edge_count = len(self._graph_snapshot.get("edges", []))
        summary = self._graph_snapshot.get("summary", {})
        graph_canvas = getattr(self, "graph_canvas", None)
        # The Graph page is built on demand rather than at startup -- it came
        # off the sidebar when the window was simplified, so its widgets do not
        # exist until it is opened. The snapshot still arrives (contacts sync
        # requests one at boot), and assuming the label existed crashed the
        # whole UI queue handler on every launch.
        status = getattr(self, "graph_status", None)
        if status is not None:
            try:
                status.configure(
                    text=f"{node_count} entities | {edge_count} facts")
            except Exception:
                pass
        details = getattr(self, "graph_details", None)
        if details is not None and edge_count:
            classes = ", ".join(
                f"{key.replace('_', ' ').title()}: {value}"
                for key, value in sorted(summary.get("fact_classes", {}).items()))
            reviews = ", ".join(
                f"{key.replace('_', ' ').title()}: {value}"
                for key, value in sorted(summary.get("review_statuses", {}).items()))
            details.configure(state="normal")
            details.delete("0.0", tk.END)
            details.insert("0.0", (
                "What is currently visible\n\n"
                f"Entities: {node_count}\nFacts: {edge_count}\n"
                f"Fact types: {classes or 'legacy'}\n"
                f"Review state: {reviews or 'accepted'}\n\n"
                "Click a node to inspect its exact relationships and provenance."
            ))
            details.configure(state="disabled")
        # Same reasoning: nothing to draw onto until the page is built.
        if graph_canvas is not None:
            self._draw_graph()

    # The snapshot carries no node type -- only edges -- so kind is inferred
    # from the relationship that introduced the node. It is a heuristic, but
    # an honest one: what a thing is related by is most of what it is. Every
    # entity rendering in the same green said nothing about any of them.
    GRAPH_PREDICATE_COLORS = (
        (("CONTACT", "KNOWS", "FRIEND", "FAMILY", "SIBLING", "PARENT",
          "COLLEAGUE", "TEACHES"), "#0a84ff"),          # people
        (("LIVES", "LOCATED", "VISITED", "TRAVEL", "FROM"), "#ff9f0a"),   # places
        (("WORKS_ON", "BUILDS", "PROJECT", "GOAL", "STUDIES",
          "ENROLLED"), "#bf5af2"),                       # work
        (("PREFERS", "LIKES", "DISLIKES", "HABIT", "ROUTINE",
          "AVOIDS"), "#30d158"),                         # preferences
        (("ATTENDS", "SCHEDULED", "EVENT", "DUE"), "#64d2ff"),            # events
        (("OWNS", "USES", "HAS_DEVICE", "SUBSCRIBES"), "#ffd60a"),        # things
    )

    def _graph_node_color(self, predicate):
        upper = str(predicate or "").upper()
        for needles, color in self.GRAPH_PREDICATE_COLORS:
            if any(needle in upper for needle in needles):
                return color
        return LABEL_SECONDARY

    def _draw_graph(self):
        if not hasattr(self, "graph_canvas"):
            return
        canvas = self.graph_canvas
        canvas.delete("all")
        snapshot = getattr(self, "_graph_snapshot", {"nodes": [], "edges": []})
        nodes = snapshot.get("nodes", [])
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)
        if not nodes:
            canvas.create_text(
                width // 2, height // 2, text="No matching graph facts",
                fill=LABEL_SECONDARY, font=(self.FONT_TEXT, 14))
            self._graph_positions = {}
            return

        from graph_explorer import radial_layout
        positions = radial_layout(snapshot, width, height)
        self._graph_positions = positions
        edges = snapshot.get("edges", [])

        # When one predicate accounts for nearly every edge, printing it on
        # all of them is seventy copies of the same word laid over the graph.
        # The status line says it once instead, and the label only survives
        # on edges that actually differ from it.
        counts = {}
        for edge in edges:
            counts[edge["predicate"]] = counts.get(edge["predicate"], 0) + 1
        dominant = None
        if counts:
            top, top_count = max(counts.items(), key=lambda item: item[1])
            if top_count >= max(6, 0.6 * len(edges)):
                dominant = top

        label_edges = len(edges) <= 30
        for edge in edges:
            start, end = positions.get(edge["source"]), positions.get(edge["target"])
            if not start or not end:
                continue
            canvas.create_line(*start, *end, fill="#2e2e31", width=1.0)
            if not label_edges or edge["predicate"] == dominant:
                continue
            mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
            canvas.create_text(mx, my, text=edge["predicate"].replace("_", " ")[:22],
                               fill=LABEL_TERTIARY, font=(self.FONT_MONO, 9))

        # A node is coloured by whatever relationship first reached it.
        introduced_by = {}
        for edge in edges:
            introduced_by.setdefault(edge["target"], edge["predicate"])
            introduced_by.setdefault(edge["source"], edge["predicate"])

        node_map = {node["id"]: node for node in nodes}
        ordered = sorted(positions.items(),
                         key=lambda item: node_map[item[0]]["degree"])
        crowded = len(nodes) > 40
        for node_id, (x, y) in ordered:
            node = node_map[node_id]
            hub = node["degree"] > 2
            radius = 13 if hub else 6
            fill = self._graph_node_color(introduced_by.get(node_id))
            if hub:
                canvas.create_oval(x - radius - 4, y - radius - 4,
                                   x + radius + 4, y + radius + 4,
                                   outline=_dim(fill, 0.55), width=1)
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                               fill=fill, outline=SURFACE, width=2)
            # Leaf labels alternate above and below their dot: on a radial
            # layout every label sat on the same ring and they overlapped
            # into an unreadable band.
            label = node["label"]
            # Long names at adjacent angles run into each other on a radial
            # layout, and a wall of overlapping text is worse than a short one.
            limit = 20 if hub else (12 if crowded else 18)
            if len(label) > limit:
                label = label[:limit - 1] + "…"
            offset = radius + 11 if (hub or int(x + y) % 2 == 0) else -(radius + 9)
            canvas.create_text(x, y + offset, text=label,
                               fill=LABEL_PRIMARY if hub else LABEL_SECONDARY,
                               font=(self.FONT_TEXT, 11 if hub else 9))

        if dominant:
            canvas.create_text(
                12, height - 14, anchor="w",
                text=f"{len(edges)} edges, mostly {dominant.replace('_', ' ').lower()}",
                fill=LABEL_TERTIARY, font=(self.FONT_MONO, 9))

    def _on_graph_click(self, event):
        positions = getattr(self, "_graph_positions", {})
        if not positions:
            return
        nearest = min(
            positions,
            key=lambda node_id: (positions[node_id][0]-event.x)**2 + (positions[node_id][1]-event.y)**2,
        )
        x, y = positions[nearest]
        if (x-event.x)**2 + (y-event.y)**2 > 32**2:
            return
        snapshot = self._graph_snapshot
        node = next((item for item in snapshot["nodes"] if item["id"] == nearest), None)
        if not node:
            return
        lines = [node["label"], f"Connections: {node['degree']}", ""]
        for edge in snapshot["edges"]:
            if edge["source"] == nearest or edge["target"] == nearest:
                other_id = edge["target"] if edge["source"] == nearest else edge["source"]
                other = next((item["label"] for item in snapshot["nodes"] if item["id"] == other_id), other_id)
                direction = "->" if edge["source"] == nearest else "<-"
                lines.append(f"{direction} {edge['predicate'].replace('_', ' ')}")
                lines.append(f"   {other}")
                lines.append(f"   {edge['fact_class']} | {edge['confidence']:.2f} | {edge['sensitivity']}")
                lines.append(
                    f"   evidence: {edge.get('evidence_type', 'legacy')} | "
                    f"authority: {edge.get('authority', 'unknown')} | "
                    f"source: {edge.get('evidence_source_id', 'legacy')}")
        self.graph_details.configure(state="normal")
        self.graph_details.delete("0.0", tk.END)
        self.graph_details.insert("0.0", "\n".join(lines))
        self.graph_details.configure(state="disabled")

    def _show_graph_error(self, message):
        self._graph_loading = False
        if hasattr(self, "graph_status"):
            self.graph_status.configure(text=message)
    def _start_dashboard_drag(self, event):
        self._dashboard_drag_offset = (
            event.x_root - self.dashboard_win.winfo_x(),
            event.y_root - self.dashboard_win.winfo_y(),
        )

    def _drag_dashboard(self, event):
        offset = getattr(self, "_dashboard_drag_offset", None)
        if not offset:
            return
        x = max(0, min(event.x_root - offset[0], self.SW - self.dashboard_win.winfo_width()))
        y = max(0, min(event.y_root - offset[1], self.SH - self.dashboard_win.winfo_height()))
        self.dashboard_win.geometry(f"+{x}+{y}")

    CONSOLE_PAGES = ("Home", "Control", "Chat", "Activity", "Output", "Brain")

    def _console_enabled(self):
        return os.getenv("VANTA_CONSOLE_WINDOW", "1").strip().lower() not in ("0", "false", "no", "off")

    def _console(self):
        """The web console's bridge, wired to this UI the first time it is needed."""
        import console_window
        bridge = console_window.bridge()
        if not getattr(self, "_console_wired", False):
            self._console_wired = True
            base = os.path.dirname(os.path.abspath(__file__))

            def screenshot():
                import system_tools
                self._q.put(("control_result", str(system_tools.take_screenshot(base))))

            def launch(target):
                os.startfile(target)
                self._q.put(("control_result", f"[Success] Opened {target}."))

            def brain():
                import brain_server
                info = brain_server.ensure_running(base)
                brain_server.open_in_browser(info, "")

            bridge.sinks.update(
                command=lambda text: self._in_q.put(("text", text)),
                screenshot=screenshot, launch=launch, brain=brain,
                close=console_window.close_window)
        return bridge

    def _web_console(self, page=None, toggle=False):
        """Show the dashboard as a designed window. False means use the Tk one."""
        if not self._console_enabled():
            return False
        try:
            import console_window
            self._console()
            if toggle:
                return console_window.toggle(page or "Home") is not None
            return bool(console_window.open_window(page or "Home")["available"])
        except Exception as exc:
            print(f"[Console] Web console unavailable ({type(exc).__name__}: {exc}); "
                  f"using the built-in dashboard.")
            return False

    def _console_note(self, kind, value):
        """Keep the web console's record current; never let it disturb the UI."""
        try:
            bridge = self._console()
            if kind == "answer":
                bridge.answered(value)
            elif kind == "user":
                bridge.user_said(value)
            elif kind == "activity":
                bridge.activity(value)
            elif kind == "report":
                bridge.show_report(value)
            elif kind == "state":
                bridge.set_state(value)
            elif kind == "mode":
                bridge.set_mode(value.get("mode") or value.get("label"), value.get("label"))
        except Exception:
            pass

    def _toggle_dashboard(self, event=None):
        if self._web_console(toggle=True):
            return
        self.dashboard_visible = not self.dashboard_visible
        if self.dashboard_visible:
            self.dashboard_win.deiconify()
        else:
            self.dashboard_win.withdraw()

    def _show_dashboard(self):
        if not self.dashboard_visible:
            self.dashboard_visible = True
            self.dashboard_win.deiconify()
            self.dashboard_win.lift()

    def _hide_dashboard(self):
        if self.dashboard_visible:
            self.dashboard_visible = False
            self.dashboard_win.withdraw()


    # ------------------------------------------------------------ #
    # Fullscreen click-through ambient layer (border glow + ripples)
    # ------------------------------------------------------------ #
    def _build_border_window(self):
        self.root = tk.Tk()
        # A Tk root is mapped as an ordinary application window unless it is
        # withdrawn before its first idle update. Configure the ambient layer
        # completely before exposing it so the shell never sees a taskbar
        # candidate for one frame.
        self.root.withdraw()
        window_identity.apply_to(self.root, base_dir=os.path.dirname(__file__))
        self.root.title("VANTA Ambient")
        self.root.attributes('-fullscreen', True)
        self.root.attributes('-topmost', True)
        self.root.overrideredirect(True)
        self.root.attributes('-transparentcolor', MAGIC)
        self.root.config(bg=MAGIC)

        self.root.wm_attributes('-disabled', True)

        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            exstyle = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, exstyle | 0x00000020 | 0x00080000)
        except Exception as e:
            print(f"[UI] Could not set WS_EX_TRANSPARENT: {e}")

        window_identity.hide_from_taskbar(self.root)

        self.SW = self.root.winfo_screenwidth()
        self.SH = self.root.winfo_screenheight()

        self.bc = Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self.bc.pack(fill=tk.BOTH, expand=True)
        self.root.deiconify()

    # ------------------------------------------------------------ #
    # Dynamic-Island-style notch
    #
    # Same trick the original code used (a rounded CTkFrame pushed
    # partly above y=0 so only its bottom corners are visible), kept
    # because it's already proven not to fight the transparent-color
    # window hack -- just tuned for real notch proportions and given a
    # smooth width/height tween between a small idle "pill" and a
    # wider "active" state, instead of resizing instantly.
    # ------------------------------------------------------------ #
    def _build_notch(self):
        # At rest this is the mark, not a word. "VANTA" written across the top
        # of the screen is a label nobody needs to read twice, and a grey dot
        # says nothing at all. The mark sits in a circle and grows into a pill
        # only when there is something to say.
        # Idle is a compact orb. It grows into a pill only when there is
        # something to hear, read, or answer.
        self.NOTCH_IDLE_W,   self.NOTCH_IDLE_H   = 54, 54
        self.NOTCH_ACTIVE_W, self.NOTCH_ACTIVE_H = 300, 44
        self.NOTCH_SPEAK_W,  self.NOTCH_SPEAK_H  = 640, 46
        self.NOTCH_CARD_W,   self.NOTCH_CARD_MAX_H = 620, 220
        self._notch_radius = 21
        self._mark_rotation = 0.0
        self._mark_last = None
        self._notch_target_w = self.NOTCH_IDLE_W

        self.pill_win = ctk.CTkToplevel(self.root, fg_color=MAGIC)
        self.pill_win.withdraw()
        self.pill_win.overrideredirect(True)
        self.pill_win.attributes('-topmost', True)
        self.pill_win.wm_attributes("-transparentcolor", MAGIC)
        window_identity.hide_from_taskbar(self.pill_win, self.root)

        self.pill_frame = ctk.CTkFrame(
            self.pill_win, corner_radius=self._notch_radius,
            fg_color=SURFACE, border_color=SEPARATOR, border_width=1
        )
        self.pill_frame.pack(fill=tk.BOTH, expand=True)

        # Pinned left rather than centred: with a centred row the mark slid
        # sideways every time the label changed length, which made a status
        # line look like something was moving on its own.
        row = ctk.CTkFrame(self.pill_frame, fg_color="transparent")
        self._notch_row = row
        row.place(relx=0.5, rely=0.5, anchor="center")

        # The mark carries state in its hexagon: colour for what VANTA is
        # doing, rotation for the fact that it is still doing it. The V stays
        # still and white throughout, so the identity never flickers.
        self.notch_mark = tk.Canvas(
            row, width=36, height=36, highlightthickness=0, bd=0, bg=SURFACE)
        self.notch_mark.pack(side=tk.LEFT)

        self.notch_label = ctk.CTkLabel(
            row, text="",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="normal"),
            text_color=LABEL_PRIMARY,
            wraplength=self.NOTCH_SPEAK_W - 80,
            justify="left"
        )
        self.notch_label.pack(side=tk.LEFT)

        # During push-to-talk the identity mark gives way to a restrained live
        # waveform. It confirms that recording is active without doing audio
        # work on Tk's event thread.
        self.notch_wave = tk.Canvas(
            row, width=72, height=28, highlightthickness=0, bd=0, bg=SURFACE)

        # The answer card occupies this same top-level window. Keeping one
        # window prevents focus flicker and makes the expansion feel physical.
        self._notch_card = ctk.CTkFrame(self.pill_frame, fg_color="transparent")
        self._notch_question_label = ctk.CTkLabel(
            self._notch_card, text="", anchor="w", justify="left",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11, weight="normal"),
            text_color=LABEL_TERTIARY, wraplength=self.NOTCH_CARD_W - 44)
        self._notch_question_label.pack(fill="x", padx=22, pady=(15, 3))
        self._notch_answer_label = ctk.CTkLabel(
            self._notch_card, text="", anchor="w", justify="left",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="normal"),
            text_color=LABEL_PRIMARY, wraplength=self.NOTCH_CARD_W - 44)
        self._notch_answer_label.pack(fill="x", padx=22, pady=(0, 16))

        # Clicking the orb or pressing F8 opens the Control Center.
        self.pill_frame.bind("<Button-1>", self._toggle_dashboard)
        self.notch_label.bind("<Button-1>", self._toggle_dashboard)
        self.notch_mark.bind("<Button-1>", self._toggle_dashboard)
        self.notch_wave.bind("<Button-1>", self._toggle_dashboard)
        self._notch_card.bind("<Button-1>", self._toggle_dashboard)
        self._notch_question_label.bind("<Button-1>", self._toggle_dashboard)
        self._notch_answer_label.bind("<Button-1>", self._toggle_dashboard)

        self._cur_w, self._cur_h = self.NOTCH_IDLE_W, self.NOTCH_IDLE_H
        self._tween_steps = []
        self._set_notch_geometry(self._cur_w, self._cur_h, instant=True)
        self._draw_mark()
        self.pill_win.deiconify()

    #: How much larger the idle mark is drawn before being resampled down.
    #: Four is enough to hide the stepping on a diagonal at this size and
    #: cheap enough to redraw as the breath changes colour.
    _MARK_SUPERSAMPLE = 4

    def _idle_breath_colour(self):
        """The pill's border at rest: a slow breath, barely there.

        On the border rather than inside the mark, so the identity itself
        never flickers and there is only one outline on screen. Quantised to
        a few steps because the border is reconfigured rarely, not per frame.
        """
        breathe = (math.sin(self._t * 0.55) + 1) / 2
        step = round(breathe * 3) / 3
        return _blend(SEPARATOR, _dim(LABEL_SECONDARY, 0.55), step)

    def _render_idle_mark(self, tint):
        """The idle V, drawn large and resampled down so its edges are clean.

        Cached per colour: the breath moves through a handful of tints, so
        this renders a few images once rather than one per frame.
        """
        cache = getattr(self, "_idle_mark_cache", None)
        if cache is None:
            cache = self._idle_mark_cache = {}
        if tint in cache:
            return cache[tint]
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:
            return None
        try:
            size = 32 * self._MARK_SUPERSAMPLE
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            unit = self._MARK_SUPERSAMPLE
            # A plain V, optically centred: slightly above the geometric
            # middle, because a downward point reads as low when centred.
            draw.line(
                [(10 * unit, 11 * unit), (16 * unit, 22 * unit),
                 (22 * unit, 11 * unit)],
                fill=LABEL_PRIMARY, width=int(2.2 * unit), joint="curve")
            # No ring. The pill is already a rounded shape around this mark,
            # and an arc inside it produced two concentric outlines around a
            # glyph 26 pixels across -- which is what made the idle state look
            # fussy rather than quiet. At rest there is now one shape and one
            # letter, and the breathing moved to the pill's own border where
            # it does not have to compete with anything.
            image = image.resize((32, 32), Image.LANCZOS)
            cache[tint] = ImageTk.PhotoImage(image)
            return cache[tint]
        except Exception as exc:
            print(f"[UI] Could not render the idle mark: {exc}")
            return None

    SOUL_SIZE = 36

    def _draw_mark(self):
        """Paint VANTA's soul: one living light whose mood follows the state.

        Called from the animation loop, so it advances by real elapsed time
        (not a fixed step) and the soul eases between moods rather than
        cutting, which is what keeps a change of state smooth at any frame
        rate. Falls back to a still dot if numpy/PIL are unavailable.
        """
        now = time.monotonic()
        mind = getattr(self, "_soul", None)
        if mind is None:
            try:
                import soul as _soul
                mind = self._soul = _soul.Soul("idle")
            except Exception:
                self._soul = False
                mind = False
            self._soul_clock = now
        if mind is False:
            self.notch_mark.delete("orb-decoration")
            self.notch_mark.create_oval(13, 13, 23, 23, fill=LABEL_PRIMARY, outline="",
                                        tags="orb-decoration")
            return
        dt = now - getattr(self, "_soul_clock", now)
        self._soul_clock = now
        state = self._state
        mind.set_state({STATE_LISTENING: "listening", STATE_THINKING: "thinking",
                        STATE_SPEAKING: "speaking", STATE_LOADING: "loading"}.get(state, "idle"))
        # Whether the owner is confirmed shows in the soul at rest: alive
        # (bright, moving, breathing) when the camera has matched him, dull
        # (small, grey, nearly still) otherwise. Read at most once a second.
        if now - getattr(self, "_identity_polled", 0.0) >= 1.0:
            self._identity_polled = now
            try:
                import camera_awareness
                status = camera_awareness.trust_status()
            except Exception:
                status = "unknown"
            alive = status in ("verified", "override")
            was_alive = getattr(self, "_identity_alive", None)
            self._identity_alive = alive
            self._identity_status = status
            if alive and was_alive is False:
                mind.pulse()
            mind.set_guarded(not alive)
        if state == STATE_SPEAKING:
            mind.set_level(0.35 + 0.35 * abs(math.sin(self._t * 3.3)) * (0.6 + 0.4 * math.sin(self._t * 1.1)))
        elif state == STATE_LISTENING:
            mind.set_level(getattr(self, "_mic_level", 0.0) or
                           0.25 + 0.2 * abs(math.sin(self._t * 2.7)))
        else:
            mind.set_level(0.0)
        mind.step(dt)
        try:
            from PIL import ImageTk
            image = mind.render(self.SOUL_SIZE, SURFACE)
            photo = getattr(self, "_soul_photo", None)
            if photo is None:
                photo = self._soul_photo = ImageTk.PhotoImage(image)
            else:
                # Repaint the one image Tk already holds. Handing the canvas a
                # new PhotoImage every frame meant allocating one and deleting
                # the last -- about 3 ms per frame, most of the stutter.
                photo.paste(image)
        except Exception:
            return
        half = self.SOUL_SIZE // 2
        if not self.notch_mark.find_withtag("soul"):
            self.notch_mark.delete("orb-decoration")
            self.notch_mark.create_image(half, half, image=photo, tags=("soul", "orb-decoration"))
        # Persistent voice state belongs on the always-visible idle object,
        # not in a transient toast. A small crossed speaker leaves the V and
        # its animated state readable while making silence unmistakable.
        self.notch_mark.delete("voice-muted")
        if getattr(self, "_voice_muted", False):
            colour = "#ff9f0a"
            self.notch_mark.create_polygon(
                21, 24, 24, 24, 28, 21, 28, 30, 24, 27, 21, 27,
                fill=colour, outline="", tags="voice-muted")
            self.notch_mark.create_line(
                20, 20, 31, 31, fill=colour, width=2,
                capstyle=tk.ROUND, tags="voice-muted")

    def _set_notch_geometry(self, w, h, instant=False):
        x = (self.SW - w) // 2
        y = 10
        self.pill_win.geometry(f"{w}x{h}+{x}+{y}")
        self._cur_w, self._cur_h = w, h

    def _animate_notch_to(self, target_w, target_h, duration_ms=240):
        """Smooth ease-out width/height tween instead of an instant resize --
        this single touch is most of what makes a state change feel like a
        considered UI rather than a debug overlay."""
        steps = max(1, duration_ms // 16)
        start_w, start_h = self._cur_w, self._cur_h
        self._tween_steps = []
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** 3  # ease-out cubic
            w = start_w + (target_w - start_w) * eased
            h = start_h + (target_h - start_h) * eased
            self._tween_steps.append((int(w), int(h)))

    # How many frames of the working animation have elapsed. Movement is the
    # only honest signal that something is still happening.
    _WORKING_FRAMES = ("", ".", "..", "...")

    # Text VANTA was fed, as opposed to anything Himanshu said. A proactive
    # startup turn carries the whole system prompt as its detail, so the orb
    # read "[SYSTEM EVENT: STARTUP SYSTEM CHE..." -- VANTA's own scaffolding,
    # shown to the one person who has no use for it.
    _INTERNAL_PREFIXES = ("[system event", "[system startup", "[proactive",
                          "[barge-in", "[clarification", "analyze this",
                          "you are vanta")

    #: How long a step line stays on the notch before it reads as stale. A
    #: step that is still running after this says so by being replaced with
    #: the plain state word rather than by claiming to still be at a step it
    #: finished.
    STEP_LINE_TTL = 45.0

    @staticmethod
    def _show_steps():
        """Whether the notch narrates each step ("Searching the web", "Running
        npm install"). Off by default: a working VANTA shows only that it is
        working, and the steps stay in the Activity page. VANTA_SHOW_STEPS=1
        brings the running commentary back."""
        return os.getenv("VANTA_SHOW_STEPS", "0").strip().lower() in ("1", "true", "yes", "on")

    def _set_step_line(self, text):
        """What VANTA is doing right now, in one line on the notch.

        These messages were being dropped on the floor: the queue carried
        "Running: npm install" and the handler was `pass`, so the only way to
        see which step a turn was on was the Activity page or the terminal.
        The alternative that had grown up instead was opening a 680x720
        results card, which answers the question by covering the work.
        """
        if not self._show_steps():
            return
        clean = self._notch_clean_text(text, 48)
        if not clean:
            return
        self._step_line = clean
        self._step_line_at = time.monotonic()
        # The expanded card shows the utterance it heard above a generic
        # "Working on it..."; that second line is exactly where the step
        # belongs when the card happens to be up.
        if (self._notch_question
                and hasattr(self, "_notch_answer_label")
                and self._notch_card.winfo_manager()):
            self._notch_answer_label.configure(text=clean)
        self._update_notch()

    def _current_step_line(self):
        """The step line if it is still current, else ""."""
        line = getattr(self, "_step_line", "")
        if not line:
            return ""
        if time.monotonic() - getattr(self, "_step_line_at", 0.0) > self.STEP_LINE_TTL:
            self._step_line = ""
            return ""
        return line

    def _clear_step_line(self):
        self._step_line = ""
        self._step_line_at = 0.0

    def _is_internal_detail(self, text):
        low = str(text or "").strip().lower()
        if not low:
            return True
        if low.startswith("[") and "]" in low[:60]:
            return True
        return any(low.startswith(prefix) for prefix in self.__class__._INTERNAL_PREFIXES)

    def _update_notch(self):
        # Dashboard state must follow the runtime even while the response card
        # temporarily owns the notch's pixels.
        state_changed = self._state != self._prev_rendered_state
        if state_changed:
            self._mark_last = None
            self._cc_set_state(self._state)
            self._prev_rendered_state = self._state
            if self._state not in (STATE_THINKING, STATE_LOADING):
                # The work is over, so the last step it ran is history. Left
                # up, it would sit on an idle notch describing something that
                # finished minutes ago.
                self._clear_step_line()

        if self._notch_answer and time.monotonic() < self._notch_answer_until:
            self._show_notch_card()
            return
        if self._notch_answer and time.monotonic() >= self._notch_answer_until:
            self._notch_answer = ""
            self._notch_answer_until = 0.0

        self._notch_card.place_forget()
        self._notch_row.place(relx=0.5, rely=0.5, anchor="center")
        label = base_label = STATE_LABELS.get(self._state, "VANTA")

        if self._state == STATE_IDLE:
            # Assistant is the neutral state; named modes remain visible so a
            # glance explains why VANTA is quiet or behaving differently.
            mode_label = getattr(self, "_active_mode_label", "ASSISTANT")
            label = base_label = ("" if mode_label == "ASSISTANT"
                                  else mode_label.title())
        elif self._state == STATE_SPEAKING:
            # One word. The voice is already carrying the content, and a
            # transcript racing the audio is two things to follow at once.
            label = base_label = STATE_LABELS[STATE_SPEAKING]
        elif self._state == STATE_LISTENING:
            # The local follow-up gate knows when Himanshu has begun an
            # utterance. Say "Speaking" then; push-to-talk and clarification
            # recording, where the mic is merely open, remain "Listening".
            detail = getattr(self, "_status_detail", "")
            label = base_label = ("Speaking" if detail == "Speaking"
                                  else STATE_LABELS[STATE_LISTENING])
        elif self._state in (STATE_THINKING, STATE_LOADING):
            # The step being run beats the echo of what was said: by the time
            # a turn is three actions deep, "what is it doing now" is the
            # question, and the utterance is already on the card above.
            if self._show_steps():
                detail = (self._current_step_line() or self._notch_question
                          or getattr(self, "_status_detail", ""))
            else:
                # Steps are off: the plain state word (or what he just said),
                # never a running commentary of what each action is doing.
                detail = self._notch_question
            if self._is_internal_detail(detail):
                # Nothing he said, so nothing to echo: fall back to the plain
                # state word rather than showing him the machinery.
                base_label = STATE_LABELS.get(self._state, "Working")
            else:
                # A short line, not the whole utterance: the orb is a status
                # light, not a transcript.
                detail = " ".join(detail.split())
                if len(detail) > 32:
                    detail = detail[:31].rstrip() + "…"
                base_label = detail
            dots = self._WORKING_FRAMES[int(self._t * 2) % len(self._WORKING_FRAMES)]
            label = f"{base_label}{dots}"

        height = (self.NOTCH_IDLE_H if self._state == STATE_IDLE
                  else self.NOTCH_SPEAK_H if self._state == STATE_SPEAKING
                  else self.NOTCH_ACTIVE_H)
        # Measured from the label without its animated dots, and measured
        # before the dotted text goes in -- otherwise the pill twitches wider
        # and narrower four times a second for the whole of a long task.
        width = self._notch_width_for(base_label, height)

        self.notch_label.configure(text=label)
        self.notch_label.pack_configure(padx=(0 if not label else 10, 0))
        # The soul answers the voice itself while listening, so the separate
        # waveform is gone; the soul stays on the left of the label in every
        # state.
        self.notch_wave.pack_forget()
        if not self.notch_mark.winfo_manager():
            self.notch_mark.pack(side=tk.LEFT, before=self.notch_label)

        # Border colour can change while state and geometry stay identical:
        # opening/expiring the hands-free window is exactly that case.  Keep
        # styling outside the resize guard so the blue contract is immediate.
        self.pill_frame.configure(
            corner_radius=height // 2 if self._state == STATE_IDLE
            else self._notch_radius,
            border_width=1,
            border_color=_dim(ACCENTS[STATE_LISTENING], 0.35)
            if (self._state == STATE_IDLE
                and time.monotonic() < self._conversation_until)
            else self._idle_breath_colour()
            if self._state == STATE_IDLE
            else _dim(ACCENTS.get(self._state, LABEL_SECONDARY), 0.72))
        if state_changed or abs(width - self._notch_target_w) > 6:
            # A circle needs its corner radius to be half its height; the pill
            # keeps the softer fixed radius.
            self._notch_target_w = width
            self._animate_notch_to(width, height)

    @staticmethod
    def _notch_clean_text(text, limit):
        text = " ".join(str(text or "").split())
        if len(text) <= limit:
            return text
        return text[:limit - 3].rstrip() + "..."

    def _set_notch_question(self, text):
        """Put the recognized utterance in the existing notch, compactly.

        The former behaviour expanded a second, rectangular HEARD card as
        soon as transcription completed.  That duplicated the assistant UI
        and covered the screen.  The same notch now grows only as wide as the
        short transcript requires; answer cards remain available for actual
        answers that need reading.
        """
        clean = self._notch_clean_text(text, 180)
        if not clean or self._is_internal_detail(clean):
            return
        self._notch_question = clean
        self._notch_answer = ""
        self._notch_answer_until = 0.0
        # A new utterance supersedes the old action label immediately.  The
        # next real action will publish its own fresh step line.
        self._clear_step_line()
        self._update_notch()

    def _show_notch_question(self):
        """Expand as soon as transcription completes so it can be checked."""
        if not self._notch_question:
            return
        self._notch_row.place_forget()
        self._notch_question_label.configure(text="HEARD  " + self._notch_question)
        self._notch_answer_label.configure(text="Working on it...")
        self._notch_card.place(relx=0, rely=0, relwidth=1, relheight=1)
        q_lines = max(1, (len(self._notch_question) + 60) // 61)
        height = min(132, 62 + q_lines * 17)
        width = min(560, max(360, 220 + min(len(self._notch_question), 110) * 3))
        self.pill_frame.configure(
            corner_radius=self._notch_radius,
            border_color=_dim(ACCENTS[STATE_THINKING], 0.72))
        self._notch_target_w = width
        if abs(width - self._cur_w) > 3 or abs(height - self._cur_h) > 3:
            self._animate_notch_to(width, height, duration_ms=240)

    def _set_notch_answer(self, text):
        clean = self._notch_clean_text(text, 700)
        if not clean:
            return
        self._notch_answer = clean
        words = max(1, len(clean.split()))
        self._notch_answer_until = (
            time.monotonic() + min(22.0, max(8.0, words * 0.32)))
        self._show_notch_card()

    def _show_notch_card(self):
        question = self._notch_question
        answer = self._notch_answer
        self._notch_row.place_forget()
        self._notch_question_label.configure(
            text=("YOU  " + question) if question else "VANTA")
        self._notch_answer_label.configure(text=answer)
        self._notch_card.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Labels perform the real wrapping; these line estimates only choose a
        # calm, bounded target size before Tk completes its next layout pass.
        q_lines = max(1, (len(question) + 78) // 79) if question else 1
        a_lines = max(1, (len(answer) + 72) // 73)
        height = min(self.NOTCH_CARD_MAX_H, 48 + q_lines * 16 + a_lines * 20)
        width = min(
            self.NOTCH_CARD_W, max(380, 250 + min(len(answer), 120) * 3))
        self.pill_frame.configure(corner_radius=self._notch_radius)
        self._notch_target_w = width
        if abs(width - self._cur_w) > 3 or abs(height - self._cur_h) > 3:
            self._animate_notch_to(width, height, duration_ms=280)

    def _draw_voice_wave(self):
        if (self._state != STATE_LISTENING
                or not self.notch_wave.winfo_manager()):
            return
        self.notch_wave.delete("all")
        accent = ACCENTS[STATE_LISTENING]
        center = 14
        for i in range(9):
            phase = self._t * 5.2 + i * 0.72
            amplitude = 3.0 + 8.0 * (0.35 + 0.65 * abs(math.sin(phase)))
            x = 4 + i * 8
            self.notch_wave.create_line(
                x, center - amplitude / 2, x, center + amplitude / 2,
                fill=accent, width=2, capstyle=tk.ROUND)

    def _notch_width_for(self, base_label, height):
        """Size the pill to what it is actually saying.

        A fixed 300px pill is a banner: short states float in dead space and
        long ones clip. Measuring the label means the orb only ever takes the
        room it needs, which is what makes it read as one object reacting
        rather than a bar that happens to change colour.

        Cached by text, because this runs on every animation frame and the
        measurement costs a widget reflow.
        """
        if not base_label:
            return self.NOTCH_IDLE_W
        if self._state == STATE_IDLE:
            return min(180, max(96, 58 + len(base_label) * 8))
        cached = getattr(self, "_notch_width_cache", None)
        if cached and cached[0] == base_label:
            return cached[1]
        try:
            self.notch_label.configure(text=base_label)
            self.notch_label.update_idletasks()
            text_px = self.notch_label.winfo_reqwidth()
            scaling = ctk.ScalingTracker.get_window_scaling(self.pill_win)
            text_w = text_px / max(scaling, 0.1)
        except Exception:
            text_w = len(base_label) * 7.5
        # mark + gap + text + room for the three working dots + end padding
        width = 36 + 10 + text_w + 20 + 26
        width = int(max(self.NOTCH_IDLE_W, min(width, self.NOTCH_SPEAK_W)))
        self._notch_width_cache = (base_label, width)
        return width

    # ------------------------------------------------------------ #
    # Spotlight-style command palette
    # ------------------------------------------------------------ #
    def _build_input_window(self):
        self.input_win = ctk.CTkToplevel(self.root, fg_color=MAGIC)
        self.input_win.withdraw()
        self.input_win.overrideredirect(True)
        self.input_win.attributes('-topmost', True)
        self.input_win.wm_attributes("-transparentcolor", MAGIC)
        window_identity.hide_from_taskbar(self.input_win, self.root)

        w, h = 680, 64
        x = (self.SW - w) // 2
        y = (self.SH - h) // 2 - 120
        self.input_win.geometry(f"{w}x{h}+{x}+{y}")

        self.input_frame = ctk.CTkFrame(
            self.input_win, corner_radius=22, fg_color=SURFACE_DEEP,
            border_width=1, border_color=SEPARATOR
        )
        self.input_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.brain_var = ctk.StringVar(value="L1")
        self.brain_selector = ctk.CTkOptionMenu(
            self.input_frame,
            values=["L1", "L2", "L3", "L4"],
            variable=self.brain_var,
            command=self._on_brain_switch,
            width=130,
            fg_color=SURFACE_DEEP,
            button_color=SURFACE_DEEP,
            button_hover_color=SEPARATOR,
            dropdown_fg_color=SURFACE,
            dropdown_text_color=LABEL_PRIMARY,
            text_color=LABEL_PRIMARY,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="bold")
        )
        self.brain_selector.pack(side=tk.RIGHT, padx=(0, 16), pady=8)

        self.entry_input = ctk.CTkEntry(
            self.input_frame,
            placeholder_text="Ask Vanta to do something...",
            placeholder_text_color=LABEL_TERTIARY,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=17),
            fg_color="transparent",
            border_width=0,
            text_color=LABEL_PRIMARY,
        )
        self.entry_input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(22, 0), pady=8)

        self.entry_input.bind("<Return>", self._on_input_submit)
        self.entry_input.bind("<Escape>", self._on_input_cancel)

        self.input_win.withdraw()

    def _on_brain_switch(self, choice):
        # Notify the backend
        self._in_q.put(f"/brain {choice.lower()}")
        # Show a quick confirmation in the UI pill
        self._last_info = f"Switched to {choice}"
        self._state = STATE_SPEAKING
        self._update_notch()
        self.root.after(2500, self._reset_idle)

    def _reset_idle(self):
        if self._state == STATE_SPEAKING and self._last_info.startswith("Switched"):
            self._state = STATE_IDLE
            self._update_notch()

    # ------------------------------------------------------------ #
    # Console card
    # ------------------------------------------------------------ #
    def _build_info_window(self):
        self.info_win = ctk.CTkToplevel(self.root, fg_color="#101216")
        self.info_win.withdraw()
        window_identity.apply_to(
            self.root, self.info_win, base_dir=os.path.dirname(__file__))
        self.info_win.title("VANTA")
        self.info_win.overrideredirect(False)
        self.info_win.attributes('-topmost', False)
        self.info_win.protocol("WM_DELETE_WINDOW", self._close_details)
        # Keep the ordinary frame and its useful system controls, but make it
        # an owned application surface so it never becomes a second taskbar
        # application. Ownership is reapplied after every remap by the helper.
        window_identity.hide_from_taskbar(
            self.info_win, self.root, tool_window=False)

        # Keep the whole card inside Tk's own logical screen coordinates. This
        # matters on Windows display scaling: right-aligning a large custom
        # window put its titlebar controls beyond the physical edge at 125%.
        screen_w = self.info_win.winfo_screenwidth()
        screen_h = self.info_win.winfo_screenheight()
        # A reading surface belongs near the visual centre, but slightly high
        # so the first answer/table is where the eye naturally lands and the
        # current application remains visible around it. Keep every edge on
        # screen even under Windows display scaling.
        w = max(520, min(680, int(screen_w * 0.42)))
        h = max(400, min(620, int(screen_h * 0.60)))
        x = max(16, (screen_w - w) // 2)
        y = max(48, min(int(screen_h * 0.10), screen_h - h - 24))
        self.info_win.geometry(f"{w}x{h}+{x}+{y}")
        self.info_win.minsize(480, 360)

        self.info_frame = ctk.CTkFrame(
            self.info_win, corner_radius=0, fg_color="#15181d"
        )
        self.info_frame.pack(fill=tk.BOTH, expand=True)

        self._detail_drag_origin = None

        header = ctk.CTkFrame(self.info_frame, fg_color="transparent", height=36)
        header.pack(fill=tk.X, padx=16, pady=(14, 6))

        self.info_title = ctk.CTkLabel(
            header, text="VANTA",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=15, weight="bold"),
            text_color=LABEL_PRIMARY
        )
        self.info_title.pack(side=tk.LEFT)

        self.info_status = ctk.CTkLabel(
            self.info_frame, text="RESULT",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11, weight="bold"),
            text_color="#8e8e93", anchor="w"
        )
        self.info_status.pack(fill=tk.X, padx=22, pady=(0, 8))

        self.info_research = ctk.CTkFrame(
            self.info_frame, fg_color="transparent", corner_radius=0)
        self.info_summary_card = ctk.CTkFrame(
            self.info_research, fg_color=SURFACE_DEEP, corner_radius=8,
            border_color=SEPARATOR, border_width=1)
        self.info_summary_card.pack(fill=tk.X, padx=16, pady=(2, 10))
        self.info_summary_kicker = ctk.CTkLabel(
            self.info_summary_card, text="WHAT MATTERS", anchor="w",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11, weight="bold"),
            text_color="#64d2ff")
        self.info_summary_kicker.pack(fill=tk.X, padx=16, pady=(14, 5))
        self.info_summary = ctk.CTkLabel(
            self.info_summary_card, text="", anchor="w", justify="left",
            wraplength=max(360, w - 92),
            font=ctk.CTkFont(family=self.FONT_TEXT, size=14),
            text_color=LABEL_PRIMARY)
        self.info_summary.pack(fill=tk.X, padx=16, pady=(0, 14))
        self.info_sources_label = ctk.CTkLabel(
            self.info_research, text="SOURCES", anchor="w",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=11, weight="bold"),
            text_color=LABEL_TERTIARY)
        self.info_sources_label.pack(fill=tk.X, padx=22, pady=(2, 5))
        self.info_source_list = ctk.CTkScrollableFrame(
            self.info_research, fg_color="transparent", corner_radius=0)
        self.info_source_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 12))

        # Ordinary <REPORT> output has its own visual language.  Research uses
        # source cards above; briefings use one card per section below.  Both
        # replace the old "put everything in a textbox" behaviour.
        self.info_report = ctk.CTkScrollableFrame(
            self.info_frame, fg_color="transparent", corner_radius=0)

        # Images are first-class result artifacts, not a path printed into a
        # text box.  This panel is populated only by an explicit display/view
        # action; ordinary camera capture stays unobtrusive.
        self.info_image = ctk.CTkFrame(
            self.info_frame, fg_color="transparent", corner_radius=0)
        self.info_image_card = ctk.CTkFrame(
            self.info_image, fg_color=SURFACE_DEEP, corner_radius=8,
            border_color=SEPARATOR, border_width=1)
        self.info_image_card.pack(fill=tk.BOTH, expand=True, padx=16,
                                  pady=(2, 10))
        self.info_image_label = ctk.CTkLabel(
            self.info_image_card, text="", fg_color="transparent")
        self.info_image_label.pack(fill=tk.BOTH, expand=True, padx=12,
                                   pady=(12, 6))
        self.info_image_caption = ctk.CTkLabel(
            self.info_image_card, text="", anchor="w", justify="left",
            wraplength=max(360, w - 104),
            font=ctk.CTkFont(family=self.FONT_TEXT, size=12),
            text_color=LABEL_SECONDARY)
        self.info_image_caption.pack(fill=tk.X, padx=14, pady=(0, 12))
        self._info_image_ref = None

        btn_copy = ctk.CTkButton(
            header, text="Copy", width=48, height=28, corner_radius=7,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=12),
            fg_color=SURFACE_DEEP, hover_color=SEPARATOR,
            text_color=LABEL_SECONDARY, command=self._copy_details
        )
        btn_copy.pack(side=tk.RIGHT, padx=(0, 6))

        self.info_text_widget = ctk.CTkTextbox(
            self.info_frame,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=15),
            fg_color=SURFACE_DEEP,
            text_color=LABEL_PRIMARY,
            wrap="word",
            corner_radius=10,
            border_spacing=14
        )
        self.info_text_widget.pack(fill=tk.BOTH, expand=True, padx=16, pady=(2, 16))
        # CTkTextbox intentionally exposes a conservative public API; its
        # underlying Tk text control gives us typography and clickable links
        # without adding a browser-rendering dependency.
        native_text = getattr(self.info_text_widget, "_textbox", None)
        if native_text is not None:
            native_text.configure(spacing1=4, spacing3=8, padx=6, pady=4)
            native_text.tag_configure("section", foreground="#64d2ff",
                                      font=(self.FONT_TEXT, 12, "bold"),
                                      spacing1=14, spacing3=5)
            native_text.tag_configure("source", foreground=LABEL_PRIMARY,
                                      font=(self.FONT_TEXT, 13, "bold"),
                                      spacing1=9)
            native_text.tag_configure("link", foreground="#5ac8fa",
                                      underline=True)

        self.info_win.withdraw()

        # Compatibility-only hidden window retained for capture filtering.
        # The native Windows title bar now owns minimize and restore; VANTA
        # must not leave a custom floating restore pill on the desktop.
        self.info_restore_win = ctk.CTkToplevel(self.root, fg_color=MAGIC)
        self.info_restore_win.withdraw()
        self.info_restore_win.overrideredirect(True)
        self.info_restore_win.attributes("-topmost", True)
        self.info_restore_win.wm_attributes("-transparentcolor", MAGIC)
        window_identity.hide_from_taskbar(self.info_restore_win, self.root)
        restore_w, restore_h = 116, 42
        restore_x, restore_y = 32, max(32, screen_h - restore_h - 64)
        self.info_restore_win.geometry(
            f"{restore_w}x{restore_h}+{restore_x}+{restore_y}")
        self.info_restore_button = ctk.CTkButton(
            self.info_restore_win, text="VANTA  Results", width=108, height=34,
            corner_radius=10, fg_color=SURFACE, hover_color=SURFACE_DEEP,
            border_color=SEPARATOR, border_width=1,
            text_color=LABEL_PRIMARY,
            font=ctk.CTkFont(family=self.FONT_TEXT, size=12, weight="bold"),
            command=self._restore_details,
        )
        self.info_restore_button.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.info_restore_win.withdraw()

    def _begin_detail_drag(self, event):
        self._detail_drag_origin = (
            event.x_root, event.y_root,
            self.info_win.winfo_x(), self.info_win.winfo_y(),
        )

    def _drag_details(self, event):
        if not self._detail_drag_origin:
            return
        sx, sy, wx, wy = self._detail_drag_origin
        x = wx + event.x_root - sx
        y = wy + event.y_root - sy
        max_x = max(0, self.info_win.winfo_screenwidth() - self.info_win.winfo_width())
        max_y = max(0, self.info_win.winfo_screenheight() - self.info_win.winfo_height())
        self.info_win.geometry(f"+{max(0, min(x, max_x))}+{max(0, min(y, max_y))}")

    def _copy_details(self):
        if not hasattr(self, "info_text_widget"):
            return
        text = getattr(self, "_info_copy_text", "") or self.info_text_widget.get("0.0", tk.END).strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _close_details(self):
        """Close only the durable-detail surface, not action activity."""
        self._artifact_visible = False
        self._artifact_signature = None
        self._info_wants_visible = False
        self.info_win.withdraw()
        self.info_restore_win.withdraw()

    def _minimize_details(self):
        """Use the ordinary Windows minimize lifecycle and retain content."""
        if not getattr(self, "_artifact_visible", False):
            self.info_win.withdraw()
            return
        self._info_wants_visible = False
        self.info_restore_win.withdraw()
        self.info_win.iconify()

    def _restore_details(self):
        if not getattr(self, "_artifact_visible", False):
            return
        self._info_wants_visible = True
        self.info_restore_win.withdraw()
        self.info_win.deiconify()
        self.info_win.lift()
        self.info_win.attributes("-topmost", False)

    def _show_artifacts(self, payload):
        """Dedicated visual channel for links, paths and identifiers.

        It is deliberately separate from the notch: the notch mirrors exactly
        what VANTA says, while this card preserves exact machine-readable text
        that would be irritating or ambiguous when spoken aloud.
        """
        payload = payload if isinstance(payload, dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not items:
            return
        signature = tuple((str(item.get("kind", "detail")),
                           str(item.get("value", ""))) for item in items
                          if isinstance(item, dict) and item.get("value"))
        if (not signature
                or not any(display_worthy_text(value) for _kind, value in signature)
                or signature == getattr(self, "_artifact_signature", None)):
            return
        self._artifact_signature = signature
        self._artifact_visible = True
        self._info_copy_text = "\n".join(value for _kind, value in signature)
        if hasattr(self, "info_title"):
            self.info_title.configure(text=str(payload.get("title") or "VANTA Details"))
        status = str(payload.get("status") or "RESULT")
        if hasattr(self, "info_status"):
            status_color = "#ff9f0a" if "PARTIAL" in status.upper() else "#30d158"
            self.info_status.configure(text=status, text_color=status_color)
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        summary = str(payload.get("summary") or "").strip()
        if summary or sources:
            self.info_image.pack_forget()
            self._show_research_cards(summary, sources)
        else:
            self.info_research.pack_forget()
            self.info_report.pack_forget()
            self.info_image.pack_forget()
            if not self.info_text_widget.winfo_manager():
                self.info_text_widget.pack(fill=tk.BOTH, expand=True,
                                           padx=16, pady=(2, 16))
        lines = []
        for kind, value in signature:
            lines.append(kind.upper())
            lines.append(value)
            lines.append("")
        self.info_text_widget.configure(state="normal")
        self.info_text_widget.delete("0.0", tk.END)
        self.info_text_widget.insert("0.0", "\n".join(lines).rstrip())
        native_text = getattr(self.info_text_widget, "_textbox", None)
        if native_text is not None:
            # Rebuild semantic styling each time; unique URL tags make every
            # source independently clickable.
            for tag in tuple(native_text.tag_names()):
                if tag.startswith("url_"):
                    native_text.tag_delete(tag)
            rendered = "\n".join(lines).rstrip().splitlines()
            for line_no, line in enumerate(rendered, 1):
                start, end = f"{line_no}.0", f"{line_no}.end"
                if line in {"SUMMARY", "EVIDENCE"} or line.startswith("SOURCES  ·"):
                    native_text.tag_add("section", start, end)
                elif __import__("re").match(r"^\d{2}\s{2}", line):
                    native_text.tag_add("source", start, end)
                elif line.startswith(("https://", "http://")):
                    tag = f"url_{line_no}"
                    native_text.tag_add("link", start, end)
                    native_text.tag_add(tag, start, end)
                    native_text.tag_bind(
                        tag, "<Button-1>",
                        lambda _event, url=line: webbrowser.open(url),
                    )
                    native_text.tag_bind(tag, "<Enter>",
                                         lambda _event: native_text.configure(cursor="hand2"))
                    native_text.tag_bind(tag, "<Leave>",
                                         lambda _event: native_text.configure(cursor="xterm"))
        self.info_text_widget.configure(state="disabled")
        self._info_wants_visible = True
        self.info_restore_button.configure(text="⌕  Results")
        self.info_restore_win.withdraw()
        self.info_win.deiconify()
        self.info_win.lift()
        self.info_win.attributes("-topmost", False)

    def _show_image_artifact(self, payload):
        """Render an explicitly requested local image in VANTA's result card."""
        payload = payload if isinstance(payload, dict) else {}
        path = os.path.abspath(str(payload.get("path") or ""))
        if not path or not os.path.isfile(path):
            self._show_artifacts({
                "title": "Image unavailable", "status": "ERROR",
                "items": [{"kind": "error",
                           "value": "The requested image no longer exists."}],
            })
            return
        try:
            from PIL import Image
            with Image.open(path) as source:
                image = source.convert("RGB").copy()
            max_w = max(320, self.info_win.winfo_width() - 72)
            max_h = max(240, self.info_win.winfo_height() - 190)
            image.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            self._info_image_ref = ctk.CTkImage(
                light_image=image, dark_image=image, size=image.size)
            self.info_image_label.configure(
                image=self._info_image_ref, text="")
        except Exception as exc:
            self._show_artifacts({
                "title": "Image unavailable", "status": "ERROR",
                "items": [{"kind": "error", "value": str(exc)}],
            })
            return

        self._artifact_signature = (("image", path),)
        self._artifact_visible = True
        self._info_wants_visible = True
        self._info_copy_text = path
        self.info_title.configure(text=str(payload.get("title") or "Camera capture"))
        self.info_status.configure(text="IMAGE", text_color="#64d2ff")
        self.info_image_caption.configure(
            text=str(payload.get("caption") or os.path.basename(path)))
        self.info_text_widget.pack_forget()
        self.info_research.pack_forget()
        self.info_report.pack_forget()
        if not self.info_image.winfo_manager():
            self.info_image.pack(fill=tk.BOTH, expand=True)
        self.info_restore_button.configure(text="◉  Image")
        self.info_restore_win.withdraw()
        self.info_win.deiconify()
        self.info_win.lift()
        self.info_win.attributes("-topmost", False)

    def _show_research_cards(self, summary, sources):
        """Render research as scannable cards; raw receipts stay copyable."""
        self.info_text_widget.pack_forget()
        self.info_report.pack_forget()
        self.info_image.pack_forget()
        if not self.info_research.winfo_manager():
            self.info_research.pack(fill=tk.BOTH, expand=True)
        self.info_summary.configure(
            text=summary or "The search returned sources but no reliable summary.")
        self.info_sources_label.configure(
            text=f"SOURCES  ·  {len(sources)}")
        for child in self.info_source_list.winfo_children():
            child.destroy()
        for index, source in enumerate(sources[:12], 1):
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "Untitled source").strip()
            url = str(source.get("url") or "").strip()
            domain = url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
            card = ctk.CTkFrame(
                self.info_source_list, fg_color=SURFACE_DEEP,
                corner_radius=8, border_color=SEPARATOR, border_width=1)
            card.pack(fill=tk.X, padx=6, pady=5)
            number = ctk.CTkLabel(
                card, text=f"{index:02d}", width=34,
                font=ctk.CTkFont(family=self.FONT_MONO, size=11),
                text_color=LABEL_TERTIARY)
            number.pack(side=tk.LEFT, padx=(12, 4), pady=14)
            body = ctk.CTkFrame(card, fg_color="transparent")
            body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=10)
            ctk.CTkLabel(
                body, text=title, anchor="w", justify="left",
                wraplength=390,
                font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="bold"),
                text_color=LABEL_PRIMARY).pack(fill=tk.X)
            metadata = " · ".join(value for value in (
                str(source.get("source") or "").strip(),
                str(source.get("published") or "").strip(),
            ) if value)
            if metadata:
                ctk.CTkLabel(
                    body, text=metadata, anchor="w", justify="left",
                    wraplength=390,
                    font=ctk.CTkFont(family=self.FONT_TEXT, size=10),
                    text_color=LABEL_TERTIARY).pack(fill=tk.X, pady=(3, 0))
            ctk.CTkButton(
                body, text=f"{domain or 'Open source'}  ↗", width=1, height=22,
                anchor="w", corner_radius=4,
                fg_color="transparent", hover_color="#18222c",
                text_color="#64d2ff",
                font=ctk.CTkFont(family=self.FONT_TEXT, size=10),
                command=lambda target=url: webbrowser.open(target),
            ).pack(fill=tk.X, pady=(3, 0))

    def _web_report(self, payload):
        """Show a report as a designed page in its own window.

        The Tk reading surface put Markdown into text widgets: tables as
        aligned text, diagrams as source, every line the same weight. The same
        content is drawn here as typography, tables with status badges, KPI
        tiles, SVG charts and flow diagrams, with motion. The Tk surface stays
        as the fallback for when no browser engine can be started.
        """
        if os.getenv("VANTA_REPORT_WINDOW", "1").strip().lower() in ("0", "false", "no", "off"):
            return False
        try:
            import report_html
            import report_window
            text, actions = report_html.blocks_from_payload(payload)
            if not str(text).strip() or not display_worthy_text(text):
                return False
            title = str((payload or {}).get("title") or "VANTA Report")
            info = report_window.present(
                text, title, actions,
                command_sink=lambda command: self._in_q.put(("text", command)))
            print(f"[Report] {title!r} shown in {info['browser']}"
                  f"{' (new window)' if info['launched'] else ' (updated in place)'}.")
            return True
        except Exception as exc:
            print(f"[Report] Web report unavailable ({type(exc).__name__}: {exc}); "
                  f"using the built-in window.")
            return False

    def _show_result(self, value):
        """Make a model answer visible, whether it is prose or structured.

        The notch is a speech/status surface. This is the durable reading
        surface. Keeping that distinction means VANTA may speak a short digest
        while the exact answer remains available without opening a terminal.
        """
        payload = value if isinstance(value, dict) else {"content": value}
        text = str(payload.get("content") or payload.get("text") or "").strip()
        if text:
            self._console_note("report", text)
        if self._web_report(payload):
            return True
        if isinstance(payload.get("blocks"), list):
            return self._show_rich_result(payload)
        if not display_worthy_text(text):
            return False
        return self._show_report_if_structured(
            text, force=True, title=str(payload.get("title") or "").strip())

    def _show_report_if_structured(self, value, force=False, title=""):
        """Render prose, sections, tables, code and diagrams as readable UI."""
        import report_view
        text = str(value or "").strip()
        blocks = report_view.parse(text)
        meaningful = [block for block in blocks
                      if block.get("kind") not in {"heading", "spacer", "rule"}]
        if not meaningful:
            return False
        tally = report_view.counts(blocks)
        # A single labelled sentence is not a dashboard.  Requiring either
        # multiple sections or several bullets prevents status/error messages
        # from popping open a grand visual surface they do not deserve.
        if (not force and (report_view.is_plain_prose(blocks)
                or (tally.get("heading", 0) < 2
                    and tally.get("bullet", 0) < 3))):
            return False

        self._artifact_visible = True
        self._artifact_signature = (("report", text),)
        self._info_copy_text = text
        outline = report_view.outline(blocks)
        self.info_title.configure(text=title or (outline[0] if outline else "VANTA Result"))
        self.info_status.configure(text="RESULT", text_color="#64d2ff")
        self.info_text_widget.pack_forget()
        self.info_research.pack_forget()
        self.info_image.pack_forget()
        if not self.info_report.winfo_manager():
            self.info_report.pack(fill=tk.BOTH, expand=True,
                                  padx=10, pady=(0, 12))
        for child in self.info_report.winfo_children():
            child.destroy()

        sections = []
        current = {"title": "Overview", "items": []}
        for block in blocks:
            if block["kind"] == "heading":
                if current["items"]:
                    sections.append(current)
                current = {"title": block.get("text", "Overview"), "items": []}
            elif block["kind"] not in {"spacer", "rule"}:
                current["items"].append(block)
        if current["items"]:
            sections.append(current)

        colours = ("#64d2ff", "#bf5af2", "#30d158", "#ff9f0a", "#0a84ff")
        for index, section in enumerate(sections):
            self._render_report_section(section, colours[index % len(colours)])

        self._info_wants_visible = True
        self.info_restore_win.withdraw()
        self.info_win.deiconify()
        self.info_win.lift()
        self.info_win.attributes("-topmost", False)
        return True

    def _show_rich_result(self, payload):
        """Render an explicit model-authored block document.

        Supported blocks are deliberately data, not executable UI code:
        text/markdown, table, diagram, code and command buttons. Interactive
        buttons feed a new user command through the normal agent pipeline, so
        they cannot bypass authorization or action validation.
        """
        blocks = list(payload.get("blocks") or [])
        if not rich_blocks_have_content(blocks):
            return False
        if self._web_report(payload):
            return True
        self._artifact_visible = True
        self._artifact_signature = (("rich_result", str(payload)),)
        self._info_copy_text = str(payload.get("content") or "")
        self.info_title.configure(text=str(payload.get("title") or "VANTA Result"))
        self.info_status.configure(text="RESULT", text_color="#64d2ff")
        self.info_text_widget.pack_forget()
        self.info_research.pack_forget()
        self.info_image.pack_forget()
        if not self.info_report.winfo_manager():
            self.info_report.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 12))
        for child in self.info_report.winfo_children():
            child.destroy()
        section = {"title": str(payload.get("title") or "Result"), "items": []}
        for raw in blocks:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or raw.get("kind") or "text").lower()
            if kind == "markdown":
                import report_view
                section["items"].extend(
                    item for item in report_view.parse(raw.get("content", ""))
                    if item.get("kind") not in {"heading", "spacer", "rule"})
            elif kind == "actions":
                section["items"].append({"kind": "actions",
                                         "items": raw.get("items") or []})
            else:
                item = dict(raw)
                item["kind"] = kind
                section["items"].append(item)
        self._render_report_section(section, "#64d2ff")
        self._info_wants_visible = True
        self.info_restore_win.withdraw()
        self.info_win.deiconify()
        self.info_win.lift()
        self.info_win.attributes("-topmost", False)
        return True

    def _result_command(self, command):
        command = str(command or "").strip()
        if command:
            self._in_q.put(("text", command))

    def _render_result_table(self, parent, block, accent):
        headers = [str(value) for value in block.get("headers", [])]
        rows = [[str(value) for value in row]
                for row in block.get("rows", []) if isinstance(row, (list, tuple))]
        columns = max(len(headers), max((len(row) for row in rows), default=0))
        if not columns:
            return
        headers = (headers + [""] * columns)[:columns]
        grid = ctk.CTkFrame(parent, fg_color="#0f1216", corner_radius=8)
        grid.pack(fill=tk.X, pady=(2, 8))
        for column in range(columns):
            grid.grid_columnconfigure(column, weight=1, uniform="result_table")
            ctk.CTkLabel(
                grid, text=headers[column], anchor="w",
                font=ctk.CTkFont(family=self.FONT_TEXT, size=12, weight="bold"),
                text_color=accent).grid(row=0, column=column, sticky="ew", padx=9, pady=8)
        for row_index, values in enumerate(rows[:40], 1):
            values = (values + [""] * columns)[:columns]
            for column, value in enumerate(values):
                ctk.CTkLabel(
                    grid, text=value, anchor="w", justify="left",
                    wraplength=max(100, 600 // columns),
                    font=ctk.CTkFont(family=self.FONT_TEXT, size=12),
                    text_color=LABEL_PRIMARY if row_index % 2 else LABEL_SECONDARY,
                ).grid(row=row_index, column=column, sticky="nsew", padx=9, pady=6)

    def _render_result_diagram(self, parent, block, accent):
        """Draw a compact node/edge diagram from data or simple Mermaid."""
        import re
        nodes = list(block.get("nodes") or [])
        edges = list(block.get("edges") or [])
        source = str(block.get("text") or block.get("content") or "")
        labels = {}
        parsed_edges = []
        for line in re.split(r"[;\n]+", source):
            match = re.search(
                r"([A-Za-z0-9_]+)(?:\[([^\]]+)\]|\(([^)]+)\))?\s*"
                r"(?:-->|---|-\.->)\s*"
                r"([A-Za-z0-9_]+)(?:\[([^\]]+)\]|\(([^)]+)\))?", line)
            if match:
                left, right = match.group(1), match.group(4)
                labels[left] = match.group(2) or match.group(3) or left
                labels[right] = match.group(5) or match.group(6) or right
                parsed_edges.append((left, right))
        if not nodes and labels:
            nodes = [{"id": key, "label": value} for key, value in labels.items()]
        if not edges and parsed_edges:
            edges = [{"from": left, "to": right} for left, right in parsed_edges]
        normal = []
        for index, node in enumerate(nodes[:12]):
            if isinstance(node, dict):
                normal.append((str(node.get("id") or index),
                               str(node.get("label") or node.get("text") or node.get("id") or index)))
            else:
                normal.append((str(index), str(node)))
        if not normal:
            ctk.CTkLabel(parent, text=source or "No diagram data.", anchor="w",
                         justify="left", text_color=LABEL_SECONDARY).pack(fill=tk.X)
            return
        height = max(180, 92 * ((len(normal) + 2) // 3))
        canvas = tk.Canvas(parent, height=height, bg="#0f1216",
                           highlightthickness=0)
        canvas.pack(fill=tk.X, pady=(2, 8))
        width = 650
        positions = {}
        for index, (node_id, label) in enumerate(normal):
            column, row = index % 3, index // 3
            x, y = 105 + column * 220, 48 + row * 90
            positions[node_id] = (x, y)
            canvas.create_rectangle(x - 78, y - 25, x + 78, y + 25,
                                    fill="#1a2027", outline=accent, width=1)
            canvas.create_text(x, y, text=label[:36], fill="#f2f4f7",
                               width=142, font=(self.FONT_TEXT, 10))
        for edge in edges[:24]:
            if isinstance(edge, dict):
                left = str(edge.get("from") or edge.get("source") or "")
                right = str(edge.get("to") or edge.get("target") or "")
            elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                left, right = str(edge[0]), str(edge[1])
            else:
                continue
            if left in positions and right in positions:
                x1, y1 = positions[left]
                x2, y2 = positions[right]
                canvas.create_line(x1, y1 + 27, x2, y2 - 27,
                                   fill="#69727d", width=2, arrow=tk.LAST)

    def _render_report_section(self, section, accent):
        card = ctk.CTkFrame(
            self.info_report, fg_color=SURFACE_DEEP, corner_radius=8,
            border_color=SEPARATOR, border_width=1)
        card.pack(fill=tk.X, padx=6, pady=6)
        heading = ctk.CTkFrame(card, fg_color="transparent")
        heading.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Canvas(heading, width=4, height=18, bg=accent,
                  highlightthickness=0).pack(side=tk.LEFT, padx=(0, 10))
        ctk.CTkLabel(
            heading, text=str(section.get("title") or "Overview").upper(),
            anchor="w",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=12, weight="bold"),
            text_color=LABEL_PRIMARY).pack(side=tk.LEFT, fill=tk.X, expand=True)

        for block in section.get("items", []):
            kind = block.get("kind")
            if kind == "table":
                self._render_result_table(card, block, accent)
                continue
            if kind == "diagram":
                self._render_result_diagram(card, block, accent)
                continue
            if kind == "code":
                code = ctk.CTkTextbox(
                    card, height=min(260, max(80, 22 * (str(block.get("text") or "").count("\n") + 2))),
                    font=ctk.CTkFont(family=self.FONT_MONO, size=12),
                    fg_color="#0f1216", text_color="#d7dde5", wrap="none",
                    corner_radius=8)
                code.pack(fill=tk.X, padx=16, pady=(2, 9))
                code.insert("0.0", str(block.get("text") or block.get("content") or ""))
                code.configure(state="disabled")
                continue
            if kind in {"math", "formula", "latex"}:
                formula = str(block.get("latex") or block.get("text")
                              or block.get("content") or "").strip()
                if formula:
                    ctk.CTkLabel(
                        card, text=formula, anchor="center", justify="center",
                        wraplength=540,
                        font=ctk.CTkFont(family=self.FONT_MONO, size=16),
                        fg_color="#0f1216", corner_radius=8,
                        text_color=LABEL_PRIMARY,
                    ).pack(fill=tk.X, padx=16, pady=(2, 9), ipady=14)
                continue
            if kind == "actions":
                controls = ctk.CTkFrame(card, fg_color="transparent")
                controls.pack(fill=tk.X, padx=16, pady=(3, 10))
                for item in list(block.get("items") or [])[:6]:
                    if not isinstance(item, dict):
                        continue
                    command = str(item.get("command") or "").strip()
                    if not command:
                        continue
                    ctk.CTkButton(
                        controls, text=str(item.get("label") or command)[:28],
                        height=30, corner_radius=8, fg_color="#202630",
                        hover_color="#2a3340", text_color=LABEL_PRIMARY,
                        command=lambda value=command: self._result_command(value),
                    ).pack(side=tk.LEFT, padx=(0, 8))
                continue
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill=tk.X, padx=16, pady=(2, 8))
            if kind == "bullet":
                ctk.CTkLabel(row, text="•", width=14, anchor="n",
                             font=ctk.CTkFont(family=self.FONT_TEXT, size=15),
                             text_color=accent).pack(side=tk.LEFT, anchor="n")
            label = str(block.get("label") or "").strip()
            body = str(block.get("text") or "").strip()
            text_host = ctk.CTkFrame(row, fg_color="transparent")
            text_host.pack(side=tk.LEFT, fill=tk.X, expand=True,
                           padx=(5 if kind == "bullet" else 0, 0))
            if label:
                ctk.CTkLabel(
                    text_host, text=label, anchor="w", justify="left",
                    font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="bold"),
                    text_color=LABEL_PRIMARY).pack(anchor="w")
            if kind == "link":
                ctk.CTkButton(
                    text_host, text="Open link", width=82, height=28,
                    corner_radius=6, fg_color="#1f2933",
                    hover_color="#293746", text_color="#64d2ff",
                    command=lambda target=body: webbrowser.open(target),
                ).pack(anchor="w")
            elif body:
                size = 17 if block.get("quantity") else 14
                weight = "bold" if block.get("quantity") else "normal"
                ctk.CTkLabel(
                    text_host, text=body, anchor="w", justify="left",
                    wraplength=540,
                    font=ctk.CTkFont(family=self.FONT_TEXT, size=size,
                                     weight=weight),
                    text_color=(LABEL_PRIMARY if block.get("quantity")
                                else LABEL_SECONDARY)).pack(fill=tk.X, anchor="w")

    # ------------------------------------------------------------ #
    # Input handling (unchanged behavior)
    # ------------------------------------------------------------ #
    def _show_input(self):
        self.is_inputting = True
        self.input_win.deiconify()
        self.entry_input.delete(0, tk.END)
        self.entry_input.focus()

    def _on_input_submit(self, event):
        challenge = getattr(self, "_auth_challenge", None)
        if challenge:
            # A password, not a message: it goes back as its own kind of input,
            # is cleared from the box at once, and is never shown, logged or
            # treated as something to say to the assistant.
            secret = self.entry_input.get()
            self.entry_input.delete(0, tk.END)
            self._end_auth_prompt()
            self._in_q.put(("auth_secret", {"id": challenge, "secret": secret}))
            return
        text = self.entry_input.get().strip()
        if text:
            self._in_q.put(("text", text))
        self.is_inputting = False
        self.input_win.withdraw()

    def _on_input_cancel(self, event):
        challenge = getattr(self, "_auth_challenge", None)
        if challenge:
            self.entry_input.delete(0, tk.END)
            self._end_auth_prompt()
            self._in_q.put(("auth_secret", {"id": challenge, "secret": None}))
            return
        self.is_inputting = False
        self.input_win.withdraw()

    def _begin_auth_prompt(self, challenge_id, prompt):
        """Show the input box as a masked password field."""
        self._auth_challenge = challenge_id
        self.is_inputting = True
        self.entry_input.configure(show="•", placeholder_text=str(prompt or "Password"))
        self.input_win.deiconify()
        self.input_win.lift()
        self.entry_input.delete(0, tk.END)
        self.entry_input.focus()
        self.input_win.focus_force()

    def _end_auth_prompt(self):
        self._auth_challenge = None
        self.entry_input.configure(show="", placeholder_text="Ask Vanta to do something...")
        self.is_inputting = False
        self.input_win.withdraw()

    # ------------------------------------------------------------ #
    # Queue polling -- message contract unchanged, only rendering differs
    # ------------------------------------------------------------ #
    def _poll(self):
        try:
            while True:
                s, t = self._q.get_nowait()
                if s == "click":
                    self._clicks.append({"x": t[0], "y": t[1], "age": 0.0})
                elif s == "prompt_input":
                    self._show_input()
                elif s == "auth_prompt":
                    payload = t if isinstance(t, dict) else {}
                    self._begin_auth_prompt(payload.get("id"), payload.get("prompt"))
                elif s == "auth_close":
                    if t and getattr(self, "_auth_challenge", None) == t:
                        self.entry_input.delete(0, tk.END)
                        self._end_auth_prompt()
                elif s == "identity_gate_show":
                    self._show_identity_gate(t)
                elif s == "identity_gate_frame":
                    self._update_identity_gate(t)
                elif s == "identity_gate_status":
                    self._build_identity_gate()
                    payload = t if isinstance(t, dict) else {}
                    self._identity_gate_percent.configure(
                        text=f"{int(payload.get('percent') or 0)}%")
                    self._identity_gate_message.configure(
                        text=str(payload.get("message") or "Verifying"))
                elif s == "identity_gate_hide":
                    self._hide_identity_gate()
                elif s == "toggle_input":
                    if self.is_inputting:
                        self._on_input_cancel(None)
                    else:
                        self._show_input()
                elif s == "hide_overlay":
                    self.root.withdraw()
                    self.pill_win.withdraw()
                elif s == "show_overlay":
                    self.root.deiconify()
                    self.pill_win.deiconify()
                elif s == "voice_mute_state":
                    self._voice_muted = bool(t)
                    self._mark_last = None
                    self._update_notch()
                elif s == "standby":
                    # Standby leaves no VANTA surface competing with the
                    # desktop. Wake input restores only the minimal notch;
                    # panels stay closed until explicitly opened again.
                    self.is_inputting = False
                    for window_name in ("input_win", "info_win", "dashboard_win"):
                        window = getattr(self, window_name, None)
                        if window is not None:
                            window.withdraw()
                    self.dashboard_visible = False
                    try:
                        import console_window
                        console_window.close_window()
                    except Exception:
                        pass
                    self._board_hide()
                    self._wb_hide()
                    self._hide_light()
                    self.root.withdraw()
                    self.pill_win.withdraw()
                elif s == "flash_screen":
                    self._flash_frames = 6
                elif s == "screen_light":
                    self._show_light(t or {})
                elif s == "screen_light_off":
                    self._hide_light()
                elif s == "annotate":
                    self._draw_annotation(t or {})
                elif s == "annotate_clear":
                    self._clear_annotations()
                elif s == "draw_scene":
                    self._draw_scene(t or {})
                elif s == "board_write":
                    self._board_write(t or {})
                elif s == "board_diagram":
                    self._board_diagram(t or {})
                elif s == "board_math":
                    self._board_math(t or {})
                elif s == "board_plot":
                    self._board_plot(t or {})
                elif s == "board_chart":
                    self._board_chart(t or {})
                elif s == "board_table":
                    self._board_table(t or {})
                elif s == "board_rule":
                    self._board_rule(t or {})
                elif s == "board_section":
                    self._board_section(t or {})
                elif s == "board_goto":
                    self._board_goto(t or {})
                elif s == "board_edit_page":
                    self._board_edit_page(t or {})
                elif s == "board_animate":
                    self._board_animate(t or {})
                elif s == "board_plot3d":
                    self._board_plot3d(t or {})
                elif s == "board_matrix":
                    self._board_matrix(t or {})
                elif s == "board_field":
                    self._board_field(t or {})
                elif s == "board_contour":
                    self._board_contour(t or {})
                elif s == "board_capture":
                    self._board_capture(t or {})
                elif s == "board_frames":
                    # Rotation frames finished rendering on a worker thread.
                    self._board_frames(t or {})
                elif s == "board_outline":
                    self._append_control_text(
                        "activity_text", self._board_outline() + "\n\n")
                elif s == "board_ingest":
                    # The page just finished is the unit worth keeping: a
                    # whole lesson is too coarse to extract from, a single
                    # line too thin to mean anything.
                    import lessons
                    lessons.ingest_page(
                        os.path.dirname(os.path.abspath(__file__)),
                        str(t) or getattr(self, "_board_topic", "") or "lesson",
                        self._page + 1 if hasattr(self, "_page") else 1,
                        self._board_page_text(), self._q)
                elif s == "board_clear":
                    self._board_clear()
                elif s == "board_hide":
                    self._board_hide()
                elif s == "board_toggle":
                    self._board_toggle()
                elif s == "dashboard_page":
                    # VANTA driving its own dashboard: open it and land on the
                    # page it wants Himanshu looking at.
                    if str(t) in self.CONSOLE_PAGES and self._web_console(str(t)):
                        pass
                    else:
                        self._show_dashboard()
                        self._show_page(str(t))
                elif s == "dashboard_document":
                    self._cc_show_document(t or {})
                elif s == "panels_changed":
                    self._cc_refresh_panels()
                elif s == "project_opened":
                    # VANTA opened a project itself. The coding surface is
                    # where code happens now; the Control Center's Code page
                    # stays correct behind it rather than becoming the place
                    # you are expected to work in.
                    self._wb_open(str(t or ""))
                    self._cc_refresh_projects()
                elif s == "project_changed":
                    self._cc_refresh_projects()
                    self._wb_project_changed(str(t or ""))
                elif s == "workbench_open":
                    self._wb_open(str(t or ""))
                elif s == "workbench_close":
                    self._wb_close()
                elif s == "workbench_toggle":
                    self._wb_toggle()
                elif s == "workbench_index":
                    self._wb_apply_index(t)
                elif s == "rest_begin":
                    self._rest_show(str(t or ""))
                elif s == "rest_end":
                    self._rest_hide()
                elif s == "toggle_dashboard":
                    self._toggle_dashboard()
                elif s == "show_dashboard":
                    if not self._web_console():
                        self._show_dashboard()
                elif s == "hide_dashboard":
                    try:
                        import console_window
                        console_window.close_window()
                    except Exception:
                        pass
                    self._hide_dashboard()
                elif s == "dashboard_update":
                    # CPU/RAM/battery now arrive from the telemetry thread, so
                    # this message carries only the agent's context summary.
                    import re
                    raw_ctx = t.get("context_str", "") if isinstance(t, dict) else ""
                    sections = []
                    location = re.search(r'Location.*', raw_ctx)
                    if location:
                        sections.append(f"Location: {location.group(0)}")
                    calendar = re.search(r'--- UPCOMING CALENDAR.*?(?=---|$)',
                                         raw_ctx, re.DOTALL)
                    if calendar:
                        sections.append(calendar.group(0).strip())
                    alarms = re.search(r'--- ACTIVE ALARMS.*', raw_ctx, re.DOTALL)
                    if alarms:
                        sections.append(alarms.group(0).strip())
                    self.dash_text.configure(state="normal")
                    self.dash_text.delete("0.0", tk.END)
                    self.dash_text.insert("0.0", "\n\n".join(sections).strip()
                                          or "Nothing scheduled.")
                    self.dash_text.configure(state="disabled")
                elif s == "cc_fast":
                    self._cc_apply_fast(t)
                elif s == "cc_slow":
                    self._cc_apply_slow(t)
                elif s == "control_result":
                    # Machine controls report where the rest of the record is
                    # kept, and surface failures where they will be seen.
                    self._append_control_text("activity_text", f"{t}\n\n")
                    self._console_note("activity", t)
                    self._cc_note_recent(t)
                    if not str(t).startswith("[Success]"):
                        self.control_status.configure(text=str(t)[:60])
                        self.root.after(
                            6000, lambda: self.control_status.configure(
                                text=STATE_LABELS.get(self._state, "Ready")))
                elif s == "mode_changed":
                    payload = t if isinstance(t, dict) else {"label": str(t), "dimmed": False}
                    label = payload.get("label", "ASSISTANT")
                    self._active_mode_label = str(label or "ASSISTANT").upper()
                    self._console_note("mode", payload)
                    dimmed = bool(payload.get("dimmed", False))
                    if hasattr(self, "mode_label"):
                        self.mode_label.configure(text=label)
                    self._cc_highlight_mode(payload.get("mode") or label)
                    # Dimming is how a quiet mode announces itself without
                    # saying anything; full opacity everywhere else.
                    self.dashboard_win.attributes("-alpha", 0.80 if dimmed else 1.0)
                    if hasattr(self, "response_text"):
                        self.response_text.configure(
                            text_color=LABEL_TERTIARY if dimmed else LABEL_PRIMARY)
                    self._update_notch()
                elif s == "agent_response":
                    self._clear_step_line()
                    self._set_notch_answer(t)
                    self.response_text.configure(state="normal")
                    self.response_text.delete("0.0", tk.END)
                    self.response_text.insert("0.0", t)
                    self.response_text.configure(state="disabled")
                    self._append_control_text("chat_history", f"VANTA\n{t}\n\n")
                    self._console_note("answer", t)
                    self._cc_note_recent(t)
                    self._wb_note_response(t)
                elif s == "user_query":
                    self._set_notch_question(t)
                    self._console_note("user", t)
                elif s == "conversation_window":
                    try:
                        seconds = max(0.0, float(t or 0.0))
                    except (TypeError, ValueError):
                        seconds = 0.0
                    self._conversation_until = time.monotonic() + seconds
                    self._conversation_was_active = seconds > 0
                    self._update_notch()
                elif s == "notch_question_clear":
                    # A human utterance belongs to exactly one foreground
                    # turn. Keeping it after completion made the next ambient
                    # or proactive THINKING state reopen an old question.
                    self._notch_question = ""
                    if not self._notch_answer:
                        self._update_notch()
                elif s == "action":
                    self._action_until = time.monotonic() + 2.5
                    self._set_step_line(t)
                elif s == "show_vision_preview":
                    pass
                elif s == "hide_vision_preview":
                    pass
                elif s == "conversation_history":
                    for item in (t or []):
                        self._append_control_text("chat_history", f"{item}\n\n")
                elif s == "file_scan_result":
                    self._apply_file_rows(t)
                elif s == "file_preview_error":
                    self._show_file_error(str(t))
                elif s == "graph_result":
                    self._apply_graph_snapshot(t)
                elif s == "graph_error":
                    self._show_graph_error(str(t))
                elif s == "hud_refresh":
                    self._hud_refresh(t)
                elif s == "route_panel":
                    self._cc_show_route(t)
                elif s == "route_map_image":
                    self._cc_apply_route_image(t)
                elif s == "hud_hide":
                    self._hud_hide(t)
                elif s == "approval_request":
                    # A permission request with nowhere to go is how one sat
                    # unanswered through eleven model calls.
                    self._show_approval_request(t or {})
                elif s == "approval_settled":
                    self._hide_approval_request()
                elif s == "info":
                    display_text = user_activity_text(t)
                    if not display_text:
                        continue
                    self._last_info = display_text
                    if display_text.lstrip().startswith(("[Error]", "> ")) and \
                            "[Error]" in display_text[:400]:
                        self._error_until = time.monotonic() + 1.4
                    self._append_control_text(
                        "activity_text", f"{display_text}\n\n")
                    self._console_note("activity", display_text)
                    self._wb_note_tool(display_text)
                    # Detailed model output belongs in Activity by default.
                    # Opening a floating results window is a user-visible
                    # interruption, so only an explicit display request may
                    # do it (SHOW_ON_SCREEN uses the HUD; show_report is the
                    # dedicated rich-report event).
                elif s == "report_stream":
                    try:
                        import report_window
                        report_window.stream(t, command_sink=lambda command: self._in_q.put(("text", command)))
                    except Exception as exc:
                        print(f"[Report] Preview unavailable: {type(exc).__name__}")
                elif s == "show_report":
                    self._show_result(t)
                elif s == "show_result":
                    self._show_result(t)
                elif s == "rich_result":
                    self._show_rich_result(t or {})
                elif s == "hide_result":
                    self._minimize_details()
                elif s == "close_result":
                    try:
                        import report_window
                        report_window.close_window()
                    except Exception:
                        pass
                    self._close_details()
                elif s == "toggle_result":
                    if getattr(self, "_info_wants_visible", False):
                        self._minimize_details()
                    else:
                        self._restore_details()
                elif s == "show_widget":
                    try:
                        import widget_view
                        widget_view.show(self, t or {})
                    except Exception as exc:
                        print(f"[Widget] Could not render: {type(exc).__name__}: {exc}")
                elif s == "hide_widget":
                    try:
                        import widget_view
                        widget_view.close(self, str(t or ""))
                    except Exception as exc:
                        print(f"[Widget] Could not close: {type(exc).__name__}: {exc}")
                elif s == "artifacts":
                    self._show_artifacts(t)
                elif s == "image_artifact":
                    self._show_image_artifact(t)
                elif s == "sync_brain":
                    self.brain_var.set(t.title())
                    self._update_notch()
                elif s == "show_info":
                    if hasattr(self, 'info_win'):
                        self._restore_details()
                elif s == "hide_info":
                    # Normal action cleanup must not erase a link/path card
                    # that was deliberately shown to the user.
                    if (hasattr(self, 'info_win')
                            and not getattr(self, '_artifact_visible', False)):
                        self._info_wants_visible = False
                        self.info_win.withdraw()
                elif s == "temp_hide_info":
                    if hasattr(self, 'info_win'):
                        self.info_win.withdraw()
                elif s == "restore_info":
                    if hasattr(self, 'info_win') and getattr(self, '_info_wants_visible', False):
                        self._restore_details()
                elif s == "toggle_info":
                    if hasattr(self, 'info_win'):
                        self._info_wants_visible = not getattr(self, '_info_wants_visible', False)
                        if self._info_wants_visible:
                            self._restore_details()
                        else:
                            self._minimize_details()
                else:
                    self._state = s
                    # The detail carried with the state was being discarded, so
                    # a long task showed a frozen "PROCESSING" and looked hung.
                    self._status_detail = str(t or "").strip() if isinstance(t, str) else ""
                    self._console_note("state", s)
                    self._update_notch()
        except queue.Empty:
            pass        # the normal way out of the drain loop
        except Exception as exc:
            # This used to be a bare 'except Exception: pass', which caught
            # the Empty that ends the loop AND every real bug in the handlers
            # below it -- so a broken handler produced no output at all and the
            # interface just quietly stopped doing what it was told.
            import traceback
            print(f"[UI] Handling a queue message failed: {exc}")
            traceback.print_exc()
        self.root.after(50, self._poll)

    @staticmethod
    def _hsv(h, s, v):
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
        return f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'

    # ------------------------------------------------------------ #
    # Ambient border + click ripples + pill animation
    # ------------------------------------------------------------ #
    def _animate(self):
        # Real elapsed time, not a fixed step: when the machine is busy a frame
        # arrives late, and a fixed step made every animation slow down with it.
        _now = time.monotonic()
        _dt = min(0.1, _now - getattr(self, "_frame_clock", _now - 0.03))
        self._frame_clock = _now
        self._t += _dt * (0.05 / 0.03)

        conversation_active = time.monotonic() < self._conversation_until
        if conversation_active != self._conversation_was_active:
            self._conversation_was_active = conversation_active
            self._update_notch()

        # The blackboard is a full-screen opaque window. Everything this loop
        # draws -- the border, the ripples, the flash -- is behind it and
        # cannot be seen, so doing the work anyway just competes with the
        # lesson for the event loop. The orb still updates: it sits above.
        board = getattr(self, "_board_win", None)
        if board is not None and board.winfo_exists() and board.state() == "normal":
            self._draw_mark()
            if self._tween_steps:
                width, height = self._tween_steps.pop(0)
                self._set_notch_geometry(width, height)
            self.root.after(45, self._animate)
            return

        # Keep the working indicator moving. Without this the notch only
        # repaints on a state change, so a task that runs for a minute shows a
        # motionless label and reads as frozen.
        if self._state in (STATE_THINKING, STATE_LOADING):
            self._update_notch()

        self.bc.delete("all")

        if getattr(self, '_flash_frames', 0) > 0:
            # Fades out over its remaining frames rather than an 8px white
            # slab: a flash should register peripherally, not startle.
            self._flash_frames -= 1
            strength = self._flash_frames / 6.0
            self.bc.create_rectangle(
                1, 1, self.SW - 1, self.SH - 1,
                outline=_blend(SURFACE, LABEL_PRIMARY, strength), width=2)

        if hasattr(self, '_vision_img') and self._vision_img:
            if self._state == STATE_THINKING:
                # Developer Button: Only show vision overlay if 'Alt' (VK_MENU=0x12) is held down
                try:
                    import ctypes
                    dev_key_held = (ctypes.windll.user32.GetAsyncKeyState(0x12) & 0x8000) != 0
                except Exception:
                    dev_key_held = True
                    
                if dev_key_held:
                    self.bc.create_image(0, 0, image=self._vision_img, anchor="nw")
            else:
                self._vision_img = None

        # The mark's hexagon carries state in every state, including idle,
        # so this runs unconditionally -- it is the orb's whole heartbeat.
        self._draw_mark()
        self._draw_voice_wave()

        # Expire a completed answer even if no subsequent state event arrives.
        if (self._notch_answer
                and time.monotonic() >= self._notch_answer_until):
            self._notch_answer = ""
            self._notch_answer_until = 0.0
            self._notch_question = ""
            self._update_notch()

        if self._state != STATE_IDLE:
            # One steady accent per state instead of cycling hues. A colour
            # that keeps changing in the corner of the eye is read as an event
            # every time it moves, which is exactly what makes a long task
            # impossible to work alongside.
            base_color = ACCENTS.get(self._state, LABEL_SECONDARY)
            # The mark's hexagon is the only thing that takes the state colour.
            # A saturated ring around the whole pill as well was the same fact
            # shouted twice, and it is what made the orb read as a gadget.
            self.notch_label.configure(text_color=LABEL_PRIMARY)
            self.pill_frame.configure(border_color=_dim(base_color, 0.72))

        self._draw_state_border()

        # Click ripples: a single thin ring in system blue, quick clean fade.
        alive_clicks = []
        for c in self._clicks:
            c["age"] += 0.08
            if c["age"] < 1.0:
                alive_clicks.append(c)
                x, y = c["x"], c["y"]
                progress = c["age"]
                r = 5 + 26 * math.sqrt(progress)
                t = 1.0 - progress
                ripple_col = _blend(SURFACE, ACCENTS[STATE_LISTENING], t)
                self.bc.create_oval(x - r, y - r, x + r, y + r, outline=ripple_col, width=2)
        self._clicks = alive_clicks

        # Advance any in-progress notch resize tween by one step.
        if self._tween_steps:
            w, h = self._tween_steps.pop(0)
            self._set_notch_geometry(w, h)

        # Aim for a steady ~33 ms between frames: wait for what is left of the
        # frame rather than a fixed 30 ms on top of the drawing time, which
        # made every frame late by exactly the cost of drawing it.
        # Idle draws almost nothing, so it can afford 40 fps and the soul
        # glides; a busy state also redraws the border, so it keeps 30.
        budget = 25 if self._state == STATE_IDLE else 33
        spent_ms = (time.monotonic() - _now) * 1000.0
        self.root.after(max(2, int(budget - spent_ms)), self._animate)

    # ------------------------------------------------------------ #
    # The screen edge as a status light
    # ------------------------------------------------------------ #
    def _protocol_for_border(self):
        """The active protocol's name, polled at most every few seconds."""
        now = time.monotonic()
        if now - getattr(self, "_protocol_polled", 0.0) < 3.0:
            return getattr(self, "_protocol_cached", None)
        self._protocol_polled = now
        try:
            import protocol_manager
            active = protocol_manager.active_protocol()
            self._protocol_cached = active.value if active is not None else None
        except Exception:
            self._protocol_cached = None
        return self._protocol_cached

    def _draw_state_border(self):
        import screen_border
        now = time.monotonic()
        try:
            import screen_rest
            resting = screen_rest.is_resting()
        except Exception:
            resting = False
        mode = screen_border.choose(
            self._state, now,
            action_until=getattr(self, "_action_until", 0.0),
            error_until=getattr(self, "_error_until", 0.0),
            protocol=self._protocol_for_border(), resting=resting)
        if mode is None:
            return
        colour = screen_border.COLORS[mode]
        pulse = screen_border.breath(self._t, 0.9)
        if mode == "error":
            fade = max(0.0, (self._error_until - now) / screen_border.ERROR_FLASH_SECONDS)
            self.bc.create_rectangle(
                1, 1, self.SW - 1, self.SH - 1,
                outline=_blend(SURFACE, colour, 0.35 + 0.65 * fade), width=4)
            return
        # A thin steady line; brightness breathes very slightly so a lit border
        # does not look like a dead pixel row.
        base = _blend(SURFACE, colour, 0.34 + 0.10 * pulse)
        self.bc.create_rectangle(1, 1, self.SW - 1, self.SH - 1, outline=base, width=2)
        if mode in screen_border.TRAVELLING:
            head = (self._t * 0.085) % 1.0
            points = screen_border.comet(head, self.SW, self.SH)
            total = len(points) - 1
            for index in range(total):
                strength = (index + 1) / total
                self.bc.create_line(
                    *points[index], *points[index + 1],
                    fill=_blend(SURFACE, colour, 0.25 + 0.75 * strength),
                    width=2 + int(3 * strength), capstyle=tk.ROUND)

    def run(self):
        # The assistant runs speech recognition, the wake word and network
        # threads beside this one. Python hands the interpreter lock over every
        # 5 ms by default, so a busy thread could hold up a frame by that much
        # each time it was the UI's turn; 1 ms keeps the animation even.
        import sys
        try:
            sys.setswitchinterval(0.001)
        except Exception:
            pass
        # Likewise the Windows timer: by default Tk's after() can only fire on
        # 15.6 ms ticks, so a 33 ms wait lands anywhere from 31 to 47 ms and
        # the animation visibly judders. Ask for 1 ms and the frames come even.
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        self.root.mainloop()
