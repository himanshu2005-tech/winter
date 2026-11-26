# -*- coding: utf-8 -*-
r"""
action_attrs.py -- reading the attributes of an ACTION tag without losing half.

Attributes were parsed with (\w+)="([^"]+)", which stops dead at the first quote
inside a value. So a PowerShell command written the way PowerShell needs it:

    <ACTION type="RUN_COMMAND" cmd="powershell -Command \"$keys = @(...)\"" />

parsed as cmd='powershell -Command \' and everything after the escaped quote was
discarded. The approved command that finally ran was a lone backslash, and the
console reported:

    \ : The term '\' is not recognized as the name of a cmdlet

The model was not wrong to escape its quotes. The parser was wrong not to read
them, and the failure looked like a model mistake, which is the expensive kind of
bug: it sends you correcting the wrong thing.

Single-quoted attributes are accepted too. A command full of double quotes is far
easier to write inside single ones, and there was never a reason to refuse it.
"""

import re

_ESCAPED_DOUBLE = r'"((?:[^"\\]|\\.)*)"'
_ESCAPED_SINGLE = r"'((?:[^'\\]|\\.)*)'"
_PAIR = re.compile(r'(\w+)\s*=\s*(?:' + _ESCAPED_DOUBLE + r'|' + _ESCAPED_SINGLE + r')')

# Only the escapes that exist to survive the attribute are undone. \n and \t are
# left alone: a command may legitimately contain them, and quietly rewriting a
# command before running it would be its own bug.
_UNESCAPE = ((r'\"', '"'), (r"\'", "'"), (r'\\', '\\'))


def parse(action_str):
    """The attribute pairs in an ACTION tag body, as a dict."""
    found = {}
    for match in _PAIR.finditer(str(action_str or "")):
        name = match.group(1)
        raw = match.group(2) if match.group(2) is not None else match.group(3)
        if raw is None:
            continue
        value = raw
        for escaped, plain in _UNESCAPE:
            value = value.replace(escaped, plain)
        found[name] = value
    return found
