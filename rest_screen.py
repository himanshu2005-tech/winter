# -*- coding: utf-8 -*-
"""
rest_screen.py -- the dark screen, as a mixin onto WinterUI.

When nobody has touched the machine for a while and the picture has not
changed, the display is showing last night's work to an empty room. This
covers it: black, with VANTA's mark breathing quietly in the middle, the time
beneath it, and one line saying how to get out.

It is not a screensaver and not a lock. Nothing is secured. Automatic screen
rest leaves everything behind it running; manual standby reuses this same
surface while runtime_power pauses background intelligence. The surface only
presents the state -- it does not own it.

Three things it must get right, because each one would be worse than the
problem:

*It must come down instantly.* A blackout that takes a moment to notice a
keypress reads as a crash. The key and mouse bindings dismiss it directly
rather than waiting for the watcher's next poll.

*It must never cover something being watched.* That is handled before this is
ever shown -- `screen_rest` requires the frame to be still as well as the
hands -- but the surface also yields immediately to any input rather than
insisting on a grace period.

*It must not be mistaken for a crash.* So it says what it is, and what to do.
"""

import time
import tkinter as tk
import tkinter.font as tkfont

import winter_mark
import window_identity

REST_BG = "#000000"
REST_MARK = "#2a2f36"
REST_TEXT = "#3a4049"
REST_DIM = "#23272d"

# Sleep and explicit standby are held states, so they get a small piece of
# persistent UI rather than looking like an accidental black screen.  Tk does
# not support per-widget blur; these layers reproduce frosted dark glass while
# keeping the desktop fully obscured (blurring a screenshot would leak whatever
# was open when Himanshu went to sleep).
GLASS_SURFACE = "#000000"
GLASS_BORDER = "#252d38"
GLASS_HIGHLIGHT = "#394452"
GLASS_SHADOW = "#020306"
GLASS_PRIMARY = "#f5f5f7"
GLASS_SECONDARY = "#a6adb8"
GLASS_MUTED = "#687281"
SLEEP_ACCENT = "#8b7cff"
STANDBY_ACCENT = "#4aa8ff"

#: The mark breathes between these, slowly. Slow enough to read as alive
#: rather than as something demanding attention: it is the only moving thing
#: in a dark room, and at any speed worth noticing it becomes a strobe.
BREATH_SECONDS = 7.0
MARK_RADIUS = 46


class RestScreenMixin:
    """The dark screen. Mixed into WinterUI beside the blackboard."""

    def _rest_exists(self):
        window = getattr(self, "_rest_win", None)
        return window is not None and window.winfo_exists()

    def _ensure_rest_screen(self):
        if self._rest_exists():
            return self._rest_win
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window_identity.hide_from_taskbar(window, self.root)
        window.configure(bg=REST_BG)
        # Cover the whole virtual desktop, including monitors left/above the
        # primary one. A primary-monitor-only blackout looked like standby had
        # failed whenever a second display was attached.
        try:
            from vision import get_virtual_screen_bounds
            left, top, width, height = get_virtual_screen_bounds()
        except Exception:
            left, top = 0, 0
            width = window.winfo_screenwidth()
            height = window.winfo_screenheight()
        window.geometry(f"{width}x{height}{left:+d}{top:+d}")
        canvas = tk.Canvas(window, bg=REST_BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        try:
            import win32gui
            from vision import register_winter_hwnd
            register_winter_hwnd(win32gui.GetParent(window.winfo_id())
                                 or window.winfo_id())
        except Exception as exc:
            print(f"[Rest] Could not register the window: {exc}")

        self._rest_win = window
        self._rest_canvas = canvas
        self._rest_job = None
        self._rest_soul = None
        self._rest_soul_photo = None
        self._rest_soul_clock = time.monotonic()
        self._rest_started = time.monotonic()
        # Bound on the window, and for every kind of input: the surface has
        # to go the moment he is back, not when the watcher next looks.
        for sequence in ("<Key>", "<Button>"):
            window.bind(sequence, self._rest_dismiss)
        # Movement dismisses an idle blackout but not a sleeping one.
        for sequence in ("<MouseWheel>", "<Motion>"):
            window.bind(sequence, self._rest_movement)
        window.focus_force()
        return window

    def _rest_movement(self, event=None):
        if (getattr(self, "_rest_sleeping", False)
                or getattr(self, "_rest_standby", False)):
            return
        self._rest_dismiss(event)

    def _rest_show(self, reason=""):
        window = self._ensure_rest_screen()
        self._rest_reason = reason or ""
        self._rest_sleeping = str(reason or "").lower().startswith("sleep")
        self._rest_standby = str(reason or "").lower().startswith("standby")

        window.deiconify()
        window.lift()
        try:
            window.focus_force()
        except tk.TclError:
            pass
        self._rest_started = time.monotonic()
        self._rest_tick()

    def _rest_hide(self):
        job = getattr(self, "_rest_job", None)
        if job:
            try:
                self._rest_win.after_cancel(job)
            except Exception:
                pass
            self._rest_job = None
        if self._rest_exists():
            self._rest_win.withdraw()

    def _rest_dismiss(self, _event=None):
        """He is back. Take it down here and tell the watcher."""
        sleeping = getattr(self, "_rest_sleeping", False)
        standby = getattr(self, "_rest_standby", False)
        self._rest_hide()
        if sleeping:
            # He clicked, or pressed a key, on a screen shown because he was
            # asleep: he is awake. Hand it to the agent, which owns the mode
            # and the catch-up of what arrived while he slept.
            try:
                self._in_q.put(("wake_sleep", "click"))
            except Exception as exc:
                print(f"[Rest] Could not leave sleep mode: {exc}")
        try:
            import screen_rest
            screen_rest.end(None, "input on the dark screen")
            # Not immediately again: he may have moved the mouse to read
            # something rather than to work.
            screen_rest.suspend(minutes=screen_rest.IDLE_MINUTES)
        except Exception as exc:
            print(f"[Rest] Could not clear the resting state: {exc}")
        if standby:
            try:
                import runtime_power
                runtime_power.resume()
                self._q.put(("show_overlay", ""))
                print("[Standby] Resumed by input on the dark screen.")
            except Exception as exc:
                print(f"[Standby] Could not resume: {exc}")

    # ------------------------------------------------------------ #
    # Drawing
    # ------------------------------------------------------------ #
    def _rest_font(self, size, weight="normal"):
        cache = getattr(self, "_rest_fonts", None)
        if cache is None:
            cache = self._rest_fonts = {}
        key = (size, weight)
        if key not in cache:
            cache[key] = tkfont.Font(family="Segoe UI", size=size,
                                     weight=weight)
        return cache[key]

    @staticmethod
    def _rest_breath(elapsed):
        """0.0 to 1.0 and back, once per BREATH_SECONDS. Pure, so the shape
        of the animation can be checked without a screen."""
        import math
        phase = (elapsed % BREATH_SECONDS) / BREATH_SECONDS
        return (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0

    @staticmethod
    def _rest_blend(start, end, amount):
        a = tuple(int(start.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        b = tuple(int(end.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        amount = max(0.0, min(1.0, float(amount)))
        return "#%02x%02x%02x" % tuple(
            int(a[i] + (b[i] - a[i]) * amount) for i in range(3))

    @staticmethod
    def _rest_round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        """Draw a smooth rounded card without adding a heavyweight UI layer."""
        radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        points = (
            x1 + radius, y1, x2 - radius, y1, x2, y1,
            x2, y1 + radius, x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2, x1, y2,
            x1, y2 - radius, x1, y1 + radius, x1, y1,
        )
        return canvas.create_polygon(
            points, smooth=True, splinesteps=24, **kwargs)

    def _rest_draw_soul(self, canvas, centre_x, centre_y):
        """Render the same living idle presence used by the top-screen orb."""
        now = time.monotonic()
        mind = getattr(self, "_rest_soul", None)
        if mind is None:
            try:
                import soul
                mind = self._rest_soul = soul.Soul("idle")
            except Exception:
                self._rest_soul = False
                mind = False
            self._rest_soul_clock = now
        if mind is False:
            canvas.create_oval(
                centre_x - 18, centre_y - 18, centre_x + 18, centre_y + 18,
                fill="#252a32", outline=GLASS_BORDER, width=1,
                tags="rest-ui")
            return

        dt = now - getattr(self, "_rest_soul_clock", now)
        self._rest_soul_clock = now
        mind.set_state("idle")
        mind.set_guarded(False)
        mind.set_level(0.0)
        mind.step(dt)
        try:
            from PIL import ImageTk
            image = mind.render(104, GLASS_SURFACE, supersample=1)
            photo = getattr(self, "_rest_soul_photo", None)
            if photo is None:
                photo = self._rest_soul_photo = ImageTk.PhotoImage(image)
            else:
                photo.paste(image)
            canvas.create_image(
                centre_x, centre_y, image=photo, tags="rest-ui")
        except Exception:
            canvas.create_oval(
                centre_x - 18, centre_y - 18, centre_x + 18, centre_y + 18,
                fill="#252a32", outline=GLASS_BORDER, width=1,
                tags="rest-ui")

    def _rest_draw_glass(self, canvas, width, height, sleeping, standby):
        """Draw the held state as a single quiet focus on pure black."""
        canvas.delete("rest-ui")
        canvas.delete("rest-text")
        canvas.delete("vanta-mark")
        centre_x = width // 2
        centre_y = int(height * 0.46)
        accent = SLEEP_ACCENT if sleeping else STANDBY_ACCENT
        self._rest_draw_soul(canvas, centre_x, centre_y - 92)
        canvas.create_text(
            centre_x, centre_y, text=time.strftime("%H:%M:%S"),
            fill=GLASS_PRIMARY, font=self._rest_font(36, "normal"),
            tags="rest-ui")

        standby_status = "Standby"
        if standby:
            try:
                import runtime_power
                remaining = runtime_power.remaining_seconds()
                if remaining is not None:
                    total = max(0, int(remaining + 0.999))
                    minutes, seconds = divmod(total, 60)
                    standby_status = f"Standby  ·  {minutes:02d}:{seconds:02d}"
            except Exception:
                pass
        status = "Sleep" if sleeping else standby_status
        canvas.create_text(
            centre_x, centre_y + 54, text=status,
            fill=accent, font=self._rest_font(11, "normal"),
            tags="rest-ui")
        canvas.create_text(
            centre_x, height - 44,
            text=("Press any key to wake" if sleeping else
                  "Say Vanta or press any key"),
            fill=GLASS_MUTED, font=self._rest_font(9, "normal"),
            tags="rest-ui")

    def _rest_tick(self):
        if not self._rest_exists() or not self._rest_win.winfo_viewable():
            return
        canvas = self._rest_canvas
        sleeping = getattr(self, "_rest_sleeping", False)
        standby = getattr(self, "_rest_standby", False)
        width = canvas.winfo_width() or canvas.winfo_screenwidth()
        height = canvas.winfo_height() or canvas.winfo_screenheight()
        if sleeping or standby:
            self._rest_draw_glass(canvas, width, height, sleeping, standby)
            # Four frames per second is enough for a sleeping breath and does
            # not wake the GPU or compete with background work overnight.
            self._rest_job = self._rest_win.after(250, self._rest_tick)
            return
        centre_x, centre_y = width // 2, int(height * 0.44)

        amount = self._rest_breath(time.monotonic() - self._rest_started)
        colour = self._rest_blend(REST_DIM, REST_MARK, amount)
        winter_mark.draw_on_canvas(canvas, centre_x, centre_y, MARK_RADIUS,
                                   color=colour, ring_color=colour)

        canvas.delete("rest-text")
        canvas.create_text(
            centre_x, centre_y + MARK_RADIUS + 54,
            text=time.strftime("%H:%M:%S"), fill=REST_TEXT,
            font=self._rest_font(34, "normal"), tags="rest-text")
        # It must not read as a crash, so it says what it is and that nothing
        # has stopped.
        standby_status = "Standby mode · background intelligence paused"
        if standby:
            try:
                import runtime_power
                remaining = runtime_power.remaining_seconds()
                if remaining is not None:
                    total = max(0, int(remaining + 0.999))
                    minutes, seconds = divmod(total, 60)
                    standby_status = (f"Standby mode · resumes in "
                                      f"{minutes:02d}:{seconds:02d}")
            except Exception:
                pass
        canvas.create_text(
            centre_x, centre_y + MARK_RADIUS + 96,
            text=("Sleep mode · VANTA is still working" if sleeping else
                  standby_status if standby else
                  "Screen resting · VANTA is still working"),
            fill=REST_DIM, font=self._rest_font(11), tags="rest-text")
        canvas.create_text(
            centre_x, height - 48,
            text=("click or press a key to wake" if sleeping else
                  "say Vanta, press any key, or move the mouse" if standby else
                  "press any key or move the mouse"),
            fill=REST_DIM, font=self._rest_font(10), tags="rest-text")

        self._rest_job = self._rest_win.after(120, self._rest_tick)
