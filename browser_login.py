# -*- coding: utf-8 -*-
"""
browser_login.py -- borrow the sessions from Himanshu's own Firefox.

VANTA drives Playwright's own Firefox build against a profile under
`vault/browser_profile_firefox`. That isolation is deliberate and worth
keeping: nothing is installed into his browser, his profile is never locked or
migrated, and a test navigating to httpbin cannot write cookies next to his
Microsoft sign-in.

The cost of it is that VANTA's browser is logged into nothing. Asked to open
the placements portal it arrives at a sign-in page, and there is no password it
could type -- the whole point of being already logged in is that there is no
credential to replay, just a session. So VANTA's browser could reach every
page on the internet except the ones that mattered.

WHAT THIS COPIES, AND WHAT IT REFUSES TO
----------------------------------------
Cookies, and nothing else.

Firefox keeps cookie values in plain text in cookies.sqlite, so a session needs
no key material to use. Saved passwords are a different thing entirely: they
live in logins.json encrypted against key4.db, and being logged in does not
require them. So this reads cookies.sqlite and never opens logins.json or
key4.db -- VANTA gets the sessions he already has, not the ability to sign in
as him somewhere new.

It also never touches the live file. Firefox holds its profile open, and a
write into it from outside is how a profile gets corrupted; the database is
copied first, with its write-ahead log so the copy is current, and read from
there. His Firefox can be running throughout and notices nothing.

Cookies are handed to Playwright as cookies, through `add_cookies`, rather than
by dropping a cookies.sqlite into VANTA's profile directory. Playwright's
Firefox is a different build from his, so the two profiles' schemas are only
coincidentally compatible -- and a schema mismatch there does not announce
itself, it just silently starts from a blank slate, which looks exactly like
being logged out of everything.
"""

import configparser
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

#: Cookies belonging to a container tab or a private window carry origin
#: attributes that scope them to that container. Replayed without it they land
#: in the wrong partition, where they are either ignored or actively wrong.
_PLAIN_ORIGIN = ""

#: Firefox's sameSite column: 0 none, 1 lax, 2 strict. Anything else -- 256
#: turns up throughout a current profile -- means the server never said, so the
#: browser default applies, and that default is Lax.
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}

#: An expiry past this is milliseconds, not seconds. Current Firefox writes
#: milliseconds and older builds wrote seconds, and the column is documented as
#: seconds in both, so the unit has to be inferred rather than trusted.
_MILLISECOND_THRESHOLD = 100_000_000_000


def firefox_root():
    """Where Firefox keeps its profiles on this machine."""
    appdata = os.getenv("APPDATA", "")
    if appdata:
        return Path(appdata) / "Mozilla" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


def profiles(root=None):
    """Every real Firefox profile, the default one first.

    Read from profiles.ini rather than by listing directories, because the
    directory listing includes background-task profiles that have never been
    signed into anything.
    """
    base = Path(root) if root else firefox_root()
    config_path = base / "profiles.ini"
    if not config_path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except (configparser.Error, OSError):
        return []

    installs = set()
    for section in parser.sections():
        if section.startswith("Install"):
            value = parser.get(section, "Default", fallback="").strip()
            if value:
                installs.add(value.replace("\\", "/"))

    found = []
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        relative = parser.get(section, "Path", fallback="").strip()
        if not relative:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
        path = (base / relative) if is_relative else Path(relative)
        if not path.is_dir():
            continue
        found.append({
            "name": parser.get(section, "Name", fallback=path.name),
            "path": path,
            # Two different notions of "default" live in this file. The one
            # that matters is the install default -- the profile the
            # installed Firefox actually opens -- not the Default=1 flag,
            # which on this machine points at an empty legacy profile.
            "installed": relative.replace("\\", "/") in installs,
            "cookies": (path / "cookies.sqlite").exists(),
        })
    found.sort(key=lambda row: (not row["installed"], not row["cookies"],
                                row["name"]))
    return found


def default_profile(root=None):
    """The profile that actually holds sessions.

    Candidates are tried in order and each one's database is read, because
    "has a cookies.sqlite" and "has cookies" are different: a profile that has
    been opened once and signed into nothing has the file, the schema and no
    rows, and choosing it would borrow nothing while reporting success.

    Returns None when no profile on this machine holds a usable session.
    """
    candidates = profiles(root)
    for row in candidates:
        if not row["cookies"]:
            continue
        try:
            if read_cookies(row["path"]):
                return row
        except (RuntimeError, OSError):
            continue
    return None


def _snapshot(path):
    """A readable copy of a database Firefox may have open.

    The write-ahead log comes too, or the copy is whatever the file looked
    like at the last checkpoint -- which can be hours out of date, and
    silently so.
    """
    directory = tempfile.mkdtemp(prefix="vanta_cookies_")
    target = os.path.join(directory, "cookies.sqlite")
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        extra = str(path) + suffix
        if os.path.exists(extra):
            try:
                shutil.copy2(extra, target + suffix)
            except OSError:
                pass
    return directory, target


def read_cookies(profile_path, domains=None, include_expired=False):
    """Every usable cookie in a Firefox profile, as plain dictionaries.

    `domains` filters to hosts containing any of the given strings, which is
    how a caller asks for one site's session rather than all 525 of them.
    """
    source = Path(profile_path) / "cookies.sqlite"
    if not source.exists():
        return []
    directory, copied = _snapshot(source)
    now = time.time()
    rows = []
    try:
        connection = sqlite3.connect(copied)
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, "
            "sameSite, originAttributes FROM moz_cookies")
        for row in cursor:
            if (row["originAttributes"] or "") != _PLAIN_ORIGIN:
                continue          # a container tab or a private window
            host = (row["host"] or "").strip()
            name = (row["name"] or "").strip()
            if not host or not name:
                continue
            if domains and not any(str(fragment).lower().lstrip(".") in host.lower()
                                   for fragment in domains):
                continue
            expires = _expiry_seconds(row["expiry"])
            if not include_expired and 0 < expires < now:
                continue
            rows.append({
                "name": name,
                "value": row["value"] or "",
                "domain": host,
                "path": row["path"] or "/",
                "expires": expires,
                "secure": bool(row["isSecure"]),
                "httpOnly": bool(row["isHttpOnly"]),
                "sameSite": _SAME_SITE.get(row["sameSite"], "Lax"),
            })
        connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"could not read Firefox's cookie database ({exc}). If Firefox "
            f"was mid-write, try again in a moment.") from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return rows


def _expiry_seconds(raw):
    """Playwright wants seconds, and -1 for a session cookie."""
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return -1.0
    if value <= 0:
        return -1.0
    if value > _MILLISECOND_THRESHOLD:
        value = value / 1000.0
    return value


def summarise(cookies):
    """What was imported, by site. Never a value -- these are live sessions."""
    hosts = {}
    for cookie in cookies:
        host = cookie["domain"].lstrip(".")
        hosts[host] = hosts.get(host, 0) + 1
    top = sorted(hosts.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
    return ", ".join(f"{host} ({count})" for host, count in top)


def import_into(context, profile_path=None, domains=None):
    """Put Himanshu's sessions into a running Playwright context.

    Returns a receipt naming how many cookies arrived and which sites they
    belong to, read back from the context rather than from the import, because
    "I called add_cookies" and "the browser has them" are different facts.
    """
    profile = profile_path
    if profile is None:
        found = default_profile()
        if found is None:
            return ("[Error] No Firefox profile with cookies was found on this "
                    "machine, so there is no session to borrow.")
        profile = found["path"]

    cookies = read_cookies(profile, domains=domains)
    if not cookies:
        scope = f" for {', '.join(domains)}" if domains else ""
        return (f"[Error] Himanshu's Firefox profile holds no current "
                f"cookies{scope}. Nothing was imported.")

    before = len(context.cookies())
    context.add_cookies(cookies)
    after = context.cookies()
    names = {(cookie["name"], cookie["domain"]) for cookie in after}
    landed = len([cookie for cookie in cookies
                  if (cookie["name"], cookie["domain"].lstrip(".")) in
                  {(n, d.lstrip(".")) for n, d in names}])
    return (f"[Success] Imported {landed} of {len(cookies)} cookies from "
            f"Firefox profile '{Path(profile).name}'; the browser now holds "
            f"{len(after)} (was {before}). Sites: {summarise(cookies)}. "
            f"Saved passwords were not read.")


def enabled():
    """Whether to borrow sessions automatically when the browser starts."""
    raw = (os.getenv("WINTER_BROWSER_IMPORT_COOKIES", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")
