# -*- coding: utf-8 -*-
"""
blackboard.py -- the teaching surface, as a mixin onto WinterUI.

When VANTA is teaching, the screen is the lesson. This blacks out the display
and gives the model a real board: typography with a hierarchy, actual rendered
mathematics, plotted functions, and diagrams it composes itself. The old board
could write six text styles and draw two diagrams, which is enough to narrate a
lesson and not enough to teach one -- you cannot teach a derivative without
writing a derivative, and `dy/dx = lim(h->0)...` typed as plain text is not
writing it.

Three things make this work as a board rather than a slide:

*Mathematics is rendered, not typed.* Formulas go through matplotlib's mathtext
to a real PNG, so they arrive set properly with fractions, integrals, Greek and
subscripts. A formula that fails to parse falls back to monospace with the
error attached, because silently dropping the one line a proof turns on is
worse than showing it broken.

*The board scrolls and forgets.* A lesson runs for as long as it runs, so
content scrolls up and anything fully off the top is deleted. Without that the
canvas grows until the UI stalls, half an hour into exactly the session you
least want to interrupt.

*It is click-through.* A full-screen window that swallows the pointer is
indistinguishable from a hang.
"""

import csv
import io
import json
import math
import threading
import tkinter as tk
import tkinter.font as tkfont

import window_identity

# The board lives on the UI thread; the model's actions run on another. Asking
# "what have I already taught?" was therefore unanswerable -- BOARD_PAGES could
# only push the outline at the activity log and tell the model to go look, so
# the model, which cannot go look, taught page four over again. This is the
# missing half: the UI publishes a plain-data snapshot on every change, and
# anyone may read it without touching a widget.
_STATE_LOCK = threading.Lock()
_STATE = {"open": False, "page": 0, "count": 0, "outline": "", "current": ""}


def board_state():
    """The lesson so far, readable from any thread."""
    with _STATE_LOCK:
        return dict(_STATE)


def _set_board_state(**fields):
    with _STATE_LOCK:
        _STATE.update(fields)

# Chalk palette. Deep slate rather than black: pure black with pure white text
# is painful to read for the length of an actual lesson, and the faint blue
# cast is what stops it reading as a terminal.
BOARD_BG = "#111418"
CHALK = "#e9edf1"
CHALK_DIM = "#93a1ad"
CHALK_FAINT = "#5c6b78"
ACCENT_TITLE = "#7dd3fc"
ACCENT_HEAD = "#fbbf24"
ACCENT_SUB = "#a5b4fc"
ACCENT_CODE = "#86efac"
ACCENT_WARN = "#fca5a5"
ACCENT_RULE = "#2a333c"

MARGIN = 72
COLUMN_MAX = 1180          # long lines are unreadable however wide the screen


def _blend_hex(start, end, amount):
    """Mix two hex colours; used for attention strand strength."""
    a = tuple(int(start.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    b = tuple(int(end.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    amount = max(0.0, min(1.0, float(amount)))
    return "#%02x%02x%02x" % tuple(
        int(a[i] + (b[i] - a[i]) * amount) for i in range(3))


class BlackboardMixin:
    """Everything the teacher draws. Mixed into WinterUI."""

    # ------------------------------------------------------------ #
    # Surface
    # ------------------------------------------------------------ #
    def _ensure_board(self):
        if getattr(self, "_board_win", None) is not None and self._board_win.winfo_exists():
            return self._board_canvas

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        win.configure(bg=BOARD_BG)
        win.geometry(f"{win.winfo_screenwidth()}x{win.winfo_screenheight()}+0+0")
        canvas = tk.Canvas(win, bg=BOARD_BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        try:
            import win32con
            import win32gui
            hwnd = win32gui.GetParent(win.winfo_id()) or win.winfo_id()
            styles = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE,
                styles | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
            # A layered window with no layer attributes set draws nothing at
            # all -- Windows treats it as fully transparent, so the board went
            # up, filled with the lesson, and stayed invisible. Opaque alpha
            # restores the pixels while keeping the click-through.
            win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, 0x00000002)
            from vision import register_winter_hwnd
            register_winter_hwnd(hwnd)
        except Exception as exc:
            print(f"[Board] Could not make the board click-through: {exc}")

        self._board_win = win
        self._board_canvas = canvas
        self._board_y = MARGIN
        self._board_images = []      # PhotoImage refs, or Tk garbage-collects them
        self._board_math_cache = {}
        self._board_title = ""
        # A lesson is a list of pages, and a page is the list of draw commands
        # that made it -- not a rendered bitmap. Keeping the commands is what
        # lets a page be re-rendered at any time, which is what pagination and
        # "answer my question on that page" both need.
        self._pages = [{"title": "", "items": []}]
        self._page = 0
        self._anim_job = None
        self._anim_state = {}
        # Prose and pictures used to share one downward flow, so a diagram
        # landed wherever the text happened to stop and the two overlapped.
        # A page with anything visual on it splits into two columns instead:
        # words on the left, pictures on the right, each with its own flow.
        self._column = "full"
        self._column_y = {"left": MARGIN, "right": MARGIN, "full": MARGIN}
        self._overflow_warned = False
        self._build_pager()
        win.deiconify()
        return canvas

    # What belongs in the right-hand column. Everything else is prose and
    # stays on the left, including mathematics -- a formula is part of the
    # argument being made, not an illustration beside it.
    VISUAL_OPS = frozenset({"diagram", "table", "plot", "chart", "animate", "plot3d",
                            "matrix", "field", "contour", "capture"})

    def _usable_width(self):
        return self._board_canvas.winfo_screenwidth() - MARGIN * 2

    def _column_box(self, column=None):
        """(x, width) for a column. 'full' spans the page."""
        column = column or getattr(self, "_column", "full")
        usable = self._usable_width()
        if column == "full":
            width = min(COLUMN_MAX, usable)
            return MARGIN, width
        gap = 56
        half = (usable - gap) / 2
        if column == "left":
            return MARGIN, int(half)
        return int(MARGIN + half + gap), int(half)

    def _board_width(self):
        """The width available to whatever is being drawn right now."""
        return self._column_box()[1]

    def _x0(self):
        """The left edge of the active column.

        Every drawing routine below used MARGIN directly, which was the same
        thing back when the page was one column. It is not any more: a diagram
        anchored to the page margin lands underneath the prose instead of
        beside it.
        """
        return self._column_box()[0]

    def _page_bottom(self):
        # Clear of the pager bar, which sits 46px up from the bottom edge.
        return self._board_canvas.winfo_screenheight() - MARGIN - 70

    def _enter_column(self, column):
        """Point the cursor at a column, remembering where each one had got to.

        Leaving the full-width column pushes both halves down past whatever it
        drew. Without that, a title spanning the page left the columns still
        at the top margin and the first paragraph was written over it.
        """
        previous = getattr(self, "_column", "full")
        self._column_y[previous] = self._board_y
        if previous == "full":
            for side in ("left", "right"):
                self._column_y[side] = max(self._column_y[side], self._board_y)
        elif column == "full":
            self._column_y["full"] = max(self._column_y["full"],
                                        self._column_y["left"],
                                        self._column_y["right"])
        self._column = column
        self._board_y = self._column_y.get(column, MARGIN)

    def _fits(self, needed):
        return self._board_y + needed <= self._page_bottom()

    def _board_show(self):
        self._board_win.deiconify()
        self._board_win.lift()
        self._draw_escape_hint()
        # The pager must come up AFTER the board. Both are topmost, so
        # whichever is lifted last wins, and the board is full-screen -- it
        # was hiding the only controls the lesson has.
        pager = getattr(self, "_pager_win", None)
        if pager is not None and pager.winfo_exists():
            pager.deiconify()
            pager.lift()

    def _draw_escape_hint(self):
        """A permanent note in the corner saying how to leave.

        Written on every show and re-drawn after every page render, because
        the one thing a full-screen window must never do is leave someone
        with no idea how to get out of it.
        """
        canvas = self._board_canvas
        canvas.delete("escape-hint")
        canvas.create_text(
            canvas.winfo_screenwidth() - 22, 18, anchor="ne",
            text="F11 hide  \u00b7  Esc close  \u00b7  \u2190 \u2192 pages",
            fill=CHALK_FAINT, font=self._board_font("mono", 12, "normal"),
            tags="escape-hint")

    def _board_scroll_if_needed(self, needed):
        """Make room for `needed` more pixels, or move to a continuation page.

        This used to scroll the canvas and delete whatever left the top, which
        is how a lesson lost its own beginning. Now that the board is
        paginated there is somewhere better for overflow to go: the page
        continues. Nothing is cut off and nothing is thrown away.
        """
        if self._fits(needed):
            return

        # A column that has run out may still have a neighbour with room.
        if getattr(self, "_column", "full") == "left":
            self._enter_column("right")
            if self._fits(needed):
                return
            self._enter_column("left")

        if getattr(self, "_replaying", False):
            # Mid-replay there is no new page to make. This used to clamp the
            # cursor to the last usable line, which drew every remaining item
            # at the same y -- three bullets stacked into one and the rest of
            # the list simply looked missing. Overlapping is the one failure
            # that cannot be seen, so the overflow runs on instead and says so.
            if not self._overflow_warned:
                self._overflow_warned = True
                print(f"[Board] Page {self._page + 1} holds more than fits at "
                      f"this screen size; the tail runs past the bottom.")
            return
        self._continue_page()

    def _continue_page(self):
        """Open a continuation of the current page and draw there from now on."""
        title = (self._current()["title"] or "").strip()
        base = title[:-7].strip() if title.endswith("(cont.)") else title
        self._pages.insert(self._page + 1,
                           {"title": f"{base} (cont.)" if base else "(cont.)",
                            "items": []})
        self._page += 1
        self._stop_animation()
        self._board_canvas.delete("all")
        self._board_images = []
        self._reset_columns(self._current())
        self._draw_escape_hint()
        if self._current()["title"]:
            heading = {"op": "write", "style": "heading",
                       "text": self._current()["title"]}
            self._current()["items"].append(heading)
            self._draw_write({k: v for k, v in heading.items() if k != "op"})
        self._safe_pager()

    def _reset_columns(self, page):
        """Choose single or split for a page, and rewind both columns."""
        self._overflow_warned = False
        split = any(item["op"] in self.VISUAL_OPS for item in page["items"])
        self._column = "left" if split else "full"
        self._column_y = {"left": MARGIN, "right": MARGIN, "full": MARGIN}
        self._board_y = MARGIN
        return split

    # ------------------------------------------------------------ #
    # Text
    # ------------------------------------------------------------ #
    STYLES = {
        #  name          font key   size  weight    colour         lead  gap  indent
        "title":      ("display", 40, "bold",   ACCENT_TITLE, 52, 26, 0),
        "heading":    ("display", 28, "bold",   ACCENT_HEAD,  38, 16, 0),
        "subheading": ("display", 22, "bold",   ACCENT_SUB,   30, 12, 0),
        "body":       ("text",    20, "normal", CHALK,        30, 12, 0),
        "point":      ("text",    20, "normal", CHALK,        30, 8,  36),
        "subpoint":   ("text",    18, "normal", CHALK_DIM,    27, 6,  76),
        "note":       ("text",    17, "normal", CHALK_DIM,    26, 10, 0),
        "quote":      ("text",    19, "normal", CHALK_DIM,    29, 12, 44),
        "code":       ("mono",    17, "normal", ACCENT_CODE,  25, 10, 36),
        "warning":    ("text",    19, "bold",   ACCENT_WARN,  28, 12, 0),
        "definition": ("text",    19, "normal", CHALK,        28, 12, 36),
        "example":    ("text",    19, "normal", CHALK,        28, 12, 36),
        "answer":     ("display", 24, "bold",   ACCENT_CODE,  34, 16, 0),
    }

    # These styles get a vertical rule down their left edge so a block of them
    # reads as one aside rather than as more body text.
    RULED_STYLES = {"quote": CHALK_FAINT, "definition": ACCENT_SUB,
                    "example": ACCENT_CODE, "code": ACCENT_RULE}

    def _board_font(self, key, size, weight):
        family = {"display": self.FONT_DISPLAY, "text": self.FONT_TEXT,
                  "mono": self.FONT_MONO}.get(key, self.FONT_TEXT)
        return (family, size, weight) if weight != "normal" else (family, size)

    def _line_height(self, font):
        """The real height of a line in this font, measured not assumed.

        The STYLES table carries a leading per style, written as though a
        point equalled a pixel. It does not: at this DPI a 40pt title renders
        about 70px tall against a leading of 52, so the second line of a
        wrapped title was drawn 18px into the first. Asking the font is the
        only way to be right on every display.
        """
        cache = getattr(self, "_line_height_cache", None)
        if cache is None:
            cache = self._line_height_cache = {}
        key = tuple(font)
        if key not in cache:
            try:
                measure = tkfont.Font(family=font[0], size=font[1],
                                      weight=font[2] if len(font) > 2 else "normal")
                cache[key] = int(measure.metrics("linespace"))
            except Exception:
                cache[key] = int(font[1] * 1.6)
        return cache[key]

    def _board_wrap(self, text, font, max_width):
        measure = tkfont.Font(family=font[0], size=font[1],
                              weight=font[2] if len(font) > 2 else "normal")
        lines = []
        for raw in str(text).split("\n"):
            if not raw.strip():
                lines.append("")
                continue
            current = ""
            for word in raw.split():
                trial = f"{current} {word}".strip()
                if measure.measure(trial) <= max_width or not current:
                    current = trial
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    def _draw_write(self, spec):
        canvas = self._ensure_board()
        text = str(spec.get("text", "") or "")
        style = str(spec.get("style", "body")).lower()
        if style not in self.STYLES:
            style = "body"
        key, size, weight, colour, lead, gap, indent = self.STYLES[style]
        font = self._board_font(key, size, weight)
        colour = spec.get("color") or colour
        width = self._board_width() - indent

        prefix = ""
        # A wrapped continuation of a bullet must not sprout a second bullet.
        if not spec.get("continuation"):
            if style == "point":
                prefix = "•  "
            elif style == "subpoint":
                prefix = "–  "

        lines = self._board_wrap(prefix + text, font, width)
        # The configured leading is a minimum; the font decides the rest.
        lead = max(lead, self._line_height(font) + 3)
        block_height = max(lead, len(lines) * lead)
        self._board_scroll_if_needed(block_height + gap)
        top = self._board_y

        left = self._column_box()[0]
        for line in lines:
            canvas.create_text(left + indent, self._board_y, text=line,
                               fill=colour, font=font, anchor="nw")
            self._board_y += lead

        if style in self.RULED_STYLES:
            canvas.create_line(left + indent - 18, top,
                               left + indent - 18, self._board_y,
                               fill=self.RULED_STYLES[style], width=3)
        if style == "title":
            canvas.create_line(left, self._board_y + 12,
                               left + self._board_width(), self._board_y + 12,
                               fill=ACCENT_RULE, width=2)
            self._board_y += 22
            self._board_title = text

        self._board_y += gap
        self._board_show()

    def _draw_rule(self, spec=None):
        canvas = self._ensure_board()
        self._board_scroll_if_needed(26)
        self._board_y += 10
        left, width = self._column_box()
        canvas.create_line(left, self._board_y, left + width,
                           self._board_y, fill=ACCENT_RULE, width=1)
        self._board_y += 16
        self._board_show()

    # ------------------------------------------------------------ #
    # Mathematics
    # ------------------------------------------------------------ #
    def _figure_image(self, figure, dpi=140, pad=0.04):
        """Render a matplotlib figure onto the board's background as a Tk image."""
        from PIL import Image, ImageTk

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight",
                       pad_inches=pad, transparent=True)
        buffer.seek(0)
        rendered = Image.open(buffer).convert("RGBA")
        # Tk cannot composite alpha itself, so flatten onto the board colour.
        backing = Image.new("RGBA", rendered.size, BOARD_BG)
        flattened = Image.alpha_composite(backing, rendered).convert("RGB")

        flattened = self._fit_into_column(flattened)
        photo = ImageTk.PhotoImage(flattened)
        self._board_images.append(photo)
        return photo

    def _fit_into_column(self, image):
        """Shrink a rendered figure until it cannot overlap anything.

        Two bounds, and the height one is the important one: a figure is
        placed at the current cursor, so anything taller than the room left
        below it is drawn straight over whatever comes next. Capping against
        the *remaining* space rather than a fixed fraction of the screen is
        what makes overlap impossible rather than merely unlikely.
        """
        from PIL import Image

        width_limit = max(120, self._board_width())
        if image.width > width_limit:
            ratio = width_limit / image.width
            image = image.resize((width_limit, max(1, int(image.height * ratio))),
                                 Image.LANCZOS)

        remaining = self._page_bottom() - self._board_y - 40
        # Below about a third of the page there is no point shrinking further;
        # the figure would be unreadable, so it moves to a fresh page instead.
        floor = int(self._board_canvas.winfo_screenheight() * 0.30)
        height_limit = max(floor, remaining)
        if image.height > height_limit:
            ratio = height_limit / image.height
            image = image.resize((max(1, int(image.width * ratio)), height_limit),
                                 Image.LANCZOS)
        return image

    def _typeset(self, latex, size, colour):
        """Render one formula, retrying once with the escaping undone.

        A model writing LaTeX into an XML attribute frequently escapes its
        backslashes, so `\\frac` arrives where `\frac` was meant. mathtext
        rejects that outright, and the lesson loses the one line it turned on.
        Collapsing doubled backslashes and trying again costs nothing and
        rescues the common case.
        """
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure

        attempts = [latex]
        collapsed = latex.replace("\\\\", "\\")
        if collapsed != latex:
            attempts.append(collapsed)

        last = None
        for source in attempts:
            try:
                figure = Figure(figsize=(0.01, 0.01))
                figure.patch.set_alpha(0.0)
                figure.text(0, 0, f"${source}$", fontsize=size, color=colour)
                return self._figure_image(figure, dpi=150, pad=0.02)
            except Exception as exc:
                last = exc
        raise last

    def _draw_math(self, spec):
        """Set a formula properly, through matplotlib's mathtext.

        Falls back to monospace with the parse error attached rather than
        dropping the line: in a derivation the formula is the content, and a
        blank where it should be teaches nothing.
        """
        canvas = self._ensure_board()
        latex = str(spec.get("latex") or spec.get("text") or "").strip()
        if not latex:
            return
        latex = latex.strip("$")
        size = int(spec.get("size", 30) or 30)
        colour = spec.get("color") or CHALK
        caption = str(spec.get("caption", "") or "").strip()
        align = str(spec.get("align", "center")).lower()

        cache_key = (latex, size, colour)
        photo = self._board_math_cache.get(cache_key)
        if photo is None:
            try:
                photo = self._typeset(latex, size, colour)
                self._board_math_cache[cache_key] = photo
            except Exception as exc:
                # Show the source rather than a blank space -- in a derivation
                # the formula is the content -- and name the reason, which an
                # empty parenthesis never did.
                reason = str(exc).splitlines()[0].strip() or type(exc).__name__
                self._draw_write({"style": "code", "text": latex})
                self._draw_write({"style": "note",
                                   "text": f"(could not typeset: {reason[:100]})"})
                return

        height = photo.height() + (26 if caption else 0)
        self._board_scroll_if_needed(height + 24)
        if not self._fits(height + 24):
            # The formula alone is taller than the page; nothing more can be
            # done about it, but say so rather than drawing over the page.
            print(f"[Board] A formula on page {self._page + 1} is taller than "
                  "the page allows.")
        self._board_y += 10
        left, width = self._column_box()
        x = left if align.startswith("l") else \
            left + max(0, (width - photo.width())) // 2
        canvas.create_image(x, self._board_y, image=photo, anchor="nw")
        self._board_y += photo.height() + 6
        if caption:
            canvas.create_text(left + width // 2, self._board_y,
                               text=caption, fill=CHALK_DIM,
                               font=self._board_font("text", 16, "normal"),
                               anchor="n")
            self._board_y += 24
        self._board_y += 14
        self._board_show()

    # ------------------------------------------------------------ #
    # Plots -- the part that makes physics teachable
    # ------------------------------------------------------------ #
    SAFE_MATH = None

    def _plot_namespace(self):
        """Numpy only. The expression comes from the model, so it evaluates in
        a namespace with no builtins and nothing that touches the machine."""
        if self.SAFE_MATH is None:
            import numpy as np
            BlackboardMixin.SAFE_MATH = {
                name: getattr(np, name) for name in (
                    "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh",
                    "cosh", "tanh", "exp", "log", "log10", "log2", "sqrt", "abs",
                    "floor", "ceil", "sign", "power", "pi", "e", "maximum",
                    "minimum", "where", "clip", "heaviside", "gradient")
                if hasattr(np, name)
            }
            BlackboardMixin.SAFE_MATH["np"] = np
        return dict(self.SAFE_MATH)

    def _draw_plot(self, spec):
        """Plot one or more functions of x on a dark, labelled set of axes."""
        self._ensure_board()
        expressions = [e.strip() for e in
                       str(spec.get("expr") or spec.get("y") or "").split(";")
                       if e.strip()]
        if not expressions:
            self._draw_write({"style": "note",
                               "text": "(BOARD_PLOT needs expr=\"sin(x)\")"})
            return
        try:
            import matplotlib
            matplotlib.use("Agg")
            import numpy as np
            from matplotlib.figure import Figure

            start = float(spec.get("xmin", -10))
            stop = float(spec.get("xmax", 10))
            if stop <= start:
                start, stop = -10.0, 10.0
            x = np.linspace(start, stop, 900)

            figure = Figure(figsize=(8.4, 4.1))
            figure.patch.set_facecolor(BOARD_BG)
            axes = figure.add_subplot(111)
            axes.set_facecolor(BOARD_BG)

            labels = [s.strip() for s in str(spec.get("labels", "")).split(",")]
            palette = (ACCENT_TITLE, ACCENT_HEAD, ACCENT_CODE, ACCENT_SUB, ACCENT_WARN)
            namespace = self._plot_namespace()
            namespace["x"] = x
            plotted = 0
            for index, expression in enumerate(expressions):
                try:
                    y = eval(expression, {"__builtins__": {}}, namespace)
                except Exception as exc:
                    self._board_write({
                        "style": "note",
                        "text": f"(could not plot '{expression}': {str(exc)[:60]})"})
                    continue
                y = np.broadcast_to(np.asarray(y, dtype=float), x.shape)
                label = labels[index] if index < len(labels) and labels[index] \
                    else expression
                axes.plot(x, y, color=palette[index % len(palette)], linewidth=2.2,
                          label=label)
                plotted += 1
            if not plotted:
                return

            axes.axhline(0, color=CHALK_FAINT, linewidth=1)
            axes.axvline(0, color=CHALK_FAINT, linewidth=1)
            axes.grid(True, color=ACCENT_RULE, linewidth=0.8, alpha=0.7)
            for side in axes.spines.values():
                side.set_color(ACCENT_RULE)
            axes.tick_params(colors=CHALK_DIM, labelsize=11)
            axes.set_xlabel(str(spec.get("xlabel", "x")), color=CHALK_DIM, fontsize=13)
            axes.set_ylabel(str(spec.get("ylabel", "y")), color=CHALK_DIM, fontsize=13)
            if spec.get("title"):
                axes.set_title(str(spec["title"]), color=CHALK, fontsize=15, pad=12)
            if spec.get("ymin") is not None and spec.get("ymax") is not None:
                try:
                    axes.set_ylim(float(spec["ymin"]), float(spec["ymax"]))
                except (TypeError, ValueError):
                    pass
            if plotted > 1 or labels[0]:
                legend = axes.legend(facecolor=BOARD_BG, edgecolor=ACCENT_RULE,
                                     labelcolor=CHALK, fontsize=11)
                legend.get_frame().set_alpha(0.9)

            self._board_place_figure(figure)
        except Exception as exc:
            self._draw_write({"style": "note", "text": f"(plot failed: {exc})"})

    def _draw_chart(self, spec):
        """Bar or pie chart from labels and values."""
        self._ensure_board()
        try:
            import matplotlib
            matplotlib.use("Agg")
            from matplotlib.figure import Figure

            labels = [s.strip() for s in str(spec.get("labels", "")).split(",") if s.strip()]
            values = []
            for raw in str(spec.get("values", "")).split(","):
                raw = raw.strip()
                if raw:
                    values.append(float(raw))
            if not labels or len(labels) != len(values):
                self._board_write({
                    "style": "note",
                    "text": "(chart needs matching labels=\"a,b\" and values=\"1,2\")"})
                return

            kind = str(spec.get("kind", "bar")).lower()
            figure = Figure(figsize=(7.8, 3.9))
            figure.patch.set_facecolor(BOARD_BG)
            axes = figure.add_subplot(111)
            axes.set_facecolor(BOARD_BG)
            palette = (ACCENT_TITLE, ACCENT_HEAD, ACCENT_CODE, ACCENT_SUB,
                       ACCENT_WARN, CHALK_DIM)
            colours = [palette[i % len(palette)] for i in range(len(values))]

            if kind == "pie":
                axes.pie(values, labels=labels, colors=colours,
                         textprops={"color": CHALK, "fontsize": 12},
                         wedgeprops={"edgecolor": BOARD_BG, "linewidth": 2})
            else:
                axes.bar(labels, values, color=colours)
                axes.grid(True, axis="y", color=ACCENT_RULE, linewidth=0.8, alpha=0.6)
                for side in axes.spines.values():
                    side.set_color(ACCENT_RULE)
                axes.tick_params(colors=CHALK_DIM, labelsize=11)
            if spec.get("title"):
                axes.set_title(str(spec["title"]), color=CHALK, fontsize=15, pad=12)
            self._board_place_figure(figure)
        except Exception as exc:
            self._draw_write({"style": "note", "text": f"(chart failed: {exc})"})

    # ------------------------------------------------------------ #
    # The hard visualisations
    #
    # Structure diagrams carry an argument; these carry a function. A loss
    # landscape with a saddle in it, an attention matrix, a gradient field --
    # none of them can be drawn with boxes and arrows, and describing them in
    # words is what makes hard material sound like hand-waving.
    #
    # All of them go through matplotlib, and all of them end at
    # _board_place_figure, so the same fitting rule applies: a figure is
    # bounded by the room left in its column and can never overlap.
    # ------------------------------------------------------------ #
    @staticmethod
    def _opt(spec, key, default):
        """A spec value, treating empty as absent.

        The action dispatcher fills every known attribute, so anything the
        model omitted arrives as "" rather than missing. dict.get returns that
        empty string instead of the default, which is how every one of these
        renderers ended up asking matplotlib for a colour map called "".
        """
        value = spec.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return value

    def _dark_axes(self, axes, three_d=False):
        """The board's palette, applied to a matplotlib axis."""
        axes.set_facecolor(BOARD_BG)
        axes.tick_params(colors=CHALK_DIM, labelsize=9)
        if three_d:
            for pane in (axes.xaxis, axes.yaxis, axes.zaxis):
                pane.set_pane_color((0.07, 0.09, 0.11, 1.0))
                pane._axinfo["grid"]["color"] = ACCENT_RULE
                pane.label.set_color(CHALK_DIM)
        else:
            for side in axes.spines.values():
                side.set_color(ACCENT_RULE)
            axes.grid(True, color=ACCENT_RULE, linewidth=0.7, alpha=0.6)
        return axes

    def _new_figure(self, width_inches=7.4, height_inches=4.6):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure

        figure = Figure(figsize=(width_inches, height_inches))
        figure.patch.set_facecolor(BOARD_BG)
        return figure

    def _grid(self, spec, points=70):
        """The x, y mesh every surface and field is evaluated on."""
        import numpy as np

        def number(key, fallback):
            try:
                return float(spec.get(key, fallback))
            except (TypeError, ValueError):
                return float(fallback)

        x_min, x_max = number("xmin", -3), number("xmax", 3)
        y_min, y_max = number("ymin", x_min), number("ymax", x_max)
        if x_max <= x_min:
            x_min, x_max = -3.0, 3.0
        if y_max <= y_min:
            y_min, y_max = -3.0, 3.0
        x = np.linspace(x_min, x_max, points)
        y = np.linspace(y_min, y_max, points)
        return np.meshgrid(x, y)

    def _evaluate(self, expression, **variables):
        """Evaluate a model-supplied expression in the numpy-only namespace."""
        namespace = self._plot_namespace()
        namespace.update(variables)
        return eval(str(expression), {"__builtins__": {}}, namespace)

    def _render_3d(self, spec, azimuth=None):
        """One frame of a 3D figure, as a matplotlib Figure."""
        import numpy as np

        kind = str(self._opt(spec, "kind", "surface")).lower()
        figure = self._new_figure(7.0, 5.0)
        axes = figure.add_subplot(111, projection="3d")
        self._dark_axes(axes, three_d=True)
        colour_map = str(self._opt(spec, "cmap", "viridis"))

        if kind in ("parametric", "curve", "line"):
            t = np.linspace(float(self._opt(spec, "tmin", 0) or 0),
                            float(self._opt(spec, "tmax", 12) or 12), 700)
            x = self._evaluate(self._opt(spec, "x", "cos(t)"), t=t)
            y = self._evaluate(self._opt(spec, "y", "sin(t)"), t=t)
            z = self._evaluate(self._opt(spec, "z", "t/6"), t=t)
            axes.plot(np.broadcast_to(x, t.shape), np.broadcast_to(y, t.shape),
                      np.broadcast_to(z, t.shape), color=ACCENT_TITLE, linewidth=2.2)
        elif kind in ("scatter", "points"):
            values = [float(v) for v in str(self._opt(spec, "values", "")).split(",")
                      if v.strip()]
            points = np.array(values).reshape(-1, 3) if len(values) >= 3 else None
            if points is None:
                raise ValueError('a 3D scatter needs values="x,y,z,x,y,z,..."')
            axes.scatter(points[:, 0], points[:, 1], points[:, 2],
                         c=points[:, 2], cmap=colour_map, s=44, depthshade=True)
        else:
            mesh_x, mesh_y = self._grid(spec, points=64)
            mesh_z = self._evaluate(self._opt(spec, "expr", "sin(sqrt(x**2 + y**2))"),
                                    x=mesh_x, y=mesh_y)
            mesh_z = np.broadcast_to(np.asarray(mesh_z, dtype=float), mesh_x.shape)
            if kind in ("wireframe", "wire", "mesh"):
                axes.plot_wireframe(mesh_x, mesh_y, mesh_z, color=ACCENT_TITLE,
                                    linewidth=0.6, rstride=3, cstride=3)
            else:
                axes.plot_surface(mesh_x, mesh_y, mesh_z, cmap=colour_map,
                                  linewidth=0, antialiased=True, alpha=0.95)
                if str(self._opt(spec, "contours", "")).lower() == "true":
                    axes.contour(mesh_x, mesh_y, mesh_z, zdir="z",
                                 offset=float(np.min(mesh_z)), cmap=colour_map,
                                 linewidths=0.8)

        axes.set_xlabel(str(self._opt(spec, "xlabel", "x")), color=CHALK_DIM, fontsize=10)
        axes.set_ylabel(str(self._opt(spec, "ylabel", "y")), color=CHALK_DIM, fontsize=10)
        axes.set_zlabel(str(self._opt(spec, "zlabel", "z")), color=CHALK_DIM, fontsize=10)
        if spec.get("title"):
            axes.set_title(str(spec["title"]), color=CHALK, fontsize=13, pad=8)
        axes.view_init(elev=float(self._opt(spec, "elev", 28) or 28),
                       azim=float(self._opt(spec, "azim", -55) or -55)
                       if azimuth is None else azimuth)
        return figure

    def _draw_plot3d(self, spec):
        try:
            figure = self._render_3d(spec)
        except Exception as exc:
            self._draw_write({"style": "note",
                              "text": f"(3D plot failed: {str(exc)[:110]})"})
            return
        self._board_place_figure(figure, dpi=104)
        if str(self._opt(spec, "rotate", "")).lower() in ("true", "1", "yes"):
            self._start_rotation(spec)

    def _start_rotation(self, spec):
        """Render a turn of the object off-thread, then cycle the frames.

        A 3D figure costs a couple of hundred milliseconds to render, so a
        rotation cannot be produced frame by frame in the animation loop
        without stalling everything. The turn is rendered in the background
        and played back as images, which costs nothing per frame.
        """
        import threading

        frames = max(8, min(int(self._opt(spec, "frames", 18) or 18), 36))
        image_id = self._board_canvas.find_withtag("figure-latest")
        target = image_id[-1] if image_id else None
        if target is None:
            return
        base_azimuth = float(self._opt(spec, "azim", -55) or -55)

        def worker():
            produced = []
            for index in range(frames):
                try:
                    figure = self._render_3d(
                        spec, azimuth=base_azimuth + index * (360.0 / frames))
                    produced.append(self._figure_to_image(figure, dpi=104))
                except Exception as exc:
                    print(f"[Board] Rotation frame {index} failed: {exc}")
                    return
            try:
                self._q.put(("board_frames", {"item": target, "images": produced}))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="vanta-board-rotation").start()

    def _figure_to_image(self, figure, dpi=110, pad=0.04):
        """A figure as a PIL image, flattened and fitted. Safe off-thread."""
        from PIL import Image

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight",
                       pad_inches=pad, transparent=True)
        buffer.seek(0)
        rendered = Image.open(buffer).convert("RGBA")
        backing = Image.new("RGBA", rendered.size, BOARD_BG)
        return self._fit_into_column(
            Image.alpha_composite(backing, rendered).convert("RGB"))

    def _board_frames(self, payload):
        """Play back pre-rendered frames by swapping one canvas image."""
        from PIL import ImageTk

        images = payload.get("images") or []
        target = payload.get("item")
        if not images or target is None:
            return
        try:
            photos = [ImageTk.PhotoImage(image) for image in images]
        except Exception:
            return
        self._board_images.extend(photos)
        self._stop_animation()
        self._anim_state = {"tick": 0.0, "frames": photos, "item": target,
                            "draw": self._anim_swap, "top": 0, "height": 0,
                            "spec": {}}
        self._animate_tick()

    def _anim_swap(self, state):
        frames = state["frames"]
        index = int(state["tick"] * self.ANIMATION_FPS / 2) % len(frames)
        try:
            self._board_canvas.itemconfigure(state["item"], image=frames[index])
        except Exception:
            self._stop_animation()

    def _draw_matrix(self, spec):
        """A heatmap. Attention weights, confusions, correlations, kernels."""
        import numpy as np

        try:
            rows = [r for r in str(self._opt(spec, "values", "")).split("|") if r.strip()]
            grid = np.array([[float(cell) for cell in row.split(",") if cell.strip()]
                             for row in rows], dtype=float)
            if grid.ndim != 2 or grid.size == 0:
                raise ValueError("no usable numbers")
        except Exception as exc:
            self._draw_write({
                "style": "note",
                "text": f'(BOARD_MATRIX needs values="1,2|3,4": {str(exc)[:70]})'})
            return

        figure = self._new_figure(6.6, 5.0)
        axes = figure.add_subplot(111)
        axes.set_facecolor(BOARD_BG)
        image = axes.imshow(grid, cmap=str(self._opt(spec, "cmap", "magma")),
                            aspect="auto", interpolation="nearest")
        columns = [c.strip() for c in str(self._opt(spec, "columns", "")).split(",") if c.strip()]
        row_labels = [r.strip() for r in str(self._opt(spec, "rows", "")).split(",") if r.strip()]
        if columns:
            axes.set_xticks(range(grid.shape[1]))
            axes.set_xticklabels(columns[:grid.shape[1]], color=CHALK_DIM,
                                 fontsize=10, rotation=30, ha="right")
        if row_labels:
            axes.set_yticks(range(grid.shape[0]))
            axes.set_yticklabels(row_labels[:grid.shape[0]], color=CHALK_DIM,
                                 fontsize=10)
        axes.tick_params(colors=CHALK_DIM, labelsize=9)
        for side in axes.spines.values():
            side.set_color(ACCENT_RULE)
        # Annotating only small grids on purpose: numbers on a 40x40 matrix
        # are unreadable and turn a clear picture into grey noise.
        if str(self._opt(spec, "annotate", "true")).lower() != "false" and grid.size <= 64:
            middle = (grid.max() + grid.min()) / 2
            for (row, column), value in np.ndenumerate(grid):
                axes.text(column, row, f"{value:.2f}", ha="center", va="center",
                          fontsize=9,
                          color=BOARD_BG if value > middle else CHALK)
        bar = figure.colorbar(image, ax=axes, fraction=0.045)
        bar.ax.tick_params(colors=CHALK_DIM, labelsize=8)
        bar.outline.set_edgecolor(ACCENT_RULE)
        if spec.get("title"):
            axes.set_title(str(spec["title"]), color=CHALK, fontsize=13, pad=8)
        self._board_place_figure(figure, dpi=104)

    def _draw_field(self, spec):
        """A vector field, with optional level sets underneath it."""
        import numpy as np

        try:
            mesh_x, mesh_y = self._grid(spec, points=int(self._opt(spec, "density", 18) or 18))
            u = np.broadcast_to(
                np.asarray(self._evaluate(self._opt(spec, "fx", "-y"), x=mesh_x, y=mesh_y),
                           dtype=float), mesh_x.shape)
            v = np.broadcast_to(
                np.asarray(self._evaluate(self._opt(spec, "fy", "x"), x=mesh_x, y=mesh_y),
                           dtype=float), mesh_x.shape)
        except Exception as exc:
            self._draw_write({"style": "note",
                              "text": f"(field failed: {str(exc)[:110]})"})
            return

        figure = self._new_figure(6.8, 4.8)
        axes = self._dark_axes(figure.add_subplot(111))
        if spec.get("potential"):
            fine_x, fine_y = self._grid(spec, points=120)
            try:
                height = np.broadcast_to(np.asarray(
                    self._evaluate(spec["potential"], x=fine_x, y=fine_y),
                    dtype=float), fine_x.shape)
                axes.contourf(fine_x, fine_y, height, levels=22,
                              cmap=str(self._opt(spec, "cmap", "magma")), alpha=0.55)
            except Exception:
                pass
        magnitude = np.hypot(u, v)
        axes.quiver(mesh_x, mesh_y, u, v, magnitude,
                    cmap=str(self._opt(spec, "arrow_cmap", "cool")), pivot="mid",
                    scale=float(self._opt(spec, "scale", 0) or 0) or None)
        axes.set_xlabel(str(self._opt(spec, "xlabel", "x")), color=CHALK_DIM, fontsize=11)
        axes.set_ylabel(str(self._opt(spec, "ylabel", "y")), color=CHALK_DIM, fontsize=11)
        if spec.get("title"):
            axes.set_title(str(spec["title"]), color=CHALK, fontsize=13, pad=8)
        self._board_place_figure(figure, dpi=104)

    def _draw_contour(self, spec):
        """Level sets of a function of two variables, optionally with a path."""
        import numpy as np

        try:
            mesh_x, mesh_y = self._grid(spec, points=140)
            height = np.broadcast_to(np.asarray(
                self._evaluate(self._opt(spec, "expr", "x**2 + y**2"), x=mesh_x, y=mesh_y),
                dtype=float), mesh_x.shape)
        except Exception as exc:
            self._draw_write({"style": "note",
                              "text": f"(contour failed: {str(exc)[:110]})"})
            return

        figure = self._new_figure(6.8, 4.8)
        axes = self._dark_axes(figure.add_subplot(111))
        filled = axes.contourf(mesh_x, mesh_y, height, levels=26,
                               cmap=str(self._opt(spec, "cmap", "viridis")), alpha=0.9)
        axes.contour(mesh_x, mesh_y, height, levels=14, colors=BOARD_BG,
                     linewidths=0.5)
        # A path over the level sets is how gradient descent stops being an
        # equation and becomes a picture of something rolling downhill.
        path = [p.strip() for p in str(self._opt(spec, "path", "")).split(",") if p.strip()]
        if len(path) >= 4:
            try:
                numbers = [float(value) for value in path]
                axes.plot(numbers[0::2], numbers[1::2], color="#ffffff",
                          marker="o", markersize=4, linewidth=1.8)
            except ValueError:
                pass
        bar = figure.colorbar(filled, ax=axes, fraction=0.045)
        bar.ax.tick_params(colors=CHALK_DIM, labelsize=8)
        bar.outline.set_edgecolor(ACCENT_RULE)
        if spec.get("title"):
            axes.set_title(str(spec["title"]), color=CHALK, fontsize=13, pad=8)
        self._board_place_figure(figure, dpi=104)

    # ------------------------------------------------------------ #
    # Bringing the screen onto the board
    #
    # "Explain this slide" is not a request for a fresh lesson about the
    # topic -- it is a request about the thing on screen. Pulling the slide
    # onto the board puts the explanation next to the original instead of
    # replacing it, which is what makes it an explanation rather than a
    # substitution.
    # ------------------------------------------------------------ #
    def _capture_region(self, spec):
        """(left, top, right, bottom) in screen pixels, from the spec."""
        canvas = self._board_canvas
        width, height = canvas.winfo_screenwidth(), canvas.winfo_screenheight()
        raw = str(self._opt(spec, "region", "")).strip()
        if raw:
            try:
                numbers = [int(float(n)) for n in raw.replace(",", " ").split()]
            except ValueError:
                numbers = []
            if len(numbers) == 4:
                left, top, third, fourth = numbers
                # Accept both spellings rather than guessing wrong: a width and
                # height are always smaller than the far edge they imply.
                right = left + third if third <= width and third < left + third else third
                bottom = top + fourth if fourth <= height and fourth < top + fourth else fourth
                return (max(0, left), max(0, top),
                        min(width, max(left + 8, right)),
                        min(height, max(top + 8, bottom)))
        window = str(self._opt(spec, "window", "")).strip()
        if window:
            try:
                import desktop
                matches = desktop._find_window(window)
                if matches:
                    import win32gui
                    left, top, right, bottom = win32gui.GetWindowRect(matches[0][0])
                    return (max(0, left), max(0, top),
                            min(width, right), min(height, bottom))
            except Exception as exc:
                print(f"[Board] Could not find the window '{window}': {exc}")
        return (0, 0, width, height)

    def _board_capture(self, spec):
        """Photograph the screen and put it on the board.

        The board is hidden for the grab. It is full-screen and opaque, so
        capturing with it up would photograph the board itself -- and once a
        lesson is running, that is exactly when this gets asked for.
        """
        import time
        from PIL import ImageGrab

        self._ensure_board()
        window = self._board_win
        pager = getattr(self, "_pager_win", None)
        was_visible = window.winfo_exists() and window.state() == "normal"

        if was_visible:
            window.withdraw()
            if pager is not None and pager.winfo_exists():
                pager.withdraw()
            window.update_idletasks()
            # The compositor needs a moment to actually stop drawing the
            # window; grabbing immediately still catches it.
            time.sleep(0.22)

        try:
            box = self._capture_region(spec)
            image = ImageGrab.grab(bbox=box, all_screens=True)
        except Exception as exc:
            if was_visible:
                self._board_show()
            self._draw_write({"style": "note",
                              "text": f"(could not capture the screen: {exc})"})
            return
        finally:
            if was_visible:
                window.deiconify()
                window.lift()
                if pager is not None and pager.winfo_exists():
                    pager.deiconify()
                    pager.lift()

        import os
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "captures", "board")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("screen_%Y%m%d_%H%M%S.png"))
        image.save(path)
        # Stored as a path, not as pixels: a page is replayed from its
        # commands, and holding a full-screen bitmap per capture would make a
        # long lesson expensive to keep.
        self._record("capture", {"path": path,
                                 "caption": str(self._opt(spec, "caption", ""))})

    def _draw_capture(self, spec):
        from PIL import Image, ImageTk

        path = str(spec.get("path", ""))
        try:
            image = Image.open(path).convert("RGB")
        except Exception as exc:
            self._draw_write({"style": "note",
                              "text": f"(the captured image is gone: {exc})"})
            return

        self._board_scroll_if_needed(
            int(self._board_canvas.winfo_screenheight() * 0.30))
        fitted = self._fit_into_column(image)
        photo = ImageTk.PhotoImage(fitted)
        self._board_images.append(photo)

        canvas = self._board_canvas
        self._board_scroll_if_needed(photo.height() + 42)
        left, width = self._column_box()
        x = left + max(0, (width - photo.width())) // 2
        self._board_y += 10
        # A thin frame, so a white slide on a dark board still reads as a
        # picture of something rather than a hole in the page.
        canvas.create_rectangle(x - 2, self._board_y - 2,
                                x + photo.width() + 2,
                                self._board_y + photo.height() + 2,
                                outline=ACCENT_RULE, width=2)
        canvas.create_image(x, self._board_y, image=photo, anchor="nw")
        self._board_y += photo.height() + 8
        caption = str(spec.get("caption", "") or "").strip()
        if caption:
            canvas.create_text(x + photo.width() / 2, self._board_y,
                               text=caption, fill=CHALK_DIM, anchor="n",
                               font=self._board_font("text", 15, "normal"))
            self._board_y += 26
        self._board_y += 12
        self._board_show()

    def _board_place_figure(self, figure, dpi=110):
        # Ask for the room first: rendering against the cursor and only then
        # discovering there is no space is how a figure ends up on top of the
        # paragraph below it. If the page is full this moves to a new one, and
        # the figure is rendered against the fresh cursor.
        self._board_scroll_if_needed(int(self._board_canvas.winfo_screenheight() * 0.32))
        photo = self._figure_image(figure, dpi=dpi)
        canvas = self._board_canvas
        self._board_scroll_if_needed(photo.height() + 30)
        self._board_y += 12
        left, width = self._column_box()
        x = left + max(0, (width - photo.width())) // 2
        canvas.dtag("figure-latest", "figure-latest")
        canvas.create_image(x, self._board_y, image=photo, anchor="nw",
                            tags="figure-latest")
        self._board_y += photo.height() + 20
        self._board_show()

    # ------------------------------------------------------------ #
    # Diagrams
    # ------------------------------------------------------------ #
    def _draw_diagram(self, spec):
        canvas = self._ensure_board()
        kind = str(spec.get("kind", "flow")).strip().lower().replace("-", "_")
        accent = spec.get("color") or ACCENT_TITLE
        drawer = {
            "flow": self._diagram_flow, "flowchart": self._diagram_flow,
            "steps": self._diagram_flow,
            "pipeline": self._diagram_flow, "process": self._diagram_flow,
            "architecture": self._diagram_boxes,
            "system": self._diagram_boxes, "block": self._diagram_boxes,
            "block_diagram": self._diagram_boxes,
            "boxes": self._diagram_boxes, "sequence": self._diagram_boxes,
            "cycle": self._diagram_cycle, "loop": self._diagram_cycle,
            "tree": self._diagram_tree, "hierarchy": self._diagram_tree,
            "timeline": self._diagram_timeline,
            "compare": self._diagram_compare, "vs": self._diagram_compare,
            "forces": self._diagram_forces, "freebody": self._diagram_forces,
            "venn": self._diagram_venn,
            "table": self._diagram_table,
            "neural_network": self._diagram_network, "nn": self._diagram_network,
            "network": self._diagram_network,
        }.get(kind)
        if drawer is None:
            # Models naturally name a diagram by its subject ("architecture",
            # "pipeline", "concept map") rather than by our renderer name.
            # If the request contains a usable ordered node set, a clean flow
            # is a more useful and honest fallback than silently drawing
            # nothing or exposing the renderer vocabulary to the user.
            if self._items(spec, "steps", "items", "nodes"):
                drawer = self._diagram_flow
            else:
                self._draw_write({
                    "style": "note",
                    "text": f"(I need nodes or steps to draw the {kind} diagram.)"})
                return
        drawer(canvas, spec, accent)
        self._board_show()

    @staticmethod
    def _items(spec, *keys):
        for key in keys:
            raw = spec.get(key)
            if raw:
                return [s.strip() for s in str(raw).split(",") if s.strip()]
        return []

    def _fit_text(self, canvas, x, y, text, width, font, fill, anchor="center",
                  tags=None):
        """Draw text clipped to a width, shrinking once before truncating.

        `tags` matters for anything redrawn per frame: an untagged label
        survives the caller's canvas.delete(tag), so an animation quietly
        stacked a fresh copy of every label thirty times a second.
        """
        measure = tkfont.Font(family=font[0], size=font[1],
                              weight=font[2] if len(font) > 2 else "normal")
        label = str(text)
        if measure.measure(label) > width:
            font = (font[0], max(11, int(font[1] * 0.82))) + tuple(font[2:])
            measure = tkfont.Font(family=font[0], size=font[1],
                                  weight=font[2] if len(font) > 2 else "normal")
            while label and measure.measure(label + "…") > width:
                label = label[:-1]
            if label != str(text):
                label += "…"
        return canvas.create_text(x, y, text=label, fill=fill, font=font,
                                  anchor=anchor, tags=tags or ())

    def _diagram_flow(self, canvas, spec, accent):
        steps = self._items(spec, "steps", "items", "nodes")
        if not steps:
            return
        horizontal = str(spec.get("direction", "down")).lower().startswith("r")
        width = self._board_width()
        if horizontal:
            box_w = min(220, (width - 40 * (len(steps) - 1)) // max(1, len(steps)))
            box_h, arrow = 74, 40
            self._board_scroll_if_needed(box_h + 40)
            top = self._board_y + 10
            x = self._x0()
            for index, step in enumerate(steps):
                canvas.create_rectangle(x, top, x + box_w, top + box_h,
                                        outline=accent, fill="#17202a", width=2)
                self._fit_text(canvas, x + box_w / 2, top + box_h / 2, step,
                               box_w - 16, self._board_font("text", 16, "normal"),
                               CHALK)
                if index < len(steps) - 1:
                    canvas.create_line(x + box_w, top + box_h / 2,
                                       x + box_w + arrow, top + box_h / 2,
                                       fill=accent, width=2, arrow="last")
                x += box_w + arrow
            self._board_y = top + box_h + 26
            return

        box_w = min(560, width)
        box_h, gap = 62, 28
        self._board_scroll_if_needed(len(steps) * (box_h + gap) + 16)
        x1 = self._x0()
        x2 = self._x0() + box_w
        for index, step in enumerate(steps):
            y1 = self._board_y
            y2 = y1 + box_h
            canvas.create_rectangle(x1, y1, x2, y2, outline=accent,
                                    fill="#17202a", width=2)
            self._fit_text(canvas, (x1 + x2) / 2, (y1 + y2) / 2, step, box_w - 24,
                           self._board_font("text", 17, "normal"), CHALK)
            if index < len(steps) - 1:
                canvas.create_line((x1 + x2) / 2, y2, (x1 + x2) / 2, y2 + gap,
                                   fill=accent, width=2, arrow="last")
            self._board_y = y2 + gap
        self._board_y += 8

    def _diagram_boxes(self, canvas, spec, accent):
        spec = dict(spec)
        spec["direction"] = "right"
        self._diagram_flow(canvas, spec, accent)

    def _diagram_cycle(self, canvas, spec, accent):
        steps = self._items(spec, "steps", "items", "nodes")
        if len(steps) < 2:
            return
        size = 380
        self._board_scroll_if_needed(size + 40)
        top = self._board_y + 16
        cx = self._x0() + self._board_width() / 2
        cy = top + size / 2
        radius = size / 2 - 52
        points = []
        for index in range(len(steps)):
            angle = -math.pi / 2 + index * (2 * math.pi / len(steps))
            points.append((cx + math.cos(angle) * radius,
                           cy + math.sin(angle) * radius))
        for index, (x, y) in enumerate(points):
            nx, ny = points[(index + 1) % len(points)]
            # Pull the arc ends back off the node circles so the arrowheads
            # land on the gap rather than inside the next box.
            angle = math.atan2(ny - y, nx - x)
            canvas.create_line(x + math.cos(angle) * 46, y + math.sin(angle) * 30,
                               nx - math.cos(angle) * 46, ny - math.sin(angle) * 30,
                               fill=accent, width=2, arrow="last", smooth=True)
        for index, (x, y) in enumerate(points):
            canvas.create_oval(x - 44, y - 30, x + 44, y + 30,
                               outline=accent, fill="#17202a", width=2)
            self._fit_text(canvas, x, y, steps[index], 80,
                           self._board_font("text", 14, "normal"), CHALK)
        if spec.get("title"):
            # The gap across the ring, not its radius: half the available room
            # was truncating titles that fit perfectly well.
            self._fit_text(canvas, cx, cy, str(spec["title"]), radius * 1.7,
                           self._board_font("display", 17, "bold"), CHALK_DIM)
        self._board_y = top + size + 24

    def _diagram_tree(self, canvas, spec, accent):
        """`nodes` as "root: a, b" or levels separated by |."""
        raw = str(spec.get("nodes") or spec.get("steps") or "")
        levels = [[s.strip() for s in level.split(",") if s.strip()]
                  for level in raw.split("|") if level.strip()]
        if not levels:
            return
        width = self._board_width()
        level_h, box_h = 108, 52
        self._board_scroll_if_needed(len(levels) * level_h + 30)
        top = self._board_y + 12
        placed = []
        for depth, row in enumerate(levels):
            y = top + depth * level_h
            slot = width / len(row)
            positions = []
            for index, label in enumerate(row):
                cx = self._x0() + slot * index + slot / 2
                box_w = min(slot - 22, 220)
                canvas.create_rectangle(cx - box_w / 2, y, cx + box_w / 2, y + box_h,
                                        outline=accent, fill="#17202a", width=2)
                self._fit_text(canvas, cx, y + box_h / 2, label, box_w - 14,
                               self._board_font("text", 15, "normal"), CHALK)
                positions.append((cx, y, y + box_h))
            if placed:
                for cx, y0, _ in positions:
                    # Join to the nearest parent: without a declared edge list
                    # this is the only honest reading of a level-by-level tree.
                    parent = min(placed[-1], key=lambda p: abs(p[0] - cx))
                    canvas.create_line(parent[0], parent[2], cx, y0,
                                       fill=ACCENT_RULE, width=2)
            placed.append(positions)
        self._board_y = top + len(levels) * level_h + 12

    def _diagram_timeline(self, canvas, spec, accent):
        events = self._items(spec, "events", "steps", "items")
        if not events:
            return
        height = 150
        self._board_scroll_if_needed(height + 20)
        top = self._board_y + 20
        width = self._board_width()
        line_y = top + 56
        canvas.create_line(self._x0(), line_y, self._x0() + width, line_y,
                           fill=accent, width=3, arrow="last")
        slot = width / max(1, len(events))
        for index, event in enumerate(events):
            x = self._x0() + slot * index + slot / 2
            canvas.create_oval(x - 7, line_y - 7, x + 7, line_y + 7,
                               fill=accent, outline=BOARD_BG, width=2)
            above = index % 2 == 0
            label, _, detail = event.partition(":")
            self._fit_text(canvas, x, line_y - 20 if above else line_y + 20,
                           label.strip(), slot - 10,
                           self._board_font("text", 15, "bold"), CHALK,
                           anchor="s" if above else "n")
            if detail.strip():
                self._fit_text(canvas, x, line_y - 42 if above else line_y + 42,
                               detail.strip(), slot - 10,
                               self._board_font("text", 13, "normal"), CHALK_DIM,
                               anchor="s" if above else "n")
        self._board_y = top + height

    def _diagram_compare(self, canvas, spec, accent):
        left_title = str(spec.get("left", "A"))
        right_title = str(spec.get("right", "B"))
        left_items = self._items(spec, "left_items", "left_points")
        right_items = self._items(spec, "right_items", "right_points")
        rows = max(len(left_items), len(right_items))
        header_h, row_h = 46, 34
        height = header_h + rows * row_h + 24
        self._board_scroll_if_needed(height + 20)
        top = self._board_y + 10
        width = self._board_width()
        mid = self._x0() + width / 2

        for index, (title, x0, x1) in enumerate(
                ((left_title, self._x0(), mid - 10), (right_title, mid + 10, self._x0() + width))):
            colour = accent if index == 0 else ACCENT_HEAD
            canvas.create_rectangle(x0, top, x1, top + header_h,
                                    outline=colour, fill="#17202a", width=2)
            self._fit_text(canvas, (x0 + x1) / 2, top + header_h / 2, title,
                           x1 - x0 - 16, self._board_font("display", 17, "bold"),
                           colour)
            items = left_items if index == 0 else right_items
            for row, item in enumerate(items):
                self._fit_text(canvas, x0 + 14, top + header_h + 12 + row * row_h,
                               "•  " + item, x1 - x0 - 24,
                               self._board_font("text", 15, "normal"), CHALK,
                               anchor="nw")
        self._board_y = top + height

    def _diagram_forces(self, canvas, spec, accent):
        """A free-body diagram: the workhorse of every mechanics lesson.

        `forces` reads as "name:direction[:magnitude]", direction being up,
        down, left, right or degrees anticlockwise from east.
        """
        forces = self._items(spec, "forces", "vectors")
        size = 330
        self._board_scroll_if_needed(size + 24)
        top = self._board_y + 12
        cx = self._x0() + self._board_width() / 2
        cy = top + size / 2
        body = str(spec.get("body", "m"))

        canvas.create_rectangle(cx - 42, cy - 32, cx + 42, cy + 32,
                                outline=CHALK, fill="#1d2731", width=2)
        self._fit_text(canvas, cx, cy, body, 74,
                       self._board_font("display", 19, "bold"), CHALK)

        named = {"up": 90, "down": 270, "left": 180, "right": 0,
                 "north": 90, "south": 270, "west": 180, "east": 0}
        for entry in forces:
            parts = [p.strip() for p in entry.split(":")]
            name = parts[0] if parts else "F"
            direction = parts[1].lower() if len(parts) > 1 else "up"
            try:
                length = float(parts[2]) if len(parts) > 2 else 1.0
            except ValueError:
                length = 1.0
            degrees = named.get(direction)
            if degrees is None:
                try:
                    degrees = float(direction)
                except ValueError:
                    degrees = 90.0
            radians = math.radians(degrees)
            reach = 56 + 74 * max(0.3, min(length, 2.0))
            dx, dy = math.cos(radians), -math.sin(radians)
            x0, y0 = cx + dx * 46, cy + dy * 36
            x1, y1 = cx + dx * reach, cy + dy * reach
            canvas.create_line(x0, y0, x1, y1, fill=accent, width=3, arrow="last",
                               arrowshape=(14, 17, 5))
            # Anchor the label away from the tip along the vector's own axis.
            # Centring it put every horizontal label straight over its arrowhead.
            if abs(dx) > abs(dy):
                anchor = "w" if dx > 0 else "e"
                label_x, label_y = x1 + dx * 14, y1
            else:
                anchor = "n" if dy > 0 else "s"
                label_x, label_y = x1, y1 + dy * 12
            self._fit_text(canvas, label_x, label_y, name, 190,
                           self._board_font("text", 15, "bold"), accent,
                           anchor=anchor)
        self._board_y = top + size + 12

    def _diagram_venn(self, canvas, spec, accent):
        left = str(spec.get("left", "A"))
        right = str(spec.get("right", "B"))
        overlap = str(spec.get("overlap", ""))
        height = 260
        self._board_scroll_if_needed(height + 20)
        top = self._board_y + 12
        cx = self._x0() + self._board_width() / 2
        cy = top + height / 2
        radius, offset = 104, 64
        canvas.create_oval(cx - offset - radius, cy - radius,
                           cx - offset + radius, cy + radius,
                           outline=accent, width=2)
        canvas.create_oval(cx + offset - radius, cy - radius,
                           cx + offset + radius, cy + radius,
                           outline=ACCENT_HEAD, width=2)
        self._fit_text(canvas, cx - offset - 40, cy, left, 130,
                       self._board_font("text", 15, "normal"), CHALK)
        self._fit_text(canvas, cx + offset + 40, cy, right, 130,
                       self._board_font("text", 15, "normal"), CHALK)
        if overlap:
            self._fit_text(canvas, cx, cy, overlap, 2 * (radius - offset) - 8,
                           self._board_font("text", 13, "bold"), ACCENT_CODE)
        self._board_y = top + height

    @staticmethod
    def _table_data(spec):
        """Return table headers and rows from JSON or the compact legacy form.

        JSON is the lossless representation (cells may contain commas).  The
        original ``A,B|C,D`` syntax remains accepted so old lessons replay.
        """
        def decode(value, rows=False):
            if isinstance(value, (list, tuple)):
                parsed = list(value)
            else:
                raw = str(value or "").strip()
                if not raw:
                    return []
                if raw[:1] in "[{":
                    try:
                        parsed = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, dict):
                        parsed = parsed.get("rows" if rows else "headers", [])
                    if isinstance(parsed, list):
                        return ([list(map(str, item)) if isinstance(item, (list, tuple))
                                 else [str(item)] for item in parsed]
                                if rows else [str(item) for item in parsed])
                parsed = None
                if rows:
                    return [[cell.strip() for cell in next(csv.reader([line]))]
                            for line in raw.split("|") if line.strip()]
                return [cell.strip() for cell in next(csv.reader([raw]))]
            return ([list(map(str, item)) if isinstance(item, (list, tuple))
                     else [str(item)] for item in parsed]
                    if rows else [str(item) for item in parsed])

        return decode(spec.get("headers") or spec.get("columns"), False), \
            decode(spec.get("rows"), True)

    def _draw_table(self, spec):
        """Render a real, replayable table rather than disguising it as a diagram."""
        canvas = self._ensure_board()
        accent = spec.get("color") or ACCENT_TITLE
        headers, rows = self._table_data(spec)
        if not headers and not rows:
            return
        columns = max([len(headers)] + [len(row) for row in rows] or [1])
        width = self._board_width()
        col_w = width / columns
        font = self._board_font("text", 15, "normal")
        header_font = self._board_font("text", 15, "bold")

        def height_for(row, header=False):
            active_font = header_font if header else font
            lines = max((len(self._board_wrap(
                row[index] if index < len(row) else "", active_font,
                max(40, col_w - 24))) for index in range(columns)), default=1)
            return max(42, min(104, lines * self._line_height(active_font) + 18))

        header_h = height_for(headers, True) if headers else 0
        row_heights = [height_for(row) for row in rows]
        title = str(spec.get("title") or "").strip()
        title_h = 42 if title else 0
        height = title_h + header_h + sum(row_heights) + 16
        self._board_scroll_if_needed(height + 20)
        top = self._board_y + 10
        y = top

        if title:
            canvas.create_text(
                self._x0(), y, text=title, fill=CHALK,
                font=self._board_font("display", 19, "bold"), anchor="nw")
            y += title_h

        if headers:
            canvas.create_rectangle(self._x0(), y, self._x0() + width, y + header_h,
                                    outline=accent, fill="#17202a", width=2)
            for index in range(columns):
                label = headers[index] if index < len(headers) else ""
                canvas.create_text(
                    self._x0() + col_w * index + 12, y + header_h / 2,
                    text=label, width=max(20, col_w - 24), justify="left",
                    font=header_font, fill=accent, anchor="w")
                if index:
                    x = self._x0() + col_w * index
                    canvas.create_line(x, y, x, y + header_h,
                                       fill=ACCENT_RULE, width=1)
            y += header_h

        for row_index, row in enumerate(rows):
            row_h = row_heights[row_index]
            if row_index % 2:
                canvas.create_rectangle(self._x0(), y, self._x0() + width, y + row_h,
                                        outline="", fill="#151b21")
            for index in range(columns):
                cell = row[index] if index < len(row) else ""
                canvas.create_text(
                    self._x0() + col_w * index + 12, y + row_h / 2,
                    text=cell, width=max(20, col_w - 24), justify="left",
                    font=font, fill=CHALK, anchor="w")
                if index:
                    x = self._x0() + col_w * index
                    canvas.create_line(x, y, x, y + row_h,
                                       fill=ACCENT_RULE, width=1)
            canvas.create_line(self._x0(), y + row_h, self._x0() + width, y + row_h,
                               fill=ACCENT_RULE, width=1)
            y += row_h
        self._board_y = y + 16

    def _diagram_table(self, canvas, spec, accent):
        # Backward compatibility for saved BOARD_DIAGRAM kind="table" pages.
        enriched = dict(spec)
        enriched.setdefault("color", accent)
        self._draw_table(enriched)

    def _diagram_network(self, canvas, spec, accent):
        try:
            layers = [max(1, min(9, int(n))) for n in
                      str(spec.get("layers", "3,4,2")).split(",") if n.strip()]
        except ValueError:
            layers = [3, 4, 2]
        labels = self._items(spec, "labels")
        radius, spacing = 20, 66
        height = (max(layers) - 1) * spacing + radius * 2 + 70
        self._board_scroll_if_needed(height + 16)
        top = self._board_y
        width = self._board_width()
        gap_x = width / max(1, len(layers) - 1) if len(layers) > 1 else width
        columns = []
        for index, count in enumerate(layers):
            x = self._x0() + (index * gap_x if len(layers) > 1 else width / 2)
            x = min(x, self._x0() + width - radius)
            column_height = (count - 1) * spacing
            y0 = top + 44 + ((max(layers) - 1) * spacing - column_height) / 2
            columns.append([(x, y0 + i * spacing) for i in range(count)])
            label = labels[index] if index < len(labels) else (
                "input" if index == 0 else
                "output" if index == len(layers) - 1 else f"hidden {index}")
            canvas.create_text(x, top + 10, text=label, fill=CHALK_DIM,
                               font=self._board_font("text", 14, "normal"),
                               anchor="n")
        for index in range(len(columns) - 1):
            for (x1, y1) in columns[index]:
                for (x2, y2) in columns[index + 1]:
                    canvas.create_line(x1 + radius, y1, x2 - radius, y2,
                                       fill=ACCENT_RULE, width=1)
        for column in columns:
            for (x, y) in column:
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                                   outline=accent, fill="#17202a", width=2)
        self._board_y = top + height

    # ------------------------------------------------------------ #
    # Session control
    # ------------------------------------------------------------ #
    # ------------------------------------------------------------ #
    # Animation
    #
    # Drawn straight onto the Tk canvas rather than as pre-rendered
    # matplotlib frames: rendering two dozen figures blocks the UI for
    # seconds and then plays back as a flipbook, while a canvas redraw is a
    # few hundred line items and runs at 30fps without touching the main
    # loop for longer than a frame.
    #
    # An animation belongs to a page and stops when the page is left, so
    # paging through a lesson never leaves a timer running behind it.
    # ------------------------------------------------------------ #
    # 20, not 30. Three independent redraw loops -- this one, the ambient
    # overlay and the UI poll -- were between them handing Tk more timer
    # events than it could service, and an event loop that never drains is a
    # pegged CPU and a frozen interface. Nothing here moves fast enough for
    # the difference to be visible.
    ANIMATION_FPS = 20

    def _stop_animation(self):
        job = getattr(self, "_anim_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._anim_job = None
        self._anim_state = {}

    def _draw_animation(self, spec):
        kind = str(spec.get("kind", "wave")).lower()
        builder = {"wave": self._anim_wave, "plot": self._anim_wave,
                   "flow": self._anim_flow, "pipeline": self._anim_flow,
                   "attention": self._anim_attention}.get(kind)
        if builder is None:
            self._draw_write({
                "style": "note",
                "text": f"(no animation kind '{kind}'; try wave, flow or attention)"})
            return
        height = int(spec.get("height", 330))
        # 30px of clearance, not 10: an animation that labels its own top edge
        # (the attention one names the current query there) was writing over
        # the last line of prose above it.
        self._board_scroll_if_needed(height + 50)
        top = self._board_y + 30
        self._board_y = top + height + 20

        caption = str(spec.get("caption", "") or "").strip()
        if caption:
            self._board_scroll_if_needed(28)
            self._board_canvas.create_text(
                self._x0() + self._board_width() / 2, self._board_y - 14,
                text=caption, fill=CHALK_DIM,
                font=self._board_font("text", 15, "normal"), anchor="n")
            self._board_y += 12

        # One animation per page: a second would fight the first for the tag
        # it redraws, and a page with two things moving teaches neither.
        self._stop_animation()
        self._anim_state = {"tick": 0.0, "top": top, "height": height,
                            "spec": dict(spec), "draw": builder}
        self._animate_tick()

    def _animate_tick(self):
        state = getattr(self, "_anim_state", None)
        if not state or not self._board_win or not self._board_win.winfo_exists():
            return
        try:
            state["draw"](state)
        except Exception as exc:
            print(f"[Board] Animation stopped: {exc}")
            self._stop_animation()
            return
        state["tick"] += 1.0 / self.ANIMATION_FPS
        self._anim_job = self.root.after(int(1000 / self.ANIMATION_FPS),
                                         self._animate_tick)

    def _anim_axes(self, state, tag):
        """Shared axes for the animated plot, drawn once per frame."""
        canvas = self._board_canvas
        canvas.delete(tag)
        width = self._board_width()
        left, right = self._x0() + 60, self._x0() + width - 20
        top = state["top"]
        bottom = top + state["height"] - 40
        middle = (top + bottom) / 2
        canvas.create_line(left, middle, right, middle, fill=CHALK_FAINT,
                           width=1, tags=tag)
        canvas.create_line(left, top, left, bottom, fill=CHALK_FAINT,
                           width=1, tags=tag)
        return left, right, top, bottom, middle

    def _anim_wave(self, state):
        """A function of x and t, redrawn as t advances.

        This is the one that makes a wave a wave: a still picture of sin(x - t)
        is a squiggle, and the same curve moving is obviously a travelling
        wave. Decay, convergence and interference all read the same way.
        """
        import numpy as np

        spec = state["spec"]
        tag = "anim-wave"
        left, right, top, bottom, middle = self._anim_axes(state, tag)
        canvas = self._board_canvas

        expressions = [e.strip() for e in
                       str(spec.get("expr", "sin(x - t)")).split(";") if e.strip()]
        labels = [s.strip() for s in str(spec.get("labels", "")).split(",")]
        try:
            x_min = float(spec.get("xmin", 0))
            x_max = float(spec.get("xmax", 12))
        except (TypeError, ValueError):
            x_min, x_max = 0.0, 12.0
        speed = float(spec.get("speed", 1.0) or 1.0)

        x = np.linspace(x_min, x_max, 260)
        namespace = self._plot_namespace()
        namespace["x"] = x
        namespace["t"] = state["tick"] * speed
        palette = (ACCENT_TITLE, ACCENT_HEAD, ACCENT_CODE, ACCENT_SUB)
        scale_y = (bottom - top) / 2 - 12
        scale_x = (right - left) / max(1e-9, x_max - x_min)

        for index, expression in enumerate(expressions):
            try:
                y = eval(expression, {"__builtins__": {}}, namespace)
            except Exception:
                continue
            y = np.broadcast_to(np.asarray(y, dtype=float), x.shape)
            limit = float(spec.get("ymax", 1.2) or 1.2)
            points = []
            for xi, yi in zip(x, np.clip(y, -limit, limit)):
                points.extend([left + (xi - x_min) * scale_x,
                               middle - (yi / limit) * scale_y])
            canvas.create_line(*points, fill=palette[index % len(palette)],
                               width=3, smooth=True, tags=tag)
            label = labels[index] if index < len(labels) and labels[index] else expression
            canvas.create_text(right - 8, top + 16 + index * 22, text=label,
                               fill=palette[index % len(palette)], anchor="e",
                               font=self._board_font("mono", 13, "normal"), tags=tag)

    def _anim_flow(self, state):
        """A pulse travelling through a pipeline, one stage at a time."""
        canvas = self._board_canvas
        tag = "anim-flow"
        canvas.delete(tag)
        spec = state["spec"]
        steps = [s.strip() for s in str(spec.get("steps", "")).split(",") if s.strip()]
        if not steps:
            return
        width = min(620, self._board_width())
        box_h, gap = 54, 26
        x1 = self._x0()
        x2 = self._x0() + width
        dwell = float(spec.get("dwell", 0.9) or 0.9)
        active = int(state["tick"] / dwell) % len(steps)

        for index, step in enumerate(steps):
            y1 = state["top"] + index * (box_h + gap)
            y2 = y1 + box_h
            live = index == active
            canvas.create_rectangle(
                x1, y1, x2, y2, width=3 if live else 2,
                outline=ACCENT_CODE if live else ACCENT_TITLE,
                fill="#1d2e28" if live else "#17202a", tags=tag)
            self._fit_text(canvas, (x1 + x2) / 2, (y1 + y2) / 2, step, width - 24,
                           self._board_font("text", 16, "bold" if live else "normal"),
                           CHALK if live else CHALK_DIM, tags=tag)
            if index < len(steps) - 1:
                canvas.create_line((x1 + x2) / 2, y2, (x1 + x2) / 2, y2 + gap,
                                   fill=ACCENT_TITLE, width=2, arrow="last", tags=tag)
        # The travelling dot, between the box it left and the one it enters.
        progress = (state["tick"] % dwell) / dwell
        if active < len(steps) - 1:
            y = state["top"] + active * (box_h + gap) + box_h + progress * gap
            canvas.create_oval((x1 + x2) / 2 - 6, y - 6, (x1 + x2) / 2 + 6, y + 6,
                               fill=ACCENT_CODE, outline="", tags=tag)

    def _anim_attention(self, state):
        """A query token sweeping a sentence, with its attention lighting up.

        Attention is the one idea in a transformer that a still diagram makes
        look like arbitrary wiring. Watching one token look at all the others,
        weighted, is the explanation.
        """
        import math as _math

        canvas = self._board_canvas
        tag = "anim-attn"
        canvas.delete(tag)
        spec = state["spec"]
        tokens = [t.strip() for t in str(spec.get("tokens", "")).split(",") if t.strip()]
        if len(tokens) < 2:
            return
        width = self._board_width()
        top = state["top"]
        bottom = top + state["height"] - 60
        slot = width / len(tokens)
        dwell = float(spec.get("dwell", 1.4) or 1.4)
        query = int(state["tick"] / dwell) % len(tokens)

        # Weights: a smooth, deterministic stand-in for a real attention row,
        # peaked on a couple of neighbours so it looks like language rather
        # than noise. Labelled as illustrative on the board, never as measured.
        weights = []
        for index in range(len(tokens)):
            distance = abs(index - query)
            value = _math.exp(-0.55 * distance) + 0.35 * _math.exp(
                -0.8 * abs(distance - 2))
            weights.append(value)
        total = sum(weights) or 1.0
        weights = [w / total for w in weights]

        for index, token in enumerate(tokens):
            x = self._x0() + slot * index + slot / 2
            if index != query:
                strength = weights[index] / max(weights)
                canvas.create_line(
                    self._x0() + slot * query + slot / 2, top + 30, x, bottom - 30,
                    fill=_blend_hex(ACCENT_RULE, ACCENT_CODE, strength),
                    width=max(1, 1 + 6 * strength), tags=tag)
        for index, token in enumerate(tokens):
            x = self._x0() + slot * index + slot / 2
            for y, is_query in ((top + 30, True), (bottom - 30, False)):
                live = is_query and index == query
                canvas.create_rectangle(
                    x - slot / 2 + 8, y - 18, x + slot / 2 - 8, y + 18,
                    outline=ACCENT_HEAD if live else ACCENT_RULE,
                    fill="#2a2416" if live else "#17202a",
                    width=2 if live else 1, tags=tag)
                self._fit_text(canvas, x, y, token, slot - 22,
                               self._board_font("text", 14,
                                                "bold" if live else "normal"),
                               CHALK if live else CHALK_DIM, tags=tag)
            if index != query:
                canvas.create_text(x, bottom + 4, text=f"{weights[index]:.2f}",
                                   fill=CHALK_FAINT, anchor="n", tags=tag,
                                   font=self._board_font("mono", 11, "normal"))
        canvas.create_text(self._x0(), top - 4, anchor="sw", tags=tag,
                           text=f'query: "{tokens[query]}"  \u2192  every key below',
                           fill=CHALK_DIM, font=self._board_font("text", 14, "normal"))

    # ------------------------------------------------------------ #
    # Pages: recording, rendering, navigation
    # ------------------------------------------------------------ #
    RENDERERS = {
        "write": "_draw_write", "math": "_draw_math", "plot": "_draw_plot",
        "chart": "_draw_chart", "diagram": "_draw_diagram", "table": "_draw_table",
        "rule": "_draw_rule",
        "animate": "_draw_animation", "plot3d": "_draw_plot3d",
        "matrix": "_draw_matrix", "field": "_draw_field",
        "contour": "_draw_contour", "capture": "_draw_capture",
    }

    def _current(self):
        return self._pages[self._page]

    def _safe_pager(self):
        """Refresh the pager, but never let its chrome break the lesson."""
        try:
            self._refresh_pager()
        except Exception as exc:
            print(f"[Board] Pager refresh failed: {exc}")
        self._publish_board_state()

    def _board_is_showing(self):
        """Open AND not put away -- F11 withdraws the window without losing it,
        and a withdrawn board is not something to keep narrating context about.
        """
        window = getattr(self, "_board_win", None)
        try:
            return bool(window and window.winfo_exists()
                        and window.state() != "withdrawn")
        except Exception:
            return False

    def _publish_board_state(self):
        """Mirror the lesson out to where the model can read it.

        Hung off the pager refresh because that is already the one call every
        mutation ends with -- _record, _board_section, _continue_page, and
        goto and edit_page through _render_page. A snapshot built anywhere
        narrower would go stale in whichever path was forgotten.
        """
        try:
            pages = getattr(self, "_pages", []) or []
            _set_board_state(
                open=self._board_is_showing(),
                page=int(getattr(self, "_page", 0)) + 1 if pages else 0,
                count=len(pages),
                outline=self._board_outline(),
                current=self._board_page_text()[:800])
        except Exception as exc:
            # A broken mirror must never take the lesson down with it.
            print(f"[Board] Could not publish board state: {exc}")

    def _record(self, op, spec):
        """Add one command to the open page and draw it straight away.

        Recording rather than only drawing is the whole change: a lesson used
        to be pixels on a scrolling canvas, so a question about slide three
        meant retyping slide three. Commands can be replayed, so a page can be
        revisited, corrected, or extended in place.
        """
        self._ensure_board()
        # Questions arrive while an earlier page is on screen. Appending to
        # whatever is showing is what the person means; appending to the last
        # page written would answer onto a page they are not looking at.
        item = dict(spec)
        item["op"] = op
        page = self._current()
        was_split = any(i["op"] in self.VISUAL_OPS for i in page["items"])
        page["items"].append(item)

        if op in self.VISUAL_OPS and not was_split:
            # The page was laid out full-width because it had no pictures. The
            # first one changes that, so the text already on it has to be
            # reflowed into the left column rather than drawn over.
            self._render_page(self._page)
        else:
            if op == "write" and spec.get("style") == "title":
                self._enter_column("full")
            else:
                self._enter_column(self._column_for(op, spec))
            getattr(self, self.RENDERERS[op])(spec)
        self._safe_pager()
        self._board_show()

    def _column_for(self, op, spec=None):
        """Which column a command belongs in on the page as it stands."""
        page = self._current()
        split = any(item["op"] in self.VISUAL_OPS for item in page["items"])
        if not split:
            return "full"
        # Comparisons need horizontal room and are easiest to scan beneath
        # the explanation, not squeezed into the visual half-column.
        if op == "table":
            return "full"
        side = str((spec or {}).get("side", "")).lower()
        if side in ("left", "right"):
            return side
        return "right" if op in self.VISUAL_OPS else "left"

    def _render_page(self, index=None):
        """Replay a page from its commands."""
        self._ensure_board()
        if index is not None:
            self._page = max(0, min(int(index), len(self._pages) - 1))
        self._stop_animation()
        canvas = self._board_canvas
        canvas.delete("all")
        self._board_images = []
        page = self._current()
        self._reset_columns(page)
        self._draw_escape_hint()
        # A title spans the page whatever the layout; splitting a heading
        # across a column boundary reads as two different headings.
        self._replaying = True
        try:
            for item in page["items"]:
                spec = {k: v for k, v in item.items() if k != "op"}
                if item["op"] == "write" and spec.get("style") == "title":
                    self._enter_column("full")
                else:
                    self._enter_column(self._column_for(item["op"], spec))
                try:
                    getattr(self, self.RENDERERS[item["op"]])(spec)
                except Exception as exc:
                    print(f"[Board] Could not replay {item['op']}: {exc}")
        finally:
            self._replaying = False
        self._safe_pager()
        self._board_show()

    def _board_section(self, spec):
        """Start a new page. The board turns over; nothing is lost."""
        self._ensure_board()
        title = str(spec.get("title", "") or spec.get("text", "") or "").strip()
        # An untouched first page is the one to fill, not to push past.
        if self._current()["items"] or len(self._pages) > 1:
            self._pages.append({"title": title, "items": []})
            self._page = len(self._pages) - 1
        else:
            self._current()["title"] = title
        self._stop_animation()
        self._board_canvas.delete("all")
        self._board_images = []
        self._reset_columns(self._current())
        self._draw_escape_hint()
        if title:
            self._record("write", {"style": "title", "text": title})
        self._safe_pager()
        self._board_show()

    def _board_write(self, spec):
        self._record("write", spec)

    def _board_math(self, spec):
        self._record("math", spec)

    def _board_plot(self, spec):
        self._record("plot", spec)

    def _board_chart(self, spec):
        self._record("chart", spec)

    def _board_diagram(self, spec):
        self._record("diagram", spec)

    def _board_table(self, spec):
        self._record("table", spec)

    def _board_rule(self, spec=None):
        self._record("rule", spec or {})

    def _board_animate(self, spec):
        self._record("animate", spec)

    def _board_plot3d(self, spec):
        self._record("plot3d", spec)

    def _board_matrix(self, spec):
        self._record("matrix", spec)

    def _board_field(self, spec):
        self._record("field", spec)

    def _board_contour(self, spec):
        self._record("contour", spec)

    def _board_goto(self, spec):
        wanted = spec.get("page", spec.get("index", 1))
        try:
            index = int(str(wanted).strip()) - 1     # people count from one
        except (TypeError, ValueError):
            index = self._page
        self._render_page(index)

    def _board_step(self, delta):
        if getattr(self, "_pages", None):
            self._render_page(self._page + delta)

    def _board_edit_page(self, spec):
        """Replace or extend one page, then show it.

        This is what a question during a lesson turns into: rather than
        answering into the void below, the page being looked at is rewritten
        to contain the answer.
        """
        self._ensure_board()
        target = spec.get("page")
        if target in (None, "", "current"):
            index = self._page
        else:
            try:
                index = int(str(target).strip()) - 1
            except (TypeError, ValueError):
                index = self._page
        index = max(0, min(index, len(self._pages) - 1))
        if str(spec.get("mode", "append")).lower().startswith("replace"):
            title = self._pages[index]["title"]
            self._pages[index]["items"] = []
            if title:
                self._pages[index]["items"].append(
                    {"op": "write", "style": "title", "text": title})
        self._page = index
        self._render_page(index)
        return index

    def _board_page_count(self):
        return len(getattr(self, "_pages", []) or [])

    def _board_outline(self):
        """The lesson so far, as a numbered list of page titles."""
        lines = []
        for number, page in enumerate(getattr(self, "_pages", []), start=1):
            here = "  <- showing" if number - 1 == self._page else ""
            lines.append(f"{number}. {page['title'] or '(untitled)'}{here}")
        return "\n".join(lines) or "The board is empty."

    def _board_page_text(self, index=None):
        """Everything written on a page, for the model to read back."""
        pages = getattr(self, "_pages", [])
        if not pages:
            return ""
        index = self._page if index is None else max(0, min(index, len(pages) - 1))
        parts = []
        for item in pages[index]["items"]:
            if item["op"] == "write":
                parts.append(str(item.get("text", "")))
            elif item["op"] == "math":
                parts.append(str(item.get("latex", item.get("text", ""))))
            elif item["op"] == "diagram":
                bits = [str(item.get(k, "")) for k in
                        ("steps", "items", "nodes", "events", "forces", "rows")]
                parts.append(" ".join(b for b in bits if b))
            elif item["op"] == "table":
                headers, rows = self._table_data(item)
                if headers:
                    parts.append(" | ".join(headers))
                parts.extend(" | ".join(row) for row in rows)
        return "\n".join(p for p in parts if p.strip())

    # ------------------------------------------------------------ #
    # The pager: a small window that is NOT click-through, because the
    # board itself is and a slider you cannot click is decoration
    # ------------------------------------------------------------ #
    def _build_pager(self):
        import customtkinter as ctk

        if getattr(self, "_pager_win", None) is not None and self._pager_win.winfo_exists():
            return
        win = ctk.CTkToplevel(self.root, fg_color="#1b2027")
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        width, height = 640, 54
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        win.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{screen_h - height - 46}")

        frame = ctk.CTkFrame(win, fg_color="#1b2027", corner_radius=14)
        frame.pack(fill="both", expand=True)

        self._pager_prev = ctk.CTkButton(
            frame, text="\u2039", width=36, height=30, corner_radius=8,
            fg_color="#2a313a", hover_color="#39424e", text_color=CHALK,
            font=(self.FONT_DISPLAY, 18), command=lambda: self._board_step(-1))
        self._pager_prev.pack(side="left", padx=(12, 6), pady=12)

        self._pager_slider = ctk.CTkSlider(
            frame, from_=1, to=1, number_of_steps=1, height=16,
            progress_color=ACCENT_TITLE, fg_color="#2a313a",
            button_color=CHALK, button_hover_color="#ffffff",
            command=lambda value: self._render_page(int(round(value)) - 1))
        self._pager_slider.pack(side="left", fill="x", expand=True, padx=6, pady=12)

        self._pager_next = ctk.CTkButton(
            frame, text="\u203a", width=36, height=30, corner_radius=8,
            fg_color="#2a313a", hover_color="#39424e", text_color=CHALK,
            font=(self.FONT_DISPLAY, 18), command=lambda: self._board_step(1))
        self._pager_next.pack(side="left", padx=6, pady=12)

        self._pager_label = ctk.CTkLabel(
            frame, text="1 / 1", width=64, text_color=CHALK_DIM,
            font=(self.FONT_MONO, 12))
        self._pager_label.pack(side="left", padx=(4, 8))

        self._pager_ask = ctk.CTkEntry(
            frame, width=190, height=30, corner_radius=8, border_width=0,
            fg_color="#11161c", text_color=CHALK,
            font=(self.FONT_TEXT, 12), placeholder_text="Ask about this page\u2026")
        self._pager_ask.pack(side="left", padx=(0, 6), pady=12)
        self._pager_ask.bind("<Return>", self._ask_about_page)

        ctk.CTkButton(
            frame, text="\u2715", width=32, height=30, corner_radius=8,
            fg_color="#2a313a", hover_color="#a33", text_color=CHALK_DIM,
            font=(self.FONT_TEXT, 12), command=self._board_hide).pack(
                side="left", padx=(0, 12), pady=12)

        # Arrow keys only reach a window that can take focus, which the board
        # itself cannot: it is click-through by design.
        for key, delta in (("<Left>", -1), ("<Right>", 1),
                           ("<Prior>", -1), ("<Next>", 1)):
            win.bind(key, lambda _e, d=delta: self._board_step(d))
        win.bind("<Home>", lambda _e: self._render_page(0))
        win.bind("<End>", lambda _e: self._render_page(len(self._pages) - 1))
        win.bind("<Escape>", lambda _e: self._board_hide())

        self._pager_win = win

    def _refresh_pager(self):
        win = getattr(self, "_pager_win", None)
        if win is None or not win.winfo_exists():
            return
        total = max(1, len(self._pages))
        # CTkSlider computes its step as (to - from) / number_of_steps, so a
        # single-page lesson gives it a range of zero and it divides by zero
        # the moment the value is set. Every lesson starts on one page, so
        # this fired on the first BOARD_SECTION of every lesson.
        if total > 1:
            self._pager_slider.configure(to=total, number_of_steps=total - 1)
            self._pager_slider.set(self._page + 1)
            self._pager_slider.configure(state="normal")
        else:
            self._pager_slider.configure(to=2, number_of_steps=1, state="disabled")
        self._pager_label.configure(text=f"{self._page + 1} / {total}")
        self._pager_prev.configure(state="normal" if self._page else "disabled")
        self._pager_next.configure(
            state="normal" if self._page < total - 1 else "disabled")
        win.deiconify()
        win.lift()

    def _ask_about_page(self, _event=None):
        """Send a question with the page it is about, so the answer lands there."""
        question = self._pager_ask.get().strip()
        self._pager_ask.delete(0, "end")
        if not question:
            return
        page = self._page + 1
        context = self._board_page_text()[:600]
        self._in_q.put((
            "text",
            f"While teaching, on board page {page} of {len(self._pages)} which "
            f"currently says: {context!r} -- Himanshu asks: {question}. Answer by "
            f"editing that page with BOARD_EDIT_PAGE page=\"{page}\", not by "
            "starting a new lesson."))

    def _board_toggle(self):
        """F11: put the lesson away, or bring it back exactly as it was.

        Hiding must not destroy the pages. Being able to glance at the desktop
        and return to page 7 is the whole point of a toggle; a toggle that
        wiped the board would just be a slower way to lose the lesson.
        """
        window = getattr(self, "_board_win", None)
        if window is not None and window.winfo_exists() and window.state() == "normal":
            self._board_conceal()
            return "hidden"
        if not getattr(self, "_pages", None) or not any(
                page["items"] for page in self._pages):
            return "empty"
        self._ensure_board()
        self._render_page(self._page)
        return "shown"

    def _board_conceal(self):
        """Put the board away without losing anything on it."""
        self._stop_animation()
        for name in ("_board_win", "_pager_win"):
            window = getattr(self, name, None)
            if window is not None and window.winfo_exists():
                window.withdraw()

    def _board_hide(self):
        if getattr(self, "_board_win", None) is not None and self._board_win.winfo_exists():
            self._board_canvas.delete("all")
            self._board_images = []
            self._board_y = self._x0()
            self._board_win.withdraw()
            self._publish_board_state()
