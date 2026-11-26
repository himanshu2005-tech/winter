# -*- coding: utf-8 -*-
"""
approval_prompt.py -- two buttons, so an approval can actually be given.

Approvals were queued into SQLite and announced with "check dashboard or say
'approve that'". There was no dashboard control and no button, so in practice
the only route was saying a specific phrase -- and if Himanshu did not know the
phrase, the action simply sat there.

What that cost, from one real session: VANTA queued a read-only SEB check, said
it was waiting, retried it, was failed by its own critic, retried again, and
then tried to get the same result through RUN_PYTHON instead. Eleven model calls
and 71,508 prompt tokens, because a yes had nowhere to go.

So: a small window, the action it wants to run, and Approve / Decline. Clicking
pushes the same "approve" / "reject" text the voice path already used, so this
adds a way in rather than a second implementation of the decision -- the queue,
the replay and the audit trail stay exactly as they were.

It is deliberately NOT click-through and NOT auto-dismissing. A permission
prompt that can be clicked past by accident, or that disappears on its own, is
worse than none: both turn "he approved it" into something nobody can verify.
"""

import tkinter as tk

import theme
import window_identity


def _short(value, limit=120):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe(action_type, payload):
    """One line saying what is about to happen, in Himanshu's terms.

    The payload is shown rather than summarised away: approving a command
    without seeing the command is not consent to anything in particular.
    """
    payload = payload if isinstance(payload, dict) else {}
    for key in ("cmd", "command", "path", "to", "query", "url", "text", "summary"):
        if payload.get(key):
            return f"{action_type} — {_short(payload[key])}"
    if payload:
        first = next(iter(payload.items()))
        return f"{action_type} — {first[0]}={_short(first[1], 80)}"
    return action_type


class ApprovalPromptMixin:
    """Mixed into the UI. Owns one prompt window, reused for each request."""

    def _approval_colors(self):
        return {
            "surface": getattr(theme, "SURFACE_DEEP", "#1b2027"),
            "border": getattr(theme, "SEPARATOR", "#2a333c"),
            "text": getattr(theme, "LABEL_PRIMARY", "#e9edf1"),
            "dim": getattr(theme, "LABEL_SECONDARY", "#93a1ad"),
            "go": getattr(theme, "GREEN", "#30d158"),
            "no": getattr(theme, "RED", "#ff453a"),
        }

    def _build_approval_prompt(self):
        import customtkinter as ctk

        existing = getattr(self, "_approval_win", None)
        if existing is not None and existing.winfo_exists():
            return
        colors = self._approval_colors()

        win = ctk.CTkToplevel(self.root, fg_color=colors["surface"])
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        self._approval_win = win
        self._centre_approval_prompt()

        frame = ctk.CTkFrame(win, corner_radius=18, fg_color=colors["surface"],
                             border_width=1, border_color=colors["border"])
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._approval_title = ctk.CTkLabel(
            frame, text="Approval needed", anchor="w",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=13, weight="bold"),
            text_color=colors["text"])
        self._approval_title.pack(fill=tk.X, padx=16, pady=(12, 2))

        self._approval_detail = ctk.CTkLabel(
            frame, text="", anchor="w", justify="left", wraplength=self.APPROVAL_SIZE[0] - 60,
            font=ctk.CTkFont(family=self.FONT_MONO, size=11),
            text_color=colors["dim"])
        self._approval_detail.pack(fill=tk.X, padx=16, pady=(0, 10))

        # Voice and typed approval remain available, while the visible buttons
        # below use the same settlement path and audit trail.
        self._approval_hint = ctk.CTkLabel(
            frame, text="Use a button, say or type 'approved', or reply "
                        "'approved' on Telegram.",
            anchor="w", justify="left",
            font=ctk.CTkFont(family=self.FONT_TEXT, size=12),
            text_color=colors["go"])
        self._approval_hint.pack(fill=tk.X, padx=16, pady=(0, 14))

        # Voice remains available, but a visible permission request must also
        # be decidable where it is shown.  The earlier text-only prompt looked
        # actionable while providing no control, which made the pipeline feel
        # broken whenever speech recognition missed "approve".
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill=tk.X, padx=16, pady=(0, 14))
        self._approval_decline = ctk.CTkButton(
            buttons, text="Decline", height=32, corner_radius=10,
            fg_color=colors["border"], hover_color="#3a3f46",
            text_color=colors["text"], command=self._decline_pending)
        self._approval_decline.pack(side=tk.RIGHT)
        self._approval_approve = ctk.CTkButton(
            buttons, text="Approve", height=32, corner_radius=10,
            fg_color=colors["go"], hover_color="#28b84c",
            text_color="#07140b", command=self._approve_pending)
        self._approval_approve.pack(side=tk.RIGHT, padx=(0, 8))

    #: Wide enough for a real command, short enough not to dominate the screen.
    APPROVAL_SIZE = (560, 202)

    def _centre_approval_prompt(self):
        """Put the prompt in the middle of the screen, never off the edge.

        It sat bottom-right at first, on the reasoning that a prompt should stay
        out of the way. It went partly off the edge instead: those offsets
        assumed SW/SH described the usable area, which they do not once display
        scaling and the taskbar are involved. A permission prompt is the one
        window that must never be half-visible -- if it can be missed, the action
        it guards simply never happens. The centre needs no assumptions about
        where the edges are.
        """
        win = getattr(self, "_approval_win", None)
        if win is None or not win.winfo_exists():
            return
        width, height = self.APPROVAL_SIZE
        screen_w = getattr(self, "SW", 0) or win.winfo_screenwidth()
        screen_h = getattr(self, "SH", 0) or win.winfo_screenheight()
        x = max(0, (int(screen_w) - width) // 2)
        y = max(0, (int(screen_h) - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _show_approval_request(self, request):
        """Put the request on screen. request: {action_type, detail, id}."""
        request = request if isinstance(request, dict) else {}
        try:
            self._build_approval_prompt()
        except Exception as exc:
            # Falling back to the log is not good, but silently dropping a
            # permission request is worse -- that is how one sat unanswered
            # through eleven model calls.
            print(f"[Approval] Could not build the prompt ({exc}); "
                  f"say 'approve' or 'reject' instead.")
            return

        action_type = str(request.get("action_type") or "an action")
        detail = str(request.get("detail") or action_type)
        self._pending_approval_id = request.get("id")
        self._approval_title.configure(text=f"{action_type} is waiting for you")
        self._approval_detail.configure(text=detail)
        # Re-centred on every request, not just when the window was built: it is
        # built once and reused, so a position computed before the screen size
        # was known -- or before a display changed -- would persist for the rest
        # of the session.
        self._centre_approval_prompt()
        self._approval_win.deiconify()
        self._approval_win.lift()
        self._approval_win.attributes("-topmost", True)

    def _hide_approval_request(self):
        win = getattr(self, "_approval_win", None)
        if win is not None and win.winfo_exists():
            win.withdraw()
        self._pending_approval_id = None

    def _settle_approval(self, verdict):
        """Send the decision down the path the voice commands already use.

        Reusing "approve"/"reject" rather than executing from here keeps one
        implementation of what approval means, with one audit trail.
        """
        # Carry the displayed id with the decision. If a second request arrives
        # while this window is visible, the button must never approve "the
        # newest" action by accident; it approves exactly what it displayed.
        approval_id = self._pending_approval_id
        self._hide_approval_request()
        try:
            self._in_q.put(("approval", verdict, approval_id))
        except Exception as exc:
            print(f"[Approval] Could not deliver '{verdict}': {exc}")

    def _approve_pending(self):
        self._settle_approval("approve")

    def _decline_pending(self):
        self._settle_approval("reject")
