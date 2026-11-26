# -*- coding: utf-8 -*-
"""
hud.py -- the card that stays on screen while he works.

A small always-on-top panel VANTA can put a checklist, a note or a figure on,
and change while he is in the middle of something: "I'm studying" puts the list
up, "finished the derivatives one" ticks it, and the list stays visible over
whatever else is on the display.

WHY A FOURTH SURFACE
--------------------
There were already three and none of them fit. The blackboard takes the whole
screen, which is right for teaching a subject and wrong for a list that has to
sit beside the work. Control Center panels live inside a window he has to open,
so they are invisible exactly when they matter. The notch shows one line.

HOW IT BEHAVES, AND WHY
-----------------------
*Topmost, and it never takes focus.* It appears over the work rather than
beside it, and a panel that stole the caret while he was typing an answer would
be removed within the hour.

*Clickable, not click-through.* The annotation overlay is click-through because
a full-screen window that swallows the pointer is indistinguishable from a
hang. This one is small and in a corner, and being able to tick an item with
the mouse is worth more than the few pixels it costs.

*It renders from hud_state and holds no state of its own.* The interesting
failures here are logical -- ticking the wrong line, losing the list on a
restart -- and they are tested in hud_state without a screen. This file is only
pixels, which is the part that cannot be tested headlessly and so is kept as
thin as possible.
"""

import tkinter as tk

import hud_state
import theme
import window_identity

#: Where it sits. Top-right keeps it clear of the taskbar and of the notch.
MARGIN = 28
WIDTH = 330


def fitted_height(requested, screen_height, margin=MARGIN):
    """Content height constrained to the visible desktop.

    Kept pure so the geometry rule is testable without creating a window.
    """
    available = max(72, int(screen_height) - (2 * int(margin)))
    return max(72, min(int(requested), available))


def placement(screen_w, screen_h, content_h, scale=1.0,
              margin=MARGIN, width=WIDTH):
    """(logical_width, logical_height, x, y) that keeps the window on screen.

    CustomTkinter multiplies a window's width and height by the DPI scale but
    leaves its x/y alone. At 125% a 330px overlay is really 412px wide, so a
    position worked out from 330 put its right edge 82px off the screen --
    the "cut off on the right" the map window showed. Position is therefore
    computed from the physical width, and the size handed back is logical so
    CustomTkinter's own scaling lands on the intended pixels.
    """
    scale = max(0.5, float(scale or 1.0))
    physical_w = int(round(width * scale))
    # content_h is a measured (physical) height; constrain it, then convert.
    physical_h = fitted_height(content_h, screen_h, margin)
    x = max(0, int(screen_w) - physical_w - int(margin))
    return (width, max(72, int(physical_h / scale)), x, int(margin))


class HudMixin:
    """Mixed into the UI. Owns one overlay window, rebuilt from state."""

    def _hud_colors(self):
        return {
            "bg": getattr(theme, "SURFACE_DEEP", "#0f0f11"),
            "line": getattr(theme, "SEPARATOR", "#2e2e31"),
            "text": getattr(theme, "LABEL_PRIMARY", "#f5f5f7"),
            "dim": getattr(theme, "LABEL_SECONDARY", "#98989d"),
            "done": getattr(theme, "GREEN", "#30d158"),
        }

    def _ensure_hud(self):
        import customtkinter as ctk

        existing = getattr(self, "_hud_win", None)
        if existing is not None and existing.winfo_exists():
            return existing

        colors = self._hud_colors()
        win = ctk.CTkToplevel(self.root, fg_color=colors["bg"])
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        try:
            # Never steal the caret. He is usually typing when this appears.
            win.attributes("-alpha", 0.96)
        except Exception:
            pass

        self._hud_frame = ctk.CTkFrame(
            win, corner_radius=16, fg_color=colors["bg"],
            border_width=1, border_color=colors["line"])
        self._hud_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._hud_win = win
        try:
            # So VANTA's own vision does not mistake this for the app he is
            # working in when it looks at the screen.
            import win32gui
            from vision import register_winter_hwnd
            register_winter_hwnd(win32gui.GetParent(win.winfo_id())
                                 or win.winfo_id())
        except Exception:
            pass
        return win

    def _hud_refresh(self, _payload=None):
        """Rebuild the overlay from hud_state. Safe to call on every change."""
        cards = hud_state.cards()
        if not cards:
            win = getattr(self, "_hud_win", None)
            if win is not None and win.winfo_exists():
                win.withdraw()
            return
        try:
            import customtkinter as ctk
            win = self._ensure_hud()
        except Exception as exc:
            print(f"[HUD] Could not build the overlay ({exc}); the list is "
                  f"still tracked and visible in the Control Center.")
            return

        colors = self._hud_colors()
        for child in self._hud_frame.winfo_children():
            child.destroy()

        for card in cards:
            title = ctk.CTkLabel(
                self._hud_frame, text=card["title"], anchor="w",
                font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="bold"),
                text_color=colors["text"], wraplength=WIDTH - 40)
            title.pack(fill=tk.X, padx=14, pady=(12, 4))
            if card.get("kind") == "route":
                import route_render
                box_h = 190
                canvas = tk.Canvas(self._hud_frame, height=box_h,
                                   bg=colors["bg"], highlightthickness=0, bd=0)
                canvas.pack(fill=tk.X, padx=12, pady=(0, 2))
                # Drawn after layout, or the canvas reports width 1 and the
                # whole route lands in a single pixel column.
                canvas.after(60, lambda c=canvas, r=card.get("payload"),
                             h=box_h: self.draw_route(
                                 c, r, max(80, c.winfo_width()), h))
                caption = route_render.describe_all(card.get("payload") or {})
                if caption:
                    ctk.CTkLabel(
                        self._hud_frame, text=caption, anchor="w",
                        wraplength=WIDTH - 36,
                        font=ctk.CTkFont(family=self.FONT_TEXT, size=11),
                        text_color=colors["dim"]).pack(fill=tk.X, padx=16,
                                                       pady=(0, 10))
                continue

            for index, item in enumerate(card["items"]):
                done = bool(item["done"])
                line = ctk.CTkLabel(
                    self._hud_frame,
                    text=("✓  " if done else "○  ") + item["text"],
                    anchor="w", justify="left", wraplength=WIDTH - 44,
                    font=ctk.CTkFont(family=self.FONT_TEXT, size=12,
                                     overstrike=done),
                    text_color=colors["done"] if done else colors["dim"])
                line.pack(fill=tk.X, padx=18, pady=1)
                # Ticking with the mouse, for when speaking is not convenient.
                line.bind("<Button-1>",
                          lambda _event, c=card["id"], t=item["text"],
                          state=done: self._hud_toggle(c, t, not state))
        # Estimate-by-row clipped every wrapped item: a three-line sentence
        # was counted as one 26px row. Ask Tk for the real laid-out height
        # after wrapping, then make the window fit that content exactly.
        win.update_idletasks()
        screen_w = getattr(self, "SW", 0) or win.winfo_screenwidth()
        screen_h = getattr(self, "SH", 0) or win.winfo_screenheight()
        try:
            from customtkinter.windows.widgets.scaling import ScalingTracker
            scale = ScalingTracker.get_window_scaling(win)
        except Exception:
            scale = 1.0
        w, h, x, y = placement(screen_w, screen_h,
                               self._hud_frame.winfo_reqheight() + 8, scale)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)

    def draw_route(self, canvas, route, width, height):
        """Draw a route onto a Tk canvas. Shared by the overlay and the panel.

        One function so the small on-screen card and the large Control Center
        panel cannot drift apart: the same geometry, the same congestion
        colours, only a different box. All the arithmetic is in route_render,
        which is tested without a display -- this places lines where it is
        told and nothing more.
        """
        import route_render

        canvas.delete("all")
        points = route_render.coordinates_of(route)
        if len(points) < 2:
            canvas.create_text(width // 2, height // 2,
                               text="no route geometry",
                               fill=self._hud_colors()["dim"])
            return 0

        routes = route_render.all_routes(route)
        placed_all = route_render.project_all(routes, width, height)
        colors = self._hud_colors()
        # Alternatives first so the recommended route lies on top of them.
        for index in range(len(routes) - 1, 0, -1):
            line = placed_all[index]
            if len(line) < 2:
                continue
            flat = [value for point in line for value in point]
            canvas.create_line(*flat, fill="#0b0f14", width=7,
                               capstyle="round", joinstyle="round")
            canvas.create_line(*flat, fill=route_render.route_color(index),
                               width=4, capstyle="round", joinstyle="round")
        placed = placed_all[0]
        pairs = route_render.segments(
            placed, routes[0].get("congestion_levels_per_segment")
            or routes[0].get("congestion"))
        flat = [value for point in placed for value in point]
        canvas.create_line(*flat, fill="#0b0f14", width=7,
                           capstyle="round", joinstyle="round", smooth=True)
        for start, end, colour in pairs:
            canvas.create_line(start[0], start[1], end[0], end[1],
                               fill=colour, width=4, capstyle="round")
        if len(routes) > 1:
            for index, line in enumerate(placed_all):
                if len(line) < 2:
                    continue
                x, y = route_render.label_anchor(line)
                tag = route_render.route_label(index)
                minutes = routes[index].get("duration_minutes")
                text = f"{tag} {minutes}m" if minutes else tag
                canvas.create_rectangle(
                    x - 22, y - 9, x + 22, y + 9, fill="#0b0f14",
                    outline=route_render.route_color(index))
                canvas.create_text(x, y, text=text, fill="#f5f5f7",
                                   font=("Segoe UI", 8, "bold"))
        canvas.create_oval(placed[0][0] - 5, placed[0][1] - 5,
                           placed[0][0] + 5, placed[0][1] + 5,
                           fill=colors["done"], outline="#0b0f14", width=2)
        canvas.create_oval(placed[-1][0] - 5, placed[-1][1] - 5,
                           placed[-1][0] + 5, placed[-1][1] + 5,
                           fill="#ef4444", outline="#0b0f14", width=2)
        return len(routes)

    def _hud_toggle(self, card_id, text, done):
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        hud_state.check(base, text, done=done, card_id=card_id)
        self._hud_refresh()

    def _hud_hide(self, _payload=None):
        win = getattr(self, "_hud_win", None)
        if win is not None and win.winfo_exists():
            win.withdraw()
