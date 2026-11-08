# -*- coding: utf-8 -*-
"""Authenticate the primary user before VANTA starts any heavy service.

This process loads only dotenv, Tk, the camera and the local face recogniser.
It never imports the agent, Neo4j, an LLM, speech, schedulers or background
workers.  Success creates a short-lived signed ticket consumed once by the
normal process; closing/aborting exits without starting anything else.
"""
from __future__ import annotations

import base64
from io import BytesIO
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import window_identity

# Claim the application identity before Tk registers its first native HWND.
# More importantly, the fullscreen verification root is marked as a passive
# surface below so startup never leaves a transient Python/VANTA taskbar item.
window_identity.configure_process()

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

BG = "#000000"
SURFACE = "#000000"
TEXT = "#f5f5f7"
MUTED = "#98989d"
ACCENT = "#0a84ff"
SUCCESS = "#30d158"
RING = "#2c2c2e"
SIZE = 244


def _enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _face_crop(source, box, size=SIZE):
    width, height = source.size
    try:
        x1, y1, x2, y2 = [float(value) for value in box]
        if max(x1, y1, x2, y2) <= 1.01:
            x1, x2 = x1 * width, x2 * width
            y1, y2 = y1 * height, y2 * height
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        side = max(x2 - x1, y2 - y1) * 1.85
    except Exception:
        cx, cy, side = width / 2.0, height / 2.0, min(width, height)
    side = max(80.0, min(float(max(width, height)), side))
    left = max(0.0, min(width - side, cx - side / 2.0))
    top = max(0.0, min(height - side, cy - side / 2.0))
    right, bottom = min(width, left + side), min(height, top + side)
    crop = source.crop((int(left), int(top), int(right), int(bottom)))
    return crop.resize((size, size), Image.Resampling.LANCZOS)


class PreflightWindow:
    def __init__(self):
        _enable_dpi_awareness()
        self.events = queue.Queue()
        self.accepted = False
        self.photo = None
        self.closed = False

        self.root = tk.Tk()
        self.root.withdraw()
        window_identity.apply_to(self.root, base_dir=ROOT)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        window_identity.hide_from_taskbar(self.root)
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        self.root.geometry(f"{width}x{height}+0+0")

        stage = tk.Frame(self.root, bg=BG, width=340, height=410)
        stage.place(relx=.5, rely=.5, anchor="center")
        stage.pack_propagate(False)

        self.title = tk.Label(
            stage, text="Verifying", bg=BG, fg=TEXT,
            font=("Segoe UI Variable Display", 18, "bold"))
        self.title.pack(pady=(2, 16))

        self.canvas = tk.Canvas(
            stage, width=SIZE + 24, height=SIZE + 24, bg=BG,
            highlightthickness=0, bd=0)
        self.canvas.pack(pady=(0, 8))
        self._empty_circle()

        self.percent = tk.Label(
            stage, text="0%", bg=BG, fg=TEXT,
            font=("Segoe UI Variable Display", 16, "bold"))
        self.percent.pack()
        self.message = tk.Label(
            stage, text="Center your face", bg=BG, fg=MUTED,
            font=("Segoe UI Variable Text", 11))
        self.message.pack(pady=(0, 10))
        abort = tk.Button(
            stage, text="Cancel", command=self.abort,
            bg=BG, fg=MUTED, activebackground=BG,
            activeforeground=TEXT, relief="flat", bd=0, padx=18, pady=4,
            font=("Segoe UI Variable Text", 10))
        abort.pack()

        self.root.bind("<Escape>", lambda _event: self.abort())
        self.root.protocol("WM_DELETE_WINDOW", self.abort)
        self.root.after(25, self._poll)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _empty_circle(self):
        self.canvas.delete("all")
        self.canvas.create_oval(
            3, 3, SIZE + 21, SIZE + 21,
            fill=BG, outline="#111111", width=1)
        self.canvas.create_oval(
            12, 12, SIZE + 12, SIZE + 12,
            fill=SURFACE, outline=RING, width=2)

    def _frame(self, payload):
        encoded = payload.get("image")
        if encoded:
            source = Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
            image = _face_crop(source, payload.get("box") or ())
            mask = Image.new("L", (SIZE, SIZE), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, SIZE - 1, SIZE - 1), fill=255)
            composed = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
            composed.paste(image.convert("RGBA"), (0, 0), mask)
            self.photo = ImageTk.PhotoImage(composed)
            self.canvas.delete("all")
            centre = (SIZE + 24) // 2
            self.canvas.create_image(centre, centre, image=self.photo)
            progress = max(0, min(100, int(payload.get("percent") or 0)))
            colour = SUCCESS if payload.get("matched") else ACCENT
            self.canvas.create_oval(
                12, 12, SIZE + 12, SIZE + 12,
                outline=RING, width=2)
            self.canvas.create_arc(
                12, 12, SIZE + 12, SIZE + 12, start=90,
                extent=-(progress * 3.6), style="arc",
                outline=colour, width=4)
            self.canvas.create_oval(
                7, 7, SIZE + 17, SIZE + 17,
                outline="#111111", width=1)
            self.title.configure(
                text="Verified" if payload.get("matched") else "Verifying")
        self.percent.configure(text=f"{int(payload.get('percent') or 0)}%")
        self.message.configure(
            text=str(payload.get("message") or "Center your face"))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                payload = payload if isinstance(payload, dict) else {}
                if kind == "identity_gate_frame":
                    self._frame(payload)
                elif kind == "identity_gate_status":
                    self.percent.configure(
                        text=f"{int(payload.get('percent') or 0)}%")
                    self.message.configure(
                        text=str(payload.get("message") or "Verifying"))
                elif kind == "identity_gate_hide":
                    import face_identity
                    face_identity.issue_startup_ticket()
                    self.accepted = True
                    self.close()
                    return
        except queue.Empty:
            pass
        except Exception as exc:
            self.message.configure(text="Verification could not continue")
            print(f"[Preflight] UI update failed: {type(exc).__name__}: {exc}")
        self.root.after(25, self._poll)

    def abort(self):
        try:
            import authentication_gate
            authentication_gate.abort()
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        import authentication_gate
        import face_identity

        state = face_identity.boot_cache_status()
        if not state["ready"]:
            self.message.configure(text="Startup identity is not prepared")
            print(f"[Preflight] Boot identity unavailable: {state['error']}")
            self.root.mainloop()
            return False

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        remote_ready = bool(token and os.getenv("AUTH_PASSWORD", "").strip())
        authentication_gate.configure(
            str(ROOT), self.events, boot_only=True,
            allow_remote=remote_ready)
        if remote_ready:
            # This worker is still part of the tiny authentication process. It
            # may consume only the paired chat's password while the lifecycle
            # gate is locked; Neo4j, the agent and normal tasks remain absent.
            from providers.telegram_bot import start_telegram_worker
            start_telegram_worker(token, queue.Queue())
        threading.Thread(
            target=authentication_gate.run, args=("startup preflight",),
            daemon=True, name="vanta-preflight-face").start()
        self.root.mainloop()
        return self.accepted


def main():
    # A ticket is valid for one launch attempt, not one successful process.
    # Remove anything left by a prior crash before asking for fresh evidence.
    import face_identity
    face_identity.discard_startup_ticket()
    print("[Preflight] Verify identity before VANTA services start.")
    return 0 if PreflightWindow().run() else 2


if __name__ == "__main__":
    raise SystemExit(main())
