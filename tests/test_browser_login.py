# -*- coding: utf-8 -*-
"""Borrowing the sessions from Himanshu's own Firefox.

VANTA drives Playwright's own Firefox against a profile under
`vault/browser_profile_firefox`. That isolation is worth keeping -- his
profile is never locked, migrated or written to -- but its cost was a browser
signed into nothing. Asked to open the placements portal it arrived at a login
page, and there is no password it could type: the point of being already
logged in is that there is no credential to replay, only a session. So VANTA
could reach every page on the internet except the ones that mattered.

Measured on the real machine: his profile held 525 cookies for Google,
ChatGPT, Claude and accounts.ccbp.in; VANTA's held 96, for Hotstar, Microsoft
and Twitter -- things it had signed into itself, and nothing he had.

Two properties matter more than the mechanism, and most of this file is about
them: that saved passwords are never read, and that his live profile is never
opened for writing.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_login

#: Columns in the order current Firefox writes them.
_SCHEMA = """
CREATE TABLE moz_cookies (
    id INTEGER PRIMARY KEY, originAttributes TEXT DEFAULT '', name TEXT,
    value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER,
    creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER,
    inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0,
    schemeMap INTEGER DEFAULT 0, isPartitionedAttributeSet INTEGER DEFAULT 0,
    updateTime INTEGER DEFAULT 0)
"""


def make_profile(root, name="abc123.default-release", cookies=(),
                 with_passwords=True):
    """A Firefox profile directory, close enough to a real one."""
    path = Path(root) / "Profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "cookies.sqlite")
    connection.execute(_SCHEMA)
    for row in cookies:
        connection.execute(
            "INSERT INTO moz_cookies (originAttributes, name, value, host, "
            "path, expiry, isSecure, isHttpOnly, sameSite) "
            "VALUES (?,?,?,?,?,?,?,?,?)", row)
    connection.commit()
    connection.close()
    if with_passwords:
        # The saved-password store. Its presence is the point: these tests
        # check that it is left alone.
        (path / "logins.json").write_text('{"logins": []}', encoding="utf-8")
        (path / "key4.db").write_bytes(b"not a real key database")
    return path


def cookie(name="sid", value="secret-session", host=".example.com",
           expiry=None, secure=1, http_only=1, same_site=1, origin=""):
    if expiry is None:
        expiry = int((time.time() + 86400) * 1000)      # ms, as Firefox writes
    return (origin, name, value, host, "/", expiry, secure, http_only,
            same_site)


class FindingTheProfile(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write_ini(self, body):
        (self.root / "profiles.ini").write_text(body, encoding="utf-8")

    def test_the_installed_profile_wins_over_the_default_flag(self):
        """On the real machine these disagree.

        profiles.ini marks an empty legacy profile Default=1 while the
        installed Firefox actually opens default-release. Trusting the flag
        would borrow the sessions of a profile that has never signed into
        anything.
        """
        make_profile(self.root, "aaa.default", cookies=[])
        make_profile(self.root, "bbb.default-release", cookies=[cookie()])
        self.write_ini(
            "[Profile0]\nName=default-release\nIsRelative=1\n"
            "Path=Profiles/bbb.default-release\n\n"
            "[Install123]\nDefault=Profiles/bbb.default-release\nLocked=1\n\n"
            "[Profile1]\nName=default\nIsRelative=1\n"
            "Path=Profiles/aaa.default\nDefault=1\n")
        chosen = browser_login.default_profile(self.root)
        self.assertEqual(chosen["name"], "default-release")

    def test_a_profile_without_cookies_is_not_chosen(self):
        make_profile(self.root, "aaa.default", cookies=[])
        self.write_ini("[Profile0]\nName=default\nIsRelative=1\n"
                       "Path=Profiles/aaa.default\nDefault=1\n")
        self.assertIsNone(browser_login.default_profile(self.root))

    def test_background_task_profiles_are_not_listed(self):
        """They appear in the directory listing and in their own ini section,
        and have never been signed into anything."""
        make_profile(self.root, "zzz.MozillaBackgroundTask-x",
                     cookies=[cookie()])
        make_profile(self.root, "bbb.default-release", cookies=[cookie()])
        self.write_ini(
            "[BackgroundTasksProfiles]\nMozillaBackgroundTask-x=zzz."
            "MozillaBackgroundTask-x\n\n"
            "[Profile0]\nName=default-release\nIsRelative=1\n"
            "Path=Profiles/bbb.default-release\n")
        names = [row["name"] for row in browser_login.profiles(self.root)]
        self.assertEqual(names, ["default-release"])

    def test_no_firefox_at_all_is_not_an_error(self):
        self.assertEqual(browser_login.profiles(self.root), [])
        self.assertIsNone(browser_login.default_profile(self.root))

    def test_a_corrupt_ini_is_not_an_error(self):
        self.write_ini("this is not an ini file at all\x00")
        self.assertEqual(browser_login.profiles(self.root), [])


class ReadingCookies(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def read(self, cookies, **kwargs):
        path = make_profile(self.root, cookies=cookies)
        return browser_login.read_cookies(path, **kwargs)

    def test_a_cookie_becomes_a_playwright_cookie(self):
        rows = self.read([cookie(name="sid", host=".example.com")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "sid")
        self.assertEqual(rows[0]["domain"], ".example.com")
        self.assertEqual(rows[0]["path"], "/")
        self.assertTrue(rows[0]["secure"])
        self.assertTrue(rows[0]["httpOnly"])

    def test_milliseconds_are_recognised(self):
        """Current Firefox writes milliseconds into a column documented as
        seconds. Read as seconds, every cookie expires in the year 58,000."""
        soon = int((time.time() + 3600) * 1000)
        rows = self.read([cookie(expiry=soon)])
        self.assertAlmostEqual(rows[0]["expires"], soon / 1000.0, places=0)

    def test_seconds_are_still_recognised(self):
        """Older builds wrote seconds, and the unit is not declared anywhere."""
        soon = int(time.time() + 3600)
        rows = self.read([cookie(expiry=soon)])
        self.assertAlmostEqual(rows[0]["expires"], float(soon), places=0)

    def test_an_expired_cookie_is_left_behind(self):
        rows = self.read([cookie(expiry=int((time.time() - 60) * 1000))])
        self.assertEqual(rows, [])

    def test_a_session_cookie_is_marked_as_one(self):
        rows = self.read([cookie(expiry=0)])
        self.assertEqual(rows[0]["expires"], -1.0)

    def test_container_and_private_cookies_are_skipped(self):
        """Replayed without their origin attributes they land in the wrong
        partition, where they are either ignored or actively wrong."""
        rows = self.read([
            cookie(name="normal", origin=""),
            cookie(name="container", origin="^userContextId=2"),
            cookie(name="private", origin="^privateBrowsingId=1"),
        ])
        self.assertEqual([row["name"] for row in rows], ["normal"])

    def test_same_site_is_translated(self):
        rows = self.read([
            cookie(name="none", same_site=0),
            cookie(name="lax", same_site=1),
            cookie(name="strict", same_site=2),
        ])
        got = {row["name"]: row["sameSite"] for row in rows}
        self.assertEqual(got, {"none": "None", "lax": "Lax",
                               "strict": "Strict"})

    def test_an_unknown_same_site_becomes_the_browser_default(self):
        """256 appears throughout a current profile and means the server
        never said, so the browser default applies."""
        rows = self.read([cookie(same_site=256)])
        self.assertEqual(rows[0]["sameSite"], "Lax")

    def test_domains_can_be_scoped(self):
        rows = self.read([cookie(name="a", host=".example.com"),
                          cookie(name="b", host="accounts.ccbp.in")],
                         domains=["ccbp.in"])
        self.assertEqual([row["name"] for row in rows], ["b"])

    def test_a_leading_dot_in_the_filter_still_matches(self):
        rows = self.read([cookie(host=".example.com")],
                         domains=[".example.com"])
        self.assertEqual(len(rows), 1)

    def test_a_nameless_cookie_is_skipped(self):
        self.assertEqual(self.read([cookie(name="")]), [])

    def test_a_missing_database_is_not_an_error(self):
        empty = self.root / "Profiles" / "nothing"
        empty.mkdir(parents=True)
        self.assertEqual(browser_login.read_cookies(empty), [])


class ItNeverTouchesWhatItShouldNot(unittest.TestCase):
    """The two properties that matter more than the mechanism."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.profile = make_profile(self.root, cookies=[cookie()])

    def test_the_saved_password_store_is_never_opened(self):
        """Being logged in needs a session, not a credential. logins.json and
        key4.db are the password vault and are not required for any of it."""
        opened = []
        real_open = open

        def watched(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", watched):
            browser_login.read_cookies(self.profile)
        touched = [path for path in opened
                   if "logins.json" in path or "key4.db" in path]
        self.assertEqual(touched, [], "it read the saved-password store")

    def test_the_module_does_not_even_mention_the_password_files(self):
        """A standing check, so adding it later is a deliberate act."""
        import inspect
        source = inspect.getsource(browser_login)
        body = source.split('"""', 2)[-1]        # past the module docstring
        for name in ("logins.json", "key4.db"):
            self.assertNotIn(name, body,
                             f"{name} is referenced outside the docstring")

    def test_the_live_database_is_not_opened_by_sqlite(self):
        """Firefox holds its profile open, and a write from outside is how a
        profile gets corrupted."""
        connected = []
        real_connect = sqlite3.connect

        def watched(target, *args, **kwargs):
            connected.append(str(target))
            return real_connect(target, *args, **kwargs)

        with patch("sqlite3.connect", watched):
            browser_login.read_cookies(self.profile)
        self.assertTrue(connected)
        for target in connected:
            self.assertNotIn(str(self.profile), target,
                             "it opened the live profile directly")

    def test_the_profile_is_left_exactly_as_it_was(self):
        source = self.profile / "cookies.sqlite"
        before = (source.stat().st_mtime, source.stat().st_size)
        browser_login.read_cookies(self.profile)
        after = (source.stat().st_mtime, source.stat().st_size)
        self.assertEqual(before, after)

    def test_the_write_ahead_log_is_copied_too(self):
        """Without it the copy is whatever the file looked like at the last
        checkpoint, which can be hours stale and silently so."""
        import inspect
        self.assertIn("-wal", inspect.getsource(browser_login._snapshot))

    def test_no_cookie_value_is_ever_summarised(self):
        rows = browser_login.read_cookies(self.profile)
        summary = browser_login.summarise(rows)
        self.assertNotIn("secret-session", summary)
        self.assertIn("example.com", summary)


class ImportingIntoAContext(unittest.TestCase):
    """The receipt is read back from the browser, not from the import."""

    class Context:
        def __init__(self, existing=()):
            self._cookies = list(existing)
            self.added = []

        def cookies(self):
            return list(self._cookies)

        def add_cookies(self, cookies):
            self.added.extend(cookies)
            self._cookies.extend(cookies)

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.profile = make_profile(
            self.root, cookies=[cookie(name="sid", host="accounts.ccbp.in"),
                                cookie(name="pref", host=".google.com")])

    def test_cookies_reach_the_browser(self):
        context = self.Context()
        receipt = browser_login.import_into(context, self.profile)
        self.assertIn("[Success]", receipt)
        self.assertEqual(len(context.added), 2)

    def test_the_receipt_counts_what_the_browser_holds(self):
        context = self.Context()
        receipt = browser_login.import_into(context, self.profile)
        self.assertIn("the browser now holds 2", receipt)
        self.assertIn("was 0", receipt)

    def test_the_receipt_names_the_sites_but_no_values(self):
        context = self.Context()
        receipt = browser_login.import_into(context, self.profile)
        self.assertIn("accounts.ccbp.in", receipt)
        self.assertNotIn("secret-session", receipt)

    def test_the_receipt_says_passwords_were_not_read(self):
        context = self.Context()
        self.assertIn("Saved passwords were not read",
                      browser_login.import_into(context, self.profile))

    def test_it_can_be_scoped_to_one_site(self):
        context = self.Context()
        browser_login.import_into(context, self.profile, domains=["ccbp.in"])
        self.assertEqual([c["name"] for c in context.added], ["sid"])

    def test_an_empty_profile_is_an_error_not_a_quiet_success(self):
        empty = make_profile(self.root, "empty.default", cookies=[])
        receipt = browser_login.import_into(self.Context(), empty)
        self.assertIn("[Error]", receipt)
        self.assertNotIn("[Success]", receipt)

    def test_a_scope_that_matches_nothing_says_which_scope(self):
        receipt = browser_login.import_into(
            self.Context(), self.profile, domains=["nowhere.invalid"])
        self.assertIn("[Error]", receipt)
        self.assertIn("nowhere.invalid", receipt)

    def test_no_firefox_on_the_machine_is_reported_plainly(self):
        with patch.object(browser_login, "default_profile", return_value=None):
            receipt = browser_login.import_into(self.Context())
        self.assertIn("[Error]", receipt)
        self.assertIn("no session to borrow", receipt)


class TheSwitch(unittest.TestCase):
    def test_it_is_on_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WINTER_BROWSER_IMPORT_COOKIES", None)
            self.assertTrue(browser_login.enabled())

    def test_it_can_be_turned_off(self):
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                with patch.dict(os.environ,
                                {"WINTER_BROWSER_IMPORT_COOKIES": value}):
                    self.assertFalse(browser_login.enabled())


class WiredIntoTheBrowser(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_the_sessions_are_borrowed_when_the_browser_starts(self):
        with open(os.path.join(self.ROOT, "browser.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("browser_login.import_into", source)

    def test_a_failed_import_does_not_stop_the_browser(self):
        """A browser that will not start is worse than one not signed in."""
        with open(os.path.join(self.ROOT, "browser.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("browser_login.import_into")
        self.assertIn("except Exception", source[start:start + 400])

    def test_there_is_an_operation_to_refresh_it(self):
        import browser
        self.assertTrue(callable(browser.sync_login))
        self.assertIn("sync_login", browser._OPERATION_ACTIONS)

    def test_the_capability_is_wired_through_every_layer(self):
        import action_bus
        import action_schema
        import actions          # registers the handlers
        import agent
        import autonomy
        import capabilities
        name = "SYNC_BROWSER_LOGIN"
        self.assertIsNotNone(capabilities.get(name))
        self.assertIn(name, autonomy.KNOWN_ACTIONS)
        self.assertIn(name, action_schema.ACTION_SPECS)
        self.assertIn(name, "\n".join(agent.ACTION_CATALOGS.values()))
        self.assertTrue(action_bus.BUS.handles(name))

    def test_only_himanshu_may_borrow_his_own_sessions(self):
        """A background turn must not pull his live sessions into a browser
        on its own initiative."""
        import capabilities
        self.assertEqual(capabilities.get("SYNC_BROWSER_LOGIN").origins,
                         ("user",))


if __name__ == "__main__":
    unittest.main()
