# -*- coding: utf-8 -*-
"""
The retrieval corpus: can VANTA reach its own capabilities from plain speech?

Every failure in this suite looked, from the outside, like a missing feature.
"Correct my review time and remind me at 4:30" retrieved no alarm action, so
VANTA behaved as though it could not set reminders. "Tell Madhu I'll be late"
retrieved no messaging action. Asked what it could see, VANTA said it had no
camera. In all three cases the implementation existed and worked; the ranking
simply never put it in front of the model.

So this is the file that decides whether VANTA is intelligent in the only sense
that matters day to day: given how a person actually speaks, does the right
tool come up. It is also the regression gate for retrieval tuning. It exists
because a sixteen-alias improvement to the camera put CAPTURE_CAMERA at rank 1
for "where am i" -- a change that fixed the case it was aimed at and quietly
broke an unrelated one, with nothing to catch it.

HOW THESE ASSERT
----------------
On rank thresholds, never exact orderings. A retrieval change that moves
ADD_TASK from rank 4 to rank 2 is not a regression and must not read as one,
or the suite becomes something people edit to stay green. What matters is:
the capability is inside the bounded shortlist the model actually sees, and
nothing irrelevant is sitting at the top of it.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_registry
import capabilities

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The shortlist size the agent really uses for an ordinary turn.
SHORTLIST = 12


def _catalogs():
    source = open(os.path.join(BASE_DIR, "agent.py"), encoding="utf-8",
                  errors="replace").read()
    catalogs = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_CATALOGS":
                    catalogs = ast.literal_eval(node.value)
    catalogs.update(capabilities.catalog_overlay())
    return catalogs


# ====================================================================== #
# The corpus
# ====================================================================== #
# (request, any_of)  -- at least one of any_of must make the shortlist.
# Phrased the way Himanshu actually speaks, including the sentences that
# failed in real use. No request names an internal action, because needing to
# say "CAPTURE_CAMERA" or "do you not have calendar controls" is the bug.

CORPUS = [
    # --- the sentences that actually failed ----------------------------- #
    ("correct my review time and remind me at 4:30",
     ["UPDATE_CALENDAR_EVENT"]),
    ("tell madhu i'll be late",
     ["SEND_WHATSAPP", "SEND_TELEGRAM", "LOOKUP_CONTACTS"]),
    ("where am i",
     ["GEOCODE", "GET_WEATHER"]),
    ("what can you see",
     ["CAPTURE_CAMERA", "TAKE_SCREENSHOT"]),
    ("look through the camera and tell me what i'm holding",
     ["CAPTURE_CAMERA"]),

    # --- calendar: operation intent, not the word "calendar" ------------ #
    ("move my meeting to 1 pm", ["UPDATE_CALENDAR_EVENT"]),
    ("push my review an hour later", ["UPDATE_CALENDAR_EVENT"]),
    ("cancel tomorrow's standup", ["DELETE_CALENDAR_EVENT"]),
    ("what do i have on tomorrow",
     ["READ_CALENDAR", "LIST_CALENDAR_EVENTS"]),
    ("block out two hours on friday afternoon", ["ADD_CALENDAR_EVENT"]),
    ("when am i free this week", ["FIND_FREE_SLOTS"]),

    # --- reminders and tasks -------------------------------------------- #
    ("remind me at 4:30 to start my new project",
     ["SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"]),
    ("wake me at 6", ["SET_ALARM", "ADD_ALARM"]),
    ("add buy milk to my list", ["ADD_TASK", "ADD_TASKS"]),
    ("what's still on my list", ["LIST_TASKS"]),
    ("i finished the rl review", ["COMPLETE_TASK", "COMPLETE_COMMITMENT"]),

    # --- messaging: the operation is "tell/send", not an app name -------- #
    ("let madhu know i'm running behind",
     ["SEND_WHATSAPP", "SEND_TELEGRAM", "LOOKUP_CONTACTS"]),
    ("message my mother that i'll call tonight",
     ["SEND_WHATSAPP", "SEND_TELEGRAM", "LOOKUP_CONTACTS"]),
    ("email the team the updated deck", ["SEND_EMAIL"]),
    ("what's in my inbox", ["READ_EMAILS"]),

    # --- music ----------------------------------------------------------- #
    ("play something calm", ["MUSIC_PLAY", "SPOTIFY_PLAY"]),
    ("stop the music", ["MUSIC_PAUSE", "SPOTIFY_PAUSE"]),
    ("what's this song", ["MUSIC_NOW_PLAYING", "SPOTIFY_NOW_PLAYING"]),
    ("turn it down", ["VOLUME", "SPOTIFY_VOLUME"]),

    # --- machine and files ----------------------------------------------- #
    ("find my resume", ["FIND_FILES"]),
    ("what's eating my battery", ["LIST_PROCESSES", "SYSTEM_STATUS"]),
    ("screen's too bright", ["SET_BRIGHTNESS"]),
    ("lock the laptop", ["LOCK_SCREEN"]),
    ("what's on my screen right now",
     ["TAKE_SCREENSHOT", "READ_SCREEN_TEXT"]),
    ("this text is too small to read", ["ZOOM", "READ_SCREEN_TEXT"]),
    ("list the saved wifi networks on this computer", ["RUN_COMMAND"]),

    # --- web, research, travel ------------------------------------------- #
    ("look up the amazon ml challenge deadline",
     ["SEARCH_WEB", "KNOWLEDGE_SEARCH", "DEEP_RESEARCH"]),
    ("how long to get to the airport", ["GET_TRAVEL_TIME"]),
    ("show me the route there", ["SHOW_ROUTE", "GET_TRAVEL_TIME"]),
    ("what's the weather like", ["GET_WEATHER"]),
    ("open chrome", ["BROWSER_START", "LAUNCH_APP"]),
    ("upload my resume to this job application", ["BROWSER_UPLOAD"]),

    # --- memory ---------------------------------------------------------- #
    ("remember that madhu's birthday is in march",
     ["REMEMBER_FACT", "TRACK_COMMITMENT"]),
    ("what do you know about my project deadlines",
     ["RECALL_FACTS", "SEARCH_RECALL"]),

    # --- quiet states ---------------------------------------------------- #
    ("i'm going to sleep", ["QUIET_FOR"]),
    ("i'm watching a movie", ["QUIET_FOR"]),

    # --- background reading ----------------------------------------------- #
    ("what have you been studying while i was away", ["CURIOSITY_LOG"]),
    ("what did you learn in the background", ["CURIOSITY_LOG"]),
    ("stop researching things in the background", ["CURIOSITY_PAUSE"]),
    ("leave the machine alone while i sleep", ["CURIOSITY_PAUSE"]),

    # --- a finished thing stops needing its reminder ---------------------- #
    # The sentence that failed in real use: told the gate pass was done, the
    # model said calendar actions were unavailable, because a phantom "apply
    # for it" outcome had replaced the real intent in retrieval.
    ("its completed, delete the event",
     ["DELETE_CALENDAR_EVENT"]),
    ("i already did that, remove it from my calendar",
     ["DELETE_CALENDAR_EVENT"]),

    # --- work in flight --------------------------------------------------- #
    ("i want to start looking for a flat, it'll take weeks",
     ["START_PURSUIT"]),
    ("what am i in the middle of", ["LIST_PURSUITS"]),
    ("what's still outstanding", ["LIST_PURSUITS", "LIST_TASKS"]),
    ("how far along is the flat search", ["PURSUIT_STATUS", "LIST_PURSUITS"]),
    ("that's done now, tick it off", ["ADVANCE_PURSUIT", "COMPLETE_TASK"]),
    ("i'm giving up on that one", ["CLOSE_PURSUIT"]),

    # --- code ------------------------------------------------------------ #
    ("open project", ["CODING_MODE", "OPEN_PROJECT"]),
    ("activate coding mode", ["CODING_MODE"]),
    ("let's work on the codebase", ["CODING_MODE"]),
    ("i want to write some code", ["CODING_MODE", "WRITE_CODE"]),
    ("refactor this with me", ["CODING_MODE", "WRITE_CODE"]),

    # --- persistent executable projects --------------------------------- #
    ("throw me a basic neural network with ten layers in blender",
     ["RUNTIME_APPLY"]),
    ("connect a layer one node to two nodes in layer two",
     ["RUNTIME_APPLY"]),
    ("modify the roof in my blender scene", ["RUNTIME_APPLY"]),
    ("show me the current objects in that model", ["RUNTIME_STATUS"]),
]

#: Requests where something must NOT be at the top of the shortlist.
#: These are the regressions tuning produces: a capability that earns its
#: place for one phrasing and then colonises unrelated ones.
NEGATIVE_CORPUS = [
    # The camera's conversational aliases put it first for a location
    # question, because an alias token survived into the match set.
    ("where am i", "CAPTURE_CAMERA"),
    # "project" is a subject noun that turns up in calendar entries, task
    # names and essays. The coding surface may not claim a sentence merely
    # for containing it.
    ("move my final-year project review to 1 pm", "CODING_MODE"),
    ("remind me about the project deadline", "CODING_MODE"),
    ("what's the weather like", "CAPTURE_CAMERA"),
    ("remind me at 4:30 to start my new project", "CAPTURE_CAMERA"),
    ("find my resume", "CAPTURE_CAMERA"),
    # "project" is a subject noun; it must not turn a calendar edit into
    # project tooling.
    ("move my final-year project review to 1 pm", "GITHUB_READ_REPO"),
    ("move my final-year project review to 1 pm", "CREATE_PROJECT"),
    ("move my final-year project review to 1 pm", "BROWSER_START"),
    ("move my final-year project review to 1 pm", "RUNTIME_APPLY"),
    ("move my final-year project review to 1 pm", "RUNTIME_STATUS"),
    # "start" is an operation word owned by no single tool.
    ("remind me at 4:30 to start my new project", "BROWSER_START"),
    # A reminder is not a browser task.
    ("wake me at 6", "BROWSER_START"),
]

#: Compound requests: every outcome must be served, not just the loudest one.
MULTI_OUTCOME_CORPUS = [
    ("move my final-year project review to 1 PM and remind me at 4:30 "
     "to start my new project",
     ["move the final-year project review to 1 PM",
      "remind me at 4:30 to start my new project"],
     [["UPDATE_CALENDAR_EVENT"],
      ["SET_ALARM", "ADD_TASK", "ADD_CALENDAR_EVENT"]]),
    ("tell madhu i'll be late and push my meeting to 2",
     ["tell madhu i will be late",
      "push my meeting to 2pm"],
     [["SEND_WHATSAPP", "SEND_TELEGRAM", "LOOKUP_CONTACTS"],
      ["UPDATE_CALENDAR_EVENT"]]),
]


class _Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        action_registry.configure(_catalogs())
        action_registry.warm_semantic_index(background=False)

    def shortlist(self, request, limit=SHORTLIST):
        return [entry["name"] for entry in action_registry.search(
            BASE_DIR, request, None, limit, "user")]


class ReachableFromPlainSpeech(_Corpus):
    """The whole point: no internal names, no naming the application."""

    def test_corpus(self):
        missed = []
        for request, expected in CORPUS:
            names = self.shortlist(request)
            if not any(name in names for name in expected):
                missed.append(f"{request!r}\n      wanted one of {expected}"
                              f"\n      got {names[:8]}")
        self.assertEqual(
            missed, [],
            f"\n\n{len(missed)} of {len(CORPUS)} plain-speech requests cannot "
            f"reach a capability that exists:\n\n  " + "\n\n  ".join(missed))

    def test_nothing_irrelevant_leads(self):
        """A capability must not colonise requests it has no business in."""
        offences = []
        for request, forbidden in NEGATIVE_CORPUS:
            names = self.shortlist(request)
            if names and names[0] == forbidden:
                offences.append(f"{request!r} -> {forbidden} ranked 1st "
                                f"(shortlist {names[:5]})")
        self.assertEqual(offences, [],
                         "\n\n" + "\n  ".join(offences))


class CompoundRequests(_Corpus):
    """Every outcome gets served, not only the one with the richest nouns."""

    def test_each_outcome_is_served(self):
        failures = []
        for request, outcomes, expectations in MULTI_OUTCOME_CORPUS:
            merged = [entry["name"] for entry in
                      action_registry.search_outcomes(
                          BASE_DIR, outcomes, None, SHORTLIST, "user")]
            for outcome, expected in zip(outcomes, expectations):
                if not any(name in merged for name in expected):
                    failures.append(
                        f"{request[:50]!r}\n      outcome {outcome!r}"
                        f"\n      wanted one of {expected}"
                        f"\n      merged {merged[:8]}")
        self.assertEqual(failures, [], "\n\n" + "\n\n  ".join(failures))


class ShortlistDiscipline(_Corpus):
    """Bounded retrieval must stay bounded, and discovery always reachable."""

    def test_shortlist_respects_its_limit(self):
        for request, _ in CORPUS[:8]:
            with self.subTest(request=request):
                entries = action_registry.search(
                    BASE_DIR, request, None, SHORTLIST, "user")
                # The limit bounds ranked results; DISCOVERY is appended on
                # top of it as the guaranteed escape hatch.
                ranked = [e for e in entries
                          if e["catalog"] != action_registry.DISCOVERY_CATALOG]
                self.assertLessEqual(len(ranked), SHORTLIST)

    def test_discovery_is_always_offered(self):
        """Imperfect ranking is only safe while a wider search stays reachable."""
        for request, _ in CORPUS[:8]:
            with self.subTest(request=request):
                self.assertIn("DISCOVER_ACTIONS", self.shortlist(request))

    def test_no_duplicate_names_in_a_shortlist(self):
        for request, _ in CORPUS:
            with self.subTest(request=request):
                names = self.shortlist(request)
                self.assertEqual(len(names), len(set(names)))

    def test_naming_an_action_outright_still_wins(self):
        """Explicit naming must remain the strongest possible signal."""
        self.assertEqual(self.shortlist("use SET_ALARM")[0], "SET_ALARM")


class AliasHygiene(_Corpus):
    """The specific defect that produced the camera regression."""

    def test_alias_tokens_exclude_stopwords(self):
        """A stopword in an alias must not score.

        Query tokens are stopword-filtered but alias tokens were not, so the
        phrase "what am i holding" let the token "am" earn a full alias hit
        and ranked the camera first for "where am i".
        """
        offenders = {}
        for entry in action_registry._entries:
            leaked = sorted(set(entry.get("alias_tokens", ()))
                            & action_registry._STOPWORDS)
            if leaked:
                offenders[entry["name"]] = leaked
        self.assertEqual(offenders, {},
                         f"stopwords are scoring as alias matches: {offenders}")


if __name__ == "__main__":
    unittest.main()
