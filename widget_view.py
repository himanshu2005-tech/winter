"""Small taskbar-free widgets rendered inside the Tk UI process."""
from __future__ import annotations

import time
import tkinter as tk
import customtkinter as ctk

import window_identity
from theme import BLUE, GREEN, LABEL_PRIMARY, LABEL_SECONDARY


def close(owner, widget_id=""):
    win = getattr(owner, "_widget_win", None)
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass
    owner._widget_win = None
    owner._widget_id = ""


def _label(parent, text, size=13, colour=LABEL_PRIMARY, bold=False, **pack):
    widget = ctk.CTkLabel(
        parent, text=str(text), anchor="w", justify="left", wraplength=350,
        text_color=colour,
        font=ctk.CTkFont(family=getattr(parent.winfo_toplevel(), "_vanta_font", "Segoe UI"),
                         size=size, weight="bold" if bold else "normal"))
    widget.pack(fill="x", **pack)
    return widget


def show(owner, raw_spec):
    from widget_spec import normalize
    spec = normalize(raw_spec)
    close(owner)
    win = ctk.CTkToplevel(owner.root, fg_color="#0b0c0f")
    owner._widget_win = win
    owner._widget_id = spec["id"]
    win._vanta_font = getattr(owner, "FONT_TEXT", "Segoe UI")
    win.withdraw()
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    window_identity.hide_from_taskbar(win, owner.root)
    width = 410
    height = min(520, max(178, 138 + _content_height(spec)))
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{width}x{height}+{max(12, sw-width-28)}+{max(70, (sh-height)//2)}")

    shell = ctk.CTkFrame(win, fg_color="#101216", corner_radius=22,
                         border_width=1, border_color="#2b2f36")
    shell.pack(fill="both", expand=True)
    header = ctk.CTkFrame(shell, fg_color="transparent", height=50)
    header.pack(fill="x", padx=18, pady=(12, 2))
    title = ctk.CTkLabel(header, text=spec["title"], anchor="w",
                         font=ctk.CTkFont(family=owner.FONT_DISPLAY, size=15,
                                          weight="bold"),
                         text_color="#f5f5f7")
    title.pack(side="left", fill="x", expand=True)
    ctk.CTkButton(header, text="–", width=30, height=28, corner_radius=14,
                  fg_color="transparent", hover_color="#24272d",
                  command=win.withdraw).pack(side="right", padx=(4, 0))
    ctk.CTkButton(header, text="×", width=30, height=28, corner_radius=14,
                  fg_color="transparent", hover_color="#3a2023",
                  command=lambda: close(owner)).pack(side="right", padx=(4, 0))
    body = ctk.CTkFrame(shell, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=20, pady=(4, 18))
    if spec.get("subtitle"):
        _label(body, spec["subtitle"], 11, LABEL_SECONDARY, pady=(0, 10))
    _render(body, spec, owner)

    drag = {"x": 0, "y": 0}
    def start(event):
        drag.update(x=event.x_root - win.winfo_x(), y=event.y_root - win.winfo_y())
    def move(event):
        win.geometry(f"+{event.x_root-drag['x']}+{event.y_root-drag['y']}")
    for target in (header, title):
        target.bind("<ButtonPress-1>", start)
        target.bind("<B1-Motion>", move)
    win.deiconify()
    win.lift()
    return spec


def _content_height(spec):
    kind = spec["kind"]
    if kind in {"list", "checklist", "timeline", "flow"}:
        return 32 * len(spec.get("items", [])) + 24
    if kind == "table":
        return 34 * (len(spec.get("rows", [])) + 1) + 16
    if kind == "bar_chart":
        return 220
    return 80


def _render(parent, spec, owner):
    kind = spec["kind"]
    if kind == "timer":
        value = _label(parent, "", 38, "#f5f5f7", True, pady=(8, 3))
        note = _label(parent, spec.get("value") or "Remaining", 12,
                      LABEL_SECONDARY)
        def tick():
            if not value.winfo_exists():
                return
            left = max(0, int(spec["deadline"] - time.time()))
            hours, rem = divmod(left, 3600)
            minutes, seconds = divmod(rem, 60)
            value.configure(text=(f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                                  if hours else f"{minutes:02d}:{seconds:02d}"),
                            text_color=GREEN if left == 0 else "#f5f5f7")
            if left:
                value.after(250 if left < 10 else 1000, tick)
            else:
                note.configure(text="Complete")
        tick()
    elif kind in {"metric", "status"}:
        _label(parent, spec.get("value") or "—", 34, "#f5f5f7", True,
               pady=(10, 2))
        if spec.get("unit"):
            _label(parent, spec["unit"], 12, LABEL_SECONDARY)
    elif kind == "progress":
        _label(parent, spec.get("value") or f"{spec['progress']:.0%}", 28,
               "#f5f5f7", True, pady=(8, 8))
        bar = ctk.CTkProgressBar(parent, height=8, progress_color=BLUE,
                                 fg_color="#282b31")
        bar.pack(fill="x", pady=(0, 4)); bar.set(spec["progress"])
    elif kind in {"list", "checklist", "timeline", "flow"}:
        for index, item in enumerate(spec.get("items", [])):
            prefix = ("✓" if item["done"] else "○") if kind == "checklist" \
                     else (f"{index+1}." if kind in {"timeline", "flow"} else "•")
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=prefix, width=24, anchor="nw",
                         text_color=GREEN if item["done"] else BLUE).pack(side="left")
            text = item["label"] + (("\n" + item["detail"]) if item["detail"] else "")
            ctk.CTkLabel(row, text=text, anchor="w", justify="left",
                         wraplength=330, text_color=LABEL_PRIMARY).pack(
                             side="left", fill="x", expand=True)
    elif kind == "table":
        grid = ctk.CTkFrame(parent, fg_color="#17191e", corner_radius=10)
        grid.pack(fill="both", expand=True)
        rows = [spec.get("columns", [])] + spec.get("rows", [])
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                label = ctk.CTkLabel(grid, text=cell, anchor="w",
                                     text_color="#f5f5f7" if r == 0 else LABEL_PRIMARY,
                                     font=ctk.CTkFont(size=11, weight="bold" if r == 0 else "normal"))
                label.grid(row=r, column=c, sticky="nsew", padx=7, pady=4)
                grid.grid_columnconfigure(c, weight=1)
    elif kind == "bar_chart":
        canvas = tk.Canvas(parent, height=205, bg="#101216", bd=0,
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        series = spec.get("series", [])
        maximum = max([abs(item["value"]) for item in series] or [1])
        for i, item in enumerate(series):
            y = 12 + i * max(24, 180 // max(1, len(series)))
            canvas.create_text(4, y + 8, text=item["label"][:18], anchor="w",
                               fill="#d7d7da", font=("Segoe UI", 9))
            width = int(210 * abs(item["value"]) / maximum)
            canvas.create_rectangle(125, y, 125 + width, y + 16,
                                    fill=BLUE, outline="")
            canvas.create_text(132 + width, y + 8, text=f"{item['value']:g}",
                               anchor="w", fill="#aeb0b5", font=("Segoe UI", 9))
