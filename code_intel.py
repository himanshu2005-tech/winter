# -*- coding: utf-8 -*-
"""
code_intel.py -- understanding a codebase by its structure, not by its bytes.

A coding model is only as good as what it is shown. Asked to change something
in a project, VANTA's options were to paste whole files into the prompt or to
guess from filenames: the first blows the context window on a repository of
any size and buries the relevant twenty lines in two thousand, and the second
edits code it has not read. Both produce the failure that has cost this
project most -- a confident change to the wrong place.

So the code is parsed instead of read. Python's own `ast` gives the exact
structure: every function and class, where it begins and ends, what it takes,
what it calls, what the module imports. From that come the four things a model
actually needs:

  a map        every symbol in the project, compact enough to fit in a prompt
  a location   the exact line range of a named symbol, for an edit that
               replaces a function rather than string-matching near it
  a neighbourhood  what a symbol calls and what calls it, which is what
               "related code" really means
  a context    the handful of symbol bodies relevant to a request, instead of
               whole files

WHY AST AND NOT SEARCH
----------------------
Text search finds the word `capture` in a comment, a docstring, a variable and
a function name, and cannot tell them apart. The parser knows which is a
definition and which is a call, and it knows where the definition ends -- so
an edit can replace exactly that function and nothing else. That difference is
the whole value: it turns "somewhere around here" into a line range.

SCOPE, HONESTLY
---------------
Python only. `ast` is the standard library's parser for Python and nothing
else; JavaScript, Go and the rest need tree-sitter, which is a separate
dependency and a separate piece of work. A non-Python file is reported as
unparsed rather than silently skipped, because a map that quietly omits half a
project is worse than one that says what it could not read.
"""

import ast
import os

#: Directories never worth indexing. A virtualenv is bigger than the project
#: and none of it is the project's own code.
SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules", "dist",
    "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
    "site-packages", ".next", "target", "captures", "weights",
})

#: Files above this are generated, vendored, or data. Parsing them costs more
#: than they inform.
MAX_FILE_BYTES = 400_000


class Symbol:
    """One definition, and where it lives.

    The line range is the point of this whole module: it is what lets an edit
    replace a function rather than search for text near it.
    """

    __slots__ = ("name", "kind", "path", "line", "end_line", "signature",
                 "doc", "parent", "calls", "decorators", "is_async")

    def __init__(self, name, kind, path, line, end_line, signature="",
                 doc="", parent="", calls=(), decorators=(), is_async=False):
        self.name = name
        self.kind = kind                  # "function", "class", "method"
        self.path = path
        self.line = int(line)
        self.end_line = int(end_line)
        self.signature = signature
        self.doc = doc
        self.parent = parent              # enclosing class, for a method
        self.calls = tuple(calls)
        self.decorators = tuple(decorators)
        self.is_async = bool(is_async)

    @property
    def qualified(self):
        return f"{self.parent}.{self.name}" if self.parent else self.name

    @property
    def span(self):
        return self.end_line - self.line + 1

    def header(self):
        """One line describing it, for a map the model reads."""
        prefix = "async def" if self.is_async else (
            "class" if self.kind == "class" else "def")
        marks = "".join(f"@{item} " for item in self.decorators[:2])
        return f"{marks}{prefix} {self.qualified}{self.signature}"

    def __repr__(self):
        return f"Symbol({self.qualified!r}, {self.kind!r}, {self.path}:{self.line})"


class Module:
    """One parsed file."""

    __slots__ = ("path", "symbols", "imports", "error", "lines")

    def __init__(self, path, symbols=(), imports=(), error="", lines=0):
        self.path = path
        self.symbols = list(symbols)
        self.imports = list(imports)
        #: Why it could not be parsed. Reported rather than hidden: a map that
        #: silently omits a file is a map that lies about the project.
        self.error = error
        self.lines = int(lines)

    @property
    def ok(self):
        return not self.error

    def __repr__(self):
        return (f"Module({self.path!r}, {len(self.symbols)} symbols"
                + (f", error={self.error!r}" if self.error else "") + ")")


def _signature(node):
    """The parameter list as written, without evaluating defaults."""
    try:
        args = node.args
        parts = []
        positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
        defaults = list(args.defaults)
        pad = len(positional) - len(defaults)
        for index, arg in enumerate(positional):
            text = arg.arg
            if index >= pad:
                text += "=..."
            parts.append(text)
        if args.vararg:
            parts.append("*" + args.vararg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            parts.append(arg.arg + ("=..." if default is not None else ""))
        if args.kwarg:
            parts.append("**" + args.kwarg.arg)
        return "(" + ", ".join(parts) + ")"
    except Exception:
        return "(...)"


def _called_names(node):
    """Names this definition calls, as written.

    Attribute calls keep their final component -- `store.query` is recorded as
    `query` and as `store.query` -- because a model looking for callers of
    `query` should find both.
    """
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        if isinstance(target, ast.Name):
            found.append(target.id)
        elif isinstance(target, ast.Attribute):
            found.append(target.attr)
            try:
                found.append(ast.unparse(target))
            except Exception:
                pass
    # Stable order, no duplicates: this goes into a prompt.
    seen, ordered = set(), []
    for name in found:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _decorator_names(node):
    names = []
    for item in getattr(node, "decorator_list", []):
        try:
            if isinstance(item, ast.Name):
                names.append(item.id)
            elif isinstance(item, ast.Attribute):
                names.append(item.attr)
            elif isinstance(item, ast.Call):
                names.append(ast.unparse(item.func))
        except Exception:
            continue
    return names


def parse_module(path, source=None):
    """Parse one Python file into its symbols and imports."""
    try:
        if source is None:
            # PowerShell 5 and several Windows editors legitimately write a
            # UTF-8 BOM. Python's tokenizer accepts it when the file is opened
            # as utf-8-sig; feeding the decoded U+FEFF to ast.parse instead
            # reports a fake syntax error on line one.
            with open(path, encoding="utf-8-sig", errors="replace") as handle:
                source = handle.read()
    except Exception as exc:
        return Module(path, error=f"could not read: {exc}")

    lines = source.count("\n") + 1
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # Reported, not skipped. A file that does not parse is usually the one
        # being worked on.
        return Module(path, error=f"syntax error at line {exc.lineno}",
                      lines=lines)

    symbols, imports = [], []

    def record(node, parent=""):
        is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_function or isinstance(node, ast.ClassDef):
            symbols.append(Symbol(
                name=node.name,
                kind=("method" if parent and is_function else
                      "function" if is_function else "class"),
                path=path,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=_signature(node) if is_function else "",
                doc=(ast.get_docstring(node) or "").strip().split("\n")[0][:160],
                parent=parent,
                calls=_called_names(node) if is_function else (),
                decorators=_decorator_names(node),
                is_async=isinstance(node, ast.AsyncFunctionDef),
            ))

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.extend(f"{module}.{alias.name}" if module else alias.name
                           for alias in node.names)
        record(node)
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                record(child, parent=node.name)

    return Module(path, symbols, imports, lines=lines)


def python_files(root, limit=2000):
    """Every Python file worth indexing under root."""
    found = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs
                      if name not in SKIP_DIRS and not name.startswith(".")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= limit:
                return found
    return found


def index(root, limit=2000):
    """Parse the project. Returns {relative path: Module}."""
    modules = {}
    for path in python_files(root, limit=limit):
        relative = os.path.relpath(path, root).replace("\\", "/")
        modules[relative] = parse_module(path)
    return modules


def repo_map(modules, max_chars=12000, per_file=24):
    """A compact map of the project, for a model's prompt.

    This is the single highest-value thing here. Given a map, a model asks to
    read the two files that matter; given no map, it either guesses or asks to
    read everything. Bounded by characters because it shares a context window
    with the actual work.
    """
    lines = []
    for path in sorted(modules):
        module = modules[path]
        if not module.ok:
            lines.append(f"{path}  [{module.error}]")
            continue
        if not module.symbols:
            continue
        lines.append(f"{path}  ({module.lines} lines)")
        for symbol in module.symbols[:per_file]:
            detail = f"  {symbol.line:>5}  {symbol.header()}"
            if symbol.doc:
                detail += f"  -- {symbol.doc}"
            lines.append(detail)
        if len(module.symbols) > per_file:
            lines.append(f"      ... {len(module.symbols) - per_file} more")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n... map truncated"


def find_symbol(modules, name):
    """Every definition of this name, exact match first then qualified.

    Returns the symbols, so a caller can replace a function by line range
    rather than searching for text near it.
    """
    wanted = str(name or "").strip()
    if not wanted:
        return []
    exact, partial = [], []
    for module in modules.values():
        for symbol in module.symbols:
            if symbol.name == wanted or symbol.qualified == wanted:
                exact.append(symbol)
            elif wanted.lower() in symbol.qualified.lower():
                partial.append(symbol)
    return exact + partial


def callers_of(modules, name):
    """Symbols whose body calls this name.

    What "related code" actually means for a change: the places that would
    break. A text search cannot distinguish a call from a mention in a comment.
    """
    wanted = str(name or "").strip()
    found = []
    for module in modules.values():
        for symbol in module.symbols:
            if wanted in symbol.calls:
                found.append(symbol)
    return found


def importers_of(modules, module_name):
    """Files that import this module, by relative path."""
    wanted = str(module_name or "").strip().replace("/", ".").removesuffix(".py")
    if not wanted:
        return []
    found = []
    for path, module in modules.items():
        for item in module.imports:
            if item == wanted or item.startswith(wanted + "."):
                found.append(path)
                break
    return sorted(found)


def read_span(path, start, end, pad=0):
    """The exact source of a line range, for showing or replacing it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception as exc:
        return f"[could not read {path}: {exc}]"
    first = max(0, int(start) - 1 - pad)
    last = min(len(lines), int(end) + pad)
    return "".join(lines[first:last])


def context_for(root, modules, request, budget_chars=8000, limit=6):
    """The symbol bodies most likely to matter for a request.

    The alternative was pasting whole files, which spends the context window
    on code that has nothing to do with the change and hides the part that
    does. Scored on the request's own words against each symbol's name,
    docstring and the names it calls.
    """
    words = {word.lower() for word in str(request or "").replace("_", " ").split()
             if len(word) > 2}
    if not words:
        return ""

    scored = []
    for module in modules.values():
        for symbol in module.symbols:
            haystack = " ".join((
                symbol.qualified.replace("_", " "), symbol.doc,
                " ".join(symbol.calls[:20]))).lower()
            hits = sum(1 for word in words if word in haystack)
            if not hits:
                continue
            # A name match is worth more than a mention in a docstring: the
            # thing called `capture` is more relevant than the thing that
            # mentions capturing.
            name_hits = sum(
                1 for word in words
                if word in symbol.qualified.replace("_", " ").lower())
            scored.append((hits + 3 * name_hits, symbol))

    scored.sort(key=lambda pair: -pair[0])
    pieces, used = [], 0
    for _score, symbol in scored[:limit]:
        body = read_span(symbol.path, symbol.line, symbol.end_line)
        header = (f"# {os.path.relpath(symbol.path, root)}"
                  f":{symbol.line}-{symbol.end_line}\n")
        chunk = header + body
        if used + len(chunk) > budget_chars:
            break
        pieces.append(chunk)
        used += len(chunk)
    return "\n".join(pieces)


#: The last map built per root, so that building it once per turn does not
#: cost a full re-parse. Keyed on what would change the answer: which files
#: exist and when any of them last changed. A mtime-keyed cache is wrong if
#: two edits land inside one filesystem clock tick, so the count and the total
#: size go into the key as well -- an edit that changes neither the newest
#: mtime nor the size nor the file count is not an edit.
_MAP_CACHE = {}
_MAP_CACHE_LIMIT = 8


def _fingerprint(root, limit):
    newest, total, count = 0.0, 0, 0
    for path in python_files(root, limit=limit):
        try:
            info = os.stat(path)
        except OSError:
            continue
        newest = max(newest, info.st_mtime)
        total += info.st_size
        count += 1
    return (round(newest, 3), total, count)


def structure_block(root, max_chars=6000, limit=2000):
    """A prompt-ready structural map of a project, cached between turns.

    This is what a coding model gets instead of a flat list of filenames. A
    filename says a file exists; a line number and a signature say where to
    make the change, which is the difference between asking to read four
    files and editing the right one.

    Returns "" for a project with no Python in it rather than an empty
    heading, because a heading with nothing under it reads to a model as "this
    project has no code".
    """
    key = str(root)
    try:
        fingerprint = _fingerprint(root, limit)
    except Exception:
        return ""
    if not fingerprint[2]:
        return ""
    cached = _MAP_CACHE.get(key)
    if cached and cached[0] == fingerprint and cached[1] >= max_chars:
        return cached[2][:max_chars] if len(cached[2]) > max_chars else cached[2]

    modules = index(root, limit=limit)
    numbers = stats(modules)
    text = repo_map(modules, max_chars=max_chars)
    if not text.strip():
        return ""
    header = (f"STRUCTURE ({numbers['symbols']} definitions in "
              f"{numbers['parsed']} files, parsed from the source). "
              f"The number before each definition is its first line, so an "
              f"edit can go to it directly.")
    if numbers["unparsed"]:
        header += (f" {numbers['unparsed']} file(s) did not parse and are "
                   f"marked below.")
    block = header + "\n" + text
    if len(_MAP_CACHE) >= _MAP_CACHE_LIMIT:
        _MAP_CACHE.clear()
    _MAP_CACHE[key] = (fingerprint, max_chars, block)
    return block


def stats(modules):
    """What was understood and what was not."""
    parsed = [module for module in modules.values() if module.ok]
    return {
        "files": len(modules),
        "parsed": len(parsed),
        "unparsed": len(modules) - len(parsed),
        "symbols": sum(len(module.symbols) for module in parsed),
        "lines": sum(module.lines for module in parsed),
    }
