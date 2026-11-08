# -*- coding: utf-8 -*-
"""
capabilities.py -- one place a capability is declared, instead of six.

A capability used to be spread across six files that had no idea about each
other: the prose catalogue in agent.ACTION_CATALOGS (what the model is told),
the searchable index in action_registry (built only from that prose), the input
schema in action_schema, the authorization set in autonomy.KNOWN_ACTIONS, the
mode groups in mode_manager, and the dispatcher in actions.py. Nothing checked
that the six agreed, so they drifted, and the drift was invisible until a
capability was needed.

CAPTURE_CAMERA is what that costs. It was authorized in autonomy and
classified in mode_manager -- two of six -- and absent from the catalogue. The
catalogue is the only input to the searchable registry, so DISCOVER_ACTIONS
could not find a camera no matter how it was asked:

    'what can you see through the camera' -> no result
    'webcam'                              -> no result

Asked what it could see, VANTA searched twice, found nothing, and reported that
it had no access to a live camera feed. The webcam was fine. The user was told
their hardware could not be reached because of a missing dictionary entry.

So a capability is declared once, here, and the six consumers are derived from
the declaration. A declaration that is missing a handler, a schema, an
authorization class or a test is a failure the integrity check reports at
startup, rather than a surprise the next time the capability is wanted.

WHAT THIS FILE IS NOT
---------------------
It is not a second place to add actions. The ~264 existing actions still live
in their current lists and remain authoritative for now; this registry holds
only the capabilities that have been migrated, and integrity.py checks both
populations against the same matrix. Nothing is injected into model prompts
wholesale: the generated catalogue text goes through the same bounded,
outcome-based retrieval as everything else, so declaring more capabilities
never enlarges a prompt.
"""

import unavailability

# --- authorization classes ------------------------------------------- #
# What kind of power the capability has, which decides proactive policy and
# whether a second confirmation is required. Deliberately coarse: a finer
# taxonomy invites arguing about which bucket something belongs in instead of
# noticing that it is in none of them.
OBSERVATION = "observation"   # reads the machine, the screen, a device
RETRIEVAL = "retrieval"       # reads remote data
MUTATION = "mutation"         # changes local state
EXTERNAL = "external"         # sends, posts, spends, or tells someone
SYSTEM = "system"             # power, processes, shell

AUTHORIZATION_CLASSES = (OBSERVATION, RETRIEVAL, MUTATION, EXTERNAL, SYSTEM)

# --- confirmation policy --------------------------------------------- #
CONFIRM_NEVER = "never"
CONFIRM_ALWAYS = "always"
CONFIRM_WHEN_PROACTIVE = "when_proactive"
CONFIRMATION_POLICIES = (CONFIRM_NEVER, CONFIRM_ALWAYS, CONFIRM_WHEN_PROACTIVE)

# --- verification strategy ------------------------------------------- #
# How a claim of success is checked. "none" is a legitimate answer for a pure
# read, but it has to be said out loud rather than left blank, because
# unverified-by-default is how several actions came to report success they had
# not earned.
VERIFY_NONE = "none"
VERIFY_RECEIPT = "receipt"      # the handler returns evidence
VERIFY_ARTIFACT = "artifact"    # a file must exist afterwards
VERIFY_READBACK = "readback"    # re-read the changed state
VERIFICATION_STRATEGIES = (VERIFY_NONE, VERIFY_RECEIPT, VERIFY_ARTIFACT,
                           VERIFY_READBACK)

# --- how the capability runs ----------------------------------------- #
#: Ordinary dispatcher action: returns a string result from actions.py.
RUNS_IN_DISPATCHER = "dispatcher"
#: Needs the agent loop, because its result is an image fed back to the model
#: rather than text. The camera and the zoom crop are the only two; pretending
#: otherwise is what kept them outside every registry.
RUNS_IN_AGENT_LOOP = "agent_loop"
EXECUTION_SITES = (RUNS_IN_DISPATCHER, RUNS_IN_AGENT_LOOP)

ALL_MODES = ("assistant", "study", "research", "build", "health", "sleep")


class Capability:
    """Everything true about one capability, in one object.

    Validated on construction. A declaration that cannot say how it is
    authorized, how its success is verified, or which test covers it is
    rejected here -- which is the entire point of the file, since every one of
    those was previously allowed to be silently absent.
    """

    __slots__ = ("name", "summary", "aliases", "schema", "handler",
                 "authorization", "modes", "origins", "confirmation",
                 "verification", "receipt", "runs_in", "tests", "syntax",
                 "notes")

    def __init__(self, name, summary, handler, authorization, verification,
                 receipt, tests, schema=None, aliases=(), modes=ALL_MODES,
                 origins=("user",), confirmation=CONFIRM_NEVER,
                 runs_in=RUNS_IN_DISPATCHER, syntax=None, notes=""):
        self.name = str(name).upper()
        if not self.name.replace("_", "").isalnum():
            raise ValueError(f"capability name is not an action name: {name!r}")
        if not summary.strip():
            raise ValueError(f"{self.name}: summary is required")
        if not callable(handler) and not isinstance(handler, str):
            raise ValueError(f"{self.name}: handler must be callable or a "
                             f"'module:function' string")
        if authorization not in AUTHORIZATION_CLASSES:
            raise ValueError(f"{self.name}: unknown authorization class "
                             f"{authorization!r}")
        if verification not in VERIFICATION_STRATEGIES:
            raise ValueError(f"{self.name}: unknown verification strategy "
                             f"{verification!r}")
        if confirmation not in CONFIRMATION_POLICIES:
            raise ValueError(f"{self.name}: unknown confirmation policy "
                             f"{confirmation!r}")
        if runs_in not in EXECUTION_SITES:
            raise ValueError(f"{self.name}: unknown execution site {runs_in!r}")
        if not receipt.strip():
            raise ValueError(f"{self.name}: receipt contract is required -- "
                             f"say what evidence a success returns")
        if not tests:
            raise ValueError(f"{self.name}: at least one test reference is "
                             f"required")
        unknown_modes = set(modes) - set(ALL_MODES)
        if unknown_modes:
            raise ValueError(f"{self.name}: unknown modes {sorted(unknown_modes)}")

        self.summary = summary.strip()
        self.handler = handler
        self.authorization = authorization
        self.verification = verification
        self.receipt = receipt.strip()
        self.tests = tuple(tests)
        self.schema = dict(schema or {})
        self.aliases = tuple(aliases)
        self.modes = tuple(modes)
        self.origins = tuple(origins)
        self.confirmation = confirmation
        self.runs_in = runs_in
        self.syntax = syntax or f'<ACTION type="{self.name}" />'
        self.notes = notes.strip()

    # ---------------------------------------------------------------- #
    # Derivations. Each is one consumer that used to be hand-maintained.
    # ---------------------------------------------------------------- #

    def catalog_text(self):
        """The model-facing documentation, generated rather than written.

        Shaped exactly like the hand-written catalogue entries because
        action_registry parses this text to build its index -- so an entry that
        does not look like the others is an entry that does not get indexed.
        """
        lines = [f"  {self.syntax}", f"    - {self.summary}"]
        if self.notes:
            for note in self.notes.split("\n"):
                lines.append(f"    {note.strip()}")
        return "\n".join(lines)

    def alias_text(self):
        """Vocabulary for retrieval scoring. Not shown to the model."""
        return " ".join(self.aliases)

    def schema_spec(self):
        """The action_schema entry."""
        return dict(self.schema)

    def requires_confirmation(self, origin="user"):
        if self.confirmation == CONFIRM_ALWAYS:
            return True
        if self.confirmation == CONFIRM_WHEN_PROACTIVE:
            return origin != "user"
        return False

    def permits_origin(self, origin):
        return str(origin or "user") in self.origins

    def resolve_handler(self):
        """Import the handler if it was declared as 'module:function'.

        Declared lazily so that a capability touching a heavy dependency --
        cv2, in the camera's case -- does not drag it into every import of this
        module, and so that a broken optional dependency surfaces as a handler
        failure at call time rather than an import error at startup.
        """
        if callable(self.handler):
            return self.handler
        module_name, _, function_name = str(self.handler).partition(":")
        if not function_name:
            raise ValueError(f"{self.name}: handler {self.handler!r} is not "
                             f"'module:function'")
        module = __import__(module_name, fromlist=[function_name])
        try:
            return getattr(module, function_name)
        except AttributeError as exc:
            raise unavailability.Unavailable(
                self.name, unavailability.HANDLER_FAILED,
                f"{self.handler} does not exist.") from exc

    def __repr__(self):
        return f"Capability({self.name!r}, runs_in={self.runs_in!r})"


# ====================================================================== #
# Declarations
# ====================================================================== #
# Migrated capabilities only. Phase 2 covers the two that ran inside the agent
# loop and were therefore invisible to every registry.

CAPABILITIES = {}


def declare(capability):
    """Add a capability, refusing a duplicate name.

    Unique names were previously only a convention. Two declarations of one
    name would have produced two registry entries competing for the same
    retrieval slot, with whichever lost silently unreachable -- so this is
    where that becomes impossible.
    """
    if capability.name in CAPABILITIES:
        raise ValueError(f"duplicate capability declaration: {capability.name}")
    CAPABILITIES[capability.name] = capability
    return capability


declare(Capability(
    name="REPLY_EMAIL",
    summary=("Replies to one exact Gmail message returned by READ_EMAILS, "
             "preserving Gmail thread identity and standard reply headers. "
             "Use this instead of SEND_EMAIL when Himanshu says reply, "
             "respond, or answer that email."),
    aliases=("reply email", "reply to email", "respond to email",
             "answer email", "reply to that message", "email thread"),
    syntax=('<ACTION type="REPLY_EMAIL" message_id="gmail-id" '
            'attachments="C:\\\\path\\\\optional.pdf">reply text</ACTION>'),
    notes=("First use READ_EMAILS when the exact message_id is not already in "
           "the current evidence. Sending still requires one approval."),
    schema={"requires": ["message_id"], "body": True,
            "any_of": [["__body__"], ["body"]],
            "optional": ["body", "attachments", "attachment", "file_path",
                         "confirmed", "approved"]},
    handler="actions:_run_reply_email", authorization=EXTERNAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_ALWAYS,
    verification=VERIFY_RECEIPT,
    receipt=("the original and sent Gmail message ids, thread id, resolved "
             "recipient, subject, and attachment filenames"),
    tests=("tests/test_email_reply_and_telegram_files.py",),
))


declare(Capability(
    name="DOWNLOAD_EMAIL_ATTACHMENT",
    summary=("Downloads one exact attachment from a Gmail message returned by "
             "READ_EMAILS into VANTA's vault or a requested folder."),
    aliases=("download email attachment", "save mail attachment",
             "get attached file", "download attachment from gmail"),
    syntax=('<ACTION type="DOWNLOAD_EMAIL_ATTACHMENT" message_id="gmail-id" '
            'attachment_id="attachment-id" destination="optional folder" />'),
    notes=("Use the exact message and attachment identifiers from READ_EMAILS. "
           "An exact filename is accepted only when it is unique."),
    schema={"any_of": [["message_id", "attachment_id"],
                       ["message_id", "filename"],
                       ["message_id", "name"],
                       ["id", "attachment_id"], ["id", "filename"],
                       ["id", "name"]],
            "optional": ["message_id", "id", "attachment_id", "filename",
                         "name", "destination"]},
    handler="actions:_run_download_email_attachment",
    authorization=RETRIEVAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_ARTIFACT,
    receipt="the saved absolute path, original filename, and byte count",
    tests=("tests/test_integration_contracts.py",),
))


declare(Capability(
    name="DELETE_TASK",
    summary=("Deletes one exact Google Task by the stable task ID returned by "
             "LIST_TASKS."),
    aliases=("delete task", "remove google task", "erase task"),
    syntax='<ACTION type="DELETE_TASK" task_id="google-task-id" list="" />',
    notes="List tasks first when the ID is not already in current evidence.",
    schema={"any_of": [["task_id"], ["id"]],
            "optional": ["task_id", "id", "list", "tasklist"]},
    handler="actions:_run_delete_task", authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the deleted task ID and containing Google Task list",
    tests=("tests/test_integration_contracts.py",),
))


declare(Capability(
    name="GITHUB_LIST_REPOS",
    summary=("Lists repositories accessible to VANTA's installed GitHub App, "
             "including private repositories granted to that installation."),
    aliases=("list my github repositories", "which github repos can you access",
             "show repositories", "github app repositories"),
    syntax='<ACTION type="GITHUB_LIST_REPOS" limit="100" />',
    schema={"optional": ["limit"]},
    handler="actions:_run_github_list_repos", authorization=RETRIEVAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the count and full names of repositories visible to the GitHub App",
    tests=("tests/test_integration_contracts.py",),
))


declare(Capability(
    name="SEND_TELEGRAM_FILE",
    summary=("Shares one exact local file with Himanshu through the paired "
             "Telegram chat. Use for send/share this file on Telegram; do "
             "not substitute the text-only SEND_TELEGRAM action."),
    aliases=("send telegram file", "share file telegram", "telegram document",
             "send this file to my phone", "send attachment telegram"),
    syntax=('<ACTION type="SEND_TELEGRAM_FILE" path="D:\\\\file.pdf" '
            'caption="Optional caption" />'),
    schema={"any_of": [["path"], ["file_path"]],
            "optional": ["path", "file_path", "caption", "approved",
                         "confirmed"]},
    handler="actions:_run_send_telegram_file", authorization=EXTERNAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_ALWAYS,
    verification=VERIFY_RECEIPT,
    receipt=("the filename, byte size, Telegram message id and paired "
             "recipient after Telegram accepts the upload"),
    tests=("tests/test_email_reply_and_telegram_files.py",),
))


declare(Capability(
    name="LIST_TELEGRAM_FILES",
    summary=("Lists files already received from the paired Telegram owner "
             "and saved in VANTA's private vault, including their usable "
             "local paths. Use when Himanshu says the file I sent on Telegram."),
    aliases=("telegram files", "file i sent", "received file", "downloaded "
             "telegram attachment", "latest telegram document"),
    syntax='<ACTION type="LIST_TELEGRAM_FILES" limit="10" />',
    schema={"optional": ["limit"]},
    handler="actions:_run_list_telegram_files", authorization=RETRIEVAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the saved filenames, byte sizes, MIME types and absolute paths",
    tests=("tests/test_email_reply_and_telegram_files.py",),
))


declare(Capability(
    name="GET_AIR_QUALITY",
    summary=("Looks up current WAQI/AQICN air quality by city or coordinates, "
             "including AQI, available pollutants, station and timestamp."),
    aliases=("air quality", "pollution", "aqi", "pm2.5", "pm25", "pm10",
             "pollutants", "monitoring station", "smog"),
    syntax='<ACTION type="GET_AIR_QUALITY" city="Chennai" />',
    schema={"any_of": [["city"], ["lat", "lon"]]},
    handler="actions:_run_get_air_quality", authorization=RETRIEVAL,
    verification=VERIFY_RECEIPT, receipt="WAQI station, timestamp and pollutant readings",
    tests=("tests/test_public_data_apis.py",),
))

declare(Capability(
    name="INDIA_POST_LOOKUP",
    summary=("Looks up Indian post offices by a six-digit PIN or by post-office "
             "name, returning all matches rather than assuming uniqueness."),
    aliases=("pin code", "pincode", "postal code", "post office", "district",
             "delivery office", "india post"),
    syntax='<ACTION type="INDIA_POST_LOOKUP" pin="500081" />',
    schema={"any_of": [["pin"], ["office"]]},
    handler="actions:_run_india_post_lookup", authorization=RETRIEVAL,
    verification=VERIFY_RECEIPT, receipt="matching postal offices and administrative fields",
    tests=("tests/test_public_data_apis.py",),
))

declare(Capability(
    name="CAPTURE_CAMERA",
    summary=("Takes a photo through the webcam and looks at it, so you can "
             "answer questions about what is physically in front of Himanshu "
             "-- what he is holding up, what is on his desk, what he is "
             "showing you. Use mode=\"look\" for a spoken three-second "
             "countdown before the shot, which is the default and the polite "
             "option when he knows he is being photographed. Use "
             "mode=\"verify\" to capture silently when you only need to check "
             "something and a countdown would be intrusive."),
    # Content words only. These were written as conversational phrases --
    # "what am i holding", "look at this", "in front of me" -- and every
    # ordinary word in them became a scoring token. "am" ranked the camera
    # first for "where am i", and "look" ranked it first for "look up the
    # deadline". An alias has to name the capability, not surround it.
    aliases=("camera", "webcam", "cam", "photo", "picture", "snapshot",
             "frame", "lens", "holding", "desk", "visible", "showing",
             "physically", "see", "receipt", "medicine label", "timetable",
             "handwritten note", "page", "document"),
    syntax='<ACTION type="CAPTURE_CAMERA" mode="look|verify" />',
    notes=("The image comes back to you for analysis; describe only what is\n"
           "actually visible and never guess at a face or a person who is not\n"
           "clearly in frame. If the capture fails you are told which device\n"
           "indexes were tried and what failed -- report that, and never say\n"
           "the camera is missing unless the failure says no device was found."),
    schema={"optional": ["mode", "reason"]},
    handler="camera:capture",
    authorization=OBSERVATION,
    # Deliberately absent from sleep: a silent camera capture during a declared
    # quiet window is exactly the sort of thing that must not be possible.
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_ARTIFACT,
    receipt=("the saved JPEG path, its pixel dimensions, and the camera index "
             "that actually produced the frame"),
    runs_in=RUNS_IN_AGENT_LOOP,
    tests=("tests/test_camera_capability.py",),
))


declare(Capability(
    name="ZOOM",
    summary=("Crops and magnifies one point of the current screen so you can "
             "read small text or inspect detail that a full screenshot loses. "
             "Give the centre point in screen coordinates."),
    aliases=("zoom", "magnify", "enlarge", "closer", "crop", "detail",
             "small", "tiny", "illegible", "blurry", "unreadable"),
    syntax='<ACTION type="ZOOM" x="1200" y="640" />',
    notes=("Take a screenshot first if you do not already have current\n"
           "coordinates; zooming a guessed point wastes a turn."),
    schema={"requires": ["x", "y"]},
    handler="vision:take_zoom_crop_b64",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the cropped image, and the centre point it was taken around",
    runs_in=RUNS_IN_AGENT_LOOP,
    tests=("tests/test_camera_capability.py",),
))


declare(Capability(
    name="REMEMBER_FACE",
    summary=("Captures one clear face through the webcam and links an "
             "encrypted InsightFace template (ResNet-100 recogniser) to the "
             "named Person in the knowledge graph. Use when Himanshu asks to "
             "remember a face: 'remember my face' / 'save my face' means "
             "person=Himanshu; 'remember Madhu's face' means person=Madhu."),
    aliases=("remember face", "remember my face", "save my face",
             "remember this person", "enroll face", "learn face",
             "save face template"),
    syntax=('<ACTION type="REMEMBER_FACE" person="Himanshu" />  or  '
            '<ACTION type="REMEMBER_FACE" person="Madhu" phone="+91..." />'),
    notes=("Exactly one clear face must be visible, so nobody beside the\n"
           "person is saved. The face is attached to that person's existing\n"
           "graph node (their contact, with its phone number) when there is\n"
           "one, so the face, number and facts are one record; otherwise a new\n"
           "Person node is made. Pass phone= only if Himanshu states a number.\n"
           "If several contacts could match the name the result lists them:\n"
           "ask which, then repeat with its exact name. It also refuses a face\n"
           "that already strongly matches someone else. The encrypted template\n"
           "is permanent until forgotten. A visual match is context only,\n"
           "never authorization."),
    schema={"requires": ["person"], "optional": ["phone"]},
    handler="face_identity:enroll_person",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt=("the person's name, encrypted template id, recogniser model and "
             "quality score, after Neo4j accepts the link"),
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_face_identity.py",),
))


declare(Capability(
    name="SET_STANDBY",
    summary=("Puts VANTA into its real standby lifecycle state, blacks out the "
              "screen, pauses background intelligence, and optionally resumes "
              "after an exact number of minutes. With monitor=true, VANTA "
              "periodically checks the camera and, after confirmed person "
              "detection, alerts Himanshu on Telegram and locks Windows. Use "
              "this for any explicit standby request."),
    aliases=("standby", "stand by", "inactive", "dormant", "pause assistant",
             "black screen", "temporarily unavailable"),
    syntax='<ACTION type="SET_STANDBY" minutes="5" monitor="true|false" />',
    notes=("Omit minutes for indefinite standby. Set monitor=true only when\n"
            "Himanshu asks for standby monitoring/security. Ordinary standby\n"
            "does not use the camera. This is a lifecycle control, not an\n"
            "operating-mode switch."),
    schema={"optional": ["minutes", "monitor"]},
    handler="actions:_run_set_standby",
    authorization=MUTATION,
    modes=ALL_MODES,
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_READBACK,
    receipt="active standby state, standby type, duration and state generation",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="RESUME_STANDBY",
    summary="Leaves standby and restores the screen and intelligence pipeline.",
    aliases=("wake", "resume", "leave standby", "exit standby", "come back"),
    syntax='<ACTION type="RESUME_STANDBY" />', schema={"optional": []},
    handler="actions:_run_resume_standby", authorization=MUTATION,
    modes=ALL_MODES, origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_READBACK, receipt="observed inactive standby state",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="STANDBY_STATUS",
    summary="Reads whether standby is active and its remaining duration.",
    aliases=("standby status", "are you active", "are you standing by"),
    syntax='<ACTION type="STANDBY_STATUS" />', schema={"optional": []},
    handler="actions:_run_standby_status", authorization=OBSERVATION,
    modes=ALL_MODES, origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT, receipt="active state and remaining seconds",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="CAMERA_AWARENESS_STATUS",
    summary=("Reads VANTA's latest low-frequency local presence observation "
             "and study ergonomics totals without taking a new photograph."),
    aliases=("presence status", "study attention", "study breaks",
             "posture status", "ergonomics", "camera awareness"),
    syntax='<ACTION type="CAMERA_AWARENESS_STATUS" />',
    schema={"optional": []}, handler="actions:_run_camera_awareness_status",
    authorization=OBSERVATION, modes=ALL_MODES, origins=("user",),
    confirmation=CONFIRM_NEVER, verification=VERIFY_RECEIPT,
    receipt="observation age, presence, attention, ergonomics and study totals",
    tests=("tests/test_camera_awareness.py",),
))


declare(Capability(
    name="LEARN_PROCEDURE",
    summary=("Stores an explicit lasting conditional instruction from Himanshu "
             "as a versionable procedure in Neo4j. Use when he says how VANTA "
             "should behave whenever a situation happens in the future."),
    aliases=("learn preference", "remember instruction", "follow in future",
             "whenever", "from now on", "always do", "procedure", "rule"),
    syntax=('<ACTION type="LEARN_PROCEDURE" trigger="when a webcam image is too dark" '
            'instruction="illuminate the screen, recapture, verify, then restore it" '
            'actions="SCREEN_LIGHT,CAPTURE_CAMERA,SCREEN_LIGHT_OFF" />'),
    notes=("This records behaviour, never permission. Describe the condition\n"
           "generally and use registered action names when a sequence is known."),
    schema={"requires": ["trigger", "instruction"],
            "optional": ["actions", "evidence"]},
    handler="actions:_run_learn_procedure", authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="Neo4j procedure id, trigger and action sequence",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="LIST_PROCEDURES",
    summary="Lists active behavioural procedures explicitly taught by Himanshu.",
    aliases=("learned rules", "procedures", "standing workflows", "preferences"),
    syntax='<ACTION type="LIST_PROCEDURES" />', schema={"optional": []},
    handler="actions:_run_list_procedures", authorization=RETRIEVAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT, receipt="active graph procedures",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="REMOVE_PROCEDURE",
    summary=("Retires one active user-taught behavioural procedure while "
             "preserving its Neo4j history. Use when Himanshu says a previous "
             "ongoing behaviour should stop."),
    aliases=("stop learned rule", "forget procedure", "remove behaviour",
             "do not do that anymore", "stop doing that from now on"),
    syntax='<ACTION type="REMOVE_PROCEDURE" id="procedure_..." />',
    schema={"any_of": [["id"], ["text"]]},
    handler="actions:_run_remove_procedure", authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_READBACK,
    receipt="retired procedure id and inactive graph state",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="WIDGET",
    summary=("Shows, updates, or closes one compact taskbar-free desktop widget "
             "from safe declarative JSON. Use for live timers, metrics, status, "
             "progress, checklists, lists, tables, bar charts, timelines and flows."),
    aliases=("widget", "live on screen", "floating timer", "countdown",
             "compact status", "progress card", "mini table", "live display"),
    syntax=('''<ACTION type="WIDGET" action="show">
{"id":"focus","kind":"timer","title":"Focus", "duration_seconds":900}
</ACTION>'''),
    notes=("The body is JSON data, never HTML or code. action may be show, "
           "update, hide, or close. Reusing an id replaces that widget. "
           "Supported kinds: timer, metric, status, progress, checklist, list, "
           "table, bar_chart, timeline, flow."),
    schema={"body": True, "optional": ["action", "id", "kind", "title"]},
    handler="actions:_run_widget", authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user", "proactive"), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="widget id, type and observed render event",
    tests=("tests/test_widget.py",),
))


declare(Capability(
    name="LIST_CAMERA_CAPTURES",
    summary=("Lists retained photographs by capture id and timestamp. "
             "Captures expire automatically after thirty days."),
    aliases=("camera history", "saved images", "captured photos", "photo archive"),
    syntax='<ACTION type="LIST_CAMERA_CAPTURES" limit="20" />',
    schema={"optional": ["limit"]}, handler="actions:_run_list_camera_captures",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT, receipt="capture ids and timestamps",
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="VIEW_CAMERA_CAPTURE",
    summary=("Opens a retained photograph visibly in VANTA and also loads it "
             "into the current model turn for optional visual analysis. Use "
             "latest or a capture id from the library."),
    aliases=("show captured image", "view camera photo", "analyse saved photo",
             "last retained picture", "earlier capture"),
    syntax='<ACTION type="VIEW_CAMERA_CAPTURE" id="latest" />',
    schema={"optional": ["id"]}, handler="camera_library:resolve",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_ARTIFACT,
    receipt="capture id, visible VANTA image card and image supplied to model",
    runs_in=RUNS_IN_AGENT_LOOP,
    tests=("tests/test_model_driven_adaptation.py",),
))


declare(Capability(
    name="WHO_IS_THIS",
    summary=("Looks through the webcam and says who is in front of it, by "
             "matching the face against every enrolled person and reading that "
             "person's graph node: name, match score and linked phone number. "
             "Use for 'who is this', 'who's that', 'who is in front of me', "
             "'do you know this person'. An unknown face is reported as "
             "unknown and never guessed."),
    aliases=("who is this", "who's this", "whos this", "who is that",
             "who is in front of me", "who is at the camera", "do you know him",
             "do you know her", "do you know this person", "recognise this person",
             "identify this person", "who am i looking at"),
    syntax='<ACTION type="WHO_IS_THIS" />',
    notes=("Takes a fresh frame, retrying a few times if the first has no clear\n"
           "face, and keeps nothing. Report the names and numbers it returns;\n"
           "if a face is unknown, say so and offer to remember it under a name\n"
           "(REMEMBER_FACE). A match is probabilistic: quote the score if asked."),
    schema={"optional": []},
    handler="face_identity:who_is_this",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="each visible face's name, match score and linked phone number, or unknown",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_face_identity.py",),
))


declare(Capability(
    name="LIST_KNOWN_FACES",
    summary=("Lists the people whose encrypted face templates VANTA can "
             "currently match, with template counts but no biometric data."),
    aliases=("known faces", "remembered faces", "enrolled faces",
             "list face templates", "who do you remember"),
    syntax='<ACTION type="LIST_KNOWN_FACES" />',
    # Explicit empty contract: it accepts no attributes. Keeping the key
    # present lets the generated schema distinguish "takes no arguments"
    # from "was never validated".
    schema={"optional": []},
    handler="face_identity:known_people",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the number of known people and template count for each",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_face_identity.py",),
))


declare(Capability(
    name="VERIFY_PRIMARY_FACE",
    summary=("Captures one fresh ephemeral webcam frame and locally checks "
             "whether the enrolled primary person, Himanshu, is visible now. "
             "Use for 'check my face', 'do you recognize me', or a live "
             "identity check; do not substitute LIST_KNOWN_FACES, which only "
             "lists stored templates."),
    aliases=("check my face", "verify me", "verify again", "check again",
             "recognize me", "recognise me",
             "live face check", "current face", "who is at the camera",
             "am i recognized", "am i recognised"),
    syntax='<ACTION type="VERIFY_PRIMARY_FACE" />',
    schema={"optional": []},
    handler="camera_awareness:verify_primary_now",
    authorization=OBSERVATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt=("a fresh local result: primary confirmed, another/unknown face, "
             "unclear face, no visible person, or capture failure"),
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_face_identity.py", "tests/test_camera_awareness.py"),
))


declare(Capability(
    name="FORGET_FACE",
    summary=("Permanently erases every encrypted face template linked to one "
             "named person while leaving non-biometric knowledge intact."),
    aliases=("forget", "erase", "remove", "delete", "face", "facial",
             "identity", "biometric"),
    syntax='<ACTION type="FORGET_FACE" person="Madhu" />',
    notes=("This deletes biometric ciphertext, not the Person or their ordinary\n"
           "graph relationships, and therefore always needs confirmation."),
    schema={"requires": ["person"], "optional": ["approved", "approval_id"]},
    handler="face_identity:forget_person",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_ALWAYS,
    verification=VERIFY_RECEIPT,
    receipt="the person's name and exact number of templates erased",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_face_identity.py",),
))


declare(Capability(
    name="CODING_MODE",
    summary=("Takes over the whole screen with the coding surface: the "
             "project's files, an editor with the symbol outline of whatever "
             "is open, and your own pane beside it. Use it when Himanshu asks "
             "to open a project, start coding, or work on code -- it is where "
             "code is read and written, instead of a page in the Control "
             "Center. Name the project if he named one."),
    # Words that belong to writing code and to almost nothing else. The
    # obvious additions -- "project", "file", "build", "implement" -- are
    # deliberately absent: they are subject nouns that appear in calendar
    # entries, tasks and essays, and with them here the surface ranked first
    # for "move my final-year project review to 1 PM". Sixteen
    # conversational aliases are how the camera came to answer "where am I".
    aliases=("code", "coding", "coder", "programming", "develop",
             "codebase", "repository", "editor", "ide", "workbench",
             "refactor", "debug"),
    syntax='<ACTION type="CODING_MODE" project="portfolio" />',
    notes=("Omit project to open on whichever project was last active, or\n"
           "with none selected if there is none. It does not create a\n"
           "project; CREATE_PROJECT does that, then open it here."),
    schema={"optional": ["project"]},
    handler="workbench:open_request",
    authorization=MUTATION,
    # Not in sleep: a full-screen editor is the single most intrusive surface
    # VANTA has, and nothing about coding is urgent at 3am.
    modes=("assistant", "study", "research", "build"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt=("the project the surface came up on and its file count, read "
             "back from the surface's own state rather than assumed"),
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_workbench.py",),
))


declare(Capability(
    name="PRICE_SEARCH",
    summary=("Retrieves current structured product offers and price statistics "
             "from Google Shopping through the configured SerpApi account. "
             "Use it for current product-price comparisons; do not infer a "
             "price from ordinary search snippets."),
    aliases=("price", "pricing", "cost", "cheapest", "deal", "offer",
             "shopping", "compare", "marketplace", "product"),
    syntax='<ACTION type="PRICE_SEARCH" query="MacBook Air M4 16GB 512GB" limit="8" />',
    notes=("Results are marketplace observations. Check title, variant, seller "
           "and product_id before treating rows as equivalent."),
    schema={"requires": ["query"],
            "optional": ["location", "country", "language", "limit",
                         "product_id"]},
    handler="actions:_run_price_search",
    authorization=RETRIEVAL,
    modes=("assistant", "research", "build"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the provider, query, priced offers, merchants and observed median",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pricing_api.py",),
))


declare(Capability(
    name="PRICE_WATCH_CREATE",
    summary=("Creates a durable, restart-safe product-price watch backed by "
             "structured Google Shopping observations. Use it when Himanshu "
             "asks to be told when a named product reaches a price."),
    aliases=("watch", "monitor", "alert", "notify", "price", "deal",
             "drops", "below", "product", "shopping"),
    syntax='<ACTION type="PRICE_WATCH_CREATE" query="MacBook Air M4 16GB 512GB" product_id="..." target="100000" label="MacBook Air" interval="12h" />',
    notes=("Use the matching product_id from PRICE_SEARCH. The "
           "runtime enforces a quota-safe minimum polling interval."),
    schema={"requires": ["query", "product_id", "target"],
            "optional": ["location", "label", "interval",
                         "currency"]},
    handler="actions:_run_price_watch_create",
    authorization=MUTATION,
    modes=("assistant", "research"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt=("the durable watch id, normalized threshold, polling interval "
             "and initial structured price observation"),
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pricing_api.py",),
))


# --- work that outlives a turn ---------------------------------------- #
# A pursuit is an objective being worked on across days: steps, evidence for
# the ones that are done, and a reason for the ones that are stuck. Declared
# here rather than added to the legacy lists because a declaration derives the
# catalogue, the retrieval index, the schema, the authorization class and the
# mode policy from one place -- which is the whole reason this file exists.

declare(Capability(
    name="START_PURSUIT",
    summary=("Begins tracking something that will take more than one sitting "
             "-- a search, an application round, a syllabus, anything where "
             "the work continues after the conversation ends. Use it as soon "
             "as Himanshu describes work like that, so the thread survives "
             "into the next conversation instead of living in this "
             "transcript. Give the goal as the title and his reason as why."),
    aliases=("pursue", "pursuit", "ongoing", "track", "undertake", "campaign",
             "objective", "throughout", "weeks", "months", "progress"),
    syntax='<ACTION type="START_PURSUIT" title="find a flat in Hyderabad" '
           'why="moving in January" />',
    notes=("Steps can be added now or later with PLAN_PURSUIT. This is not\n"
           "for a single-sitting task, a deadline (TRACK_COMMITMENT) or a\n"
           "condition to watch for (WATCH_CREATE)."),
    schema={"requires": ["title"], "optional": ["why"]},
    handler="pursuits:start",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the pursuit id and how many steps it starts with",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="LIST_PURSUITS",
    summary=("Everything currently being pursued, with how far along each one "
             "is, what is next, and what is in the way. Use it when Himanshu "
             "asks what he is working on, what is outstanding, or where "
             "something stands."),
    aliases=("outstanding", "ongoing", "pending", "progress", "working",
             "status", "unfinished", "open", "pursuits", "where", "stands"),
    syntax='<ACTION type="LIST_PURSUITS" />',
    notes=('Add closed="true" to include finished and abandoned ones.'),
    schema={"optional": ["closed"]},
    handler="pursuits:summary",
    authorization=OBSERVATION,
    modes=ALL_MODES,
    origins=("user", "proactive"),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="each open pursuit with its counted progress, next step and "
            "current blocker",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="PURSUIT_STATUS",
    summary=("One pursuit in full: every step, the evidence behind the "
             "finished ones, and the reason behind anything stuck. Use it "
             "before working on a pursuit, so the next step is the real next "
             "step rather than a guess."),
    aliases=("pursuit", "detail", "steps", "evidence", "blocked", "stuck",
             "progress", "far", "along"),
    syntax='<ACTION type="PURSUIT_STATUS" id="find a flat" />',
    notes=("The id may be the short id or enough of the title to be\n"
           "unambiguous."),
    schema={"requires": ["id"]},
    handler="pursuits:describe",
    authorization=OBSERVATION,
    modes=ALL_MODES,
    origins=("user", "proactive"),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the pursuit's steps with their states, receipts and blockers",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="PLAN_PURSUIT",
    summary=("Adds steps to a pursuit, one per line in the tag body. Use it "
             "once the work is understood well enough to break down, and "
             "again whenever a step turns out to need others first."),
    aliases=("plan", "steps", "break", "down", "split", "stages",
             "milestones", "roadmap", "approach"),
    syntax='<ACTION type="PLAN_PURSUIT" id="a1b2c3d4">\n'
           'shortlist areas within 5km of the office\n'
           'book three viewings\n'
           '</ACTION>',
    notes=("Write the steps as things that can be finished and evidenced,\n"
           "not as topics. A step that cannot produce a receipt cannot be\n"
           "closed."),
    schema={"requires": ["id"]},
    handler="pursuits:plan",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="how many steps were added and the pursuit's progress after",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="ADVANCE_PURSUIT",
    summary=("Records what happened to one step: finished, with the evidence "
             "it produced, or blocked, with what is in the way. A step "
             "closes only against evidence -- a path, an id, something read "
             "back -- never against a claim that it is done."),
    aliases=("done", "finished", "completed", "progress", "advance", "blocked",
             "stuck", "waiting", "evidence", "receipt", "step"),
    syntax='<ACTION type="ADVANCE_PURSUIT" id="a1b2c3d4" step="2" '
           'receipt="booked 3 viewings, confirmation ids 88121/88122/88130" />',
    notes=('Use blocked="..." instead of receipt="..." when the step cannot\n'
           'proceed, and say what would clear it. The step number is the one\n'
           'PURSUIT_STATUS prints.'),
    schema={"requires": ["id", "step"], "optional": ["receipt", "blocked"]},
    handler="pursuits:advance",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the step, the evidence recorded against it, and the pursuit's "
            "progress after",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="CLOSE_PURSUIT",
    summary=("Finishes a pursuit -- achieved, abandoned, or paused for now -- "
             "with what came of it. Use it when the goal is reached or "
             "Himanshu says to drop it, so that what is still open stays an "
             "honest list."),
    aliases=("close", "finish", "achieved", "abandon", "drop", "stop",
             "pause", "shelve", "done", "over"),
    syntax='<ACTION type="CLOSE_PURSUIT" id="a1b2c3d4" '
           'outcome="signed the lease on the Kondapur flat" />',
    notes=('state="abandoned" if it was dropped rather than achieved, or\n'
           'state="paused" to set it aside without losing it.'),
    schema={"requires": ["id"], "optional": ["outcome", "state"]},
    handler="pursuits:close",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the pursuit's final state and how many steps were left unfinished",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_pursuits.py",),
))


declare(Capability(
    name="SYNC_BROWSER_LOGIN",
    summary=("Copies the sessions from Himanshu's own Firefox into the "
             "browser you drive, so a site he is already signed into opens "
             "signed in rather than at a login page. Use it when a page you "
             "opened asks for a sign-in that he says he already has, or "
             "after he has just logged into something new. Cookies only -- "
             "his saved passwords are never read, and his Firefox can stay "
             "open."),
    aliases=("login", "logged", "signin", "signed", "session", "account",
             "cookies", "authenticated", "credentials"),
    syntax='<ACTION type="SYNC_BROWSER_LOGIN" domains="ccbp.in" />',
    notes=("Omit domains to bring everything across. This happens once\n"
           "automatically when the browser starts, so only use it when a\n"
           "session is missing or has just been created."),
    schema={"optional": ["domains"]},
    handler="browser:sync_login",
    authorization=SYSTEM,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt=("how many cookies arrived and which sites they belong to, read "
             "back from the browser; never a cookie value"),
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_browser_login.py",),
))


# --- reading while he is away ------------------------------------------ #
# The background research is autonomous, which makes it the one part of this
# system that acts when nobody asked -- so it has an off switch and a way to
# see what it has done, both his and both declared like everything else.

declare(Capability(
    name="CURIOSITY_LOG",
    summary=("What you read up on in the background while the machine was "
             "resting, in standby or left alone: each subject, why it was "
             "chosen, how much was learned and which pages it came from. "
             "Use it when he asks what you have been doing, studying or "
             "learning while he was away."),
    aliases=("studied", "studying", "learned", "learnt", "researched",
             "reading", "background", "away", "meanwhile"),
    syntax='<ACTION type="CURIOSITY_LOG" />',
    notes=('Add limit="3" for just the latest few.'),
    schema={"optional": ["limit"]},
    handler="curiosity:log_text",
    authorization=OBSERVATION,
    modes=ALL_MODES,
    origins=("user", "proactive"),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="each recent session with its subject, reason, counts and sources",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_curiosity.py",),
))


declare(Capability(
    name="CURIOSITY_PAUSE",
    summary=("Stops background research for a while, or turns it back on. "
             "Use it when he says to stop reading in the background, to "
             "leave the machine alone, or to carry on again."),
    aliases=("stop", "pause", "resume", "quiet", "enough", "again",
             "researching", "background"),
    syntax='<ACTION type="CURIOSITY_PAUSE" minutes="480" />',
    notes=('minutes="0" turns it back on. Use a large number such as 10080\n'
           'for "until I say otherwise".'),
    schema={"requires": ["minutes"]},
    handler="curiosity:pause",
    authorization=MUTATION,
    modes=ALL_MODES,
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="until when background research is paused, or that it is on",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_curiosity.py",),
))


# --- YouTube: discovery through the API, control through the browser ----- #

declare(Capability(
    name="YOUTUBE_SEARCH",
    summary=("Searches YouTube through the official YouTube Data API and "
             "returns stable video ids, titles, channels and URLs. Use this "
             "instead of a general web search when Himanshu asks to find a "
             "video on YouTube."),
    aliases=("youtube", "video", "videos", "channel", "watch", "find",
             "search", "results", "clip", "walkthrough"),
    syntax='<ACTION type="YOUTUBE_SEARCH" query="Spider-Man gameplay" limit="5" />',
    notes=("This discovers videos; it does not claim one is playing. Use\n"
           "YOUTUBE_PLAY for playback."),
    schema={"requires": ["query"], "optional": ["limit", "order"]},
    handler="providers.youtube_api:search_receipt",
    authorization=RETRIEVAL,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="the number of official API results plus each video id and URL",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_youtube.py",),
))

declare(Capability(
    name="YOUTUBE_PLAY",
    summary=("Finds a YouTube video through the official API and starts it "
             "in VANTA's controlled Firefox window. Use query for a search, "
             "or video_id/url when the exact video is already known."),
    aliases=("youtube", "play", "watch", "video", "start", "open",
             "walkthrough", "clip"),
    syntax='<ACTION type="YOUTUBE_PLAY" query="Spider-Man gameplay" />',
    notes=("The receipt reads playback state from the HTML video element.\n"
           "Do not say it is playing unless playing=true."),
    schema={"any_of": [["query"], ["video_id"], ["url"]],
            "optional": ["tab"]},
    handler="providers.youtube_api:resolve_video",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_READBACK,
    receipt="the YouTube URL, player ready state and verified playing flag",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_youtube.py",),
))

declare(Capability(
    name="YOUTUBE_FULLSCREEN",
    summary=("Makes the currently controlled YouTube video enter or leave "
             "video full-screen using the player's own control, then reads "
             "document.fullscreenElement back to verify the result."),
    aliases=("youtube", "video", "fullscreen", "full-screen", "maximize",
             "cinema", "player", "expand", "exit"),
    syntax='<ACTION type="YOUTUBE_FULLSCREEN" enabled="true" tab="optional-t1" />',
    notes=("Use this directly for 'full screen' when a YouTube video is\n"
           "playing. Never use desktop OCR or a guessed F-key."),
    schema={"optional": ["enabled", "tab"]},
    handler="browser:youtube_fullscreen",
    authorization=MUTATION,
    modes=("assistant", "study", "research", "build", "health"),
    origins=("user",),
    confirmation=CONFIRM_NEVER,
    verification=VERIFY_READBACK,
    receipt="the current YouTube URL and verified fullscreen boolean",
    runs_in=RUNS_IN_DISPATCHER,
    tests=("tests/test_youtube.py",),
))


declare(Capability(
    name="OPEN_PAPER",
    summary=("Resolves an academic paper by bibliographic identity and opens the "
             "single verified canonical result. Use this instead of inventing a "
             "paper URL or chaining generic browser navigation."),
    aliases=("open paper", "read paper", "full paper", "academic paper",
             "journal article", "research article", "open this publication"),
    syntax=('<ACTION type="OPEN_PAPER" title="exact paper title" '
            'author="optional author" year="optional year" tab="optional-t1" />'),
    notes=("Run once per requested paper. Never follow it with a guessed alternate "
           "URL when its receipt reports a verified match."),
    schema={"requires": ["title"],
            "optional": ["author", "year", "tab", "background"]},
    handler="actions:_run_open_paper", authorization=RETRIEVAL,
    origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_RECEIPT,
    receipt="matched title, source, identity score, and opened canonical URL",
    tests=("tests/test_open_paper.py",),
))

declare(Capability(
    name="ML_BUILD",
    summary=("Runs an end-to-end machine-learning build in the background: searches "
             "Kaggle or uses an owner/dataset reference, downloads bounded data, "
             "profiles it, cleans a separate copy, trains and evaluates a neural "
             "network, and saves a reusable model with receipts."),
    aliases=("machine learning", "ml model", "neural network", "kaggle dataset",
             "download data", "clean data", "train model", "fit model",
             "classification", "regression", "automated ml"),
    syntax=('<ACTION type="ML_BUILD" query="problem or dataset topic" '
            'dataset="optional-owner/dataset" target="optional-column" '
            'project="optional-name" model="neural_network" />'),
    notes=("Build Mode only. Long work is checkpointed and does not block voice or chat.\n"
           "The Kaggle token stays inside the provider and is never put in a prompt."),
    schema={"any_of": [["query"], ["dataset"]],
            "optional": ["query", "dataset", "target", "project", "model"]},
    handler="actions:_run_ml_build", authorization=SYSTEM,
    modes=("build",), origins=("user",), confirmation=CONFIRM_NEVER,
    verification=VERIFY_ARTIFACT,
    receipt="job id followed by dataset, cleaned copy, metrics and saved model artifact",
    tests=("tests/test_ml_build.py",),
))

declare(Capability(
    name="ML_BUILD_STATUS",
    summary="Reads the current stage, errors, metrics and artifacts of one ML build.",
    aliases=("ml build status", "training progress", "model progress", "training status"),
    syntax='<ACTION type="ML_BUILD_STATUS" id="ml-..." />',
    schema={"requires": ["id"]}, handler="actions:_run_ml_build",
    authorization=OBSERVATION, modes=("build",), origins=("user",),
    confirmation=CONFIRM_NEVER, verification=VERIFY_RECEIPT,
    receipt="durable ML job stage and any produced artifacts",
    tests=("tests/test_ml_build.py",),
))

declare(Capability(
    name="ML_BUILD_LIST",
    summary="Lists recent resumable ML builds and their stages.",
    aliases=("list model builds", "recent training jobs", "ml jobs"),
    syntax='<ACTION type="ML_BUILD_LIST" limit="20" />',
    schema={"optional": ["limit"]}, handler="actions:_run_ml_build",
    authorization=OBSERVATION, modes=("build",), origins=("user",),
    confirmation=CONFIRM_NEVER, verification=VERIFY_RECEIPT,
    receipt="recent ML job ids, statuses and stages",
    tests=("tests/test_ml_build.py",),
))

declare(Capability(
    name="ML_BUILD_CANCEL",
    summary="Cancels a running ML build at its next safe checkpoint.",
    aliases=("cancel training", "abort model build", "stop ml job"),
    syntax='<ACTION type="ML_BUILD_CANCEL" id="ml-..." />',
    schema={"requires": ["id"]}, handler="actions:_run_ml_build",
    authorization=MUTATION, modes=("build",), origins=("user",),
    confirmation=CONFIRM_NEVER, verification=VERIFY_READBACK,
    receipt="cancellation request followed by cancelled job state",
    tests=("tests/test_ml_build.py",),
))


# ====================================================================== #
# Generated consumers
# ====================================================================== #

#: Catalogue section the generated entries are published under. A section of
#: its own, so that migrated capabilities are visibly generated and a hand edit
#: to this text is obviously the wrong move.
declare(Capability(
    name='WINDOWS_RADIO',
    summary='Read or switch the actual Windows Wi-Fi or Bluetooth radio, verifying its observed state. Use for turn wifi off/on, disconnect wireless radio, enable Bluetooth, or check wireless state.',
    aliases=('wifi wireless bluetooth radio network disconnect reconnect enable disable switch',),
    syntax='<ACTION type="WINDOWS_RADIO" kind="wifi|bluetooth|all" state="status|on|off" />',
    schema={'optional': ['kind', 'state']}, handler='actions:_run_windows_radio',
    authorization=SYSTEM, verification=VERIFY_READBACK,
    receipt='Windows radio kind, name, prior state and observed resulting state, or explicit OS permission failure',
    tests=('tests/test_windows_radios.py',),
    notes='kind=all is read-only. Turning Wi-Fi off disconnects network access. Send the brief action speech before execution; no cloud follow-up is required for readback. Turning a radio on does not prove a particular network or headset connected. Do not disable all PnP devices.'))

GENERATED_CATALOG = "CAPABILITIES"


def names():
    """Every declared capability name."""
    return set(CAPABILITIES)


def get(name):
    return CAPABILITIES.get(str(name or "").upper())


def by_execution_site(site):
    return {name: cap for name, cap in CAPABILITIES.items()
            if cap.runs_in == site}


def catalog_section():
    """The model-facing catalogue text for every declared capability."""
    if not CAPABILITIES:
        return ""
    blocks = [f"{len(CAPABILITIES)}. CAMERA & SCREEN DETAIL:"]
    for name in sorted(CAPABILITIES):
        blocks.append(CAPABILITIES[name].catalog_text())
    return "\n".join(blocks)


def catalog_overlay():
    """What agent.ACTION_CATALOGS should be merged with.

    Returned as a mapping so the caller merges rather than replaces; the
    existing fifteen sections stay exactly as they are.
    """
    section = catalog_section()
    return {GENERATED_CATALOG: section} if section else {}


def alias_overlay():
    """Retrieval vocabulary, in the shape action_registry already uses."""
    return {name: cap.alias_text() for name, cap in CAPABILITIES.items()
            if cap.aliases}


def schema_overlay():
    """Input schemas, in the shape action_schema.ACTION_SPECS already uses."""
    return {name: cap.schema_spec() for name, cap in CAPABILITIES.items()
            if cap.schema}


def authorized_names():
    """Names that must be present in autonomy.KNOWN_ACTIONS."""
    return set(CAPABILITIES)


def mode_blocked_names(mode):
    """Declared capabilities that this mode must refuse."""
    mode = str(mode or "").lower()
    return {name for name, cap in CAPABILITIES.items() if mode not in cap.modes}


def confirmation_names():
    """Declared capabilities needing a second confirmation for any origin."""
    return {name for name, cap in CAPABILITIES.items()
            if cap.confirmation == CONFIRM_ALWAYS}


def proactive_blocked_names():
    """Declared capabilities that a background task may not run."""
    return {name for name, cap in CAPABILITIES.items()
            if "proactive" not in cap.origins}
