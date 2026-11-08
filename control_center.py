# -*- coding: utf-8 -*-
"""
control_center.py -- VANTA's dashboard, as a mixin onto WinterUI.

This is the surface Himanshu actually drives the machine from, so it is built
like an application rather than a debug panel: a navigation rail with the mark
at the top, one page per job, live telemetry, and hardware controls wired
straight to Windows.

Three decisions shape everything here.

*Controls act immediately.* Every button used to post English at the agent
("turn the volume down") and wait for a planner round-trip. Sliders now talk to
`system_controls`, which talks to the OS, so the volume moves while the hand is
still moving. The agent is for language; a dashboard is for verbs it already
knows.

*Slow work never touches the Tk thread.* Reading brightness costs a second and
Bluetooth costs two, because both shell out to PowerShell. A background
telemetry thread polls them and pushes results through the UI queue; the fast
psutil snapshot rides the same channel. A blocked main loop freezes the orb,
the overlays and the hotkeys along with the window.

*Irreversible actions arm before they fire.* Shutting down, signing out, ending
a process and emptying the Recycle Bin all destroy unsaved work, so their
buttons turn into an explicit confirmation for a few seconds rather than doing
it on the first click. Everything reversible just happens.
"""

import os
import threading
import time
import tkinter as tk

import customtkinter as ctk

import theme
import winter_mark
import window_identity
from theme import (BG_CARD, BG_CARD_HOVER, BG_CONTROL, BG_INSET, BG_RAIL,
                   BG_WINDOW, BLUE, GREEN, LABEL_PRIMARY, LABEL_SECONDARY,
                   LABEL_TERTIARY, RED, SEPARATOR, ThemedFonts)

# Page order in the rail, grouped the way they are actually reached for.
# Eight pages under three headings was more structure than there was content
# to put in it, and three of them -- Graph, Workspace, Modes -- were a click
# away from things that belong beside what they describe. Modes in particular
# is now a two-way switch, which does not need a page of its own.
# One flat list instead of eight pages filed under three headings. The
# headings were categorising a list short enough to read at a glance, and
# Modes is gone entirely: with two modes it is a switch, which belongs on
# Home beside the thing it changes, not on a page of its own.
# What is in the rail is what gets used daily. Graph and Workspace are still
# built and still open on request -- CONTROL_CENTER page="Graph" works, and
# asking for it aloud is a shorter path than a sidebar entry you scan past
# every time. Workspace also overlapped Files, which is two doors to one room.
NAV_SECTIONS = (
    ("", ("Home", "Control", "Chat", "Output", "Code", "Files", "Brain")),
)

# Reading order. Anything the policy table knows about but this list forgets
# is appended rather than dropped, so a new mode can never become unreachable
# from the window -- which is what happened to tutor when the list went stale.
_MODE_ORDER = ("assistant", "study", "research", "build", "health", "sleep")


def _known_modes():
    try:
        import mode_manager
        available = [mode.value for mode in mode_manager.POLICIES]
    except Exception:
        return _MODE_ORDER
    ordered = [name for name in _MODE_ORDER if name in available]
    return tuple(ordered + sorted(set(available) - set(ordered)))


MODE_NAMES = _known_modes()

# Diagnostic transport is useful in the terminal but it is not an event in
# Himanshu's day.  Keeping this boundary in one place prevents a new worker
# from turning a heartbeat, token counter, or retrieval count into a row in
# the dashboard merely because it reused the generic ``info`` channel.
_INTERNAL_ACTIVITY_PREFIXES = (
    "memory grounded:", "[memory grounding]", "[memory audit]",
    "[router]", "[tokens]", "[thinking", "[agent step",
    "[proactive director]", "[inference]", "[wakeword]", "[stt]",
    "[audio]", "[ui]",
)


def user_activity_text(value):
    """Return a compact user-facing activity line, or ``""`` for telemetry."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    folded = text.casefold()
    if folded.startswith(_INTERNAL_ACTIVITY_PREFIXES):
        return ""
    return text

# Named rather than inline so a test can prove every rail entry has a builder
# and every builder has a rail entry. A page reachable from neither is dead
# code; a rail entry with no builder is a crash on open.
PAGE_BUILDERS = {
    "Home": "_cc_page_home", "Control": "_cc_page_control",
    "Chat": "_cc_page_chat", "Output": "_cc_page_output",
    "Code": "_cc_page_code",
    "Files": "_cc_page_files", "Brain": "_cc_page_graph", "Graph": "_cc_page_graph",
    "Workspace": "_cc_page_workspace", "Activity": "_cc_page_activity",
    "Map": "_cc_page_map",
}

MODE_BLURBS = {
    "assistant": "Everyday help, planning, communication, and bounded initiative.",
    "study": "Quiet focus with screen-aware explanations when you ask.",
    "research": "Source-backed Tavily research that grows the knowledge graph.",
    "build": "Coding, debugging, project work, and verified implementation.",
    "health": "Calm, evidence-aware wellbeing support without overreacting.",
    "sleep": "Stay quiet, hold mail and notifications, then brief you on return.",
}

ICON_PREVIOUS = '\ue892'
ICON_PLAY_PAUSE = '\ue769'
ICON_NEXT = '\ue893'

QUICK_LAUNCH = (
    ("Files", "explorer.exe"),
    ("Settings", "ms-settings:"),
    ("Task Manager", "taskmgr.exe"),
    ("Terminal", "wt.exe"),
    ("Browser", "https://www.google.com"),
    ("Calculator", "calc.exe"),
)


class ControlCenterMixin(ThemedFonts):
    """Everything behind the F8 window. Mixed into WinterUI."""

    # ------------------------------------------------------------ #
    # Window chrome
    # ------------------------------------------------------------ #
    def _build_dashboard_window(self):
        width = min(1220, self.SW - 80)
        height = min(780, self.SH - 100)
        x = max(20, (self.SW - width) // 2)
        y = max(20, (self.SH - height) // 2)

        self.dashboard_win = ctk.CTkToplevel(self.root, fg_color=BG_WINDOW)
        self.dashboard_win.withdraw()
        self.dashboard_win.geometry(f"{width}x{height}+{x}+{y}")
        self.dashboard_win.overrideredirect(True)
        self.dashboard_win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(self.dashboard_win, self.root)
        self.dashboard_win.bind("<Escape>", lambda _e: self._hide_dashboard())

        self._cc_pages = {}
        self._cc_current_page = None
        self._cc_dragging_slider = False
        self._cc_confirmations = {}
        self._cc_stat = {}

        self._cc_shell = ctk.CTkFrame(
            self.dashboard_win, fg_color=BG_WINDOW, corner_radius=22,
            border_width=1, border_color=SEPARATOR)
        self._cc_shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        body = ctk.CTkFrame(self._cc_shell, fg_color="transparent",
                            corner_radius=21)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._cc_build_rail(body)

        main = ctk.CTkFrame(body, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self._cc_build_topbar(main)

        self._cc_page_host = ctk.CTkFrame(main, fg_color="transparent")
        self._cc_page_host.grid(row=1, column=0, sticky="nsew",
                                padx=(4, 22), pady=(0, 20))
        self._cc_page_host.grid_rowconfigure(0, weight=1)
        self._cc_page_host.grid_columnconfigure(0, weight=1)

        for _section, names in NAV_SECTIONS:
            for name in names:
                page = ctk.CTkFrame(self._cc_page_host, fg_color="transparent")
                page.grid(row=0, column=0, sticky="nsew")
                self._cc_pages[name] = page
                getattr(self, PAGE_BUILDERS[name])(page)

        self.dashboard_visible = False
        self.dashboard_win.withdraw()
        self._show_page("Home")

        self._cc_apply_window_style()
        self.root.after(400, self._cc_apply_icon)
        self.root.after(700, self._refresh_files)
        self.root.after(900, self._refresh_graph)
        self._cc_start_telemetry()

    def _cc_apply_window_style(self):
        """Windows 11 rounded corners and a hairline border.

        An overrideredirect window has no frame, so without this it is a hard
        rectangle with no shadow -- the single thing that most makes a custom
        window look homemade next to real system UI.
        """
        try:
            import ctypes
            from ctypes import byref, c_int, sizeof

            self.dashboard_win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.dashboard_win.winfo_id())
            dwm = ctypes.windll.dwmapi
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            dwm.DwmSetWindowAttribute(hwnd, 33, byref(c_int(2)), sizeof(c_int))
            # DWMWA_BORDER_COLOR = 34, as 0x00BBGGRR
            dwm.DwmSetWindowAttribute(hwnd, 34, byref(c_int(0x003A3A3A)), sizeof(c_int))
        except Exception:
            pass  # Pre-22H2 Windows simply keeps square corners.

    def _cc_apply_icon(self):
        """Give every native VANTA surface the idle orb, never Python."""
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            window_identity.apply_to(
                self.root,
                getattr(self, "info_win", None),
                getattr(self, "dashboard_win", None),
                getattr(self, "input_win", None),
                base_dir=base,
            )
        except Exception:
            pass

    # ------------------------------------------------------------ #
    # Navigation rail
    # ------------------------------------------------------------ #
    def _cc_build_rail(self, parent):
        rail = ctk.CTkFrame(parent, fg_color=BG_RAIL, corner_radius=20, width=204)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_propagate(False)
        rail.bind("<ButtonPress-1>", self._start_dashboard_drag)
        rail.bind("<B1-Motion>", self._drag_dashboard)

        brand = ctk.CTkFrame(rail, fg_color="transparent")
        brand.pack(fill=tk.X, padx=18, pady=(22, 18))
        mark = tk.Canvas(brand, width=36, height=36, bg=BG_RAIL,
                         highlightthickness=0, bd=0)
        mark.pack(side=tk.LEFT)
        winter_mark.draw_on_canvas(mark, 18, 18, 16, color=LABEL_PRIMARY)
        titles = ctk.CTkFrame(brand, fg_color="transparent")
        titles.pack(side=tk.LEFT, padx=(11, 0))
        ctk.CTkLabel(titles, text="VANTA", font=self.font("title"),
                     text_color=LABEL_PRIMARY, anchor="w").pack(anchor="w")
        ctk.CTkLabel(titles, text="Control Center", font=self.font("caption"),
                     text_color=LABEL_TERTIARY, anchor="w").pack(anchor="w")

        self._cc_nav_buttons = {}
        for section, names in NAV_SECTIONS:
            ctk.CTkLabel(rail, text=section.upper(), font=self.font("heading"),
                         text_color=LABEL_TERTIARY, anchor="w").pack(
                             fill=tk.X, padx=22, pady=(12, 4))
            for name in names:
                button = ctk.CTkButton(
                    rail, text=name, height=34, corner_radius=8, anchor="w",
                    font=self.font("body"), fg_color="transparent",
                    hover_color=BG_CARD_HOVER, text_color=LABEL_SECONDARY,
                    command=lambda n=name: self._show_page(n),
                )
                button.pack(fill=tk.X, padx=12, pady=1)
                self._cc_nav_buttons[name] = button

        footer = ctk.CTkFrame(rail, fg_color="transparent")
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=16)
        state_row = ctk.CTkFrame(footer, fg_color="transparent")
        state_row.pack(fill=tk.X)
        self._cc_state_dot = tk.Canvas(state_row, width=9, height=9, bg=BG_RAIL,
                                       highlightthickness=0, bd=0)
        self._cc_state_dot.pack(side=tk.LEFT, pady=(4, 0))
        self._cc_state_dot_id = self._cc_state_dot.create_oval(
            1, 1, 8, 8, fill=theme.ACCENTS[theme.STATE_IDLE], outline="")
        self.control_status = ctk.CTkLabel(
            state_row, text="Ready", font=self.font("label"),
            text_color=LABEL_SECONDARY, anchor="w")
        self.control_status.pack(side=tk.LEFT, padx=(8, 0))

        # A full-width saturated pill for the mode was the loudest thing in
        # the window and sat at the bottom where nothing loud belongs. It is
        # a chip now: sized to its word, tinted rather than filled.
        self.mode_label = ctk.CTkLabel(
            footer, text="ASSISTANT", height=22, corner_radius=11,
            fg_color=theme.blend(BLUE, BG_RAIL, 0.84), text_color=BLUE,
            font=self.font("heading"), padx=10)
        self.mode_label.pack(anchor="w", pady=(9, 0))
        self._cc_uptime = ctk.CTkLabel(footer, text="", font=self.font("caption"),
                                       text_color=LABEL_TERTIARY, anchor="w")
        self._cc_uptime.pack(fill=tk.X, pady=(8, 0))

    def _cc_build_topbar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent", height=64)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.bind("<ButtonPress-1>", self._start_dashboard_drag)
        bar.bind("<B1-Motion>", self._drag_dashboard)
        self.dashboard_header = bar

        self._cc_title = ctk.CTkLabel(bar, text="Home", font=self.font("display"),
                                      text_color=LABEL_PRIMARY, anchor="w")
        self._cc_title.pack(side=tk.LEFT, padx=(26, 0), pady=(18, 10))
        self._cc_title.bind("<ButtonPress-1>", self._start_dashboard_drag)
        self._cc_title.bind("<B1-Motion>", self._drag_dashboard)

        ctk.CTkButton(bar, text="✕", width=30, height=30, corner_radius=15,
                      fg_color=BG_CONTROL, hover_color=RED,
                      text_color=LABEL_SECONDARY, font=self.font("label"),
                      command=self._hide_dashboard).pack(
                          side=tk.RIGHT, padx=(10, 22), pady=(18, 10))

        # Live chips: the three facts worth a glance without changing page.
        self._cc_chips = {}
        for key in ("clock", "battery", "network"):
            chip = ctk.CTkLabel(bar, text="", font=self.font("mono"),
                                text_color=LABEL_SECONDARY)
            chip.pack(side=tk.RIGHT, padx=8, pady=(18, 10))
            self._cc_chips[key] = chip

    def _show_page(self, name):
        # Graph remains a spoken/API alias; Brain is the human-facing name.
        if name == "Graph":
            name = "Brain"
        page = self._cc_pages.get(name)
        # Pages outside the short rail are genuinely lazy, not merely hidden
        # declarations. This also keeps startup fast as more tools are added.
        if page is None and name in PAGE_BUILDERS:
            page = ctk.CTkFrame(self._cc_page_host, fg_color="transparent")
            page.grid(row=0, column=0, sticky="nsew")
            self._cc_pages[name] = page
            getattr(self, PAGE_BUILDERS[name])(page)
        if page is None:
            return
        page.tkraise()
        self._cc_current_page = name
        self._cc_title.configure(text=name)
        for key, button in self._cc_nav_buttons.items():
            selected = key == name
            button.configure(
                fg_color=BG_CARD if selected else "transparent",
                text_color=LABEL_PRIMARY if selected else LABEL_SECONDARY,
                font=self.font("body_em" if selected else "body"))

    # ------------------------------------------------------------ #
    # Reusable pieces
    # ------------------------------------------------------------ #
    def _cc_card(self, parent, title=None, subtitle=None, **grid):
        card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=14)
        if grid:
            card.grid(**grid)
        if title:
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.pack(fill=tk.X, padx=18, pady=(15, 0))
            ctk.CTkLabel(head, text=title, font=self.font("card"),
                         text_color=LABEL_PRIMARY, anchor="w").pack(anchor="w")
            if subtitle:
                ctk.CTkLabel(head, text=subtitle, font=self.font("caption"),
                             text_color=LABEL_TERTIARY, anchor="w",
                             justify="left").pack(anchor="w", pady=(2, 0))
        return card

    def _cc_stat_tile(self, parent, key, label, column):
        """A big number, its unit, and a hairline bar. Nothing else."""
        tile = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=14)
        tile.grid(row=0, column=column, sticky="nsew", padx=5)
        ctk.CTkLabel(tile, text=label.upper(), font=self.font("heading"),
                     text_color=LABEL_TERTIARY, anchor="w").pack(
                         anchor="w", padx=16, pady=(14, 0))
        value = ctk.CTkLabel(tile, text="--", font=self.font("metric"),
                             text_color=LABEL_PRIMARY, anchor="w")
        value.pack(anchor="w", padx=16, pady=(2, 0))
        detail = ctk.CTkLabel(tile, text="", font=self.font("caption"),
                              text_color=LABEL_TERTIARY, anchor="w")
        detail.pack(anchor="w", padx=16)
        bar = ctk.CTkProgressBar(tile, height=4, corner_radius=2,
                                 progress_color=BLUE, fg_color=BG_INSET)
        bar.set(0)
        bar.pack(fill=tk.X, padx=16, pady=(10, 16))
        self._cc_stat[key] = (value, detail, bar)
        return tile

    def _cc_set_stat(self, key, value, detail="", ratio=None, color=None):
        widgets = self._cc_stat.get(key)
        if not widgets:
            return
        label, detail_label, bar = widgets
        label.configure(text=value)
        detail_label.configure(text=detail)
        if ratio is not None:
            bar.set(max(0.0, min(1.0, ratio)))
            bar.configure(progress_color=color or theme.load_ratio_color(ratio))

    def _cc_action_button(self, parent, text, command, width=0, danger=False,
                          primary=False, **pack):
        colors = {
            "fg_color": BLUE if primary else BG_CONTROL,
            "hover_color": theme.blend(BLUE, "#ffffff", 0.15) if primary
            else BG_CARD_HOVER,
            "text_color": "#ffffff" if primary else LABEL_PRIMARY,
        }
        if danger:
            colors = {"fg_color": BG_CONTROL, "hover_color": RED,
                      "text_color": LABEL_PRIMARY}
        button = ctk.CTkButton(parent, text=text, height=32, corner_radius=8,
                               font=self.font("label"), command=command,
                               **({"width": width} if width else {}), **colors)
        if pack:
            button.pack(**pack)
        return button

    def _cc_confirm(self, button, prompt, action):
        """Arm an irreversible action instead of firing it on first click.

        A modal would be more conventional, but this window has no frame and
        no parent to be modal against; turning the button itself into the
        question keeps the decision where the click already is.
        """
        token = str(button)
        pending = self._cc_confirmations.get(token)
        if pending:
            after_id, original = pending
            self.root.after_cancel(after_id)
            del self._cc_confirmations[token]
            button.configure(text=original, fg_color=BG_CONTROL,
                             text_color=LABEL_PRIMARY)
            action()
            return

        original = button.cget("text")

        def disarm():
            self._cc_confirmations.pop(token, None)
            try:
                button.configure(text=original, fg_color=BG_CONTROL,
                                 text_color=LABEL_PRIMARY)
            except Exception:
                pass

        after_id = self.root.after(4000, disarm)
        self._cc_confirmations[token] = (after_id, original)
        button.configure(text=prompt, fg_color=RED, text_color="#ffffff")

    def _run_control(self, function, *args, **kwargs):
        """Run a machine control off the Tk thread and report the outcome."""
        def worker():
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                result = f"[Error] {exc}"
            try:
                self._q.put(("control_result", str(result)))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True,
                         name="vanta-control").start()

    # ------------------------------------------------------------ #
    # HOME
    # ------------------------------------------------------------ #
    def _cc_mode_switch(self, parent):
        """The mode control, as a row of two.

        This used to be a page of thirteen cards in a scrolling grid. With two
        modes it is a switch, and a switch belongs next to the state it
        changes rather than one click away behind its own heading.
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(row, text="MODE", font=self.font("label"),
                     text_color=LABEL_TERTIARY, anchor="w").pack(
                         side=tk.LEFT, padx=(9, 12))

        self._cc_mode_cards = {}
        for mode in MODE_NAMES:
            button = self._cc_action_button(
                row, mode.capitalize(),
                lambda m=mode: self._send_control_command(f"{m} mode"))
            button.pack(side=tk.LEFT, padx=(0, 8))
            self._cc_mode_cards[mode] = button

        blurb = ctk.CTkLabel(
            row, text=MODE_BLURBS.get(MODE_NAMES[0], ""),
            font=self.font("caption"), text_color=LABEL_TERTIARY, anchor="w")
        blurb.pack(side=tk.LEFT, padx=(6, 0))
        self._cc_mode_blurb = blurb
        return row

    def _cc_page_home(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        self._cc_mode_switch(page).grid(row=0, column=0, sticky="ew",
                                        pady=(0, 10))

        tiles = ctk.CTkFrame(page, fg_color="transparent")
        tiles.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for column in range(4):
            tiles.grid_columnconfigure(column, weight=1)
        for column, (key, label) in enumerate(
                (("cpu", "Processor"), ("ram", "Memory"),
                 ("disk", "Storage"), ("battery", "Battery"))):
            self._cc_stat_tile(tiles, key, label, column)

        lower = ctk.CTkFrame(page, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="nsew")
        lower.grid_columnconfigure(0, weight=3)
        lower.grid_columnconfigure(1, weight=4)
        lower.grid_rowconfigure(0, weight=3)
        lower.grid_rowconfigure(1, weight=2)

        context = self._cc_card(lower, "Context", "What VANTA is working from",
                                row=0, column=0, sticky="nsew", padx=(5, 6),
                                pady=(0, 6))
        self.dash_text = ctk.CTkTextbox(
            context, fg_color=BG_INSET, text_color=LABEL_SECONDARY,
            font=self.font("body"), wrap="word", corner_radius=10,
            border_width=0)
        self.dash_text.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        self.dash_text.insert("0.0", "Waiting for live context…")
        self.dash_text.configure(state="disabled")

        # The lower-left used to be one tall card with two lines in it. A
        # second card gives the recent trail somewhere to live on the page
        # you actually leave open, instead of only under the Activity tab.
        recent = self._cc_card(lower, "Recent", row=1, column=0, sticky="nsew",
                               padx=(5, 6), pady=(6, 0))
        self._cc_recent = ctk.CTkTextbox(
            recent, fg_color=BG_INSET, text_color=LABEL_TERTIARY,
            font=self.font("mono"), wrap="word", corner_radius=10,
            border_width=0)
        self._cc_recent.pack(fill=tk.BOTH, expand=True, padx=14, pady=(10, 14))
        self._cc_recent.insert("0.0", "Nothing yet this session.")
        self._cc_recent.configure(state="disabled")

        answer = self._cc_card(lower, "Latest answer", None,
                               row=0, column=1, sticky="nsew", padx=(6, 5),
                               pady=(0, 6))
        self.response_text = ctk.CTkTextbox(
            answer, fg_color=BG_INSET, text_color=LABEL_PRIMARY,
            font=self.font("body"), wrap="word", corner_radius=10,
            border_width=0)
        self.response_text.pack(fill=tk.BOTH, expand=True, padx=14, pady=(10, 14))
        self.response_text.insert("0.0", "Ready.")
        self.response_text.configure(state="disabled")

        # The fourth cell was 600px of empty inset. The landing page is the
        # one that stays open, so it earns the handful of things reached for
        # most -- four errands for the agent, four verbs for the machine.
        import system_controls

        actions = self._cc_card(lower, "Quick actions", row=1, column=1,
                                sticky="nsew", padx=(6, 5), pady=(6, 0))
        grid = ctk.CTkFrame(actions, fg_color="transparent")
        grid.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 14))
        for column in range(4):
            grid.grid_columnconfigure(column, weight=1)

        errands = (("Plan my day", "plan my day"),
                   ("Unread mail", "read my unread emails"),
                   ("Today", "what is on my calendar today"),
                   ("Approve", "approve that"))
        for index, (label, command) in enumerate(errands):
            self._cc_action_button(
                grid, label, lambda c=command: self._send_control_command(c)
            ).grid(row=0, column=index, sticky="ew", padx=3, pady=3)

        verbs = (
            (("Mute", self._cc_toggle_mute),
             ("Lock", lambda: self._run_control(system_controls.lock)),
             ("Sleep", lambda: self._run_control(system_controls.sleep)),
             ("Files", lambda: self._cc_launch("explorer.exe"))),
            (("Screenshot", self._cc_screenshot),
             ("Volume −", lambda: self._cc_nudge_volume(-10)),
             ("Volume +", lambda: self._cc_nudge_volume(+10)),
             ("Task Manager", lambda: self._cc_launch("taskmgr.exe"))),
        )
        for row_index, row_verbs in enumerate(verbs, start=1):
            for index, (label, function) in enumerate(row_verbs):
                self._cc_action_button(grid, label, function).grid(
                    row=row_index, column=index, sticky="ew", padx=3, pady=3)

    def _cc_nudge_volume(self, delta):
        import system_controls

        level, _ = system_controls.get_volume()
        if level is None:
            self._q.put(("control_result", "[Error] No audio endpoint."))
            return
        self._q.put(("control_result",
                     system_controls.set_volume(level + delta)))
        self._cc_refresh_audio()

    def _cc_screenshot(self):
        def capture():
            import system_tools
            return system_tools.take_screenshot(
                os.path.dirname(os.path.abspath(__file__)))
        self._run_control(capture)

    # ------------------------------------------------------------ #
    # CONTROL -- the machine itself
    # ------------------------------------------------------------ #
    def _cc_page_control(self, page):
        scroll = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=BG_CARD_HOVER,
            scrollbar_button_hover_color=SEPARATOR)
        scroll.pack(fill=tk.BOTH, expand=True)
        scroll.grid_columnconfigure(0, weight=1)
        scroll.grid_columnconfigure(1, weight=1)

        # "new" rather than "nsew": a card that stretches to fill its row
        # leaves a void under whichever of the pair has less to say.
        self._cc_card_audio(scroll, row=0, column=0)
        self._cc_card_display(scroll, row=0, column=1)
        self._cc_card_network(scroll, row=1, column=0)
        self._cc_card_power(scroll, row=1, column=1)
        self._cc_card_launch(scroll, row=2, column=0)
        self._cc_card_processes(scroll, row=2, column=1)

    def _cc_slider_row(self, card, label_text, command, release=None):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill=tk.X, padx=18, pady=(12, 4))
        ctk.CTkLabel(row, text=label_text, font=self.font("label"),
                     text_color=LABEL_SECONDARY, anchor="w").pack(side=tk.LEFT)
        value = ctk.CTkLabel(row, text="--", font=self.font("mono"),
                             text_color=LABEL_PRIMARY, anchor="e")
        value.pack(side=tk.RIGHT)

        slider = ctk.CTkSlider(
            card, from_=0, to=100, number_of_steps=100, height=18,
            progress_color=BLUE, fg_color=BG_INSET, button_color=LABEL_PRIMARY,
            button_hover_color="#ffffff", command=command)
        slider.pack(fill=tk.X, padx=18, pady=(0, 14))
        # Programmatic updates must not fight a hand on the slider.
        slider.bind("<ButtonPress-1>",
                    lambda _e: setattr(self, "_cc_dragging_slider", True))
        slider.bind("<ButtonRelease-1>", release or
                    (lambda _e: setattr(self, "_cc_dragging_slider", False)))
        return slider, value

    def _cc_icon_font(self):
        """Windows' own icon font, so transport glyphs are not emoji.

        The Unicode media characters exist, but Segoe UI has no outlines for
        them, so they fall through to the emoji font and arrive as coloured
        boxes. Segoe Fluent Icons ships with Windows 11 and MDL2 with 10.
        """
        cached = getattr(self, "_cc_icon_font_cache", None)
        if cached is not None:
            return cached
        import tkinter.font as tkfont
        try:
            families = set(tkfont.families())
        except Exception:
            families = set()
        for candidate in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
            if candidate in families:
                cached = ctk.CTkFont(family=candidate, size=13)
                break
        else:
            cached = self.font("label")
        self._cc_icon_font_cache = cached
        return cached

    def _cc_transport_glyphs(self):
        """Previous / play-pause / next, in whichever font we resolved."""
        import tkinter.font as tkfont
        try:
            families = set(tkfont.families())
        except Exception:
            families = set()
        if families & {"Segoe Fluent Icons", "Segoe MDL2 Assets"}:
            return (ICON_PREVIOUS, ICON_PLAY_PAUSE, ICON_NEXT)
        return ("|<", ">||", ">|")

    def _cc_card_audio(self, parent, row, column):
        import system_controls

        card = self._cc_card(parent, "Sound", row=row, column=column,
                             sticky="new", padx=6, pady=6)

        def on_volume(level):
            self._cc_volume_value.configure(text=f"{int(level)}%")
            system_controls.set_volume(int(level))

        self._cc_volume, self._cc_volume_value = self._cc_slider_row(
            card, "Output volume", on_volume)

        row_widgets = ctk.CTkFrame(card, fg_color="transparent")
        row_widgets.pack(fill=tk.X, padx=18, pady=(0, 16))
        self._cc_mute_button = self._cc_action_button(
            row_widgets, "Mute", self._cc_toggle_mute, width=76,
            side=tk.LEFT, padx=(0, 8))
        for glyph, action in zip(self._cc_transport_glyphs(),
                                 ("previous", "playpause", "next")):
            button = self._cc_action_button(
                row_widgets, glyph,
                lambda a=action: self._run_control(system_controls.media, a),
                width=46, side=tk.LEFT, padx=(0, 6))
            button.configure(font=self._cc_icon_font())
        ctk.CTkLabel(card, text="Transport goes to whichever app is playing.",
                     font=self.font("caption"), text_color=LABEL_TERTIARY,
                     anchor="w").pack(fill=tk.X, padx=18, pady=(0, 14))

    def _cc_toggle_mute(self):
        import system_controls

        result = system_controls.toggle_mute()
        self._q.put(("control_result", result))
        self._cc_refresh_audio()

    def _cc_refresh_audio(self):
        import system_controls

        level, muted = system_controls.get_volume()
        if level is None:
            return
        if not self._cc_dragging_slider:
            self._cc_volume.set(level)
        self._cc_volume_value.configure(text=f"{level}%")
        self._cc_mute_button.configure(text="Unmute" if muted else "Mute")
        self._cc_volume.configure(
            progress_color=LABEL_TERTIARY if muted else BLUE)

    def _cc_card_display(self, parent, row, column):
        import system_controls

        card = self._cc_card(parent, "Display", row=row, column=column,
                             sticky="new", padx=6, pady=6)

        def on_move(level):
            self._cc_brightness_value.configure(text=f"{int(level)}%")

        def on_release(_event):
            self._cc_dragging_slider = False
            # Brightness is a one-second WMI call, so it applies on release
            # rather than on every pixel of the drag.
            self._run_control(system_controls.set_brightness,
                              int(self._cc_brightness.get()))

        self._cc_brightness, self._cc_brightness_value = self._cc_slider_row(
            card, "Brightness", on_move, release=on_release)

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill=tk.X, padx=18, pady=(0, 14))
        for label, page in (("Display settings", "display"),
                            ("Night light", "nightlight")):
            self._cc_action_button(
                buttons, label,
                lambda p=page: self._run_control(system_controls.open_settings, p),
                side=tk.LEFT, padx=(0, 8))
        self._cc_display_note = ctk.CTkLabel(
            card, text="", font=self.font("caption"),
            text_color=LABEL_TERTIARY, anchor="w")
        self._cc_display_note.pack(fill=tk.X, padx=18, pady=(0, 14))

    def _cc_card_network(self, parent, row, column):
        import system_controls

        card = self._cc_card(parent, "Network", row=row, column=column,
                             sticky="new", padx=6, pady=6)
        self._cc_wifi_label = ctk.CTkLabel(
            card, text="Reading…", font=self.font("body"),
            text_color=LABEL_PRIMARY, anchor="w")
        self._cc_wifi_label.pack(fill=tk.X, padx=18, pady=(12, 0))
        self._cc_wifi_detail = ctk.CTkLabel(
            card, text="", font=self.font("caption"),
            text_color=LABEL_TERTIARY, anchor="w")
        self._cc_wifi_detail.pack(fill=tk.X, padx=18, pady=(1, 8))

        row_widgets = ctk.CTkFrame(card, fg_color="transparent")
        row_widgets.pack(fill=tk.X, padx=18, pady=(0, 10))
        self._cc_network_choice = ctk.CTkOptionMenu(
            row_widgets, values=["Saved networks…"], height=32, width=150,
            font=self.font("label"), fg_color=BG_CONTROL,
            button_color=BG_CONTROL, button_hover_color=BG_CARD_HOVER,
            text_color=LABEL_PRIMARY, dropdown_fg_color=BG_CARD,
            dropdown_text_color=LABEL_PRIMARY, corner_radius=8)
        self._cc_network_choice.pack(side=tk.LEFT, padx=(0, 8))
        self._cc_action_button(
            row_widgets, "Join",
            lambda: self._run_control(system_controls.wifi_connect,
                                      self._cc_network_choice.get()),
            width=64, side=tk.LEFT, padx=(0, 6))
        self._cc_action_button(
            row_widgets, "Disconnect",
            lambda: self._run_control(system_controls.wifi_disconnect),
            width=92, side=tk.LEFT)

        ctk.CTkFrame(card, fg_color=SEPARATOR, height=1).pack(
            fill=tk.X, padx=18, pady=6)

        bluetooth = ctk.CTkFrame(card, fg_color="transparent")
        bluetooth.pack(fill=tk.X, padx=18, pady=(4, 16))
        self._cc_bluetooth_label = ctk.CTkLabel(
            bluetooth, text="Bluetooth — reading…",
            font=self.font("body"), text_color=LABEL_PRIMARY, anchor="w")
        self._cc_bluetooth_label.pack(side=tk.LEFT)
        # The radio itself needs elevation, so this opens Settings rather
        # than offering a switch that would silently fail.
        self._cc_action_button(
            bluetooth, "Open",
            lambda: self._run_control(system_controls.open_settings, "bluetooth"),
            width=64, side=tk.RIGHT)

    def _cc_card_power(self, parent, row, column):
        import system_controls

        card = self._cc_card(parent, "Power", "Sleep and lock act at once; the "
                             "rest ask first.", row=row, column=column,
                             sticky="new", padx=6, pady=6)
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill=tk.BOTH, expand=True, padx=18, pady=(14, 16))
        for column_index in range(2):
            grid.grid_columnconfigure(column_index, weight=1)

        safe = (("Lock", system_controls.lock), ("Sleep", system_controls.sleep))
        for index, (label, function) in enumerate(safe):
            self._cc_action_button(
                grid, label, lambda f=function: self._run_control(f)
            ).grid(row=0, column=index, sticky="ew", padx=4, pady=4)

        ctk.CTkFrame(grid, fg_color=SEPARATOR, height=1).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=10)

        risky = (
            ("Sign out", "Confirm sign out",
             lambda: self._run_control(system_controls.sign_out, confirmed=True)),
            ("Restart", "Confirm restart",
             lambda: self._run_control(system_controls.shutdown, confirmed=True,
                                       restart=True)),
            ("Shut down", "Confirm shut down",
             lambda: self._run_control(system_controls.shutdown, confirmed=True)),
        )
        # The three that end the session sit in their own row, smaller: the
        # button you press twenty times a day should not be the same size as
        # the one that closes everything you have open.
        destructive = ctk.CTkFrame(grid, fg_color="transparent")
        destructive.grid(row=2, column=0, columnspan=2, sticky="ew")
        for index in range(len(risky)):
            destructive.grid_columnconfigure(index, weight=1)
        for index, (label, prompt, function) in enumerate(risky):
            button = self._cc_action_button(destructive, label, None, danger=True)
            button.configure(height=28, font=self.font("caption"),
                             text_color=LABEL_SECONDARY,
                             command=lambda b=button, p=prompt, f=function:
                             self._cc_confirm(b, p, f))
            button.grid(row=0, column=index, sticky="ew", padx=3)

    def _cc_card_launch(self, parent, row, column):
        card = self._cc_card(parent, "Open", row=row, column=column,
                             sticky="new", padx=6, pady=6)
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill=tk.X, padx=18, pady=(14, 8))
        for column_index in range(3):
            grid.grid_columnconfigure(column_index, weight=1)
        for index, (label, target) in enumerate(QUICK_LAUNCH):
            self._cc_action_button(
                grid, label, lambda t=target: self._cc_launch(t)
            ).grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)

        run_row = ctk.CTkFrame(card, fg_color="transparent")
        run_row.pack(fill=tk.X, padx=18, pady=(4, 16))
        self._cc_run_entry = ctk.CTkEntry(
            run_row, placeholder_text="App, file, folder or URL…", height=34,
            font=self.font("label"), fg_color=BG_INSET, border_width=0,
            corner_radius=8, text_color=LABEL_PRIMARY)
        self._cc_run_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._cc_run_entry.bind("<Return>", lambda _e: self._cc_launch())
        self._cc_action_button(run_row, "Open", self._cc_launch, width=64,
                               side=tk.RIGHT)

    def _cc_launch(self, target=None):
        if target is None:
            target = self._cc_run_entry.get().strip()
            self._cc_run_entry.delete(0, tk.END)
        if not target:
            return

        def opener():
            os.startfile(target)
            return f"[Success] Opened {target}."
        self._run_control(opener)

    def _cc_card_processes(self, parent, row, column):
        import system_controls

        card = self._cc_card(parent, "Running", "Grouped by app, heaviest first.",
                             row=row, column=column, sticky="new", padx=6, pady=6)
        self._cc_process_host = ctk.CTkFrame(card, fg_color="transparent")
        self._cc_process_host.pack(fill=tk.BOTH, expand=True, padx=12,
                                   pady=(12, 14))
        self._cc_process_rows = []
        ctk.CTkLabel(self._cc_process_host, text="Reading…",
                     font=self.font("caption"),
                     text_color=LABEL_TERTIARY).pack(anchor="w", padx=6)

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.pack(fill=tk.X, padx=18, pady=(0, 14))
        bin_button = self._cc_action_button(footer, "Empty Recycle Bin", None,
                                            danger=True)
        bin_button.configure(command=lambda b=bin_button: self._cc_confirm(
            b, "Confirm empty",
            lambda: self._run_control(system_controls.empty_recycle_bin,
                                      confirmed=True)))
        bin_button.pack(fill=tk.X)

    def _cc_render_processes(self, rows):
        import system_controls

        for widget in self._cc_process_host.winfo_children():
            widget.destroy()
        for entry in rows[:6]:
            line = ctk.CTkFrame(self._cc_process_host, fg_color="transparent",
                                height=30)
            line.pack(fill=tk.X, pady=1)
            name = entry["name"]
            if name.lower().endswith(".exe"):
                name = name[:-4]
            suffix = f"  ×{entry['count']}" if entry["count"] > 1 else ""
            ctk.CTkLabel(line, text=f"{name}{suffix}", font=self.font("label"),
                         text_color=LABEL_PRIMARY, anchor="w").pack(
                             side=tk.LEFT, padx=(6, 0))
            button = self._cc_action_button(line, "End", None, width=48,
                                            danger=True)
            button.configure(command=lambda b=button, n=entry["name"]:
                             self._cc_confirm(
                                 b, "Sure?",
                                 lambda: self._run_control(
                                     system_controls.end_process, name=n,
                                     confirmed=True)))
            button.pack(side=tk.RIGHT, padx=(6, 6))
            ctk.CTkLabel(line, text=system_controls.human_bytes(entry["memory"]),
                         font=self.font("mono"), text_color=LABEL_SECONDARY,
                         anchor="e").pack(side=tk.RIGHT)

    # ------------------------------------------------------------ #
    # CHAT
    # ------------------------------------------------------------ #
    def _cc_page_chat(self, page):
        card = self._cc_card(page)
        card.pack(fill=tk.BOTH, expand=True, padx=5)
        self.chat_history = ctk.CTkTextbox(
            card, fg_color=BG_INSET, text_color=LABEL_PRIMARY, wrap="word",
            font=self.font("body"), corner_radius=10, border_width=0)
        self.chat_history.pack(fill=tk.BOTH, expand=True, padx=14, pady=(14, 10))
        self.chat_history.insert("0.0", "Control Center ready.\n\n")
        self.chat_history.configure(state="disabled")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill=tk.X, padx=14, pady=(0, 14))
        self.control_command = ctk.CTkEntry(
            row, placeholder_text="Ask VANTA, or give it a task…",
            height=42, font=self.font("body"), fg_color=BG_INSET,
            border_width=0, corner_radius=10, text_color=LABEL_PRIMARY)
        self.control_command.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                  padx=(0, 8))
        self.control_command.bind("<Return>", self._send_control_command)
        self._cc_action_button(row, "Send", self._send_control_command,
                               width=84, primary=True, side=tk.RIGHT)

    # ------------------------------------------------------------ #
    # OUTPUT -- content explicitly placed here by VANTA
    # ------------------------------------------------------------ #
    def _cc_page_output(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=5, pady=(0, 10))
        self._cc_output_title = ctk.CTkLabel(
            bar, text="Nothing displayed yet", anchor="w",
            font=self.font("title"), text_color=LABEL_PRIMARY)
        self._cc_output_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._cc_output_language = ctk.CTkLabel(
            bar, text="TEXT", height=24, corner_radius=12,
            fg_color=theme.blend(BLUE, BG_WINDOW, 0.84), text_color=BLUE,
            font=self.font("heading"), padx=10)
        self._cc_output_language.pack(side=tk.LEFT, padx=(8, 10))
        self._cc_action_button(bar, "Copy", self._cc_copy_output,
                               width=66, side=tk.RIGHT)

        card = self._cc_card(page, row=1, column=0, sticky="nsew", padx=5)
        self._cc_output = ctk.CTkTextbox(
            card, fg_color=BG_INSET, text_color="#d7dae0", wrap="none",
            font=self.font("mono_body"), corner_radius=12, border_width=0,
            activate_scrollbars=True)
        self._cc_output.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        self._cc_output.insert(
            "0.0", "Ask VANTA to show an answer, report, or code here.")
        self._cc_output.configure(state="disabled")
        self._cc_output_raw = ""

    def _cc_copy_output(self):
        text = str(getattr(self, "_cc_output_raw", "") or "")
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _cc_show_document(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        content = str(payload.get("content") or "")
        if not content:
            return
        self._show_page("Output")
        self._cc_output_raw = content
        self._cc_output_title.configure(
            text=str(payload.get("title") or "VANTA Output"))
        language = str(payload.get("language") or "").strip().upper()
        self._cc_output_language.configure(text=language or "TEXT")
        self._cc_output.configure(state="normal")
        self._cc_output.delete("0.0", tk.END)
        self._cc_output.insert("0.0", content)
        self._cc_output.configure(state="disabled")
        self._show_dashboard()

    # ------------------------------------------------------------ #
    # FILES
    # ------------------------------------------------------------ #
    def _cc_page_files(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=3)
        page.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(page, fg_color="transparent")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5,
                     pady=(0, 10))
        self._cc_action_button(toolbar, "Up", self._vault_go_up, width=54,
                               side=tk.LEFT, padx=(0, 8))
        self.file_path_label = ctk.CTkLabel(
            toolbar, text="Vault", anchor="w", font=self.font("mono"),
            text_color=LABEL_SECONDARY)
        self.file_path_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._cc_action_button(toolbar, "Refresh", self._refresh_files, width=76,
                               side=tk.RIGHT)

        browser = self._cc_card(page, row=1, column=0, sticky="nsew",
                                padx=(5, 6))
        self.file_list = tk.Listbox(
            browser, bg=BG_INSET, fg=LABEL_PRIMARY, selectbackground=BLUE,
            selectforeground="#ffffff", borderwidth=0, highlightthickness=0,
            font=self.tk_font("mono"), activestyle="none")
        self.file_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.file_list.bind("<Double-Button-1>", self._open_selected_file)
        self.file_list.bind("<Return>", self._open_selected_file)

        workspace = self._cc_card(page, row=1, column=1, sticky="nsew",
                                  padx=(6, 5))
        header = ctk.CTkFrame(workspace, fg_color="transparent")
        header.pack(fill=tk.X, padx=14, pady=(12, 6))
        self.file_title = ctk.CTkLabel(
            header, text="Select a file", anchor="w", font=self.font("mono"),
            text_color=LABEL_SECONDARY)
        self.file_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for label, command in (("Save", self._save_vault_file),
                               ("Revert", self._revert_vault_file),
                               ("Reload", self._reload_vault_file)):
            self._cc_action_button(header, label, command, width=66,
                                   side=tk.RIGHT, padx=(6, 0))

        # Packed before the paned window on purpose: pack hands out space in
        # order, and a PanedWindow asks for everything, so a row packed after
        # it silently gets zero height and disappears.
        ask = ctk.CTkFrame(workspace, fg_color="transparent")
        ask.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(6, 12))

        panes = tk.PanedWindow(workspace, orient=tk.VERTICAL, bg=BG_CARD, bd=0,
                               sashwidth=6)
        panes.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 0))
        self.file_editor = ctk.CTkTextbox(
            panes, fg_color=BG_INSET, text_color="#d1d1d6", wrap="none",
            font=self.font("mono_body"), corner_radius=10, border_width=0)
        self.file_editor.bind("<KeyRelease>", lambda _e: self._update_vault_diff())
        panes.add(self.file_editor, stretch="always", minsize=180)
        self.file_diff = tk.Text(
            panes, bg=BG_INSET, fg="#c7c7cc", insertbackground="#ffffff",
            wrap="none", borderwidth=0, highlightthickness=0,
            font=self.tk_font("mono"), state="disabled")
        self.file_diff.tag_configure("added", foreground=GREEN,
                                     background="#10291a")
        self.file_diff.tag_configure("removed", foreground="#ff6961",
                                     background="#321416")
        self.file_diff.tag_configure("header", foreground=theme.TEAL)
        self.file_diff.tag_configure("context", foreground=LABEL_SECONDARY)
        panes.add(self.file_diff, stretch="always", minsize=100)

        self.file_ai_command = ctk.CTkEntry(
            ask, placeholder_text="Tell VANTA what to change in this file…",
            height=38, font=self.font("label"), fg_color=BG_INSET,
            border_width=0, corner_radius=8, text_color=LABEL_PRIMARY)
        self.file_ai_command.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                  padx=(0, 8))
        self.file_ai_command.bind("<Return>", self._send_vault_command)
        self._cc_action_button(ask, "Ask VANTA", self._send_vault_command,
                               width=100, side=tk.RIGHT)

        from control_center_files import vault_root
        self._vault_root = vault_root(os.path.dirname(os.path.abspath(__file__)))
        self._vault_current = "."
        self._file_rows = []
        self._vault_file = None
        self._vault_original = ""
        self._vault_hash = None
        self._vault_dirty = False

    # ------------------------------------------------------------ #
    # BRAIN (Neo4j knowledge graph)
    # ------------------------------------------------------------ #
    def _cc_page_graph(self, page):
        toolbar = ctk.CTkFrame(page, fg_color="transparent")
        toolbar.pack(fill=tk.X, padx=5, pady=(0, 10))
        self.graph_search = ctk.CTkEntry(
            toolbar, placeholder_text="Search a person, project, preference "
            "or goal…", height=36, font=self.font("label"),
            fg_color=BG_INSET, border_width=0, corner_radius=8,
            text_color=LABEL_PRIMARY)
        self.graph_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.graph_search.bind("<Return>", lambda _e: self._refresh_graph())
        self._cc_action_button(toolbar, "Load", self._refresh_graph, width=76,
                               primary=True, side=tk.RIGHT)

        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=5)
        body.grid_columnconfigure(0, weight=4)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        canvas_card = self._cc_card(body, row=0, column=0, sticky="nsew",
                                    padx=(0, 6))
        self.graph_canvas = tk.Canvas(canvas_card, bg=BG_INSET,
                                      highlightthickness=0, cursor="hand2")
        self.graph_canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.graph_canvas.bind("<Button-1>", self._on_graph_click)
        self.graph_canvas.bind("<Configure>", lambda _e: self._draw_graph())

        detail = self._cc_card(body, row=0, column=1, sticky="nsew", padx=(6, 0))
        self.graph_status = ctk.CTkLabel(
            detail, text="Load VANTA's brain to begin", anchor="w",
            font=self.font("caption"), text_color=LABEL_SECONDARY)
        self.graph_status.pack(fill=tk.X, padx=14, pady=(14, 6))
        self.graph_details = ctk.CTkTextbox(
            detail, fg_color=BG_INSET, text_color=LABEL_PRIMARY, wrap="word",
            font=self.font("label"), corner_radius=10, border_width=0)
        self.graph_details.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.graph_details.insert(
            "0.0", "A simple read-only view of what VANTA currently knows. "
            "Click a node to inspect its relationships, confidence and source. "
            "Restricted facts and raw evidence are never rendered here.")
        self.graph_details.configure(state="disabled")
        self._graph_snapshot = {"nodes": [], "edges": []}
        self._graph_positions = {}
        self._graph_loading = False

    # ------------------------------------------------------------ #
    # CODE -- the project VANTA is building, and its files
    # ------------------------------------------------------------ #
    def _cc_page_code(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=3)
        page.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=(0, 10))
        self._cc_project_choice = ctk.CTkOptionMenu(
            bar, values=["No projects yet"], height=34, width=210,
            font=self.font("label"), fg_color=BG_CONTROL, button_color=BG_CONTROL,
            button_hover_color=BG_CARD_HOVER, text_color=LABEL_PRIMARY,
            dropdown_fg_color=BG_CARD, dropdown_text_color=LABEL_PRIMARY,
            corner_radius=8, command=self._cc_open_project)
        self._cc_project_choice.pack(side=tk.LEFT, padx=(0, 8))
        self._cc_project_note = ctk.CTkLabel(
            bar, text="", anchor="w", font=self.font("caption"),
            text_color=LABEL_TERTIARY)
        self._cc_project_note.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._cc_action_button(bar, "Refresh", self._cc_refresh_projects,
                               width=78, side=tk.RIGHT)
        self._cc_action_button(bar, "New project", self._cc_new_project,
                               width=104, side=tk.RIGHT, padx=(0, 8))

        browser = self._cc_card(page, row=1, column=0, sticky="nsew", padx=(5, 6))
        self._cc_code_tree = tk.Listbox(
            browser, bg=BG_INSET, fg=LABEL_PRIMARY, selectbackground=BLUE,
            selectforeground="#ffffff", borderwidth=0, highlightthickness=0,
            font=self.tk_font("mono"), activestyle="none")
        self._cc_code_tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self._cc_code_tree.bind("<Double-Button-1>", self._cc_open_project_file)
        self._cc_code_tree.bind("<Return>", self._cc_open_project_file)

        workspace = self._cc_card(page, row=1, column=1, sticky="nsew", padx=(6, 5))
        header = ctk.CTkFrame(workspace, fg_color="transparent")
        header.pack(fill=tk.X, padx=14, pady=(12, 6))
        self._cc_code_title = ctk.CTkLabel(
            header, text="Open a project, then a file", anchor="w",
            font=self.font("mono"), text_color=LABEL_SECONDARY)
        self._cc_code_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for label, command in (("Save", self._cc_save_project_file),
                               ("Reload", self._cc_reload_project_file)):
            self._cc_action_button(header, label, command, width=66,
                                   side=tk.RIGHT, padx=(6, 0))

        # Packed before the editor: pack hands out space in order, and a text
        # box set to expand will otherwise swallow the row below it entirely.
        instruct = ctk.CTkFrame(workspace, fg_color="transparent")
        instruct.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(6, 12))
        self._cc_code_command = ctk.CTkEntry(
            instruct, height=40, font=self.font("label"), fg_color=BG_INSET,
            border_width=0, corner_radius=8, text_color=LABEL_PRIMARY,
            placeholder_text="Tell VANTA what to implement in this project\u2026")
        self._cc_code_command.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._cc_code_command.bind("<Return>", self._cc_send_code_command)
        self._cc_action_button(instruct, "Implement", self._cc_send_code_command,
                               width=104, primary=True, side=tk.RIGHT)

        self._cc_code_editor = ctk.CTkTextbox(
            workspace, fg_color=BG_INSET, text_color="#d1d1d6", wrap="none",
            font=self.font("mono_body"), corner_radius=10, border_width=0)
        self._cc_code_editor.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 0))

        self._cc_code_file = None
        self._cc_code_rows = []
        self.root.after(1400, self._cc_refresh_projects)

    def _cc_refresh_projects(self):
        import projects

        base = os.path.dirname(os.path.abspath(__file__))
        try:
            found = projects.listing(base)
            active = projects.get_active(base)
        except Exception as exc:
            print(f"[Code] Could not list projects: {exc}")
            return
        names = [p["name"] for p in found] or ["No projects yet"]
        self._cc_project_choice.configure(values=names)
        if active in names:
            self._cc_project_choice.set(active)
        elif names:
            self._cc_project_choice.set(names[0])
        detail = next((p for p in found if p["name"] == active), None)
        if detail:
            languages = ", ".join(detail["languages"]) or "empty"
            self._cc_project_note.configure(
                text=f"{detail['files']} files \u00b7 {languages} \u00b7 "
                     f"{detail['description'] or 'no description'}")
        else:
            self._cc_project_note.configure(
                text="Ask VANTA to create one, or press New project.")
        self._cc_load_tree()

    def _cc_load_tree(self):
        import projects

        base = os.path.dirname(os.path.abspath(__file__))
        active = projects.get_active(base)
        self._cc_code_tree.delete(0, tk.END)
        self._cc_code_rows = []
        if not active:
            return
        try:
            rows = projects.file_rows(base, active)
        except Exception as exc:
            self._cc_code_tree.insert(tk.END, f"  {exc}")
            return
        # Folder rows are inserted as the paths require them. Without these the
        # list shows indented filenames under nothing, so src/parser.py and
        # tests/test_parser.py look like they live in the same place.
        shown = set()
        for row in rows:
            parts = row["path"].split("/")
            for depth in range(len(parts) - 1):
                folder = "/".join(parts[:depth + 1])
                if folder not in shown:
                    shown.add(folder)
                    self._cc_code_tree.insert(
                        tk.END, "  " * depth + parts[depth] + "/")
                    # Folders are not openable, so they map to no file.
                    self._cc_code_rows.append(None)
            self._cc_code_tree.insert(
                tk.END, "  " * (len(parts) - 1) + parts[-1])
            self._cc_code_rows.append(row["path"])

    def _cc_open_project(self, name):
        import projects

        base = os.path.dirname(os.path.abspath(__file__))
        if name and name != "No projects yet":
            projects.set_active(base, name)
        self._cc_refresh_projects()

    def _cc_open_project_file(self, _event=None):
        import projects

        selection = self._cc_code_tree.curselection()
        if not selection:
            return
        relative = self._cc_code_rows[int(selection[0])]
        if relative is None:
            return      # a folder row; there is nothing to open
        base = os.path.dirname(os.path.abspath(__file__))
        active = projects.get_active(base)
        try:
            text, meta = projects.read_file(base, active, relative)
        except Exception as exc:
            self._cc_code_title.configure(text=str(exc)[:110])
            return
        self._cc_code_file = relative
        self._cc_code_title.configure(text=f"{active} / {relative}  ({meta['size']} bytes)")
        self._cc_code_editor.delete("0.0", tk.END)
        self._cc_code_editor.insert("0.0", text)

    def _cc_reload_project_file(self):
        if self._cc_code_file:
            self._cc_load_tree()
            for index, path in enumerate(self._cc_code_rows):
                if path == self._cc_code_file:
                    self._cc_code_tree.selection_clear(0, tk.END)
                    self._cc_code_tree.selection_set(index)
                    break
            self._cc_open_project_file()

    def _cc_save_project_file(self):
        import projects

        if not self._cc_code_file:
            return
        base = os.path.dirname(os.path.abspath(__file__))
        active = projects.get_active(base)
        body = self._cc_code_editor.get("0.0", tk.END)
        try:
            result = projects.write_file(base, active, self._cc_code_file, body,
                                         overwrite=True)
        except Exception as exc:
            result = f"[Error] {exc}"
        self._q.put(("control_result", result))
        self._cc_load_tree()

    def _cc_new_project(self):
        # Naming a project is a sentence, not a form; VANTA creates it so the
        # description and the first files come from the same conversation.
        self._send_control_command(
            "create a new project in the vault and tell me what to call it")

    def _cc_send_code_command(self, _event=None):
        import projects

        instruction = self._cc_code_command.get().strip()
        self._cc_code_command.delete(0, tk.END)
        if not instruction:
            return
        base = os.path.dirname(os.path.abspath(__file__))
        active = projects.get_active(base)
        # The open file is almost always the subject of "implement this", so it
        # goes with the instruction rather than making the model guess.
        where = f"In the project {active}" if active else "In the open project"
        if self._cc_code_file:
            where += f", file {self._cc_code_file}"
        self._send_control_command(f"{where}: {instruction}")

    # ------------------------------------------------------------ #
    # WORKSPACE -- panels VANTA builds for itself
    # ------------------------------------------------------------ #
    def _cc_page_workspace(self, page):
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill=tk.X, padx=9, pady=(0, 10))
        ctk.CTkLabel(
            header, anchor="w", font=self.font("label"),
            text_color=LABEL_SECONDARY, justify="left",
            text="Boards VANTA builds for itself. Ask for one \u2014 \u201ckeep a "
                 "panel tracking my revision\u201d \u2014 and it appears here and "
                 "survives restarts.").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._cc_action_button(header, "Refresh", self._cc_refresh_panels,
                               width=78, side=tk.RIGHT)

        self._cc_panel_host = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=BG_CARD_HOVER,
            scrollbar_button_hover_color=SEPARATOR)
        self._cc_panel_host.pack(fill=tk.BOTH, expand=True)
        for column in range(2):
            self._cc_panel_host.grid_columnconfigure(column, weight=1,
                                                     uniform="panel")
        self._cc_panels_signature = None
        self.root.after(1200, self._cc_refresh_panels)

    def _cc_refresh_panels(self):
        import panels as panel_store

        try:
            loaded = panel_store.load(os.path.dirname(os.path.abspath(__file__)))
        except Exception as exc:
            loaded = []
            print(f"[Panels] Could not load: {exc}")
        # The Workspace page is built on demand -- it came off the sidebar when
        # the window was simplified -- so its widgets do not exist until it is
        # opened. ADD_PANEL still fires a refresh from the queue, and assuming
        # the host and the signature existed took down the whole UI queue
        # handler the moment a panel was added. Same shape as the Graph page.
        if getattr(self, "_cc_panel_host", None) is None:
            # Nothing to draw onto yet. The panels are on disk; the page reads
            # them when it is built.
            self._cc_panels_signature = None
            return
        # Rebuilding every tick would destroy widgets under the pointer and
        # reset the scroll position, so redraw only when something changed.
        signature = repr(loaded)
        if signature == getattr(self, "_cc_panels_signature", None):
            return
        self._cc_panels_signature = signature
        self._cc_render_panels(loaded)

    def _cc_render_panels(self, loaded):
        import panels as panel_store

        for widget in self._cc_panel_host.winfo_children():
            widget.destroy()
        if not loaded:
            ctk.CTkLabel(self._cc_panel_host, text="Nothing here yet.",
                         font=self.font("body"), text_color=LABEL_TERTIARY,
                         justify="left").grid(row=0, column=0, sticky="w",
                                              padx=10, pady=20)
            return

        for index, panel in enumerate(loaded):
            accent = panel_store.ACCENTS.get(panel["accent"], BLUE)
            card = ctk.CTkFrame(self._cc_panel_host, fg_color=BG_CARD,
                                corner_radius=14)
            card.grid(row=index // 2, column=index % 2, sticky="nsew",
                      padx=6, pady=6)

            head = ctk.CTkFrame(card, fg_color="transparent")
            head.pack(fill=tk.X, padx=18, pady=(15, 2))
            ctk.CTkLabel(head, text=panel["title"], font=self.font("card"),
                         text_color=LABEL_PRIMARY, anchor="w").pack(side=tk.LEFT)
            if panel["pinned"]:
                ctk.CTkLabel(head, text="PINNED", font=self.font("heading"),
                             text_color=accent).pack(side=tk.RIGHT)
            if panel["note"]:
                ctk.CTkLabel(card, text=panel["note"], font=self.font("caption"),
                             text_color=LABEL_TERTIARY, anchor="w",
                             justify="left", wraplength=420).pack(
                                 anchor="w", padx=18, pady=(0, 4))

            body = ctk.CTkFrame(card, fg_color="transparent")
            body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(6, 16))
            for item in panel["items"]:
                self._cc_render_panel_item(body, item, accent)

    def _cc_render_panel_item(self, parent, item, accent):
        kind = item["kind"]
        if kind == "divider":
            ctk.CTkFrame(parent, fg_color=SEPARATOR, height=1).pack(
                fill=tk.X, pady=8)

        elif kind in ("stat", "progress"):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill=tk.X, pady=3)
            ctk.CTkLabel(row, text=item["label"], font=self.font("label"),
                         text_color=LABEL_SECONDARY, anchor="w").pack(side=tk.LEFT)
            if item["value"]:
                ctk.CTkLabel(row, text=item["value"], font=self.font("body_em"),
                             text_color=LABEL_PRIMARY, anchor="e").pack(
                                 side=tk.RIGHT)
            if item["ratio"] is not None:
                bar = ctk.CTkProgressBar(parent, height=5, corner_radius=3,
                                         progress_color=accent, fg_color=BG_INSET)
                bar.set(item["ratio"])
                bar.pack(fill=tk.X, pady=(2, 4))
            if item["detail"]:
                ctk.CTkLabel(parent, text=item["detail"],
                             font=self.font("caption"), text_color=LABEL_TERTIARY,
                             anchor="w").pack(anchor="w", pady=(0, 4))

        elif kind in ("text", "note"):
            ctk.CTkLabel(
                parent, text=item["text"], font=self.font("label"),
                text_color=LABEL_TERTIARY if kind == "note" else LABEL_SECONDARY,
                anchor="w", justify="left", wraplength=420).pack(
                    anchor="w", fill=tk.X, pady=3)

        elif kind == "bullets":
            for entry in item["items"]:
                ctk.CTkLabel(parent, text="\u2022  " + entry,
                             font=self.font("label"), text_color=LABEL_SECONDARY,
                             anchor="w", justify="left", wraplength=410).pack(
                                 anchor="w", fill=tk.X, pady=1)

        elif kind == "kv":
            for key, value in item["rows"]:
                row = ctk.CTkFrame(parent, fg_color="transparent")
                row.pack(fill=tk.X, pady=2)
                ctk.CTkLabel(row, text=key, font=self.font("label"),
                             text_color=LABEL_TERTIARY, anchor="w").pack(
                                 side=tk.LEFT)
                ctk.CTkLabel(row, text=value, font=self.font("label"),
                             text_color=LABEL_PRIMARY, anchor="e").pack(
                                 side=tk.RIGHT)

        elif kind == "button":
            # A panel button says something to VANTA; it never runs code.
            self._cc_action_button(
                parent, item["label"],
                lambda c=item["command"]: self._send_control_command(c)
            ).pack(fill=tk.X, pady=3)

        elif kind == "link":
            self._cc_action_button(
                parent, item["label"],
                lambda u=item["url"]: self._cc_launch(u)
            ).pack(fill=tk.X, pady=3)

    # ------------------------------------------------------------ #
    # MODES
    # ------------------------------------------------------------ #
    def _cc_highlight_mode(self, mode):
        """Show which mode is live, and say what it means.

        The buttons are otherwise identical, and with the switch on Home the
        blurb is the only thing explaining what the other one would do.
        """
        buttons = getattr(self, "_cc_mode_cards", None)
        if not buttons:
            return
        active = str(mode or "").strip().lower()
        for name, button in buttons.items():
            on = name == active
            try:
                button.configure(
                    fg_color=BLUE if on else BG_CARD,
                    text_color="#ffffff" if on else LABEL_SECONDARY,
                    hover_color=BLUE if on else BG_CARD_HOVER)
            except Exception:
                # A future rebuild may use a widget that takes none of these;
                # the mode still works, it just will not be highlighted.
                pass
        blurb = getattr(self, "_cc_mode_blurb", None)
        if blurb is not None:
            try:
                blurb.configure(text=MODE_BLURBS.get(active, ""))
            except Exception:
                pass

    # ------------------------------------------------------------ #
    # ACTIVITY
    # ------------------------------------------------------------ #
    def _cc_page_activity(self, page):
        card = self._cc_card(page)
        card.pack(fill=tk.BOTH, expand=True, padx=5)
        self.activity_text = ctk.CTkTextbox(
            card, fg_color=BG_INSET, text_color=LABEL_SECONDARY, wrap="word",
            font=self.font("mono_body"), corner_radius=10, border_width=0)
        self.activity_text.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        self.activity_text.insert("0.0", "Activity stream started.\n\n")
        self.activity_text.configure(state="disabled")

    # ------------------------------------------------------------ #
    # MAP -- opened on demand by SHOW_ROUTE, intentionally not in the rail
    # ------------------------------------------------------------ #
    def _cc_page_map(self, page):
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        card = self._cc_card(
            page, "Route", "Live traffic from Mapbox; OpenStreetMap fallback")
        card.grid(row=0, column=0, sticky="nsew", padx=5)

        self._cc_route_title = ctk.CTkLabel(
            card, text="Ask VANTA to show a route", anchor="w",
            font=self.font("card"), text_color=LABEL_PRIMARY)
        self._cc_route_title.pack(fill=tk.X, padx=18, pady=(14, 4))
        self._cc_route_canvas = tk.Canvas(
            card, bg=BG_INSET, highlightthickness=0, bd=0, height=470)
        self._cc_route_canvas.pack(fill=tk.BOTH, expand=True, padx=18, pady=8)
        self._cc_route_canvas.bind(
            "<Configure>", lambda event: self._cc_redraw_route(
                event.width, event.height))
        self._cc_route_caption = ctk.CTkLabel(
            card, text="", anchor="w", justify="left",
            font=self.font("body"), text_color=LABEL_SECONDARY)
        self._cc_route_caption.pack(fill=tk.X, padx=18, pady=(2, 16))

    def _cc_redraw_route(self, width=None, height=None):
        canvas = getattr(self, "_cc_route_canvas", None)
        route = getattr(self, "_cc_route", None)
        if canvas is None or not route:
            return
        width = max(120, int(width or canvas.winfo_width()))
        height = max(100, int(height or canvas.winfo_height()))
        background = getattr(self, "_cc_route_photo", None)
        if background is not None:
            canvas.delete("all")
            canvas.create_image(width // 2, height // 2, image=background)
            return
        self.draw_route(canvas, route, width, height)

    def _cc_show_route(self, route):
        """Open the lazy Map page, then fetch its basemap without blocking Tk."""
        if not isinstance(route, dict) or not route.get("ok"):
            return
        self._cc_route = route
        self._cc_route_photo = None
        generation = int(getattr(self, "_cc_route_generation", 0)) + 1
        self._cc_route_generation = generation
        self._show_dashboard()
        self._show_page("Map")

        origin = (route.get("origin") or {}).get("display_name") or "Origin"
        destination = (route.get("destination") or {}).get("display_name") or "Destination"
        self._cc_route_title.configure(text=f"{origin}  →  {destination}")
        import route_render
        caption = route_render.describe_all(route)
        incidents = len(route.get("incidents") or [])
        if incidents:
            caption += f" · {incidents} reported incident{'s' if incidents != 1 else ''}"
        self._cc_route_caption.configure(text=caption)
        self._cc_redraw_route()

        def fetch_static_map():
            try:
                import io
                import requests
                from PIL import Image
                from providers.maps_api import _mapbox_key
                url = route_render.static_map_url(
                    route, width=760, height=470, token=_mapbox_key())
                if not url:
                    return
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content)).convert("RGB")
                self._q.put(("route_map_image", {
                    "generation": generation, "image": image}))
            except Exception as exc:
                print(f"[Map] Static basemap unavailable ({type(exc).__name__}); route line remains visible.")

        threading.Thread(target=fetch_static_map, daemon=True,
                         name="vanta-route-map").start()

    def _cc_apply_route_image(self, payload):
        """Install a fetched PIL map on the Tk thread."""
        if isinstance(payload, dict):
            if payload.get("generation") != getattr(self, "_cc_route_generation", None):
                return
            image = payload.get("image")
        else:
            image = payload
        canvas = getattr(self, "_cc_route_canvas", None)
        if canvas is None or image is None:
            return
        try:
            from PIL import ImageTk
            width = max(120, canvas.winfo_width())
            height = max(100, canvas.winfo_height())
            fitted = image.copy()
            fitted.thumbnail((width, height))
            self._cc_route_photo = ImageTk.PhotoImage(fitted)
            self._cc_redraw_route(width, height)
        except Exception as exc:
            print(f"[Map] Could not display static map ({type(exc).__name__}).")

    # ------------------------------------------------------------ #
    # Telemetry
    # ------------------------------------------------------------ #
    def _cc_start_telemetry(self):
        """Poll machine state on a worker, never on the Tk thread.

        The fast psutil read costs ~30ms and the PowerShell reads cost one to
        two seconds. On the main loop either would show up as dropped frames
        in the orb, so both live out here and arrive as queue messages.
        """
        def loop():
            import system_controls

            last_slow = 0.0
            while True:
                try:
                    if self.dashboard_visible:
                        self._q.put(("cc_fast", system_controls.snapshot()))
                        if time.time() - last_slow > 12:
                            last_slow = time.time()
                            connected, ssid, signal = system_controls.wifi_status()
                            present, enabled = system_controls.bluetooth_status()
                            self._q.put(("cc_slow", {
                                "brightness": system_controls.get_brightness(),
                                "wifi": (connected, ssid, signal),
                                "bluetooth": (present, enabled),
                                "networks": system_controls.known_networks(),
                                "processes": system_controls.top_processes(9),
                            }))
                except Exception:
                    pass  # Telemetry is never worth taking the UI down for.
                time.sleep(1.0)

        threading.Thread(target=loop, daemon=True,
                         name="vanta-telemetry").start()

    def _cc_apply_fast(self, data):
        import system_controls

        self._cc_set_stat("cpu", f"{data['cpu']:.0f}%", "utilisation",
                          data["cpu"] / 100.0)
        self._cc_set_stat(
            "ram", f"{data['ram']:.0f}%",
            f"{data['ram_used_gb']:.1f} of {data['ram_total_gb']:.0f} GB",
            data["ram"] / 100.0)
        if data.get("disk") is not None:
            self._cc_set_stat("disk", f"{data['disk']:.0f}%",
                              f"{data['disk_free_gb']:.0f} GB free",
                              data["disk"] / 100.0)
        battery = data.get("battery")
        if battery is None:
            self._cc_set_stat("battery", "AC", "no battery", 1.0, LABEL_TERTIARY)
        else:
            charging = data.get("charging")
            left = data.get("battery_left")
            detail = "charging" if charging else (
                f"{system_controls.human_duration(left)} left" if left else "on battery")
            self._cc_set_stat("battery", f"{battery:.0f}%", detail,
                              battery / 100.0,
                              theme.load_ratio_color(1 - battery / 100.0, charging))
            self._cc_chips["battery"].configure(
                text=f"{battery:.0f}%" + ("  ⚡" if charging else ""))

        self._cc_chips["clock"].configure(text=time.strftime("%H:%M"))
        self._cc_chips["network"].configure(
            text=f"↓{system_controls.human_bytes(data['net_down'])}/s")
        self._cc_uptime.configure(
            text=f"Up {system_controls.human_duration(data['uptime'])}")
        self._cc_refresh_audio()

    def _cc_apply_slow(self, data):
        brightness = data.get("brightness")
        if brightness is not None:
            if not self._cc_dragging_slider:
                self._cc_brightness.set(brightness)
            self._cc_brightness_value.configure(text=f"{brightness}%")
            self._cc_display_note.configure(text="")
        else:
            self._cc_brightness_value.configure(text="n/a")
            self._cc_display_note.configure(
                text="This display does not report brightness over WMI.")

        connected, ssid, signal = data.get("wifi", (False, None, None))
        if connected and ssid:
            self._cc_wifi_label.configure(text=ssid)
            self._cc_wifi_detail.configure(
                text=f"Connected · {signal}% signal" if signal
                else "Connected")
        else:
            self._cc_wifi_label.configure(text="Not connected")
            self._cc_wifi_detail.configure(text="Wi-Fi is idle")

        present, enabled = data.get("bluetooth", (False, False))
        self._cc_bluetooth_label.configure(
            text="Bluetooth — " + ("on" if enabled else
                                        "off" if present else "not present"))

        networks = data.get("networks") or []
        if networks:
            current = self._cc_network_choice.get()
            self._cc_network_choice.configure(values=networks)
            if current not in networks:
                self._cc_network_choice.set(ssid if ssid in networks
                                            else networks[0])

        processes = data.get("processes")
        if processes:
            self._cc_render_processes(processes)
        # Panels are written by the agent and by the researcher from other
        # threads, so the page has to notice changes rather than be told.
        self._cc_refresh_panels()

    def _cc_note_recent(self, line):
        """Keep the last few things that happened, newest first."""
        widget = getattr(self, "_cc_recent", None)
        if widget is None:
            return
        text = user_activity_text(line)
        if not text:
            return
        # Consecutive identical receipts are one event, not evidence of more
        # work. This also makes retries visible by their changed result rather
        # than by a wall of identical lines.
        payloads = getattr(self, "_cc_recent_payloads", [])
        if payloads and payloads[0] == text:
            return
        payloads.insert(0, text)
        self._cc_recent_payloads = payloads[:8]
        history = getattr(self, "_cc_recent_lines", [])
        history.insert(0, f"{time.strftime('%H:%M')}  {text[:90]}")
        self._cc_recent_lines = history[:8]
        widget.configure(state="normal")
        widget.delete("0.0", tk.END)
        widget.insert("0.0", "\n".join(self._cc_recent_lines))
        widget.configure(state="disabled")

    def _cc_set_state(self, state):
        """Mirror the orb's state in the rail footer."""
        dot = getattr(self, "_cc_state_dot", None)
        if dot is None:
            return
        dot.itemconfigure(self._cc_state_dot_id,
                          fill=theme.ACCENTS.get(state, LABEL_TERTIARY))
        # The orb shows nothing at all when idle; in a row labelled with a
        # status dot, "Ready" is the word that belongs there.
        self.control_status.configure(
            text="Ready" if state == theme.STATE_IDLE
            else theme.STATE_LABELS.get(state, str(state).title()))
