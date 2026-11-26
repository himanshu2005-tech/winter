# -*- coding: utf-8 -*-
"""
workbench.py -- the coding surface, as a mixin onto WinterUI.

"Open project" used to mean the Control Center landed on its Code page: a file
list, an editor and an instruction box, sharing a window with sixteen other
pages about memory, telemetry and the knowledge graph. That is a settings
screen with a text editor in it. Writing code in it means reading a 240-pixel
column of source while the rest of the screen reports battery level.

This is the other thing: the whole display, three panes, and nothing on it that
is not the project.

    project + files + outline | tabs + editor | agent

Three decisions are worth stating, because each one is a trap avoided.

*The agent pane is not decoration.* The instruction box sends through the same
input queue a spoken sentence does, so "add a retry to the uploader" runs the
real turn -- planner, authorization, receipts and all -- rather than a second
quieter code path with its own idea of what is allowed. Everything VANTA says
or does lands in the pane as it happens. The point of a surface you are
watching is that you can see the work.

*The outline comes from the AST, not from a regular expression over the text.*
A line number from `ast` is the line the definition is on, which is what lets
an edit replace a function instead of searching for text near it. Scanning for
`^def ` finds the word `def` in a docstring and reports a function that is not
there.

*Indexing happens off the UI thread.* Parsing a project is fast but not
instant, and a surface that freezes for a second every time a file is saved
reads as a crash. The index is published back through the UI queue when it is
ready; until then the outline says so.

The surface owns no truth. Files come from `projects`, which is where path
containment lives, and structure from `code_intel`. If this module disagrees
with either of them, it is this module that is wrong.
"""

import os
import threading
import tkinter as tk
import tkinter.font as tkfont

import window_identity

# The surface lives on the UI thread; the model runs on another and may be
# asked "what am I looking at?". Same arrangement as the blackboard: the UI
# publishes plain data on every change and any thread may read it.
_STATE_LOCK = threading.Lock()
_STATE = {"open": False, "project": "", "file": "", "files": 0,
          "symbols": 0, "dirty": False, "indexed": False}


def workbench_state():
    """What the coding surface currently has open, readable from any thread."""
    with _STATE_LOCK:
        return dict(_STATE)


def _set_state(**fields):
    with _STATE_LOCK:
        _STATE.update(fields)


# Palette. Near-black with a blue cast rather than pure black: the same reason
# the board is slate, except an editor is read for hours rather than minutes.
WB_BG = "#0e1116"
WB_PANE = "#13171d"
WB_INSET = "#0a0d11"
WB_LINE = "#1e242c"
WB_TEXT = "#d7dee6"
WB_DIM = "#8b97a4"
WB_FAINT = "#5a6672"
WB_ACCENT = "#7dd3fc"
WB_GOOD = "#86efac"
WB_WARN = "#fbbf24"
WB_BAD = "#fca5a5"
WB_SELECT = "#1d2a38"

RAIL_WIDTH = 268
AGENT_WIDTH = 400
TITLE_HEIGHT = 34
STATUS_HEIGHT = 24

#: How much of the agent log to keep. A long session otherwise grows the
#: widget until scrolling it stalls the surface.
LOG_MAX_LINES = 1200

#: Suffixes the editor will open as text. Everything else is reported rather
#: than decoded into mojibake -- `projects.read_file` draws the same line, this
#: just avoids the round trip for the obvious cases.
TEXT_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt",
    ".html", ".css", ".scss", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".sh", ".ps1", ".bat", ".sql", ".c", ".h", ".cpp", ".hpp", ".java",
    ".go", ".rs", ".rb", ".php", ".kt", ".swift", ".r", ".env", ".gitignore",
)


def open_request(ui_queue, project="", base_dir=None, timeout=2.0):
    """Ask the UI thread to raise the surface, and wait to see that it did.

    The receipt is read back from `workbench_state()` rather than asserted,
    because "I put a message on a queue" and "the surface is up" are different
    facts and only the second one is what was asked for. If the surface never
    comes up -- no display, a UI thread wedged elsewhere -- that is reported as
    a failure instead of a success nobody can see.
    """
    import time

    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    name = str(project or "").strip()
    if name:
        import projects
        try:
            name = projects.match_name(base, name)
        except Exception as exc:
            return f"[Error] Could not open that vault project: {exc}"

    ui_queue.put(("workbench_open", name))
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        state = workbench_state()
        if state["open"]:
            where = state["project"] or "no project"
            detail = f"{state['files']} files" if state["project"] else \
                     "pick one in the left rail"
            return (f"[Success] Coding surface open on {where} "
                    f"({detail}). Esc closes it.")
        time.sleep(0.05)
    return ("[Error] Asked for the coding surface and it did not come up "
            "within 2 seconds. The UI may not be running.")


def is_text_file(relative):
    """Whether the editor should try to open this as source."""
    name = str(relative or "")
    suffix = os.path.splitext(name)[1].lower()
    if suffix:
        return suffix in TEXT_SUFFIXES
    # No extension at all: README, Makefile, Dockerfile, LICENSE.
    return os.path.basename(name).lower() in {
        "readme", "makefile", "dockerfile", "license", "licence",
        "requirements", "procfile",
    }


# Keywords by language family. Short lists on purpose: the value of
# highlighting is telling code from prose at a glance, and that is carried by
# keywords, strings and comments. A complete grammar per language is a parser,
# and there is already a parser here for the language that matters.
_PY_KEYWORDS = frozenset("""
    False None True and as assert async await break class continue def del
    elif else except finally for from global if import in is lambda nonlocal
    not or pass raise return try while with yield match case self cls
""".split())
_C_KEYWORDS = frozenset("""
    const let var function class extends return if else for while do switch
    case break continue new delete typeof instanceof this super import export
    from default async await try catch finally throw yield interface type enum
    public private protected static readonly void null undefined true false
    int float double char bool struct enum union static inline sizeof
""".split())
_KEYWORDS_BY_SUFFIX = {
    ".py": _PY_KEYWORDS, ".pyi": _PY_KEYWORDS,
}

_WORD = r"[A-Za-z_][A-Za-z0-9_]*"
_C_PATTERNS = (
    # A scoped (?m:...) group rather than a leading (?m): an inline flag that
    # is not the first thing in the pattern is an error in Python 3.11.
    ("com", r"//[^\n]*|/\*[\s\S]*?\*/|(?m:^[^\S\n]*#[^\n]*)"),
    ("str", r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`'),
    ("num", r"\b\d[\d_]*(?:\.\d+)?\b"),
)


def highlight_spans(text, filename=""):
    """Where the colours go, as (tag, start, end) in Tk "line.col" indices.

    Separated from the widget so it can be checked against exact positions,
    which is the only way to catch an off-by-one that paints the quote and
    not the string. Python goes through `tokenize`, which gets multi-line
    strings, f-strings and nested quotes right; everything else gets a small
    pattern set, and a Python file that does not currently tokenise falls back
    to the same -- a file mid-edit is broken most of the time it is looked at,
    and losing all colour on every unbalanced bracket is worse than
    approximating.
    """
    suffix = os.path.splitext(str(filename or ""))[1].lower()
    if suffix in (".py", ".pyi"):
        spans = _python_spans(text)
        if spans is not None:
            return spans
    return _pattern_spans(text, _KEYWORDS_BY_SUFFIX.get(suffix, _C_KEYWORDS))


def _python_spans(text):
    """Exact spans from the tokeniser, or None if it could not read the text."""
    import io
    import keyword
    import token as token_module
    import tokenize

    spans = []
    previous = ""
    try:
        for item in tokenize.generate_tokens(io.StringIO(text).readline):
            kind, value, start, end, _line = item
            tag = ""
            if kind == token_module.STRING:
                tag = "str"
            elif kind == token_module.COMMENT:
                tag = "com"
            elif kind == token_module.NUMBER:
                tag = "num"
            elif kind == token_module.NAME:
                if previous in ("def", "class"):
                    tag = "def"
                elif keyword.iskeyword(value) or value in _PY_KEYWORDS:
                    tag = "kw"
            if kind == token_module.NAME:
                previous = value
            elif kind not in (token_module.NL, token_module.NEWLINE,
                              token_module.INDENT, token_module.DEDENT):
                previous = value
            if tag:
                spans.append((tag, f"{start[0]}.{start[1]}",
                              f"{end[0]}.{end[1]}"))
    except Exception:
        return None
    return spans


def _pattern_spans(text, keywords):
    """Spans from regular expressions, for everything that is not Python."""
    import re

    # Offsets to "line.col" without counting newlines per match.
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)

    def index(offset):
        low, high = 0, len(starts) - 1
        while low < high - 1:
            middle = (low + high) // 2
            if starts[middle] <= offset:
                low = middle
            else:
                high = middle
        return f"{low + 1}.{offset - starts[low]}"

    spans, taken = [], []
    for tag, pattern in _C_PATTERNS:
        for match in re.finditer(pattern, text):
            if any(match.start() < end and start < match.end()
                   for start, end in taken):
                continue        # inside a string or comment already claimed
            taken.append((match.start(), match.end()))
            spans.append((tag, index(match.start()), index(match.end())))
    for match in re.finditer(_WORD, text):
        if match.group() not in keywords:
            continue
        if any(match.start() < end and start < match.end()
               for start, end in taken):
            continue
        spans.append(("kw", index(match.start()), index(match.end())))
    return spans


def relevant_symbols(modules, request, limit=4):
    """Which definitions this request points at, as "name:line" labels.

    The same scoring `code_intel.context_for` uses to choose what the prompt
    gets, reported rather than hidden. A wrong answer from the model is then
    traceable to the wrong context instead of being a mystery.
    """
    wanted = {word.lower() for word in str(request or "").replace("_", " ").split()
              if len(word) > 2}
    if not wanted:
        return []
    scored = []
    for module in (modules or {}).values():
        for symbol in module.symbols:
            haystack = symbol.qualified.replace("_", " ").lower()
            hits = sum(1 for word in wanted if word in haystack)
            if hits:
                scored.append((hits, symbol.line, symbol))
    scored.sort(key=lambda row: (-row[0], row[1]))
    # One label per definition. A generated copy of a file under a build
    # directory produces a second symbol with the same name and the same line,
    # and printing it twice says nothing the first one did not.
    labels = []
    for _hits, _line, symbol in scored:
        label = f"{symbol.qualified}:{symbol.line}"
        if label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def outline_rows(module):
    """The outline of one parsed module, as plain rows for the list.

    Separated from the widget so it can be checked without a display. A
    module that did not parse yields one row saying so, because the file being
    edited is exactly the file that does not parse half the time, and an
    outline that silently empties itself looks like the file has no code in
    it.
    """
    if module is None:
        return []
    if not module.ok:
        return [{"label": f"! {module.error}", "line": 1, "kind": "error"}]
    if not module.symbols:
        # An empty list is ambiguous between "no definitions" and "the
        # outline is broken", and the second is the one you act on.
        return [{"label": "no definitions", "line": 1, "kind": "empty"}]
    rows = []
    for symbol in module.symbols:
        indent = "    " if symbol.kind == "method" else ""
        mark = {"class": "C", "function": "f", "method": "m"}.get(
            symbol.kind, "?")
        rows.append({
            "label": f"{indent}{mark}  {symbol.name}",
            "line": symbol.line,
            "kind": symbol.kind,
        })
    return rows


def enclosing_symbol(module, line):
    """The innermost definition containing this line, or "".

    The status bar answers "where am I?" with it. Innermost rather than first
    match: inside a method the useful answer is the method, not the class.
    """
    if module is None or not module.ok:
        return ""
    best = None
    for symbol in module.symbols:
        if symbol.line <= line <= symbol.end_line:
            if best is None or symbol.line > best.line:
                best = symbol
    return best.qualified if best else ""


class WorkbenchMixin:
    """The coding surface. Mixed into WinterUI beside the blackboard."""

    # ------------------------------------------------------------ #
    # Surface
    # ------------------------------------------------------------ #
    def _wb_font(self, kind="mono", size=13, weight="normal"):
        cache = getattr(self, "_wb_fonts", None)
        if cache is None:
            cache = self._wb_fonts = {}
        key = (kind, size, weight)
        if key not in cache:
            family = {"mono": "Consolas", "ui": "Segoe UI"}.get(kind, "Consolas")
            cache[key] = tkfont.Font(family=family, size=size, weight=weight)
        return cache[key]

    def _wb_exists(self):
        win = getattr(self, "_wb_win", None)
        return win is not None and win.winfo_exists()

    def _wb_is_open(self):
        return self._wb_exists() and self._wb_win.winfo_viewable()

    def _ensure_workbench(self):
        if self._wb_exists():
            return self._wb_win

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        window_identity.hide_from_taskbar(win, self.root)
        win.configure(bg=WB_BG)
        width = win.winfo_screenwidth()
        height = win.winfo_screenheight()
        win.geometry(f"{width}x{height}+0+0")
        # Unlike the board, this one takes input: it is an editor. So no
        # WS_EX_TRANSPARENT here -- a click-through editor would swallow every
        # keystroke into whatever is behind it.
        try:
            from vision import register_winter_hwnd
            import win32gui
            register_winter_hwnd(win32gui.GetParent(win.winfo_id())
                                 or win.winfo_id())
        except Exception as exc:
            print(f"[Workbench] Could not register the window: {exc}")

        self._wb_win = win
        self._wb_project = ""
        self._wb_file = ""
        self._wb_open_files = []      # tab strip, most recent last
        self._wb_original = ""
        self._wb_dirty = False
        self._wb_rows = []            # the file list, as project-relative paths
        self._wb_outline = []
        self._wb_modules = {}         # code_intel index of the open project
        self._wb_module = None        # the open file's module, if Python
        self._wb_indexing = False
        self._wb_build(win)
        self._wb_bind(win)
        return win

    def _wb_build(self, win):
        win.grid_rowconfigure(1, weight=1)
        win.grid_columnconfigure(1, weight=1)

        # ---- title bar -------------------------------------------------- #
        title = tk.Frame(win, bg=WB_BG, height=TITLE_HEIGHT)
        title.grid(row=0, column=0, columnspan=3, sticky="ew")
        title.grid_propagate(False)
        tk.Label(title, text="●", bg=WB_BG, fg=WB_ACCENT,
                 font=self._wb_font("ui", 11)).pack(side=tk.LEFT, padx=(16, 8))
        self._wb_title = tk.Label(
            title, text="VANTA", bg=WB_BG, fg=WB_TEXT, anchor="w",
            font=self._wb_font("ui", 11, "bold"))
        self._wb_title.pack(side=tk.LEFT)
        tk.Label(title,
                 text="Esc close   ·   F11 hide   ·   "
                      "Ctrl+S save   ·   Ctrl+Enter implement",
                 bg=WB_BG, fg=WB_FAINT, font=self._wb_font("ui", 9)
                 ).pack(side=tk.RIGHT, padx=16)
        tk.Frame(win, bg=WB_LINE, height=1).grid(row=0, column=0,
                                                 columnspan=3, sticky="sew")

        # ---- left rail: projects, files, outline ------------------------ #
        rail = tk.Frame(win, bg=WB_PANE, width=RAIL_WIDTH)
        rail.grid(row=1, column=0, sticky="nsew")
        rail.grid_propagate(False)
        rail.grid_rowconfigure(1, weight=2)
        rail.grid_rowconfigure(3, weight=3)
        rail.grid_columnconfigure(0, weight=1)

        self._wb_project_list = self._wb_section_list(
            rail, "PROJECTS", row=0, height=5)
        self._wb_project_list.bind("<<ListboxSelect>>", self._wb_pick_project)
        self._wb_project_list.bind("<Double-Button-1>", self._wb_pick_project)

        self._wb_file_list = self._wb_section_list(rail, "FILES", row=1)
        self._wb_file_list.bind("<<ListboxSelect>>", self._wb_pick_file)
        self._wb_file_list.bind("<Return>", self._wb_pick_file)

        self._wb_filter = tk.Entry(
            rail, bg=WB_INSET, fg=WB_TEXT, insertbackground=WB_ACCENT,
            relief="flat", font=self._wb_font("mono", 10),
            highlightthickness=1, highlightbackground=WB_LINE,
            highlightcolor=WB_ACCENT)
        self._wb_filter.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 8))
        self._wb_filter.insert(0, "")
        self._wb_filter.bind("<KeyRelease>", lambda _e: self._wb_fill_files())

        self._wb_outline_list = self._wb_section_list(rail, "OUTLINE", row=3)
        self._wb_outline_list.bind("<<ListboxSelect>>", self._wb_jump_to_symbol)

        # ---- centre: tab strip + editor --------------------------------- #
        centre = tk.Frame(win, bg=WB_BG)
        centre.grid(row=1, column=1, sticky="nsew")
        centre.grid_rowconfigure(1, weight=1)
        centre.grid_columnconfigure(1, weight=1)

        self._wb_tabs = tk.Frame(centre, bg=WB_BG, height=30)
        self._wb_tabs.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._wb_tabs.grid_propagate(False)

        self._wb_gutter = tk.Text(
            centre, width=5, bg=WB_BG, fg=WB_FAINT, relief="flat",
            highlightthickness=0, borderwidth=0, takefocus=0,
            font=self._wb_font("mono", 12), state="disabled",
            spacing1=0, spacing3=0, padx=6)
        self._wb_gutter.grid(row=1, column=0, sticky="ns")
        self._wb_editor = tk.Text(
            centre, bg=WB_BG, fg=WB_TEXT, insertbackground=WB_ACCENT,
            selectbackground=WB_SELECT, relief="flat", highlightthickness=0,
            borderwidth=0, wrap="none", undo=True, padx=10,
            font=self._wb_font("mono", 12), tabs="4c")
        self._wb_editor.grid(row=1, column=1, sticky="nsew")
        scroll = tk.Scrollbar(centre, command=self._wb_scroll_both,
                              bg=WB_PANE, troughcolor=WB_BG,
                              activebackground=WB_LINE, relief="flat",
                              borderwidth=0, width=10)
        scroll.grid(row=1, column=2, sticky="ns")
        self._wb_scrollbar = scroll
        self._wb_editor.configure(yscrollcommand=self._wb_sync_scroll)
        for tag, colour in (("kw", "#c792ea"), ("str", "#c3e88d"),
                            ("com", WB_FAINT), ("num", "#f78c6c"),
                            ("def", "#82aaff")):
            self._wb_editor.tag_configure(tag, foreground=colour)
        self._wb_editor.bind("<<Modified>>", self._wb_on_modified)
        self._wb_editor.bind("<KeyRelease>", self._wb_on_cursor)
        self._wb_editor.bind("<ButtonRelease-1>", self._wb_on_cursor)
        self._wb_editor.bind("<MouseWheel>", self._wb_on_wheel)

        # ---- right: the agent ------------------------------------------- #
        agent = tk.Frame(win, bg=WB_PANE, width=AGENT_WIDTH)
        agent.grid(row=1, column=2, sticky="nsew")
        agent.grid_propagate(False)
        agent.grid_rowconfigure(1, weight=1)
        agent.grid_columnconfigure(0, weight=1)

        head = tk.Frame(agent, bg=WB_PANE)
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        tk.Label(head, text="AGENT", bg=WB_PANE, fg=WB_DIM, anchor="w",
                 font=self._wb_font("ui", 9, "bold")).pack(side=tk.LEFT)
        self._wb_agent_note = tk.Label(
            head, text="idle", bg=WB_PANE, fg=WB_FAINT, anchor="e",
            font=self._wb_font("ui", 9))
        self._wb_agent_note.pack(side=tk.RIGHT)

        self._wb_log = tk.Text(
            agent, bg=WB_INSET, fg=WB_TEXT, relief="flat",
            highlightthickness=0, borderwidth=0, wrap="word", padx=12, pady=10,
            font=self._wb_font("mono", 10), state="disabled")
        self._wb_log.grid(row=1, column=0, sticky="nsew", padx=12)
        for tag, colour in (("you", WB_ACCENT), ("vanta", WB_TEXT),
                            ("tool", WB_DIM), ("good", WB_GOOD),
                            ("warn", WB_WARN), ("bad", WB_BAD),
                            ("faint", WB_FAINT)):
            self._wb_log.tag_configure(tag, foreground=colour)

        self._wb_context_note = tk.Label(
            agent, text="", bg=WB_PANE, fg=WB_FAINT, anchor="w",
            justify="left", wraplength=AGENT_WIDTH - 36,
            font=self._wb_font("mono", 9))
        self._wb_context_note.grid(row=2, column=0, sticky="ew",
                                   padx=14, pady=(8, 4))

        self._wb_instruction = tk.Text(
            agent, bg=WB_INSET, fg=WB_TEXT, insertbackground=WB_ACCENT,
            relief="flat", highlightthickness=1, highlightbackground=WB_LINE,
            highlightcolor=WB_ACCENT, wrap="word", height=4, padx=10, pady=8,
            font=self._wb_font("mono", 11))
        self._wb_instruction.grid(row=3, column=0, sticky="ew", padx=12)
        self._wb_instruction.bind("<Control-Return>", self._wb_implement)
        self._wb_instruction.bind("<Shift-Return>", lambda _e: None)

        buttons = tk.Frame(agent, bg=WB_PANE)
        buttons.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 14))
        self._wb_button(buttons, "Implement", self._wb_implement, primary=True)
        self._wb_button(buttons, "Save", self._wb_save)
        self._wb_button(buttons, "Reload", self._wb_reload)
        self._wb_button(buttons, "Re-index", self._wb_reindex)

        # ---- status bar -------------------------------------------------- #
        tk.Frame(win, bg=WB_LINE, height=1).grid(row=2, column=0,
                                                 columnspan=3, sticky="ew")
        status = tk.Frame(win, bg=WB_PANE, height=STATUS_HEIGHT)
        status.grid(row=3, column=0, columnspan=3, sticky="ew")
        status.grid_propagate(False)
        self._wb_status = tk.Label(
            status, text="", bg=WB_PANE, fg=WB_DIM, anchor="w",
            font=self._wb_font("mono", 9))
        self._wb_status.pack(side=tk.LEFT, padx=16)
        self._wb_index_note = tk.Label(
            status, text="", bg=WB_PANE, fg=WB_FAINT, anchor="e",
            font=self._wb_font("mono", 9))
        self._wb_index_note.pack(side=tk.RIGHT, padx=16)

    def _wb_section_list(self, parent, label, row, height=None):
        """A titled listbox in the rail."""
        holder = tk.Frame(parent, bg=WB_PANE)
        holder.grid(row=row, column=0, sticky="nsew", padx=12,
                    pady=(12 if row == 0 else 6, 2))
        holder.grid_rowconfigure(1, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        tk.Label(holder, text=label, bg=WB_PANE, fg=WB_DIM, anchor="w",
                 font=self._wb_font("ui", 9, "bold")
                 ).grid(row=0, column=0, sticky="ew", pady=(0, 4))
        listbox = tk.Listbox(
            holder, bg=WB_INSET, fg=WB_TEXT, selectbackground=WB_SELECT,
            selectforeground=WB_ACCENT, relief="flat", highlightthickness=0,
            borderwidth=0, activestyle="none", exportselection=False,
            font=self._wb_font("mono", 10))
        if height:
            listbox.configure(height=height)
        listbox.grid(row=1, column=0, sticky="nsew")
        return listbox

    def _wb_button(self, parent, label, command, primary=False):
        button = tk.Label(
            parent, text=label, bg=WB_ACCENT if primary else WB_INSET,
            fg="#06121a" if primary else WB_TEXT, padx=12, pady=6,
            font=self._wb_font("ui", 10, "bold" if primary else "normal"),
            cursor="hand2")
        button.pack(side=tk.LEFT, padx=(0, 6))
        button.bind("<Button-1>", lambda _e: command())
        if not primary:
            button.bind("<Enter>", lambda _e: button.configure(bg=WB_LINE))
            button.bind("<Leave>", lambda _e: button.configure(bg=WB_INSET))
        return button

    def _wb_bind(self, win):
        # Bound on the window rather than the editor: Escape must work with
        # the focus anywhere, including in the instruction box.
        win.bind("<Escape>", lambda _e: self._wb_close())
        win.bind("<F11>", lambda _e: self._wb_hide())
        win.bind("<Control-s>", lambda _e: self._wb_save() or "break")
        win.bind("<Control-S>", lambda _e: self._wb_save() or "break")

    # ------------------------------------------------------------ #
    # Showing and hiding
    # ------------------------------------------------------------ #
    def _wb_open(self, project=""):
        """Raise the surface, on `project` if one is named."""
        import projects
        self._ensure_workbench()
        base = self._wb_base()
        name = str(project or "").strip() or projects.get_active(base)
        self._wb_fill_projects()
        if name and name != self._wb_project:
            self._wb_load_project(name)
        elif not self._wb_project and name:
            self._wb_load_project(name)
        else:
            self._wb_refresh_status()
        self._wb_win.deiconify()
        self._wb_win.lift()
        try:
            self._wb_win.focus_force()
            self._wb_instruction.focus_set()
        except tk.TclError:
            pass
        if not self._wb_project:
            self._wb_say("faint", "No project open. Pick one on the left, or "
                                  "ask VANTA to create one.\n")
        _set_state(open=True)
        return self._wb_project

    def _wb_hide(self):
        if self._wb_exists():
            self._wb_win.withdraw()
        _set_state(open=False)

    def _wb_close(self):
        """Leave the surface. An unsaved buffer is kept, not discarded."""
        if self._wb_dirty:
            self._wb_say("warn", f"{self._wb_file} has unsaved edits; "
                                 "they are still in the buffer.\n")
        self._wb_hide()

    def _wb_toggle(self):
        if self._wb_is_open():
            self._wb_close()
        else:
            self._wb_open()

    # ------------------------------------------------------------ #
    # Projects and files
    # ------------------------------------------------------------ #
    @staticmethod
    def _wb_base():
        return os.path.dirname(os.path.abspath(__file__))

    def _wb_fill_projects(self):
        import projects
        self._wb_project_list.delete(0, tk.END)
        try:
            rows = projects.listing(self._wb_base())
        except Exception as exc:
            self._wb_say("bad", f"Could not list projects: {exc}\n")
            return
        self._wb_projects = [row["name"] for row in rows]
        for row in rows:
            # Name and size only. The language list made every row wider than
            # the rail, so each one ended in a cut-off word.
            self._wb_project_list.insert(
                tk.END, f"{row['name'][:22]:<22} {row['files']:>4}")

    def _wb_pick_project(self, _event=None):
        selection = self._wb_project_list.curselection()
        if not selection:
            return
        names = getattr(self, "_wb_projects", [])
        if selection[0] < len(names):
            self._wb_load_project(names[selection[0]])

    def _wb_load_project(self, name):
        import projects
        base = self._wb_base()
        try:
            rows = projects.file_rows(base, name)
        except Exception as exc:
            self._wb_say("bad", f"Could not open {name}: {exc}\n")
            return
        projects.set_active(base, name)
        self._wb_project = name
        self._wb_rows = [row["path"] for row in rows]
        self._wb_open_files = []
        self._wb_file = ""
        self._wb_set_editor("")
        self._wb_original = ""
        self._wb_dirty = False
        self._wb_modules = {}
        self._wb_module = None
        self._wb_fill_files()
        self._wb_fill_outline([])
        self._wb_title.configure(text=f"VANTA   ·   {name}")
        self._wb_draw_tabs()
        self._wb_say("good", f"Opened {name} — {len(rows)} files.\n")
        self._wb_start_index()
        self._wb_refresh_status()
        _set_state(project=name, file="", files=len(rows), dirty=False)

    def _wb_fill_files(self):
        """The file list, narrowed by whatever is in the filter box."""
        needle = self._wb_filter.get().strip().lower()
        self._wb_file_list.delete(0, tk.END)
        self._wb_visible = [path for path in self._wb_rows
                            if not needle or needle in path.lower()]
        for path in self._wb_visible:
            self._wb_file_list.insert(tk.END, path)

    def _wb_pick_file(self, _event=None):
        selection = self._wb_file_list.curselection()
        if not selection:
            return
        visible = getattr(self, "_wb_visible", [])
        if selection[0] < len(visible):
            self._wb_open_file(visible[selection[0]])

    def _wb_open_file(self, relative):
        import projects
        if self._wb_dirty and relative != self._wb_file:
            self._wb_say("warn", f"{self._wb_file} has unsaved edits. "
                                 "Save or reload before switching.\n")
            return
        if not is_text_file(relative):
            self._wb_say("faint", f"{relative} is not a text file.\n")
            return
        try:
            text, meta = projects.read_file(self._wb_base(), self._wb_project,
                                            relative)
        except Exception as exc:
            self._wb_say("bad", f"Could not read {relative}: {exc}\n")
            return
        if meta.get("truncated"):
            self._wb_say("warn", f"{relative} is {meta['size']} bytes; "
                                 "showing the start only. Saving would "
                                 "truncate it, so saving is blocked.\n")
        self._wb_file = relative
        self._wb_truncated = bool(meta.get("truncated"))
        self._wb_set_editor(text)
        self._wb_original = text
        self._wb_dirty = False
        if relative in self._wb_open_files:
            self._wb_open_files.remove(relative)
        self._wb_open_files.append(relative)
        del self._wb_open_files[:-8]        # eight tabs is already a lot
        self._wb_draw_tabs()
        self._wb_refresh_outline()
        self._wb_refresh_status()
        _set_state(file=relative, dirty=False)

    def _wb_save(self):
        import projects
        if not self._wb_project or not self._wb_file:
            return
        if getattr(self, "_wb_truncated", False):
            self._wb_say("bad", "Refusing to save a truncated view of a "
                                "large file; it would delete the rest.\n")
            return
        text = self._wb_editor.get("1.0", "end-1c")
        try:
            result = projects.write_file(self._wb_base(), self._wb_project,
                                         self._wb_file, text, overwrite=True)
        except Exception as exc:
            self._wb_say("bad", f"Save failed: {exc}\n")
            return
        self._wb_original = text
        self._wb_dirty = False
        self._wb_say("good", f"{result}\n")
        self._wb_refresh_outline()
        self._wb_refresh_status()
        self._wb_start_index()
        _set_state(dirty=False)

    def _wb_reload(self):
        if self._wb_file:
            relative, self._wb_dirty = self._wb_file, False
            self._wb_open_file(relative)

    # ------------------------------------------------------------ #
    # The editor
    # ------------------------------------------------------------ #
    def _wb_set_editor(self, text):
        self._wb_editor.configure(state="normal")
        self._wb_editor.delete("1.0", tk.END)
        self._wb_editor.insert("1.0", text)
        self._wb_editor.edit_reset()
        self._wb_editor.edit_modified(False)
        self._wb_editor.mark_set("insert", "1.0")
        self._wb_editor.see("1.0")
        self._wb_draw_gutter()
        self._wb_highlight()

    def _wb_highlight(self):
        """Repaint the colours. Cheap enough to do on the whole buffer."""
        editor = self._wb_editor
        for tag in ("kw", "str", "com", "num", "def"):
            editor.tag_remove(tag, "1.0", tk.END)
        try:
            spans = highlight_spans(editor.get("1.0", "end-1c"), self._wb_file)
        except Exception:
            return
        for tag, start, end in spans:
            try:
                editor.tag_add(tag, start, end)
            except tk.TclError:
                pass

    def _wb_schedule_highlight(self, delay=260):
        """Repaint after the typing stops, not on every keystroke."""
        job = getattr(self, "_wb_highlight_job", None)
        if job:
            try:
                self._wb_win.after_cancel(job)
            except Exception:
                pass
        self._wb_highlight_job = self._wb_win.after(delay, self._wb_highlight)

    def _wb_draw_gutter(self):
        total = int(self._wb_editor.index("end-1c").split(".")[0])
        self._wb_gutter.configure(state="normal")
        self._wb_gutter.delete("1.0", tk.END)
        self._wb_gutter.insert(
            "1.0", "\n".join(f"{n:>4}" for n in range(1, total + 1)))
        self._wb_gutter.configure(state="disabled")
        self._wb_gutter.yview_moveto(self._wb_editor.yview()[0])

    def _wb_sync_scroll(self, first, last):
        self._wb_scrollbar.set(first, last)
        self._wb_gutter.yview_moveto(first)

    def _wb_scroll_both(self, *args):
        self._wb_editor.yview(*args)
        self._wb_gutter.yview_moveto(self._wb_editor.yview()[0])

    def _wb_on_wheel(self, event):
        self._wb_editor.yview_scroll(-1 * (event.delta // 120), "units")
        self._wb_gutter.yview_moveto(self._wb_editor.yview()[0])
        return "break"

    def _wb_on_modified(self, _event=None):
        if not self._wb_editor.edit_modified():
            return
        self._wb_editor.edit_modified(False)
        dirty = self._wb_editor.get("1.0", "end-1c") != self._wb_original
        self._wb_dirty = dirty
        self._wb_draw_gutter()
        self._wb_schedule_highlight()
        self._wb_refresh_status()
        _set_state(dirty=dirty)

    def _wb_on_cursor(self, _event=None):
        self._wb_refresh_status()

    def _wb_jump_to_symbol(self, _event=None):
        selection = self._wb_outline_list.curselection()
        if not selection or selection[0] >= len(self._wb_outline):
            return
        line = self._wb_outline[selection[0]]["line"]
        self._wb_editor.mark_set("insert", f"{line}.0")
        self._wb_editor.see(f"{max(1, line - 4)}.0")
        self._wb_editor.focus_set()
        self._wb_refresh_status()

    # ------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------ #
    def _wb_draw_tabs(self):
        for child in self._wb_tabs.winfo_children():
            child.destroy()
        for relative in self._wb_open_files:
            active = relative == self._wb_file
            tab = tk.Label(
                self._wb_tabs, text=os.path.basename(relative),
                bg=WB_BG if active else WB_PANE,
                fg=WB_ACCENT if active else WB_DIM,
                padx=14, pady=5, cursor="hand2",
                font=self._wb_font("ui", 10, "bold" if active else "normal"))
            tab.pack(side=tk.LEFT, padx=(0, 1))
            tab.bind("<Button-1>",
                     lambda _e, path=relative: self._wb_open_file(path))

    # ------------------------------------------------------------ #
    # Structure, from the AST
    # ------------------------------------------------------------ #
    def _wb_start_index(self):
        """Parse the project off the UI thread."""
        if not self._wb_project or self._wb_indexing:
            return
        import projects
        try:
            root = projects.project_path(self._wb_base(), self._wb_project)
        except Exception:
            return
        self._wb_indexing = True
        self._wb_index_note.configure(text="indexing…")

        def worker():
            try:
                import code_intel
                modules = code_intel.index(root)
                payload = {"project": self._wb_project, "modules": modules,
                           "stats": code_intel.stats(modules)}
            except Exception as exc:
                payload = {"project": self._wb_project, "error": str(exc)}
            self._q.put(("workbench_index", payload))

        threading.Thread(target=worker, daemon=True,
                         name="workbench-index").start()

    def _wb_apply_index(self, payload):
        """The index came back. Ignore it if the project changed meanwhile."""
        self._wb_indexing = False
        if not isinstance(payload, dict):
            return
        if payload.get("project") != self._wb_project:
            return
        if payload.get("error"):
            self._wb_index_note.configure(text="index failed")
            self._wb_say("warn", f"Indexing failed: {payload['error']}\n")
            return
        self._wb_modules = payload.get("modules") or {}
        numbers = payload.get("stats") or {}
        note = (f"{numbers.get('files', 0)} py · "
                f"{numbers.get('symbols', 0)} symbols")
        if numbers.get("unparsed"):
            note += f" · {numbers['unparsed']} unparsed"
        self._wb_index_note.configure(text=note)
        self._wb_refresh_outline()
        # A request made while the index was still building was answered with
        # "not indexed yet", and that answer then stayed on screen after the
        # index arrived. Re-answer it now that there is something to answer
        # with.
        pending = getattr(self, "_wb_last_instruction", "")
        if pending:
            self._wb_show_context(pending)
        _set_state(symbols=int(numbers.get("symbols", 0)), indexed=True)

    def _wb_reindex(self):
        self._wb_start_index()

    def _wb_refresh_outline(self):
        """The outline of the open file, parsed from its current buffer.

        From the buffer, not from disk: the outline of what is on screen is
        the only outline worth having while a change is being made.
        """
        if not self._wb_file or not self._wb_file.endswith(".py"):
            self._wb_module = None
            self._wb_fill_outline([])
            return
        try:
            import code_intel
            import projects
            path = projects.resolve(self._wb_base(), self._wb_project,
                                    self._wb_file)
            self._wb_module = code_intel.parse_module(
                path, source=self._wb_editor.get("1.0", "end-1c"))
        except Exception as exc:
            self._wb_module = None
            self._wb_say("warn", f"Could not parse {self._wb_file}: {exc}\n")
            self._wb_fill_outline([])
            return
        self._wb_fill_outline(outline_rows(self._wb_module))

    def _wb_fill_outline(self, rows):
        self._wb_outline = list(rows)
        self._wb_outline_list.delete(0, tk.END)
        for row in self._wb_outline:
            self._wb_outline_list.insert(tk.END, row["label"])

    # ------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------ #
    def _wb_refresh_status(self):
        if not self._wb_exists():
            return
        if not self._wb_project:
            self._wb_status.configure(text="no project open")
            return
        if not self._wb_file:
            self._wb_status.configure(
                text=f"{self._wb_project} · "
                     f"{len(self._wb_rows)} files · open one on the left")
            return
        line = int(self._wb_editor.index("insert").split(".")[0])
        total = int(self._wb_editor.index("end-1c").split(".")[0])
        parts = [self._wb_file, f"line {line}/{total}"]
        where = enclosing_symbol(self._wb_module, line)
        if where:
            parts.append(f"in {where}")
        parts.append("UNSAVED" if self._wb_dirty else "saved")
        self._wb_status.configure(text="   ·   ".join(parts))

    # ------------------------------------------------------------ #
    # The agent pane
    # ------------------------------------------------------------ #
    def _wb_say(self, tag, text):
        """Append to the agent log. Safe to call before the surface exists."""
        if not self._wb_exists():
            return
        widget = self._wb_log
        widget.configure(state="normal")
        widget.insert(tk.END, text, tag)
        # Trim from the top rather than letting the widget grow forever.
        total = int(widget.index("end-1c").split(".")[0])
        if total > LOG_MAX_LINES:
            widget.delete("1.0", f"{total - LOG_MAX_LINES}.0")
        widget.see(tk.END)
        widget.configure(state="disabled")

    def _wb_note_response(self, text):
        """VANTA said something. Mirror it into the pane if we are open."""
        if self._wb_is_open() and str(text or "").strip():
            self._wb_say("vanta", f"VANTA\n{text}\n\n")
            self._wb_agent_note.configure(text="idle")

    def _wb_note_tool(self, text):
        """An action reported a result. The receipts are the interesting part."""
        if not self._wb_is_open():
            return
        line = str(text or "").strip()
        if not line:
            return
        tag = "tool"
        if "[Error]" in line or "[Blocked]" in line:
            tag = "bad"
        elif "[Success]" in line:
            tag = "good"
        self._wb_say(tag, line[:2000] + "\n\n")

    def _wb_implement(self, _event=None):
        """Send the instruction through the ordinary turn.

        Deliberately the same queue a spoken sentence uses. A second path
        straight to the coder model would be shorter and would also skip
        planning, authorization and receipts -- which is to say it would skip
        everything that makes the result trustworthy.
        """
        instruction = self._wb_instruction.get("1.0", "end-1c").strip()
        if not instruction:
            return "break"
        self._wb_instruction.delete("1.0", tk.END)
        if not self._wb_project:
            self._wb_say("warn", "Open a project first.\n")
            return "break"
        if self._wb_dirty:
            self._wb_say("warn", f"Saving {self._wb_file} first so VANTA "
                                 "works on what you can see.\n")
            self._wb_save()
        if self._wb_dirty:
            # The save was refused -- a truncated view, or a write error. Going
            # ahead would have the model edit the version on disk, which is not
            # the version on screen, and then overwrite one with the other.
            self._wb_say("bad", "Not sending: your edits are not on disk, so "
                                "VANTA would be working on different text "
                                "than you are looking at.\n")
            return "break"

        where = f"In the project {self._wb_project}"
        if self._wb_file:
            where += f", file {self._wb_file}"
        self._wb_say("you", f"YOU\n{instruction}\n\n")
        self._wb_agent_note.configure(text="working…")
        self._wb_show_context(instruction)
        self._in_q.put(("text", f"{where}: {instruction}"))
        return "break"

    def _wb_show_context(self, instruction):
        """Say which code the structural index considers relevant.

        This is the pane's honesty: the prompt gets a map of the project, and
        this names the symbols that map points at for this request, so a wrong
        answer can be traced to the wrong context rather than guessed at.
        """
        self._wb_last_instruction = instruction
        if not self._wb_modules:
            self._wb_context_note.configure(
                text="context: project not indexed yet")
            return
        try:
            shown = relevant_symbols(self._wb_modules, instruction)
            if shown:
                self._wb_context_note.configure(
                    text="context: " + ", ".join(shown))
            else:
                self._wb_context_note.configure(
                    text="context: whole-project map (no symbol matched)")
        except Exception as exc:
            self._wb_context_note.configure(text=f"context: unavailable ({exc})")

    # ------------------------------------------------------------ #
    # Reacting to VANTA's own edits
    # ------------------------------------------------------------ #
    def _wb_project_changed(self, project=""):
        """VANTA wrote to the project. Show it, do not overwrite the buffer."""
        if not self._wb_exists() or not self._wb_project:
            return
        if project and project != self._wb_project:
            return
        import projects
        try:
            rows = projects.file_rows(self._wb_base(), self._wb_project)
        except Exception:
            return
        self._wb_rows = [row["path"] for row in rows]
        self._wb_fill_files()
        _set_state(files=len(rows))
        if self._wb_file and not self._wb_dirty:
            # Reload only a clean buffer. Discarding someone's unsaved edit
            # because the agent touched a different file is unforgivable.
            self._wb_reload()
        elif self._wb_dirty:
            self._wb_say("warn", f"{self._wb_file} changed on disk and your "
                                 "buffer has edits; not reloading.\n")
        self._wb_start_index()
