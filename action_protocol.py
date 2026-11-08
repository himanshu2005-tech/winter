"""Parse action envelopes in source order and keep each payload with its call."""
from dataclasses import dataclass
import re

import action_attrs


_TOKEN = re.compile(
    r'''<\s*(?P<close>/\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b'''
    r'''(?P<attrs>(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^'">])*)>''',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ActionEnvelope:
    attributes: str
    text: str
    body: str
    start: int
    end: int


def extract(text, bare_names=()):
    """Quote-aware envelope extraction; nested examples never become calls.

    The registry supplies accepted bare-tag names. Unknown ACTION names remain
    visible so the existing authorization/schema boundary can explain them.
    """
    text = str(text or '')
    bare = set(bare_names)
    tokens = list(_TOKEN.finditer(text))
    result, consumed = [], 0
    for index, token in enumerate(tokens):
        if token.start() < consumed or token.group('close'):
            continue
        name = token.group('name').upper()
        if name != 'ACTION' and name not in bare:
            continue
        attrs = token.group('attrs').strip()
        self_closing = attrs.endswith('/')
        attrs = attrs[:-1].rstrip() if self_closing else attrs
        if name != 'ACTION':
            attrs = f'type="{name}" ' + attrs
        if not action_attrs.parse(attrs).get('type'):
            continue
        if self_closing:
            fragment = (text[token.start():token.end()] if name == 'ACTION'
                        else f'<ACTION {attrs} />')
            result.append(ActionEnvelope(attrs, fragment,
                                         '', token.start(), token.end()))
            consumed = token.end()
            continue
        depth, closing = 1, None
        for other in tokens[index+1:]:
            if other.group('name').upper() != token.group('name').upper():
                continue
            if other.group('close'):
                depth -= 1
                if depth == 0:
                    closing = other
                    break
            elif not other.group('attrs').rstrip().endswith('/'):
                depth += 1
        if closing is None:
            continue
        body = text[token.end():closing.start()].strip()
        fragment = (text[token.start():closing.end()] if name == 'ACTION'
                    else f'<ACTION {attrs}>{body}</ACTION>')
        result.append(ActionEnvelope(
            attrs, fragment, body, token.start(), closing.end()))
        consumed = closing.end()
    return result
