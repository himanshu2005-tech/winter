"""User-outcome contracts for VANTA's external integrations.

Provider SDKs contain many helper methods and upstream APIs contain thousands
of endpoints.  Counting methods is therefore not a useful completeness test.
This module records the practical operations VANTA promises for each connected
service and lets startup/tests verify that every promised operation remains a
real, discoverable capability.

Adding an integration means adding its intended outcome surface here.  If a
later refactor drops one handler, schema, authorization entry, or descriptor,
the normal capability integrity gate identifies the exact missing action.
"""

INTEGRATION_ACTIONS = {
    "gmail": frozenset({
        "READ_EMAILS", "SEND_EMAIL", "REPLY_EMAIL",
        "DOWNLOAD_EMAIL_ATTACHMENT", "MARK_EMAIL_READ", "ARCHIVE_EMAIL",
    }),
    "google_calendar": frozenset({
        "READ_CALENDAR", "LIST_CALENDAR_EVENTS", "ADD_CALENDAR_EVENT",
        "UPDATE_CALENDAR_EVENT", "DELETE_CALENDAR_EVENT", "FIND_FREE_SLOTS",
    }),
    "google_tasks": frozenset({
        "LIST_TASK_LISTS", "LIST_TASKS", "ADD_TASK", "ADD_TASKS",
        "UPDATE_TASK", "COMPLETE_TASK", "DELETE_TASK",
    }),
    "google_drive": frozenset({
        "DRIVE_LIST", "DRIVE_SEARCH", "DRIVE_INFO", "DRIVE_READ",
        "DRIVE_RECENT", "DRIVE_DOWNLOAD", "DRIVE_UPLOAD",
        "DRIVE_CREATE_FOLDER", "DRIVE_RENAME", "DRIVE_MOVE", "DRIVE_COPY",
        "DRIVE_TRASH", "DRIVE_RESTORE", "DRIVE_SHARE",
    }),
    "google_contacts": frozenset({
        "PEOPLE_SEARCH", "PEOPLE_GET", "PEOPLE_CREATE", "PEOPLE_UPDATE",
        "PEOPLE_DELETE", "SYNC_GOOGLE_CONTACTS",
    }),
    "github": frozenset({
        "GITHUB_LIST_REPOS", "GITHUB_SEARCH", "GITHUB_READ_REPO",
        "GITHUB_READ_FILE",
    }),
    "telegram": frozenset({
        "SEND_TELEGRAM", "SEND_TELEGRAM_FILE", "LIST_TELEGRAM_FILES",
    }),
    "spotify": frozenset({
        "SPOTIFY_PLAY", "SPOTIFY_PAUSE", "SPOTIFY_SKIP",
        "SPOTIFY_NOW_PLAYING", "SPOTIFY_SEARCH", "SPOTIFY_VOLUME",
        "SPOTIFY_QUEUE",
    }),
    "youtube": frozenset({
        "YOUTUBE_SEARCH", "YOUTUBE_PLAY", "YOUTUBE_FULLSCREEN",
    }),
    "maps": frozenset({"GEOCODE", "GET_TRAVEL_TIME", "SHOW_ROUTE"}),
    "public_data": frozenset({
        "GET_WEATHER", "GET_AIR_QUALITY", "INDIA_POST_LOOKUP",
    }),
}


def missing(available):
    """Return ``{integration: missing actions}`` for an action-name set."""
    names = {str(name).upper() for name in (available or ())}
    return {
        integration: tuple(sorted(expected - names))
        for integration, expected in INTEGRATION_ACTIONS.items()
        if expected - names
    }


def promised_names():
    result = set()
    for names in INTEGRATION_ACTIONS.values():
        result.update(names)
    return frozenset(result)
