# -*- coding: utf-8 -*-
"""
notifications.py — real Windows toasts.

NOTIFY previously wrote a line to VANTA's own dashboard and a JSON file, which
only works if Himanshu happens to be looking at VANTA. A background assistant
that cannot reach him when his attention is elsewhere is not doing the job.

This raises an actual Windows notification through the built-in
Windows.UI.Notifications API, driven from PowerShell so nothing has to be
installed. Toasts land in Action Centre, survive being missed, and respect
Focus Assist — which is correct: if Windows is suppressing notifications,
VANTA should be suppressed too rather than talking over a presentation.
"""

import html
import os
import subprocess

# A registered AppUserModelID is required for a toast to be attributed and to
# persist in Action Centre. PowerShell's own ID is always present on Windows,
# so it works without installing or registering anything.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TEMPLATE = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] > $null
$xml = @"
<toast activationType="foreground" scenario="{scenario}">
  <visual><binding template="ToastGeneric">
    <text>{title}</text>
    <text>{body}</text>
  </binding></visual>
  {audio}
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)
"""


def _escape(text, limit):
    cleaned = " ".join(str(text or "").split())[:limit]
    # Both XML-escaped for the toast payload and quote-stripped so the value
    # cannot terminate the PowerShell here-string that carries it.
    return html.escape(cleaned, quote=True).replace('"@', "").replace("`", "")


def notify(title, body="", urgent=False, silent=False):
    """Raise a Windows toast. Returns an action-contract string."""
    title = _escape(title or "VANTA", 90) or "VANTA"
    body = _escape(body, 250)

    # Sleep and Study are attention contracts. Routine notifications are
    # retained locally and delivered once when the mode ends, rather than
    # appearing as a toast now and then being repeated in the catch-up.
    try:
        import mode_inbox
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if mode_inbox.record(base_dir, "notification", title, body):
            if not urgent:
                return f"[Success] Held for mode catch-up: {title}"
    except Exception as exc:
        print(f"[Notifications] Could not use mode inbox: {exc}")

    script = _TEMPLATE.format(
        title=title,
        body=body,
        app_id=_APP_ID,
        # A reminder-scenario toast stays on screen until dismissed, which is
        # right for something time-critical and wrong for everything else.
        scenario="reminder" if urgent else "default",
        audio='<audio silent="true"/>' if silent else
              '<audio src="ms-winsoundevent:Notification.Looping.Alarm2" loop="false"/>' if urgent else "",
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "[Error] The notification service did not respond."
    except Exception as exc:
        return f"[Error] Could not raise a notification: {exc}"

    if result.returncode != 0:
        detail = " ".join((result.stderr or "").split())[:160]
        return f"[Error] Windows refused the notification: {detail or 'unknown reason'}"
    return f"[Success] Notified: {title}" + (f" — {body[:60]}" if body else "")
