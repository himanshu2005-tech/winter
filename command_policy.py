# -*- coding: utf-8 -*-
"""Risk classification for VANTA's terminal-first execution path.

Ordinary shell work is immediate. The command, not the fact that a shell is
used, determines whether a checkpoint is needed.
"""

import re


_HIGH_IMPACT = (
    (r"\b(?:remove-item|del|erase|rd|rmdir)\b[^\r\n]*(?:-recurse|-force|/s|/q)",
     "it recursively or forcibly deletes files"),
    # `format` is destructive, while PowerShell's `Format-Table` and
    # `Format-List` are presentation-only. Rejecting a hyphenated suffix keeps
    # the latter out without missing `cmd /c format D:`.
    (r"\bformat(?:\.com)?\b(?!-)|"
     r"\b(?:clear-disk|initialize-disk|remove-partition|diskpart)\b",
     "it can modify or erase a disk"),
    (r"\b(?:stop-computer|restart-computer|shutdown(?:\.exe)?)\b",
     "it powers off or restarts the computer"),
    (r"\bgit\s+(?:reset\s+--hard|clean\s+-[^\s]*f|push\s+[^\r\n]*--force)\b",
     "it can discard or overwrite repository history or files"),
    (r"\b(?:drop\s+(?:database|schema|table)|truncate\s+table)\b",
     "it destructively changes database data"),
    (r"\breg(?:\.exe)?\s+delete\b|\bremove-itemproperty\b",
     "it deletes registry state"),
    (r"\b(?:mimikatz|secretsdump|procdump[^\r\n]*lsass|comsvcs[^\r\n]*minidump)\b",
     "it accesses reusable credentials or authentication memory"),
    (r"\b(?:curl|scp|sftp|rsync)\b[^\r\n]*(?:--upload-file|-t\s|\s[^\s]+@[^:]+:)",
     "it uploads local data to another system"),
    (r"\binvoke-restmethod\b[^\r\n]*(?:-method\s+(?:post|put|patch)|-infile\b)|"
     r"\binvoke-webrequest\b[^\r\n]*-infile\b",
     "it transmits data outside this computer"),
)


def confirmation_reason(command):
    """Return why *command* needs approval, or ``""`` when it may run now."""
    text = str(command or "")
    for pattern, reason in _HIGH_IMPACT:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return ""
