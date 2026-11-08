# -*- coding: utf-8 -*-
"""
capability_vocab.py -- the words people use for an operation, not for a thing.

Retrieval kept failing in one direction. "Move my final-year project review to
1 PM" returned GITHUB_READ_REPO, LIST_PROJECTS and PROJECT_TREE, because the
subject noun "project" matched those names outright while the operation --
move an existing calendar entry -- matched nothing. "Remind me at 4:30"
retrieved no alarm at all. "Tell Madhu I'll be late" retrieved BOOK_RIDE.

The existing alias table is heavily subject-noun shaped: it bridges "drive
contents" to LIST_DIRECTORY, which is a noun gap. What was missing is the verb
side. A request is mostly an operation applied to a subject, and VANTA owns
several hundred capabilities whose names are almost entirely nouns, so the noun
half of a sentence was doing all the ranking work.

This file is the verb half: what the user is trying to *do*. Keeping it apart
from the noun aliases is not tidiness -- it is so that neither half can be
quietly enlarged until it outvotes the other, which is how the camera's
sixteen conversational aliases came to rank a camera first for "where am i"
and for "look up the deadline".

RULES FOR ENTRIES
-----------------
Words and short collocations only. Never a whole sentence a user said: the
point is to generalise over how anyone might phrase an operation, and a
memorised sentence generalises to exactly one person on exactly one day.

No stopwords, no copulas, no pronouns. They match everything, which means they
discriminate nothing, and they are filtered at scoring time anyway.

Prefer words that belong to one operation. "start" belongs to launching, to
beginning a task and to creating a project, so it earns little and misleads
often; "reschedule" belongs to exactly one thing.
"""

#: Operation vocabulary by canonical action name.
#: Grouped by what the user is trying to achieve, because that is how the
#: gaps were found -- a whole area (messaging, quiet states) would be missing
#: its verbs while its nouns were well covered.
OPERATION_VOCAB = {
    # --- calendar: the verbs of moving and cancelling time -------------- #
    # "push my review an hour later" matched nothing: the existing aliases
    # covered "move/reschedule" but not push, shift, delay, bring forward.
    "UPDATE_CALENDAR_EVENT": (
        "push shift delay postpone advance bring forward later earlier "
        "rearrange adjust amend correct fix wrong rather instead"),
    # The second line is what a person says when an entry has stopped being
    # needed rather than when it is being cancelled: "you keep reminding me",
    # "dismiss that". The verbs for cancelling were here; the way a finished
    # thing's reminder is complained about was not, so a sentence of
    # exactly that shape reached browser actions and no calendar tool.
    "DELETE_CALENDAR_EVENT": (
        "cancel scrap drop call off not happening no longer "
        "delete remove clear dismiss reminding reminded nagging entry"),
    "ADD_CALENDAR_EVENT": (
        "book block reserve set aside pencil arrange put down"),
    "READ_CALENDAR": (
        "today tomorrow tonight morning afternoon evening week weekend "
        "coming up on next planned lined up busy"),
    "LIST_CALENDAR_EVENTS": (
        "today tomorrow week upcoming coming up lined up planned"),
    "FIND_FREE_SLOTS": (
        "free spare available gap opening window when can"),

    # --- reminders, alarms, tasks -------------------------------------- #
    # "remind me at 4:30" reached no alarm action whatsoever.
    "SET_ALARM": (
        "remind reminder alarm wake nudge ping prod alert buzz "
        "at oclock half past quarter"),
    "ADD_ALARM": "remind reminder alarm wake nudge alert",
    "DELETE_ALARM": "cancel remove kill silence stop alarm reminder",
    "LIST_ALARMS": "alarms reminders set pending upcoming",
    "ADD_TASK": (
        "remind todo task jot note down keep track dont forget "
        "must need to should later"),
    "ADD_TASKS": "todo tasks list several multiple jot note down",
    "LIST_TASKS": "todo tasks outstanding pending left remaining still",
    "COMPLETE_TASK": (
        "finished done completed did ticked off sorted handled dealt with"),
    "COMPLETE_COMMITMENT": (
        "finished done completed kept promised commitment "
        "reminding nagging dismiss handled"),

    # --- messaging: almost no verb coverage existed --------------------- #
    # "tell madhu i'll be late" and "let madhu know" both returned nothing
    # resembling a message.
    "SEND_WHATSAPP": (
        "tell let know message text ping whatsapp write say reply "
        "running late behind heads up"),
    "SEND_TELEGRAM": (
        "tell let know message text ping telegram write say reply"),
    "SEND_EMAIL": (
        "email mail write send reply forward compose draft attach"),
    "LOOKUP_CONTACTS": (
        "number contact details who phone reach address someone person"),
    "READ_EMAILS": "inbox unread mail anything new came in",

    # --- quiet states --------------------------------------------------- #
    # "i'm going to sleep" and "i'm watching a movie" reached nothing, so the
    # quiet state depended on the model improvising.
    "QUIET_FOR": (
        "sleep sleeping bed goodnight movie film watching focus studying "
        "quiet silence leave alone dont disturb busy later hold off"),

    # --- music ---------------------------------------------------------- #
    "MUSIC_NOW_PLAYING": (
        "song track tune what playing currently name this"),
    "SPOTIFY_NOW_PLAYING": "song track tune what playing currently",
    "MUSIC_PLAY": "play put on listen something music",
    "MUSIC_PAUSE": "stop pause quiet silence enough off",
    "VOLUME": "louder quieter down up turn volume loud soft",

    # --- knowledge and web --------------------------------------------- #
    # "look up ..." matched the camera, because "look" was a camera alias.
    "SEARCH_WEB": (
        "look up search find out check google web online lookup "
        "whats the when is deadline"),
    "KNOWLEDGE_SEARCH": "look up search find out check learn about",
    "DEEP_RESEARCH": "research investigate dig into thorough properly",
    "RECALL_FACTS": (
        "know remember recall told you about mentioned earlier "
        "what do you know"),
    "SEARCH_RECALL": "remember recall earlier told mentioned past",
    "REMEMBER_FACT": "remember note keep store dont forget save fact",
    "ML_BUILD": ("build train fit create machine learning ml neural network model "
                 "kaggle dataset clean preprocess evaluate automate"),
    "ML_BUILD_STATUS": "training progress model build status metrics stage",
    "ML_BUILD_CANCEL": "cancel abort stop training model build",

    # --- travel --------------------------------------------------------- #
    "GET_TRAVEL_TIME": (
        "long far distance away reach airport station office get there "
        "drive ride travel time"),
    "SHOW_ROUTE": "route directions way map navigate show there",
    "GEOCODE": (
        "where location place am i currently located position address "
        "which city town area"),
    "GET_WEATHER": (
        "weather rain hot cold temperature outside forecast umbrella"),

    # --- documents and uploads ------------------------------------------ #
    # "find jobs and create a word document" retrieved only BROWSER_* -- no
    # SEARCH_WEB and no SAVE_FILE. Both halves of that request were
    # unreachable, so no shortlist could have rescued it.
    "SAVE_FILE": (
        "document doc docx word file report notes write create make save "
        "generate spreadsheet txt pdf desktop documents folder put"),
    "WRITE_FILE": (
        "document doc docx word file report notes write create save "
        "generate txt desktop documents"),
    "DRIVE_UPLOAD": (
        "upload drive google cloud backup push send copy store put file "
        "document pdf share remote"),
    "DRIVE_SEARCH": "drive google cloud find file document stored remote",
    "GET_SELECTION": "this selected highlighted code function error text paragraph explain fix what does selection clipboard copied screen",
    "OPEN_BRAIN_MAP": "brain graph map knowledge constellation visualize visualise memory network nodes neo4j contacts connections show open",
    "DRIVE_READ": "read open summarise summarize contents drive google doc document notes sheet slides says",
    "DRIVE_RECENT": "recent latest lately modified changed working drive google files documents",
    "DRIVE_RESTORE": "restore recover undelete bring back trash deleted drive file",
    "PEOPLE_SEARCH": "contact contacts person find look up email address number phone who people google",
    "PEOPLE_GET": "contact details person info phone email birthday organization",
    "PEOPLE_CREATE": "save add new create contact person number email address book",
    "PEOPLE_UPDATE": "update change edit add number email contact person company birthday",
    "PEOPLE_DELETE": "delete remove contact person address book",
    "CREATE_PRESENTATION": "presentation powerpoint ppt pptx slides slide deck pitch",
    "CREATE_DOCUMENT": "word docx report letter essay resume cv proposal memo",
    "CREATE_SPREADSHEET": "spreadsheet excel xlsx budget tracker columns rows",
    "CREATE_PDF": "pdf handout printable export",
    "EXTRACT_TEXT": "summarise summarize extract contents pptx docx xlsx pdf presentation",
    "OPEN_PATH": "reveal explorer folder directory",
    "DOWNLOAD_FILE": "download fetch url link",
    "ZIP_PATH": "zip compress archive folder file pack",
    "UNZIP_FILE": "unzip extract decompress archive zip unpack",
    "CALCULATE": "calculate compute math percent percentage sum multiply divide square root tip discount",
    "TIME_IN": "time zone timezone what time clock convert world city country",
    "NOTE_ADD": "note jot idea memo",
    "NOTES_LIST": "notes jotted noted",
    "NOTE_DELETE": "delete remove note",
    "PDF_MERGE": "merge combine pdf pdfs",
    "PDF_EXTRACT": "pages split pdf range",

    # --- website workflows ----------------------------------------------- #
    # "apply for my college gate pass on the student portal" retrieved
    # TRENDING_NEWS, MOVE_FILE and COPY_FILE. Nothing connected a portal, an
    # application or a pass to the browser at all.
    # No topic nouns here -- not "gate pass", "outpass" or "hostel". A noun
    # names what a sentence is ABOUT, not what is being done to it, and these
    # matched on every mention: "you keep reminding me about the gate pass"
    # pulled the whole browser bundle and pushed out the calendar tools that
    # sentence needed. The verbs (apply, application, form, portal) already
    # carry the operation, so "apply for my gate pass" still arrives here.
    "BROWSER_GOTO": (
        "portal website site page open visit navigate login signin student "
        "college apply application form"),
    "BROWSER_SNAPSHOT": (
        "portal page form fields read see what is on the page inspect "
        "elements buttons options"),
    "BROWSER_FILL_FORM": (
        "form fill apply application submit register enrol enroll portal "
        "fields details request"),
    "BROWSER_CLICK": (
        "portal form button click choose select submit apply add new option"),
    "BROWSER_DIALOG": (
        "browser javascript alert confirm prompt popup dialog modal accept "
        "dismiss okay cancel"),
    "BROWSER_SUBMIT": "submit apply send form application portal confirm",
    "BROWSER_UPLOAD": "upload attach file document portal form browse choose",

    # --- teaching and visual explanation --------------------------------- #
    # Blackboard vocabulary deliberately resolves to the report during the
    # migration, so an old habit such as "put this on the board" still works
    # without reviving the retired full-screen surface.
    "RESULT_WINDOW": (
        "report board blackboard whiteboard explain teach derive working steps "
        "lesson formula equation latex maths math plot graph chart curve table "
        "diagram flowchart tree architecture pipeline process network compare "
        "comparison matrix visualise visualize show me how understand"),

    # --- code ----------------------------------------------------------- #
    # The verb half of coding: what someone is trying to do, never the noun
    # "project". "Move my final-year project review to 1 PM" is a calendar
    # change whose subject happens to contain that word, and an earlier draft
    # of this entry claimed it -- putting three project actions in the
    # shortlist and pushing UPDATE_CALENDAR_EVENT out of it. The noun half
    # lives in the capability's own aliases, where it is scored separately and
    # cannot outvote the verb half.
    "CODING_MODE": (
        "coding code editor ide workbench programming develop refactor "
        "implement rewrite debug codebase open launch workspace"),
    "SHOW_CODE_PANEL": "show view display code codes source panel editor",
    "RUNTIME_APPLY": (
        "build create generate model construct modify update change connect "
        "link wire attach add remove arrange transform extrude animate render "
        "architecture scene blender layers nodes components incremental"),
    "RUNTIME_STATUS": (
        "inspect examine current existing scene model structure objects links "
        "edges state revision"),
    "RUNTIME_RESUME": "resume reopen restore continue blender scene runtime",
    "RUNTIME_PRESENT": (
        "present presentation showcase model only fullscreen blender viewport "
        "orbit pan zoom hide interface exit presentation"),

    # --- work in flight --------------------------------------------------- #
    # The verb half of "this will take a while". Without these, work spanning
    # days reached ADD_TASK or TRACK_COMMITMENT -- a single item or a deadline,
    # neither of which holds steps, evidence or a blocker.
    "START_PURSUIT": (
        "pursue ongoing spanning gradually eventually throughout undertake"),
    "LIST_PURSUITS": (
        "outstanding ongoing unfinished pending midway halfway middle working"),
    "PURSUIT_STATUS": "stands standing far along progressing stuck waiting",
    "ADVANCE_PURSUIT": "finished ticked advanced progressed stuck blocked",
    "CLOSE_PURSUIT": "achieved abandoned dropped shelved abandon giving quit",

    # --- machine -------------------------------------------------------- #
    "FIND_FILES": "resume cv document file named called somewhere",
    "LIST_PROCESSES": "battery slow heavy hogging using eating running apps",
    "SYSTEM_STATUS": "battery slow performance health cpu memory disk",
    "SET_BRIGHTNESS": "bright dim darker lighter screen brightness",
    "LOCK_SCREEN": "lock away leaving secure",
    "TAKE_SCREENSHOT": (
        "screen screenshot showing displayed window looking at right now"),
    "READ_SCREEN_TEXT": "screen text read showing displayed says",
}


def vocabulary():
    """Operation vocabulary, in the shape action_registry merges aliases in."""
    return dict(OPERATION_VOCAB)


def names():
    return set(OPERATION_VOCAB)
