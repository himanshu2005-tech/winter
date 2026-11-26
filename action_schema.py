# -*- coding: utf-8 -*-
"""
action_schema.py — validate an action before it is dispatched.

The dispatcher reads attributes with `attrs.get(name, '')`, so a wrong or
missing attribute does not raise: the handler's guard clause is simply never
satisfied and the branch falls through having done nothing. The model then sees
whatever the previous action produced and reasonably concludes its own action
worked. That is the worst possible failure — silent, and it looks like success.

Specs are declared here rather than inside the handlers so the failure is
caught before any side effect, and so the error can name the exact attribute
that was wrong instead of saying "nothing happened".

Only actions where a missing attribute is genuinely unrecoverable are listed.
An unlisted action is dispatched unchanged; this layer narrows failures, it
does not gatekeep the catalog.
"""

# requires : every attribute must be present and non-empty
# any_of   : at least one of these groups must be fully present
# optional : known, not required (anything else is flagged as a likely typo)
# body     : the action carries its content between the tags
ACTION_SPECS = {
    # --- desktop -------------------------------------------------------- #
    "CLICK": {"any_of": [["name"], ["x", "y"]],
              "optional": ["control_type", "expect_window", "button", "space"]},
    "DOUBLE_CLICK": {"any_of": [["name"], ["x", "y"]], "optional": ["control_type", "expect_window", "space"]},
    "RIGHT_CLICK": {"any_of": [["name"], ["x", "y"]], "optional": ["control_type", "expect_window", "space"]},
    "CLICK_TEXT": {"any_of": [["name"], ["text"]], "optional": ["control_type", "expect_window"]},
    "TYPE": {"requires": ["text"], "optional": ["into", "expect_window"]},
    "PRESS": {"requires": ["key"], "optional": ["times", "expect_window"]},
    "HOTKEY": {"any_of": [["keys"], ["combo"]], "optional": ["expect_window"]},
    "MOVE_MOUSE": {"requires": ["x", "y"], "optional": ["space"]},
    "INSPECT_POINT": {"requires": ["x", "y"], "optional": ["space"]},
    "LOCATE_TEXT": {"any_of": [["text"], ["query"]], "optional": ["region"]},
    "CLICK_SCREEN_TEXT": {"any_of": [["text"], ["query"]],
                          "optional": ["region", "expect_window"]},
    "DRAG": {"requires": ["x1", "y1", "x2", "y2"],
             "optional": ["expect_window", "duration", "space", "button",
                          "modifiers"]},
    "FOCUS_WINDOW": {"requires": ["title"]},
    "WINDOW_STATE": {"requires": ["title"], "optional": ["state"]},
    "CLOSE_WINDOW": {"requires": ["title"]},
    "LAUNCH_APP": {"any_of": [["target"], ["name"]]},
    "SET_FIELD": {"requires": ["name", "value"], "optional": ["expect_window"]},
    "TOGGLE_FIELD": {"requires": ["name"], "optional": ["checked", "expect_window"]},
    "SELECT_OPTION": {"requires": ["name", "value"], "optional": ["expect_window"]},
    "FILL_FORM": {"requires": ["fields"], "optional": ["expect_window"]},

    # --- machine -------------------------------------------------------- #
    "SET_BRIGHTNESS": {"requires": ["percent"]},
    "VOLUME": {"optional": ["direction", "steps"]},
    "MEDIA": {"optional": ["action"]},
    "CLIPBOARD_WRITE": {"requires": ["text"]},
    "SCREEN_LIGHT": {"optional": ["preset", "color", "opacity"]},
    "SCREEN_LIGHT_OFF": {},
    "SET_STANDBY": {"optional": ["minutes", "monitor"]},
    "SHUTDOWN": {"optional": ["approval_id"]},
    "RESTART": {"optional": ["approval_id"]},
    "BEGIN_VISIBLE_SESSION": {
        "optional": ["target", "app", "maximize"],
    },
    "END_VISIBLE_SESSION": {},
    "CLIPBOARD_READ": {},
    "LIST_CONTROLS": {"optional": ["limit", "control_type"]},
    "LIST_PROCESSES": {"optional": ["limit", "filter"]},
    "LIST_WINDOWS": {"optional": ["limit"]},
    "LOCK_SCREEN": {},
    "READ_FORM": {"optional": ["limit"]},
    "SCROLL": {"optional": ["amount", "x", "y", "space"]},

    # --- files ---------------------------------------------------------- #
    # The content may arrive as an attribute or as the tag body, and both are
    # reasonable. Neither was validated, so a SAVE_FILE whose content came as
    # an attribute fell through every handler and reported "produced no
    # result ... check that its required attributes were supplied and spelled
    # correctly" -- while the attributes were in fact correct.
    "SAVE_FILE": {"requires": ["path"], "any_of": [["content"]],
                  "optional": ["filename", "confirmed", "content"],
                  "body": True},
    "WRITE_FILE": {"requires": ["path"], "any_of": [["content"]],
                   "optional": ["filename", "confirmed", "content"],
                   "body": True},

    # --- teaching ------------------------------------------------------- #
    "DRAW_ON_SCREEN": {"requires": ["x1", "y1", "x2", "y2"],
                       "optional": ["shape", "color", "width", "label", "space"]},
    "DRAW_CLEAR": {},
    "DRAW_SCENE": {"body": True, "any_of": [["__body__"], ["scene"]],
                   "optional": ["scene", "clear", "background"]},
    "BOARD_WRITE": {"any_of": [["text"]], "body": True, "optional": ["style", "color"]},
    "BOARD_DIAGRAM": {"optional": [
        "kind", "layers", "labels", "steps", "items", "nodes", "events", "left",
        "right", "left_items", "right_items", "overlap", "forces", "body",
        "headers", "rows", "title", "direction", "color"]},
    "BOARD_MATH": {"any_of": [["latex"], ["text"]], "body": True,
                   "optional": ["size", "caption", "align", "color"]},
    "BOARD_PLOT": {"any_of": [["expr"], ["y"]], "optional": [
        "xmin", "xmax", "ymin", "ymax", "labels", "xlabel", "ylabel", "title"]},
    "BOARD_CHART": {"requires": ["labels", "values"],
                    "optional": ["kind", "title"]},
      "BOARD_TABLE": {"requires": ["rows"], "optional": ["headers", "title", "color"]},
    "BOARD_GOTO": {"requires": ["page"]},
    "BOARD_PAGES": {},
    "BOARD_CAPTURE": {"optional": ["region", "window", "caption"]},
    "ANNOTATE_TEXT": {"any_of": [["text"], ["find"]], "optional": [
        "shape", "color", "label", "region", "n"]},
    "BOARD_PLOT3D": {"any_of": [["expr"], ["x"], ["values"]], "optional": [
        "kind", "expr", "x", "y", "z", "values", "xmin", "xmax", "ymin", "ymax",
        "tmin", "tmax", "xlabel", "ylabel", "zlabel", "title", "cmap", "elev",
        "azim", "rotate", "frames", "contours"]},
    "BOARD_MATRIX": {"requires": ["values"], "optional": [
        "rows", "columns", "title", "cmap", "annotate"]},
    "BOARD_FIELD": {"any_of": [["fx"], ["fy"]], "optional": [
        "fx", "fy", "potential", "xmin", "xmax", "ymin", "ymax", "density",
        "scale", "xlabel", "ylabel", "title", "cmap", "arrow_cmap"]},
    "BOARD_CONTOUR": {"requires": ["expr"], "optional": [
        "path", "xmin", "xmax", "ymin", "ymax", "title", "cmap"]},
    "BOARD_EDIT_PAGE": {"body": True, "optional": ["page", "mode", "style", "color"]},
    "BOARD_ANIMATE": {"optional": [
        "kind", "expr", "labels", "xmin", "xmax", "ymax", "speed", "steps",
        "tokens", "dwell", "caption", "height"]},
    "BOARD_RULE": {},
    "BOARD_CLEAR": {},
    "BOARD_HIDE": {},
    "BOARD_SECTION": {"any_of": [["title"], ["text"]], "optional": ["topic"]},
    "CONTROL_CENTER": {"optional": ["action", "page"]},
    "RESULT_WINDOW": {"body": True,
                      "optional": ["action", "title", "content"]},
    "SHOW_CODE_PANEL": {},
    "ADD_PANEL": {"body": True, "optional": ["title", "note", "accent", "pinned"]},
    "UPDATE_PANEL": {"any_of": [["id"], ["title"]], "body": True,
                     "optional": ["note", "accent", "pinned"]},
    "REMOVE_PANEL": {"any_of": [["id"], ["title"]]},
    "LIST_PANELS": {},
    "SET_PANEL_VALUE": {"any_of": [["id"], ["panel"]], "requires": ["label"],
                        "optional": ["value", "detail", "ratio"]},
    "LIST_PROJECTS": {},
    "CREATE_PROJECT": {"requires": ["name"],
                       "optional": ["description", "language"]},
    "OPEN_PROJECT": {"any_of": [["name"], ["project"]]},
    "PROJECT_TREE": {"optional": ["project"]},
    "READ_PROJECT_FILE": {"requires": ["path"], "optional": ["project"]},
    # Both take their payload in the tag body or in attributes. Declaring
    # body: True alone made the attribute form fail validation before it ever
    # reached the handler that supports it.
    "WRITE_PROJECT_FILE": {"requires": ["path"], "body": True,
                           "any_of": [["__body__"], ["content"]],
                           "optional": ["project", "content", "overwrite"]},
    "EDIT_PROJECT_FILE": {"requires": ["path"], "body": True,
                          "any_of": [["__body__"], ["find"]],
                          "optional": ["project", "find", "replace", "count"]},
    "DELETE_PROJECT_FILE": {"requires": ["path"],
                            "optional": ["project", "confirmed"]},
    "SEARCH_PROJECT": {"any_of": [["pattern"], ["query"]], "optional": ["project"]},
    "FIND_PROJECT_FILES": {"requires": ["pattern"], "optional": ["project"]},
    "RUN_IN_PROJECT": {"any_of": [["cmd"], ["command"]],
                       "optional": ["project", "timeout"]},
    "RUNTIME_APPLY": {"requires": ["project"], "body": True,
                      "any_of": [["__body__"], ["code"]],
                      "optional": ["adapter", "code", "instruction",
                                   "timeout", "open"]},
    "RUNTIME_STATUS": {"requires": ["project"],
                       "optional": ["adapter", "timeout"]},
    "RUNTIME_RESUME": {"requires": ["project"],
                     "optional": ["adapter", "timeout"]},
    "RUNTIME_PRESENT": {"requires": ["project"],
                         "optional": ["adapter", "action", "timeout"]},
    "IDE_STATUS": {},
    "IDE_OPEN_PROJECT": {"requires": ["path"]},
    "IDE_OPEN_FILE": {"requires": ["path"], "optional": ["line", "column"]},
    "IDE_SHOW_DIFF": {"requires": ["left", "right"]},
    "CODING_AGENT_STATUS": {},
    "CODING_TASK_START": {"requires": ["project", "prompt"],
                          "optional": ["agent", "mode"]},
    "CODING_TASK_STATUS": {"requires": ["id"]},
    "CODING_TASK_LIST": {"optional": ["limit"]},
    "CODING_TASK_CANCEL": {"requires": ["id"]},
    "SET_PERSONA": {"any_of": [["address"], ["name"]]},
    "ADD_DIRECTIVE": {"any_of": [["text"]], "body": True},
    "REMOVE_DIRECTIVE": {"any_of": [["id"], ["text"]]},
    "LIST_DIRECTIVES": {},
    "CREATE_ACTION": {"requires": ["name"], "body": True,
                      "optional": ["overwrite"]},
    "LIST_ACTIONS": {},
    "READ_ACTION": {"requires": ["name"]},
    "REMOVE_ACTION": {"requires": ["name"]},
    "CREATE_WORKFLOW": {"body": True},
    "LIST_WORKFLOWS": {},
    "READ_WORKFLOW": {"any_of": [["id"], ["name"]]},
    "DEACTIVATE_WORKFLOW": {"any_of": [["id"], ["name"]]},
    "DISCOVER_ACTIONS": {"requires": ["query"], "optional": ["limit"]},
    "KNOWLEDGE_SEARCH": {"requires": ["query"],
                         "optional": ["sources", "limit", "purpose"]},
    "SEARCH_WEB": {"requires": ["query"],
                   "optional": ["provider", "depth", "limit", "include_domains", "exclude_domains"]},
    "TRENDING_NEWS": {"requires": ["query"],
                      "optional": ["sources", "limit"]},
    "DEEP_RESEARCH": {"requires": ["topic"]},
    "SEARCH_RECALL": {"requires": ["query"]},
    "SEARCH_VAULT": {"requires": ["query"]},
    "RESEARCH_START": {"optional": ["topic", "subject"]},
    "RESEARCH_STOP": {},
    "RESEARCH_STATUS": {},
    "READ_DOCUMENT": {"any_of": [["name"], ["file"], ["path"]],
                      "optional": ["first", "last", "page", "slide", "max_chars", "outline"]},
    "SEARCH_DOCS": {"any_of": [["query"], ["q"]], "optional": ["limit"]},
    "INDEX_FOLDER": {"requires": ["path"], "optional": ["recursive"]},
    "INGEST_PATH": {"requires": ["path"], "optional": ["recursive"]},
    "LIST_DOCUMENTS": {},

    # --- code ----------------------------------------------------------- #
    "WRITE_CODE": {"any_of": [["path"], ["file"]], "body": True,
                   "optional": ["language", "overwrite", "run", "timeout"]},
    # Content can arrive three ways: in the tag body, as code=, or as a path to
    # an existing file. Requiring a body would reject the path form.
    "RUN_SANDBOX": {"any_of": [["path"], ["code"]], "body": True,
                    "optional": ["language", "timeout"]},
    "GITHUB_READ_REPO": {"any_of": [["repo"], ["url"]],
                         "optional": ["limit"]},
    "GITHUB_READ_FILE": {"any_of": [["repo"], ["url"]],
                         "optional": ["path", "ref"]},
    "GITHUB_SEARCH": {"requires": ["query"],
                      "optional": ["search_type"]},

    # --- datasets ------------------------------------------------------- #
    "PROFILE_DATASET": {"requires": ["path"]},
    "AUDIT_DATASET": {"requires": ["path"], "optional": ["target"]},
    "CREATE_DATASET_CLEANING_PREVIEW": {
        "requires": ["path"],
        "optional": ["output_dir", "target", "impute", "drop_outliers", "drop_high_missing"]},
    "SPLIT_DATASET": {"requires": ["path"],
                      "optional": ["target", "test_size", "validation_size",
                                   "stratify", "seed", "output_dir"]},
    "KEEP_DATASET_PREVIEW": {"requires": ["preview_path", "destination_path"],
                             "optional": ["confirmed"]},

    # --- workspace ------------------------------------------------------ #
    "SET_ALARM": {"requires": ["time", "label"],
                  "optional": ["recurrence"]},
    "ADD_ALARM": {"requires": ["time", "label"],
                  "optional": ["recurrence"]},
    "DELETE_ALARM": {"requires": ["id"]},
    "LIST_ALARMS": {},
    "READ_CALENDAR": {"optional": ["days_back", "days_ahead"]},
    "LIST_CALENDAR_EVENTS": {"optional": ["days_back", "days_ahead"]},
    "ADD_CALENDAR_EVENT": {
        "requires": ["summary", "purpose"],
        "any_of": [["start_time"], ["date"]],
        "optional": ["end_time", "location", "description", "confirmed"],
    },
    "UPDATE_CALENDAR_EVENT": {"requires": ["event_id"],
                              "optional": ["start_time", "end_time", "summary", "location", "description"]},
    "DELETE_CALENDAR_EVENT": {"requires": ["event_id"]},
    "COMPLETE_TASK": {"any_of": [["task_id"], ["id"]], "optional": ["list"]},
    "UPDATE_TASK": {"any_of": [["task_id"], ["id"]], "optional": ["title", "notes", "due", "list"]},
    "ADD_TASK": {"requires": ["title"], "optional": ["notes", "due", "list"]},
    "ADD_TASKS": {"body": True, "any_of": [["__body__"], ["tasks"]],
                  "optional": ["tasks", "list"]},
    "LIST_TASKS": {"optional": [
        "max_results", "list", "tasklist", "all_lists", "include_completed"]},
    "LIST_TASK_LISTS": {},
    "MARK_EMAIL_READ": {"any_of": [["message_id"], ["id"]]},
    "ARCHIVE_EMAIL": {"any_of": [["message_id"], ["id"]]},
    "READ_EMAILS": {"optional": ["query"]},
    "FIND_FREE_SLOTS": {"optional": [
        "days_ahead", "duration_minutes", "day_start_hour", "day_end_hour"]},
    "TRACK_COMMITMENT": {"requires": ["what", "hard_time"],
                         "optional": ["location", "prep_minutes", "travel_minutes"]},
    "LIST_COMMITMENTS": {"optional": ["hours"]},
    "COMPLETE_COMMITMENT": {"requires": ["id"]},
    "LOOKUP_CONTACTS": {"any_of": [["names"], ["name"]]},
    "CHECK_GOOGLE_CONNECTION": {},
    "REAUTHORIZE_GOOGLE": {},
    "SYNC_GOOGLE_CONTACTS": {},
    "LIST_RECENT_EVENTS": {"optional": ["hours_back"]},
    "LIST_FOLLOWUPS": {},
    "SEND_WHATSAPP": {"requires": ["contact", "message"], "optional": ["file_path", "confirmed"]},
    "DRIVE_LIST": {"optional": ["folder_id", "limit", "page_token"]},
    "DRIVE_SEARCH": {"requires": ["query"],
                     "optional": ["limit", "mime_type", "folder_id"]},
    "DRIVE_INFO": {"requires": ["file_id"]},
    "DRIVE_DOWNLOAD": {"requires": ["file_id"],
                       "optional": ["destination", "overwrite"]},
    "DRIVE_UPLOAD": {"requires": ["path"],
                     "optional": ["parent_id", "name", "mime_type"]},
    "DRIVE_CREATE_FOLDER": {"requires": ["name"], "optional": ["parent_id"]},
    "DRIVE_RENAME": {"requires": ["file_id", "name"]},
    "DRIVE_MOVE": {"requires": ["file_id", "parent_id"]},
    "DRIVE_COPY": {"requires": ["file_id"], "optional": ["name", "parent_id"]},
    "DRIVE_TRASH": {"requires": ["file_id"]},
    "DRIVE_RESTORE": {"requires": ["file_id"]},
    "GET_SELECTION": {},
    "OPEN_BRAIN_MAP": {"optional": ["focus"]},
    "DRIVE_READ": {"requires": ["file_id"], "optional": ["max_chars"]},
    "DRIVE_RECENT": {"optional": ["limit", "days", "mime_type"]},
    "PEOPLE_SEARCH": {"requires": ["query"], "optional": ["limit"]},
    "PEOPLE_GET": {"requires": ["contact_id"]},
    "PEOPLE_CREATE": {"requires": ["name"],
                      "optional": ["phone", "email", "organization", "title",
                                   "notes", "birthday"]},
    "PEOPLE_UPDATE": {"requires": ["contact_id"],
                      "optional": ["name", "phone", "email", "organization",
                                   "title", "notes", "birthday"]},
    "PEOPLE_DELETE": {"requires": ["contact_id"]},
    "CREATE_PRESENTATION": {"optional": ["title", "theme", "path", "footer", "author", "content"]},
    "CREATE_DOCUMENT": {"optional": ["title", "path", "author", "content"]},
    "CREATE_SPREADSHEET": {"optional": ["title", "path", "content"]},
    "CREATE_PDF": {"optional": ["title", "path", "content"]},
    "EXTRACT_TEXT": {"requires": ["path"], "optional": ["max_chars"]},
    "OPEN_PATH": {"requires": ["path"], "optional": ["reveal"]},
    "DOWNLOAD_FILE": {"requires": ["url"], "optional": ["destination", "name"]},
    "ZIP_PATH": {"requires": ["path"], "optional": ["destination"]},
    "UNZIP_FILE": {"requires": ["path"], "optional": ["destination"]},
    "CALCULATE": {"requires": ["expression"]},
    "TIME_IN": {"requires": ["place"], "optional": ["at"]},
    "NOTE_ADD": {"requires": ["text"], "optional": ["tags"]},
    "NOTES_LIST": {"optional": ["query", "limit"]},
    "NOTE_DELETE": {"requires": ["id"]},
    "PDF_MERGE": {"requires": ["paths"], "optional": ["name"]},
    "PDF_EXTRACT": {"requires": ["path", "pages"], "optional": ["name"]},
    "DRIVE_SHARE": {"requires": ["file_id", "email"], "optional": ["role"]},
    "GET_PASSWORD": {"requires": ["key"]},
    "ADD_LIFE_GOAL": {"requires": ["title"],
                      "optional": ["horizon", "priority"]},
    "SET_LIFE_PREFERENCE": {"requires": ["key", "value"]},

    # --- memory --------------------------------------------------------- #
    "REMEMBER_FACT": {"requires": ["predicate", "object"],
                      "optional": ["subject", "confidence", "sensitivity",
                                   "authority", "autonomy_eligible", "autonomy_scope",
                                   "confirmation_required", "claim_key", "evidence_quote"]},
    "CORRECT_FACT": {"requires": ["predicate"],
                     "optional": ["subject", "object", "new_object", "old_object", "reason"]},
    "FORGET_FACT": {"any_of": [["predicate"], ["object"]], "optional": ["subject", "reason"]},
    "RECALL_FACTS": {"optional": ["entity", "subject", "predicate", "query",
                                   "limit", "history"]},
    "NOTE_GAP": {"requires": ["topic", "question"], "optional": ["why", "predicate", "subject"]},
    "FILL_GAP": {"requires": ["answer"], "optional": ["id", "topic"]},
    "IMPORT_HEALTH": {"any_of": [["path"], ["file"]]},
    "WHY_FACT": {"requires": ["predicate"],
                 "optional": ["subject", "object"]},
    "LIST_GAPS": {},
    "REVIEW_INSIGHTS": {"optional": ["limit", "refresh"]},
    "CONFIRM_INSIGHT": {"requires": ["predicate"],
                        "optional": ["subject", "object"]},
    "REJECT_INSIGHT": {"requires": ["predicate"],
                       "optional": ["subject", "object"]},
    "LEARN_KNOWLEDGE": {"requires": ["text"], "optional": ["title"]},

    # --- routines ------------------------------------------------------- #
    "ADD_ROUTINE": {"requires": ["schedule"], "any_of": [["prompt"], ["do"]],
                    "optional": ["name", "expires_on"]},
    "DELETE_ROUTINE": {"any_of": [["id"], ["name"]]},
    "PAUSE_ROUTINE": {"any_of": [["id"], ["name"]]},
    "RESUME_ROUTINE": {"any_of": [["id"], ["name"]]},
    "LIST_ROUTINES": {},

    # --- browser -------------------------------------------------------- #
    "BROWSER_GOTO": {"requires": ["url"],
                     "optional": ["tab", "background", "purpose"]},
    "BROWSER_CLICK": {"any_of": [["ref"], ["selector"]], "optional": ["tab"]},
    "BROWSER_TYPE": {"any_of": [["ref"], ["selector"]], "requires": ["text"], "optional": ["tab"]},
    "BROWSER_DIALOG": {"requires": ["mode"], "optional": ["text", "tab"]},
    "BROWSER_SELECT": {"requires": ["ref", "value"], "optional": ["tab"]},
    "BROWSER_CHECK": {"requires": ["ref"], "optional": ["checked", "tab"]},
    "BROWSER_UPLOAD": {"requires": ["ref", "path"], "optional": ["tab"]},
    "BROWSER_FILL_FORM": {"requires": ["fields"], "optional": ["tab"]},
    "BROWSER_START": {"optional": ["background"]},
    "BROWSER_TABS": {},
    "BROWSER_READ": {"optional": ["tab"]},
    "BROWSER_SNAPSHOT": {"optional": ["tab", "query"]},
    "BROWSER_SUBMIT": {"optional": ["ref", "tab"]},
    "BROWSER_PRESS": {"requires": ["ref"],
                      "optional": ["key", "tab"]},
    "BROWSER_WAIT": {"optional": ["text", "ms", "tab"]},
    "BROWSER_CLOSE": {"optional": ["tab", "all"]},

    # --- files / system -------------------------------------------------- #
    "READ_FILE": {"any_of": [["path"], ["file"]]},
    "OPEN_URL": {"requires": ["url"]},
    "FIND_FILES": {"any_of": [["pattern"], ["name"], ["query"]],
                   "optional": ["root", "path", "limit"]},
    "LIST_DIRECTORY": {"requires": ["path"],
                       "optional": ["limit", "include_hidden"]},
    "DELETE_FILE": {"requires": ["path"], "optional": ["confirmed"]},
    "MOVE_FILE": {"requires": ["dst"], "optional": ["src"]},
    "DASHBOARD_SHOW_TEXT": {"optional": ["title", "language", "content"]},
    "COPY_FILE": {"requires": ["src", "dst"]},
    "RUN_COMMAND": {"requires": ["cmd"], "optional": ["timeout", "cwd", "shell"]},
    "RUN_PYTHON": {
        "body": True,
        "any_of": [["__body__"], ["code"]],
        "optional": ["code"],
    },
    "RUN_BLENDER_SCRIPT": {
        "body": True,
        "any_of": [["__body__"], ["code"]],
        "optional": ["code"],
    },
    "WAIT_FOR": {"requires": ["path"], "optional": ["timeout"]},
    "WAIT": {"optional": ["time"]},
    "GET_TRAVEL_TIME": {"requires": ["origin", "destination"], "optional": ["mode", "traffic"]},
    "FLIGHT_SEARCH": {"requires": ["origin", "destination", "departure_date"],
                      "optional": ["return_date", "adults", "travel_class", "nonstop", "max_price", "limit"]},
    "FLIGHT_WATCH_CREATE": {"requires": ["origin", "destination", "departure_date", "target"],
                            "optional": ["return_date", "adults", "travel_class", "nonstop", "label", "interval", "currency"]},
    "BOOK_RIDE": {"requires": ["destination"], "optional": ["pickup"]},
    "FIND_API": {"requires": ["need"], "optional": [
        "limit", "refresh", "auth", "category", "region", "official_only"]},
    "API_CATALOG_STATUS": {"optional": ["refresh"]},
    "API_CHECK": {"requires": ["name"], "optional": ["timeout"]},
    "GEOCODE": {"requires": ["query"]},
    "GET_WEATHER": {"any_of": [["location"], ["place"]],
                    "optional": ["days", "date"]},
    "SET_TIMER": {"any_of": [["duration"], ["in"]],
                  "optional": ["label", "display", "widget_title"]},
    "SYSTEM_STATUS": {},
    "KILL_PROCESS": {"any_of": [["name"], ["pid"]], "optional": ["confirmed"]},
    "TAKE_SCREENSHOT": {"optional": ["path", "region"]},
    "READ_SCREEN_TEXT": {"optional": ["region"]},
    "QUIET_FOR": {"requires": ["minutes"], "optional": ["reason"]},
    "SHOW_ON_SCREEN": {"body": True, "any_of": [["__body__"], ["items"]],
                       "optional": ["title", "items", "id"]},
    "TICK_ON_SCREEN": {"requires": ["item"], "optional": ["done", "id"]},
    "CLEAR_SCREEN": {"optional": ["id"]},
    "SHOW_ROUTE": {"requires": ["origin", "destination"],
                   "optional": ["mode", "traffic", "label"]},
    "SPOTIFY_NOW_PLAYING": {},
    "SPOTIFY_SEARCH": {"requires": ["query"], "optional": ["kind", "limit"]},
    "SPOTIFY_VOLUME": {"requires": ["percent"]},
    "SPOTIFY_QUEUE": {"requires": ["uri"]},
    "SPOTIFY_PLAY": {"optional": ["query"]},
    "SPOTIFY_PAUSE": {},
    "SPOTIFY_SKIP": {},
    "MUSIC_PLAY": {"optional": ["query", "song"]},
    "MUSIC_PAUSE": {}, "MUSIC_NEXT": {}, "MUSIC_PREVIOUS": {}, "MUSIC_NOW_PLAYING": {},
    "MUSIC_SEARCH": {"requires": ["query"], "optional": ["limit"]},
    "SEND_TELEGRAM": {"any_of": [["text"], ["message"]]},
    "NOTIFY": {"requires": ["text"], "optional": ["title", "urgent", "silent"]},
    # --- durable observation ------------------------------------------ #
    "WATCH_CREATE": {
        "requires": ["kind", "source", "condition"],
        "optional": ["target", "label", "interval", "currency", "url"],
    },
    "WATCH_LIST": {},
    "WATCH_STATUS": {"any_of": [["id"], ["label"]]},
    "WATCH_PAUSE": {"any_of": [["id"], ["label"]]},
    "WATCH_RESUME": {"any_of": [["id"], ["label"]]},
    "WATCH_CANCEL": {"any_of": [["id"], ["label"]]},
    "WATCH_CHECK": {"any_of": [["id"], ["label"]]},
    "WATCH_HISTORY": {"any_of": [["id"], ["label"]], "optional": ["limit"]},
    # Prefer the block form because prose survives it intact, but accept the
    # attribute form too.  Models naturally emit both and the old split
    # dispatcher rejected body="..." before the email handler could
    # normalise it.  ``attachments`` accepts a JSON list or a semicolon/newline
    # separated list; ``attachment``/``file_path`` are convenient singular
    # aliases handled at the provider boundary.
    "SEND_EMAIL": {
        "requires": ["to", "subject"],
        "body": True,
        "any_of": [["__body__"], ["body"]],
        "optional": ["body", "attachments", "attachment", "file_path",
                     "confirmed", "approved"],
    },
}

# Runtime-owned metadata that every action may carry.  ``speech`` is not an
# input to the capability itself; it is the short phrase VANTA says at the
# executor boundary immediately before the capability starts.
_ALWAYS_ALLOWED = {"type", "confirmed", "approved", "tab", "speech"}


# Schemas for capabilities declared in capabilities.py. Generated from the
# declaration so that a capability cannot be declared without its input
# contract. The integrity gate now requires the combined legacy and declared
# surfaces to have complete coverage.
try:
    import capabilities as _capabilities
    for _name, _spec in _capabilities.schema_overlay().items():
        ACTION_SPECS.setdefault(_name, _spec)
except Exception as _schema_exc:  # pragma: no cover - startup diagnostic
    print(f"[Schema] Could not publish declared capability schemas: {_schema_exc}")


# Shapes the annotation renderer actually has a branch for. Kept here rather
# than in ui.py so the dispatcher can refuse an undrawable one without
# importing tkinter -- and because the renderer's `if/elif` chain silently fell
# through to the label for anything it did not recognise. A rect asked for at
# (300,300)-(900,650) put its label on screen with no box under it, and the
# receipt said the annotation had been drawn.
DRAW_SHAPES = ("box", "circle", "ellipse", "arrow", "line", "highlight",
               "underline", "number", "callout")

#: Words the model reasonably reaches for instead of the canonical name.
DRAW_SHAPE_SYNONYMS = {
    "rect": "box", "rectangle": "box", "square": "box", "frame": "box",
    "oval": "ellipse", "round": "circle",
    "pointer": "arrow", "point": "arrow",
    "mark": "highlight", "marker": "highlight",
    "label": "callout", "badge": "number",
}


def normalise_shape(shape):
    """The canonical shape name, or "" when nothing can draw it."""
    cleaned = str(shape or "").strip().lower()
    if not cleaned:
        return "box"
    cleaned = DRAW_SHAPE_SYNONYMS.get(cleaned, cleaned)
    return cleaned if cleaned in DRAW_SHAPES else ""


def validate(action_type, attrs, has_body=False):
    """Return an error string if this action cannot possibly work, else None."""
    spec = ACTION_SPECS.get(str(action_type or "").upper())
    if spec is None:
        return None

    present = {k for k, v in (attrs or {}).items() if str(v).strip()}
    if has_body:
        present.add("__body__")

    missing = [name for name in spec.get("requires", []) if name not in present]
    if missing:
        return (
            f"[Error] {action_type} is missing required attribute"
            f"{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
            f"{_hint(action_type, spec)}"
        )

    groups = spec.get("any_of")
    if groups and not any(all(name in present for name in group) for group in groups):
        options = " or ".join("+".join(group) for group in groups)
        if spec.get("body") and "__body__" in present:
            pass  # content in the tag body satisfies the requirement
        else:
            return (
                f"[Error] {action_type} needs {options}. {_hint(action_type, spec)}"
            )

    if spec.get("body") and not has_body and not groups:
        return f"[Error] {action_type} needs its content between the opening and closing tags."

    known = set(spec.get("requires", [])) | set(spec.get("optional", []))
    for group in groups or []:
        known.update(group)
    unknown = [k for k in (attrs or {})
               if k not in known and k not in _ALWAYS_ALLOWED]
    # An explicitly declared empty schema means the action takes no
    # attributes. Previously an empty schema silently accepted misspellings.
    if unknown:
        return (
            f"[Error] {action_type} does not take {', '.join(sorted(unknown))}. "
            f"It accepts: {', '.join(sorted(known))}."
        )
    return None


def _hint(action_type, spec):
    accepted = set(spec.get("requires", [])) | set(spec.get("optional", []))
    for group in spec.get("any_of", []):
        accepted.update(group)
    if not accepted:
        return ""
    return f"Accepted attributes: {', '.join(sorted(accepted))}."


def input_contract(action_type):
    """The exact minimum input shape, shared by discovery and execution."""
    spec = ACTION_SPECS.get(str(action_type or '').upper())
    if spec is None:
        return ''
    parts = []
    if spec.get('requires'):
        parts.append('required: ' + ', '.join(spec['requires']))
    if spec.get('any_of'):
        options = [' + '.join('tag body' if field == '__body__' else field
                              for field in group) for group in spec['any_of']]
        parts.append('provide ' + ' OR '.join(options))
    if spec.get('body'):
        parts.append('payload belongs between the opening and closing ACTION tags; preserve its newlines')
    return '; '.join(parts)
