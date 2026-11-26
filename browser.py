"""Managed, isolated browser control for VANTA.

Browser work runs in a persistent worker thread.  Playwright provides the
Chromium/WebKit/patched-Firefox engines; Selenium provides stock Firefox.
Agent turns communicate through a small RPC queue, so driver objects never
cross thread boundaries.  Every engine uses a VANTA-owned profile rather than
locking or modifying the user's everyday browser profile.
"""

import ipaddress
import json
import os
import queue
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from browser_contracts import (STABLE_REFERENCES_JS, VALIDATION_JS,
                               CLICK_OBSERVER_JS, CLICK_COUNT_JS, check_deadline,
                               current_interrupt, wait_for_reply)


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "vault" / "browser_profile"
DOWNLOAD_DIR = BASE_DIR / "vault" / "downloads"
DEFAULT_TIMEOUT_MS = 30_000


def youtube_video_id(url):
    """Return the stable video identity from a YouTube URL."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            return (parse_qs(parsed.query).get("v") or [""])[0]
        if host == "youtu.be":
            return parsed.path.strip("/").split("/", 1)[0]
    except Exception:
        return ""
    return ""

#: How long to watch for a navigation after an interaction.
#:
#: An in-page click should not pay for a redirect that is never coming, and a
#: sign-in button should not be read 250ms after it was pressed. So the probe
#: is short by default and long for the elements that actually navigate --
#: links, buttons and submits.
SHORT_SETTLE_MS = 300
NAVIGATION_SETTLE_MS = 1_500

#: Once it has moved, how long to let the rest of the chain land. An SSO
#: handshake is several hops and reading after the first one means reading a
#: page that is about to be replaced.
LOAD_SETTLE_MS = 8_000
CLICK_DISPATCH_TIMEOUT_MS = 5_000

# HTML overlays are ordinary DOM, but their message is often outside the
# button/input-only snapshot selector.  Returning a small, structured summary
# lets the model understand a modal, consent sheet or alert banner before it
# chooses one of the already-ref'd controls inside it.
HTML_OVERLAY_SCRIPT = r"""() => Array.from(document.querySelectorAll(
  'dialog,[role="dialog"],[role="alertdialog"],[aria-modal="true"],[role="alert"]'
)).filter(el => {
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none' &&
         s.visibility !== 'hidden';
}).slice(0, 12).map(el => ({
  kind: el.getAttribute('role') || (el.tagName.toLowerCase() === 'dialog'
        ? 'dialog' : 'modal'),
  name: (el.getAttribute('aria-label') || '').trim().slice(0, 160),
  text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 800)
}))"""


def normalize_dialog_mode(mode):
    """Validate the small browser-dialog control vocabulary."""
    value = str(mode or "status").strip().casefold()
    aliases = {"ok": "accept", "yes": "accept", "confirm": "accept",
               "cancel": "dismiss", "no": "dismiss", "close": "dismiss",
               "inspect": "status", "check": "status"}
    value = aliases.get(value, value)
    if value not in {"status", "accept", "dismiss"}:
        raise ValueError("dialog mode must be status, accept, or dismiss")
    return value


_CLOCK_VALUE = re.compile(
    r"^\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?\s*([ap])\.?\s*m\.?\s*$",
    re.IGNORECASE,
)
_TWENTY_FOUR_HOUR_VALUE = re.compile(r"^\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?\s*$")


def normalize_control_value(value, control_type="", placeholder="", name="",
                            label=""):
    """Format a model-supplied value for the HTML control that will receive it.

    Models naturally say ``5AM`` and ``6PM``.  A control labelled as a
    24-hour time is a machine contract, though, and should receive ``5:00``
    and ``18:00``.  Keeping this at the browser boundary makes the behaviour
    work for every form without teaching the model about individual sites.
    """
    text = str(value if value is not None else "").strip()
    kind = str(control_type or "").strip().casefold()
    hint = " ".join(str(item or "") for item in (placeholder, name, label)).casefold()
    is_time = kind in {"time", "datetime-local"} or "time" in hint
    if is_time:
        match = _CLOCK_VALUE.fullmatch(text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if not 1 <= hour <= 12 or not 0 <= minute <= 59:
                raise ValueError(f"invalid clock time: {text!r}")
            hour = hour % 12 + (12 if match.group(3).casefold() == "p" else 0)
            # Native time inputs require a zero-padded hour. Plain portal text
            # fields commonly document H:mm, so retain the human-facing 5:00.
            return (f"{hour:02d}:{minute:02d}" if kind == "time"
                    else f"{hour}:{minute:02d}")
        match = _TWENTY_FOUR_HOUR_VALUE.fullmatch(text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError(f"invalid 24-hour time: {text!r}")
            return (f"{hour:02d}:{minute:02d}" if kind == "time"
                    else f"{hour}:{minute:02d}")

    # Native date controls accept ISO only.  Text controls declaring an
    # explicit dd/mm/yyyy contract get that format instead.
    date_parts = None
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        year, month, day = (int(part) for part in text.split("-"))
        date_parts = (year, month, day)
    elif re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{4}", text):
        day, month, year = (int(part) for part in re.split(r"[/-]", text))
        date_parts = (year, month, day)
    if date_parts:
        from datetime import date
        year, month, day = date_parts
        date(year, month, day)  # validate instead of silently changing a date
        if kind == "date":
            return f"{year:04d}-{month:02d}-{day:02d}"
        compact_hint = hint.replace(" ", "")
        if "dd/mm/yyyy" in compact_hint or "dd-mm-yyyy" in compact_hint:
            separator = "-" if "dd-mm-yyyy" in compact_hint else "/"
            return f"{day:02d}{separator}{month:02d}{separator}{year:04d}"
    return text


#: "firefox-real" drives the Firefox that is installed (browser_real.py) rather
#: than Playwright's patched Nightly build. It is the only engine that is
#: Himanshu's actual browser.
REAL_ENGINE = "firefox-real"

#: The first three names are attributes exposed by Playwright.  REAL_ENGINE is
#: intentionally separate: treating it as a Playwright launcher makes the
#: continuity fallback fail with ``Playwright has no attribute firefox-real``.
PLAYWRIGHT_ENGINES = ("chromium", "firefox", "webkit")
ENGINES = PLAYWRIGHT_ENGINES + (REAL_ENGINE,)

#: Engines keep their profiles apart. A Chromium profile directory means
#: nothing to Firefox, and pointing one engine at the other's profile either
#: fails or silently starts from a blank slate -- which would look like being
#: logged out of everything.
_ENGINE_PROFILE_SUFFIX = {"chromium": "", "firefox": "_firefox",
                          "webkit": "_webkit", "firefox-real": "_firefox_real"}


def engine():
    """Which engine to drive. WINTER_BROWSER_ENGINE selects it."""
    import os
    name = (os.getenv("WINTER_BROWSER_ENGINE", "") or "").strip().lower()
    return name if name in ENGINES else "chromium"


def profile_dir(engine_name=None):
    """Which browser profile to drive, resolved at launch rather than import.

    Chromium takes a lock on a persistent profile, so two processes cannot open
    the same one. The test suites each launch their own browser, and running
    them back to back would intermittently fail to acquire the lock before the
    previous process had let go -- roughly one run in fifteen.

    They also had no business being in there. This profile holds Himanshu's
    real sessions: the captcha clearance for the placements portal, his
    Microsoft sign-in, whatever else he has logged into. A test navigating to
    httpbin should not be writing cookies next to that.

    WINTER_BROWSER_PROFILE points somewhere else, which the tests use for a
    throwaway directory.
    """
    import os
    override = os.getenv("WINTER_BROWSER_PROFILE", "").strip()
    if override:
        return Path(override)
    selected = engine_name or engine()
    suffix = _ENGINE_PROFILE_SUFFIX.get(selected, "_firefox")
    return PROFILE_DIR.with_name(PROFILE_DIR.name + suffix) if suffix else PROFILE_DIR


def _validate_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute http/https URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials embedded in URLs are not allowed")
    allow_private = os.getenv("WINTER_BROWSER_ALLOW_PRIVATE", "0").lower() in {"1", "true", "yes"}
    if not allow_private:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError("Private or local network URLs are blocked")
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve URL host: {exc}") from exc
    return parsed.geturl()


#: Longest a caller will ever wait for one browser operation. Generous,
#: because the alternative is abandoning work that was succeeding, but bounded
#: so a hung page cannot hold a turn open indefinitely.
MAX_CALL_TIMEOUT = max(
    60, int(os.getenv("WINTER_BROWSER_MAX_WAIT", "") or 240))

#: Operations whose cost is roughly one Playwright action plus page work.
_OPERATION_ACTIONS = {
    "start": 2, "goto": 2, "snapshot": 3, "click": 2, "type": 2,
    "press": 2, "select": 2, "check": 2, "upload": 2, "submit": 3,
    "tabs": 1, "close": 1, "read": 3, "wait": 3, "dialog": 1,
    "youtube_play": 3, "youtube_fullscreen": 2,
    # Launching the browser plus copying and replaying a few hundred cookies.
    "sync_login": 3,
}


def call_timeout(operation, kwargs=None):
    """How long to wait for one operation, derived from the work it implies.

    A single number could not be right for both a tab list and a six-field
    form fill. It was 40 seconds while Playwright's own per-action timeout was
    30, so a fill of six fields -- each entitled to its own 30 -- overran the
    caller, which gave up while the worker was still filling. The worker runs
    serially, so everything queued behind it then timed out as well, and three
    consecutive timeouts read to the model as a dead subsystem.
    """
    kwargs = kwargs or {}
    per_action = DEFAULT_TIMEOUT_MS / 1000.0
    fields = kwargs.get("fields")
    if operation == "fill_form" and isinstance(fields, (dict, list)):
        actions = max(1, len(fields)) + 1
    else:
        actions = _OPERATION_ACTIONS.get(str(operation), 2)
    # A margin on top, so the caller always outlasts the slowest single action
    # it is waiting on rather than racing it.
    return min(MAX_CALL_TIMEOUT, per_action * actions + 10)


def timeout_message(operation, waited):
    """What a timeout means, in terms the model can act on.

    "Control service timed out" reads as a broken subsystem, and the model
    stopped and told Himanshu the portal was unusable. It was slow, and the
    work up to that point may well have landed -- so say that, and say how to
    find out.
    """
    return (f"{operation} did not finish within {int(waited)}s. It may still "
            f"be running; its result is unconfirmed and part of the work may "
            f"already have been done. Expired queued steps will not start. "
            f"Take a BROWSER_SNAPSHOT to inspect the current state before "
            f"repeating any mutation.")


class _BrowserWorker:
    def __init__(self, engine_name=None):
        selected = engine_name or engine()
        # Stock Firefox is owned by browser_real.  If that backend cannot be
        # used, this worker is the continuity path and must select the actual
        # Playwright Firefox launcher, never the synthetic "firefox-real"
        # configuration name.
        self.engine_name = (
            "firefox" if selected == REAL_ENGINE else selected
        )
        if self.engine_name not in PLAYWRIGHT_ENGINES:
            self.engine_name = "chromium"
        self.requests = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="vanta-browser")
        self.thread.start()

    def call(self, operation, **kwargs):
        reply = queue.Queue(maxsize=1)
        waiting = call_timeout(operation, kwargs)
        kwargs['_request_deadline'] = time.monotonic() + waiting
        self.requests.put((operation, kwargs, reply))
        try:
            ok, value = wait_for_reply(reply, waiting, kwargs)
        except InterruptedError as exc:
            return '[Interrupted] ' + str(exc)
        except queue.Empty:
            return "[Browser Error] " + timeout_message(operation, waiting)
        if ok:
            return value
        return f"[Browser Error] {value}"

    def _run(self):
        playwright = context = None
        context_visible = None
        page_ids = {}
        next_tab_id = 1
        # The tab selected by YOUTUBE_PLAY is an owned resource, not simply
        # whichever page happens to be last.  Fullscreen and later media
        # controls must return to this exact page.
        media_page = None
        active_page = None
        dialog_history = {}
        dialog_policies = {}
        dialog_configured = set()

        def dialog_fields(dialog):
            return {
                "type": str(getattr(dialog, "type", "dialog") or "dialog"),
                "message": str(getattr(dialog, "message", "") or "")[:1000],
                "default_value": str(
                    getattr(dialog, "default_value", "") or "")[:500],
            }

        def configure_dialogs(page):
            """Install one non-blocking dialog listener on each page.

            A Playwright page action freezes forever when a dialog listener
            observes a dialog but does not resolve it.  Informational alerts
            are accepted; confirms and prompts are dismissed unless an
            explicit, short-lived BROWSER_DIALOG policy was armed first.
            """
            marker = id(page)
            if marker in dialog_configured:
                return page
            dialog_configured.add(marker)

            def on_dialog(dialog):
                info = dialog_fields(dialog)
                policy = dialog_policies.pop(page, None)
                if policy and policy.get("expires", 0) < time.monotonic():
                    policy = None
                requested = policy.get("mode") if policy else None
                text = policy.get("text", "") if policy else ""
                chosen = requested or (
                    "accept" if info["type"] == "alert" else "dismiss")
                try:
                    if chosen == "accept":
                        if info["type"] == "prompt":
                            dialog.accept(text)
                        else:
                            dialog.accept()
                    else:
                        dialog.dismiss()
                    info.update({"handled": chosen,
                                 "explicit": bool(requested)})
                except Exception as exc:
                    info.update({"handled": "error", "error": str(exc)[:300]})
                    try:
                        dialog.dismiss()
                    except Exception:
                        pass
                history = dialog_history.setdefault(page, [])
                history.append(info)
                del history[:-10]

            page.on("dialog", on_dialog)
            return page

        def dialogs_since(page, index):
            return list(dialog_history.get(page, [])[index:])

        def discard_context(why=""):
            """Forget the current browser so the next start builds a new one."""
            nonlocal playwright, context, context_visible, media_page, active_page
            for close_it in (context, playwright):
                if close_it is None:
                    continue
                try:
                    close_it.close() if close_it is context else close_it.stop()
                except Exception:
                    pass
            context = playwright = None
            context_visible = None
            media_page = None
            active_page = None
            page_ids.clear()
            if why:
                print(f"[Browser] Discarded the old session: {why}")

        def context_is_usable():
            """Whether the context we are holding can still open a page.

            `context is not None` was the whole test, and it is not the same
            question. Closing the last tab leaves the context object alive
            with no window behind it, and Firefox then answers new_page with
            "can't access property delayedStartupPromise, window is null" --
            forever, because the early return above meant nothing ever
            rebuilt it. That is the bug where the browser opens once, and
            every task after the window is closed gets nothing at all.
            """
            if context is None:
                return False
            try:
                return any(not page.is_closed() for page in context.pages)
            except Exception:
                return False

        def ensure_started(visible=None):
            """Start if needed; ``None`` preserves a live presentation."""
            nonlocal playwright, context, context_visible
            if context is not None and context_is_usable():
                if visible is None or context_visible == bool(visible):
                    return
                # Headed/headless is a process-level browser choice. Rebuild
                # the isolated managed session when a background check must
                # stop occupying Himanshu's screen (or vice versa).
                discard_context("switching browser visibility")
            if context is not None:
                # Held but dead -- he closed the window, or the last tab went.
                discard_context("the browser window is gone")
            if visible is None:
                # There is no existing presentation to preserve.  Direct
                # observation remains visible by default; background callers
                # establish headless mode explicitly through START or GOTO.
                visible = True
            from playwright.sync_api import sync_playwright
            active_profile = profile_dir(self.engine_name)
            active_profile.mkdir(parents=True, exist_ok=True)
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            playwright = sync_playwright().start()
            engine_name = self.engine_name
            launcher = getattr(playwright, engine_name)
            options = dict(
                headless=not visible,
                accept_downloads=True,
                downloads_path=str(DOWNLOAD_DIR),
                viewport={"width": 1440, "height": 900},
            )
            if engine_name == "chromium":
                # VANTA's browser is an execution surface, not Himanshu's
                # workspace. Keep it headed for audio/OAuth compatibility but
                # never place it over the application he is using. These are
                # Chromium command-line switches: Firefox rejects unknown
                # arguments and refuses to start, so they are not passed to it.
                options["args"] = ["--start-minimized",
                                   "--autoplay-policy=no-user-gesture-required"]
            else:
                # The Firefox equivalents, set as profile preferences rather
                # than switches.
                options["firefox_user_prefs"] = {
                    "media.autoplay.default": 0,
                    "media.autoplay.blocking_policy": 0,
                    # A download must never open a chooser dialog: nothing is
                    # watching for one, so the automation would simply hang.
                    "browser.download.folderList": 2,
                    "browser.download.dir": str(DOWNLOAD_DIR),
                    "browser.download.useDownloadDir": True,
                    # Playwright's Firefox is a Nightly build and opens
                    # Mozilla's Nightly welcome page on first run of a profile.
                    # That page was the first thing VANTA's browser showed,
                    # which read as it having opened the wrong website.
                    "browser.startup.homepage": "about:blank",
                    "startup.homepage_welcome_url": "about:blank",
                    "startup.homepage_welcome_url.additional": "",
                    "browser.startup.page": 0,
                    "browser.aboutwelcome.enabled": False,
                    "browser.shell.checkDefaultBrowser": False,
                    "datareporting.policy.dataSubmissionEnabled": False,
                }
            try:
                context = launcher.launch_persistent_context(
                    str(active_profile), **options)
            except Exception as exc:
                playwright.stop()
                playwright = None
                raise RuntimeError(
                    f"Could not start {engine_name}: {exc}. If the engine is "
                    f"not installed, run: python -m playwright install "
                    f"{engine_name}") from exc
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            context_visible = bool(visible)
            context.on("page", configure_dialogs)
            for existing_page in context.pages:
                configure_dialogs(existing_page)

            # Borrow the sessions from Himanshu's own Firefox. The isolation
            # above is worth keeping -- his profile is never locked, migrated
            # or written to -- but its cost was a browser logged into nothing,
            # which meant VANTA could reach every page except the ones that
            # mattered. Cookies only, never the saved-password store, and read
            # from a copy so his Firefox can stay open throughout.
            # Whatever engine is being driven: a cookie is a cookie, and
            # Playwright replays them the same way, so there is no reason the
            # sessions should only follow the Firefox engine.
            try:
                import browser_login
                if browser_login.enabled():
                    print(f"[Browser] {browser_login.import_into(context)}")
            except Exception as exc:
                print(f"[Browser] Could not borrow Firefox sessions "
                      f"({exc}); the profile stays as it was.")

        def tab_id(page):
            nonlocal next_tab_id
            if page not in page_ids:
                page_ids[page] = f"t{next_tab_id}"
                next_tab_id += 1
            return page_ids[page]

        def get_page(requested=None, visible=None):
            nonlocal active_page
            ensure_started(visible)
            pages = [p for p in context.pages if not p.is_closed()]
            if requested:
                for page in pages:
                    if tab_id(page) == requested:
                        active_page = page
                        return configure_dialogs(page)
                raise ValueError(f"Unknown tab id: {requested}")
            if pages:
                if active_page not in pages:
                    active_page = pages[-1]
            else:
                active_page = context.new_page()
            return configure_dialogs(active_page)

        def adopt_popup(before_pages):
            nonlocal active_page
            opened = [p for p in context.pages
                      if p not in before_pages and not p.is_closed()]
            rows = []
            for page in opened:
                configure_dialogs(page)
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=3000)
                except Exception:
                    pass
                rows.append({'tab': tab_id(page), 'url': page.url})
            if len(opened) == 1:
                active_page = opened[0]
            return rows

        def media_target(requested=None):
            """The exact YouTube tab owned by the current playback action."""
            nonlocal media_page
            if requested:
                page = get_page(requested)
                media_page = page
                return page
            if media_page is not None:
                try:
                    if not media_page.is_closed():
                        return get_page(tab_id(media_page))
                except Exception:
                    pass
                media_page = None
            ensure_started()
            candidates = []
            for page in context.pages:
                try:
                    host = (urlparse(page.url).hostname or "").lower()
                    if (not page.is_closed()
                            and host in {"youtube.com", "www.youtube.com",
                                         "m.youtube.com"}
                            and page.locator("video.html5-main-video").count()):
                        candidates.append(page)
                except Exception:
                    continue
            if len(candidates) != 1:
                raise ValueError(
                    "No unique VANTA-owned YouTube playback tab exists; run "
                    "YOUTUBE_PLAY first or provide its tab id")
            media_page = configure_dialogs(candidates[0])
            return get_page(tab_id(media_page))

        def present_page(page, maximize=False):
            """Bring this managed page forward and verify OS ownership."""
            if context_visible is not True:
                return {
                    "presented": False,
                    "presentation_error": "the managed browser session is headless",
                }
            try:
                page.bring_to_front()
            except Exception:
                pass
            try:
                import browser_window
                return browser_window.present_owned_browser(
                    profile_dir(self.engine_name), title_hint=page.title(),
                    maximize=maximize)
            except Exception as exc:
                return {"presented": False,
                        "presentation_error": str(exc)[:300]}

        def interaction_may_navigate(locator):
            """Capture navigation intent before a click can detach the node."""
            if locator is None:
                return False
            try:
                tag = (locator.evaluate("el => el.tagName") or "").lower()
                kind = (locator.get_attribute("type") or "").lower()
                role = (locator.get_attribute("role") or "").lower()
                handler = locator.get_attribute("onclick") or ""
                href = locator.get_attribute("href") or ""
                return bool(tag in {"a", "button"}
                            or kind in {"submit", "button"}
                            or role in {"button", "link"}
                            or handler or href)
            except Exception:
                return True

        def click_control(page, locator):
            check_deadline(args)
            before_url = page.url
            baseline = locator.evaluate(CLICK_OBSERVER_JS)
            try:
                locator.click(timeout=CLICK_DISPATCH_TIMEOUT_MS)
            except Exception:
                if page.url != before_url:
                    return  # Navigation proves the original click landed.
                try:
                    count = locator.evaluate(CLICK_COUNT_JS, timeout=1000)
                except Exception:
                    raise RuntimeError('Click outcome is unknown; snapshot before retrying')
                if count is None:
                    raise RuntimeError('Click target changed; snapshot before retrying')
                if count == baseline:
                    locator.evaluate("el => el.click()")
                # Otherwise the native click already dispatched. Repeating it
                # could submit an application or payment a second time.

        def settle_after(page, before_url, may_navigate=False):
            """Let a navigation started by an interaction finish.

            Returns whether the page moved. The probe is short for an element
            that is unlikely to navigate and longer for a link or a submit,
            so an in-page click stays cheap while a sign-in button gets the
            time its redirect chain needs.
            """
            probe_ms = NAVIGATION_SETTLE_MS if may_navigate else SHORT_SETTLE_MS
            waited = 0
            while waited < probe_ms:
                page.wait_for_timeout(100)
                waited += 100
                if page.url != before_url:
                    break
            else:
                return False
            # It moved. Let the rest of the redirect chain land -- an SSO
            # handshake is several hops, and stopping at the first means
            # reading a page that is about to be replaced.
            for state in ("domcontentloaded", "networkidle"):
                try:
                    page.wait_for_load_state(state, timeout=LOAD_SETTLE_MS)
                except Exception:
                    pass
            return True

        def page_signature(page):
            """A small, content-free fingerprint for same-page transitions."""
            try:
                return page.evaluate("""() => {
                  const body = document.body;
                  const text = body ? (body.innerText || '').slice(0, 4000) : '';
                  const controls = document.querySelectorAll(
                    'input,select,textarea,button,[role],[contenteditable=true]').length;
                  return [location.href, document.title, text, controls].join('\u241f');
                }""")
            except Exception:
                return f"{page.url}\u241f{page.title()}"

        def control_value(locator):
            return locator.evaluate("""el => {
              const type = (el.getAttribute('type') || '').toLowerCase();
              if (type === 'checkbox' || type === 'radio') return String(!!el.checked);
              if (el.isContentEditable) return (el.innerText || el.textContent || '').trim();
              if (el.value !== undefined) return String(el.value);
              return (el.getAttribute('aria-valuetext') ||
                      el.getAttribute('aria-valuenow') ||
                      el.innerText || '').trim();
            }""")

        def fill_control(locator, wanted, ref=""):
            check_deadline(args)
            normalized = normalize_control_value(
                wanted,
                control_type=locator.get_attribute("type") or "",
                placeholder=locator.get_attribute("placeholder") or "",
                name=locator.get_attribute("name") or "",
                label=locator.get_attribute("aria-label") or ref,
            )
            locator.fill(normalized)
            actual = control_value(locator)
            if actual != normalized:
                # Controlled React/Vue fields occasionally discard a plain
                # fill. Keyboard input exercises the same event path a person
                # does, then the readback below decides whether it stuck.
                locator.click()
                locator.press("Control+A")
                locator.press("Backspace")
                locator.type(normalized)
                actual = control_value(locator)
            if actual != normalized:
                raise ValueError("the page rejected the entered value")
            return normalized

        def select_control(page, locator, wanted):
            check_deadline(args)
            wanted = str(wanted)
            tag = (locator.evaluate("el => el.tagName") or "").lower()
            if tag == "select":
                try:
                    locator.select_option(label=wanted)
                except Exception:
                    locator.select_option(value=wanted)
                selected = locator.locator("option:checked").first
                actual = (selected.inner_text() if selected.count()
                          else control_value(locator))
                if wanted.casefold() != str(actual).strip().casefold() and control_value(locator) != wanted:
                    raise ValueError("the dropdown did not retain that option")
                return

            locator.click()
            option = None
            for scope in content_frames(page):
                for role in ("option", "menuitem", "radio"):
                    try:
                        option = first_usable(
                            scope.get_by_role(role, name=wanted, exact=True))
                    except Exception:
                        option = None
                    if option is not None:
                        break
                if option is None:
                    try:
                        option = first_usable(scope.get_by_text(wanted, exact=True))
                    except Exception:
                        option = None
                if option is not None:
                    break
            if option is None:
                raise ValueError(f"no visible option called {wanted!r}")
            option_handle = option.element_handle()
            option.click()
            page.wait_for_timeout(100)
            actual = control_value(locator)
            selected = False
            if wanted.casefold() != str(actual).strip().casefold() and option_handle is not None:
                try:
                    selected = ((option_handle.get_attribute('aria-selected') or '').lower() == 'true'
                                or (option_handle.get_attribute('aria-checked') or '').lower() == 'true')
                except Exception:
                    pass
            if wanted.casefold() != str(actual).strip().casefold() and not selected:
                raise ValueError("the custom dropdown did not confirm its selection")

        def set_control_checked(locator, wanted):
            check_deadline(args)
            wanted = bool(wanted)
            tag = (locator.evaluate("el => el.tagName") or "").lower()
            kind = (locator.get_attribute("type") or "").lower()
            if tag == "input" and kind in {"checkbox", "radio"}:
                locator.check() if wanted else locator.uncheck()
                actual = locator.is_checked()
            else:
                current = (locator.get_attribute("aria-checked") or "false").lower() == "true"
                if current != wanted:
                    locator.click()
                actual = (locator.get_attribute("aria-checked") or "false").lower() == "true"
            if actual != wanted:
                raise ValueError("the control did not retain its checked state")

        def receipt(operation, page=None, **extra):
            # ok defaults to True and callers override it through extra. It
            # used to be hardcoded True, so a form where every single field
            # failed still came back {"ok": true, "filled": [], "failed":
            # [...]} -- and the model read the flag, not the detail, and
            # announced a successful login that had never happened.
            data = {"ok": True, "operation": operation}
            if page is not None:
                data.update({"tab": tab_id(page), "url": page.url, "title": page.title()})
            data.update(extra)
            # A key set to None is noise in a receipt a model has to read.
            data = {k: v for k, v in data.items() if v is not None}
            return "[Browser Receipt] " + json.dumps(data, ensure_ascii=False)

        # A form is very often inside an iframe, and page.locator only ever
        # searches the main one. placements.haveloc.com has five frames; the
        # snapshot reported zero elements and the model concluded the login
        # form could not be clicked, which was true but for the wrong reason.
        CAPTCHA_HOSTS = (
            "google.com/recaptcha", "recaptcha.net", "hcaptcha.com",
            "challenges.cloudflare.com", "turnstile",
        )

        def frame_kind(frame):
            """'captcha', 'blank', or 'content'."""
            url = (getattr(frame, "url", "") or "").lower()
            if not url or url == "about:blank":
                return "blank"
            if any(host in url for host in CAPTCHA_HOSTS):
                return "captcha"
            return "content"

        def content_frames(page):
            """Frames worth searching: the page's own, never a bot check.

            A CAPTCHA is a control asking for a person, and clicking it from
            here would be defeating it rather than passing it. Those frames are
            reported so Himanshu knows he has to step in, and are never
            searched for something to click.
            """
            frames = []
            for frame in page.frames:
                if frame_kind(frame) == "content":
                    frames.append(frame)
            return frames or [page.main_frame]

        # Words that name a field's type rather than its label. "password" is
        # never the name attribute on a well-built login form, but it is always
        # what the model writes, and input[type=password] is unambiguous.
        TYPE_WORDS = {
            "password": "password", "passwd": "password", "pass": "password",
            "email": "email", "e-mail": "email", "mail": "email",
            "search": "search", "query": "search", "q": "search",
            "tel": "tel", "phone": "tel", "mobile": "tel",
            "url": "url", "number": "number",
        }

        # Playwright's by-label and by-role lookups happily return the button
        # beside a field. On YouTube, get_by_label("search") resolved to the
        # Search *button*, and fill() then failed with "Element is not an
        # <input>". Visible and enabled is not enough; it has to be typeable.
        EDITABLE_TAGS = {"input", "textarea", "select"}

        def is_typeable(item):
            try:
                tag = item.evaluate("el => el.tagName.toLowerCase()")
                if tag in EDITABLE_TAGS:
                    return tag != "input" or (
                        item.get_attribute("type") or "text"
                    ).lower() not in {"button", "submit", "reset", "image"}
                role = (item.get_attribute("role") or "").lower()
                return (bool(item.evaluate("el => el.isContentEditable"))
                        or role in {"textbox", "combobox", "spinbutton"})
            except Exception:
                return False

        def first_usable(locator, limit=8, editable=False):
            """The first match a person could actually interact with."""
            try:
                total = locator.count()
            except Exception:
                return None
            for index in range(min(total, limit)):
                item = locator.nth(index)
                try:
                    if not (item.is_visible() and item.is_enabled()):
                        continue
                    if editable and not is_typeable(item):
                        continue
                    return item
                except Exception:
                    continue
            return None

        def find_target(page, key, editable=True, settle_ms=1200):
            """Find a target, giving a late-arriving frame one chance to appear.

            goto waits for domcontentloaded on the MAIN frame, so an iframe's
            own document can still be loading when the next action runs. The
            field is then genuinely absent, and reporting "no field by that
            ref, name, label, placeholder or type" is true but misleading: a
            moment later it is there. The model reads that as "the login form
            does not exist" and gives up on a page that was merely slow.

            One bounded retry. A field that really is missing costs an extra
            1.2s to say so, which is worth paying to stop VANTA abandoning a
            form that had not finished rendering.
            """
            for attempt in (0, 1):
                for scope in content_frames(page):
                    found = find_in_scope(scope, key, editable)
                    if found is not None:
                        return found
                if attempt == 0 and settle_ms:
                    try:
                        page.wait_for_timeout(settle_ms)
                    except Exception:
                        return None
            return None

        def find_in_scope(page, key, editable=True):
            """Locate an element by ref, or by the name a person would use.

            Refs come from a snapshot and are exact, so they still win. But the
            model reliably writes fields="username=..." -- because that is what
            the field is called on the page -- and does not reliably snapshot
            first and use e7. Insisting on refs meant a login form simply could
            not be filled: every guessed key missed, and the turn ended with a
            confident claim that it had worked. Accepting both is what makes
            the action usable by the thing that calls it.
            """
            key = str(key or "").strip()
            if not key:
                return None
            if key.startswith(('css=', 'xpath=')):
                matches = page.locator(key)
                usable = [matches.nth(i) for i in range(matches.count())
                          if matches.nth(i).is_visible() and matches.nth(i).is_enabled()]
                if len(usable) > 1:
                    raise ValueError('The explicit selector matches multiple controls; narrow it before acting')
                return usable[0] if usable else None
            safe = key.replace('"', '\\"')
            low = key.lower()

            exact_ref = page.locator(f'[data-vanta-ref="{safe}"]')
            try:
                if exact_ref.count() == 1:
                    return exact_ref
            except Exception:
                pass
            if re.fullmatch(r'e[0-9a-f]{12}-\d+', key):
                return None  # A stale reference must never become a fuzzy name.

            if editable:
                tags = ("input", "textarea", "select")
                attempts = [
                    ", ".join(f'{tag}[name="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[id="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[placeholder*="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[aria-label*="{safe}" i]' for tag in tags),
                    f'input[autocomplete*="{safe}" i]',
                    f'[contenteditable="true"][aria-label*="{safe}" i]',
                    f'[role="textbox"][aria-label*="{safe}" i]',
                    f'[role="combobox"][aria-label*="{safe}" i]',
                ]
            else:
                tags = ("button", "a", "input", "[role=button]", "[role=link]",
                        "[role=menuitem]", "[role=option]", "[role=tab]",
                        "[role=treeitem]")
                attempts = [
                    ", ".join(f'{tag}[name="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[id="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[aria-label*="{safe}" i]' for tag in tags),
                    ", ".join(f'{tag}[value*="{safe}" i]' for tag in tags),
                ]

            for selector in attempts:
                found = first_usable(page.locator(selector), editable=editable)
                if found is not None:
                    return found

            # A visible label sitting next to the field, which is how a person
            # would identify it and how the page is usually built.
            role_names = (("textbox", "combobox", "spinbutton") if editable
                          else ("button", "link", "menuitem", "option", "tab",
                                "treeitem"))
            finders = [lambda: page.get_by_label(key, exact=False),
                       lambda: page.get_by_placeholder(key, exact=False)]
            finders.extend(
                lambda role=role: page.get_by_role(role, name=key, exact=False)
                for role in role_names)
            for finder in finders:
                try:
                    found = first_usable(finder(), editable=editable)
                    if found is not None:
                        return found
                except Exception:
                    continue

            if not editable:
                try:
                    found = first_usable(page.get_by_text(key, exact=False))
                    if found is not None:
                        return found
                except Exception:
                    pass

            # Partial attribute matches, after every exact one has missed.
            # YouTube's box is name="search_query", which "search" should
            # still reach.
            partial = ", ".join(
                f'{tag}[{attr}*="{safe}" i]'
                for tag in (("input", "textarea", "select", "[contenteditable]",
                             "[role=textbox]", "[role=combobox]") if editable
                            else ("button", "a", "[role=button]", "[role=link]",
                                  "[role=menuitem]", "[role=option]", "[role=tab]"))
                for attr in ("name", "id", "title"))
            found = first_usable(page.locator(partial), editable=editable)
            if found is not None:
                return found

            # Last: the field's type, for the words that name one.
            wanted = TYPE_WORDS.get(low)
            if editable and wanted:
                found = first_usable(page.locator(f'input[type="{wanted}"]'),
                                     editable=True)
                if found is not None:
                    return found
                if wanted == "search":
                    for selector in ('input[role="combobox"]',
                                     'input[type="text"]', 'textarea',
                                     '[contenteditable="true"]'):
                        found = first_usable(page.locator(selector),
                                             editable=True)
                        if found is not None:
                            return found
            return None

        def _all_file_inputs(page):
            found = []
            for scope in content_frames(page):
                try:
                    files = scope.locator('input[type="file"]')
                    for index in range(files.count()):
                        found.append(files.nth(index))
                except Exception:
                    continue
            return found

        def _file_identity(locator):
            try:
                return locator.evaluate("""el => {
                  let label = '';
                  if (el.labels && el.labels.length) label = el.labels[0].innerText || '';
                  return [el.getAttribute('data-vanta-ref'), el.id, el.name,
                          el.getAttribute('aria-label'), el.getAttribute('title'),
                          el.getAttribute('accept'), label,
                          'file upload attachment']
                    .filter(Boolean).join(' ').toLowerCase();
                }""")
            except Exception:
                return ""

        def _files_below(locator):
            """File inputs semantically owned by a trigger's nearest region."""
            for ancestor in ("form", "*[@role='dialog']", "fieldset",
                             "section", "div", "tr"):
                try:
                    region = locator.locator(f"xpath=ancestor::{ancestor}[1]")
                    if region.count() != 1:
                        continue
                    files = region.locator('input[type="file"]')
                    if files.count() == 1:
                        return files.first
                except Exception:
                    continue
            return None

        def find_upload_target(page, key):
            """Resolve a visible upload affordance to its real file input.

            Web applications usually hide ``input[type=file]`` and show an
            "Add resume" button. Ordinary editable-field resolution requires
            visibility, so it can never find that input. This resolver uses
            semantic identity and DOM ownership, not page-specific selectors.
            """
            key = str(key or "").strip()
            terms = [word for word in re.findall(r"[a-z0-9]+", key.casefold())
                     if len(word) > 1]
            files = _all_file_inputs(page)

            # An exact snapshot ref is definitive even when the input is
            # hidden; snapshot refs are unique across content frames.
            if key:
                for item in files:
                    try:
                        if item.get_attribute("data-vanta-ref") == key:
                            return item, None, "snapshot_ref"
                    except Exception:
                        continue

            if re.fullmatch(r'e[0-9a-f]{12}-\d+', key):
                trigger = find_target(page, key, editable=False)
                if trigger is None:
                    return None, None, ''
                return _files_below(trigger), trigger, 'snapshot_trigger'

            # Prefer a file input whose own label/name says what the model
            # asked for (resume, portfolio, attachment, photograph...).
            scored = []
            for item in files:
                identity = _file_identity(item)
                score = sum(1 for term in terms if term in identity)
                if score:
                    scored.append((score, item))
            if scored:
                scored.sort(key=lambda pair: pair[0], reverse=True)
                if len(scored) == 1 or scored[0][0] > scored[1][0]:
                    return scored[0][1], None, "semantic_file_input"

            trigger = find_target(page, key, editable=False) if key else None
            if trigger is not None:
                if (trigger.evaluate('el => el.tagName.toLowerCase()') == 'input'
                        and (trigger.get_attribute('type') or '').lower() == 'file'):
                    return trigger, None, 'direct_file_input'
                owned = _files_below(trigger)
                if owned is not None:
                    return owned, trigger, "trigger_region"

            # A page with one upload slot is unambiguous regardless of how
            # poor its labels are. Multiple slots are never guessed.
            if len(files) == 1:
                return files[0], trigger, "only_file_input"
            return None, trigger, ""

        def selected_file_names(locator):
            try:
                return locator.evaluate(
                    "el => Array.from(el.files || []).map(file => file.name)") or []
            except Exception:
                return []

        def upload_file(page, key, raw_path):
            check_deadline(args)
            path = Path(str(raw_path or "")).expanduser()
            if not path.is_absolute():
                path = (BASE_DIR / path).resolve()
            if not path.is_file():
                return {"ok": False, "verified": False,
                        "error": f"Local upload file does not exist: {path}",
                        "file": path.name or str(raw_path or "")}

            target, trigger, resolved_by = find_upload_target(page, key)
            if target is None and trigger is None:
                return {"ok": False, "verified": False,
                        "error": (f"Could not resolve upload control {key!r}; "
                                  "use a targeted BROWSER_SNAPSHOT query to "
                                  "inspect the form."),
                        "file": path.name}

            if target is None and trigger is not None:
                # Some controls create their input only when clicked; others
                # open the native chooser directly. Cover both without ever
                # typing an OS path into whichever window happens to be active.
                try:
                    with page.expect_file_chooser(timeout=4000) as pending:
                        trigger.click(timeout=CLICK_DISPATCH_TIMEOUT_MS)
                    pending.value.set_files(str(path))
                    resolved_by = "native_file_chooser"
                except Exception:
                    page.wait_for_timeout(250)
                target, _, after_click = find_upload_target(page, key)
                resolved_by = after_click or resolved_by

            if target is not None:
                check_deadline(args)
                target.set_input_files(str(path))

            observed = selected_file_names(target) if target is not None else []
            # Another input holding a file with the same name is not evidence
            # that the requested upload control received this file.
            verified = path.name in observed
            return {"ok": verified, "verified": verified,
                    "file": path.name, "bytes": path.stat().st_size,
                    "observed_files": observed,
                    "resolved_by": resolved_by,
                    "error": None if verified else
                    "The browser did not report the selected file after upload."}

        def ref_state(page):
            """What refs exist right now -- evidence, not advice.

            Refs are assigned by the snapshot, so before one is taken no
            element carries a ref and every ref the model guesses will miss.
            Saying so turns a dead end into something it can act on.
            """
            try:
                existing = page.locator("[data-vanta-ref]").count()
            except Exception:
                return "no snapshot has been taken for this page, so no refs exist yet"
            if not existing:
                return ("no snapshot has been taken for this page, so no refs "
                        "exist yet; refs are created by BROWSER_SNAPSHOT")
            return (f"{existing} refs exist from the last snapshot of this page; "
                    "if the page changed since, they are stale")

        while True:
            operation, args, reply = self.requests.get()
            try:
                check_deadline(args)
                if operation == "start":
                    ensure_started(args.get("visible", True))
                    page = get_page(visible=args.get("visible", True))
                    presentation = (present_page(page)
                                    if args.get("visible", True) else
                                    {"presented": False,
                                     "background": True})
                    value = receipt("start", page, **presentation)
                elif operation == "sync_login":
                    # Re-borrow the sessions, for after he has signed into
                    # something new in his own browser. Must happen on this
                    # thread: the context belongs to it.
                    ensure_started(args.get("visible", True))
                    import browser_login
                    value = browser_login.import_into(
                        context, domains=args.get("domains") or None)
                elif operation == "tabs":
                    ensure_started()
                    tabs = [
                        {"tab": tab_id(p), "title": p.title(), "url": p.url,
                         "media_owner": p is media_page or None}
                        for p in context.pages if not p.is_closed()
                    ]
                    value = "[Browser Tabs] " + json.dumps(tabs, ensure_ascii=False)
                elif operation == "goto":
                    page = get_page(args.get("tab"), args.get("visible", True))
                    url = _validate_url(args["url"])
                    page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                    presentation = (present_page(page)
                                    if args.get("visible", True) else
                                    {"presented": False,
                                     "background": True})
                    value = receipt(
                        "goto", page,
                        ok=(bool(presentation.get("presented"))
                            if args.get("visible", True) else True),
                        **presentation)
                elif operation == "dialog":
                    page = get_page(args.get("tab"))
                    mode = normalize_dialog_mode(args.get("mode"))
                    if mode == "status":
                        recent = list(dialog_history.get(page, [])[-5:])
                        value = receipt(
                            "dialog", page, ok=bool(recent), dialogs=recent,
                            note=None if recent else
                            "No recent JavaScript dialog was observed on this tab.")
                    else:
                        dialog_policies[page] = {
                            "mode": mode,
                            "text": str(args.get("text") or ""),
                            "expires": time.monotonic() + 30.0,
                        }
                        value = receipt(
                            "dialog", page, armed_for_next_dialog=mode,
                            expires_in_seconds=30,
                            note="Run the click or submit that opens the dialog next.")
                elif operation == "snapshot":
                    page = get_page(args.get("tab"))
                    # A form cannot be filled from names alone: the model needs
                    # each field's type, what is already in it, whether it is
                    # required, and for a select, which options actually exist.
                    #
                    # And it has to look in every frame. This read the main
                    # frame only, so a login form inside an iframe came back as
                    # an empty page, and the model reported -- accurately, for
                    # the wrong reason -- that the form could not be clicked.
                    query = str(args.get("query") or "").strip()
                    script = """(els, params) => {""" + STABLE_REFERENCES_JS + """
                        const offset = Number(params.offset || 0);
                        const terms = String(params.query || '').toLowerCase()
                          .split(/[^a-z0-9]+/).filter(x => x.length > 1);
                        const ranked = els.map((el, order) => {
                          // Only what can actually be interacted with, plus
                          // hidden file inputs. A visible "Add resume" button
                          // commonly drives a hidden file input; excluding the
                          // input made the upload impossible even though the
                          // page exposed the capability.
                          //
                          // The college portal's sign-in is a 0x0 hidden
                          // <button id="submitBtn">, driven by a visible
                          // <span onclick="$('#submitBtn').click()">LOGIN</span>.
                          // Offering the hidden button as a ref is offering
                          // something that can never be clicked: Playwright
                          // resolved it, waited the full 30 seconds for it to
                          // become actionable, and the turn reported that the
                          // page's sign-in control "isn't accessible to the
                          // browser" -- asking Himanshu to sign in by hand to
                          // a session he was already signed into.
                          const r = el.getBoundingClientRect();
                          const isFile = el.tagName.toLowerCase() === 'input' &&
                            (el.getAttribute('type') || '').toLowerCase() === 'file';
                          const visible = r.width > 0 && r.height > 0;
                          if (!visible && !isFile) return null;
                          const label = el.labels && el.labels.length
                            ? (el.labels[0].innerText || '') : '';
                          const haystack = [el.innerText, el.textContent, el.id,
                            el.name, el.getAttribute('aria-label'),
                            el.getAttribute('placeholder'), el.getAttribute('title'),
                            label, isFile ? 'file upload attachment' : '']
                            .filter(Boolean).join(' ').toLowerCase();
                          const score = terms.reduce(
                            (n, term) => n + (haystack.includes(term) ? 1 : 0), 0);
                          return {el, order, score, visible};
                        }).filter(Boolean);
                        const candidates = terms.length
                          ? ranked.filter(x => x.score > 0)
                          : ranked;
                        candidates.sort((a, b) =>
                          (b.score - a.score) ||
                          (Number(b.visible) - Number(a.visible)) ||
                          (a.order - b.order));
                        return candidates.slice(0, 180).map(({el, visible}, i) => {
                          const ref = referenceFor(el);
                          const tag = el.tagName.toLowerCase();
                          let label = '';
                          if (el.labels && el.labels.length) label = el.labels[0].innerText || '';
                          if (!label && el.id) {
                            const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                            if (l) label = l.innerText || '';
                          }
                          // An icon-only control has no text of its own. The
                          // gold arrow beside LOGIN came back with an empty
                          // name, so the model could not tell what it was and
                          // did not try it. Fall back through the places a
                          // name can hide, and last of all say what the
                          // handler does.
                          let fallback = '';
                          const img = el.querySelector && el.querySelector('img');
                          if (img) fallback = img.getAttribute('alt') || '';
                          if (!fallback) fallback = el.getAttribute('title') || '';
                          if (!fallback && el.value && tag !== 'input') fallback = el.value;
                          if (!fallback) {
                            const handler = el.getAttribute('onclick') || '';
                            const target = handler.match(/#([A-Za-z0-9_-]+)/);
                            if (target) {
                              const driven = document.getElementById(target[1]);
                              fallback = driven
                                ? (driven.innerText || driven.value || target[1]).trim()
                                : target[1];
                            }
                          }
                          const name = (el.getAttribute('aria-label') || label || el.getAttribute('placeholder') || el.innerText || el.title || el.name || fallback || '').trim().replace(/\\s+/g,' ').slice(0,160);
                          const out = {ref, tag, role: el.getAttribute('role') || '', name};
                          for (const attr of ['placeholder','autocomplete','aria-expanded',
                                             'aria-selected','aria-checked','contenteditable']) {
                            const v = el.getAttribute(attr); if (v !== null) out[attr] = v;
                          }
                          if (tag === 'a' && el.getAttribute('href')) {
                            out.href = el.getAttribute('href').slice(0, 200);
                          }
                          if (tag === 'input' || tag === 'textarea' || tag === 'select') {
                            out.type = el.type || 'text';
                            if (!visible) out.hidden = true;
                            if (el.type === 'checkbox' || el.type === 'radio') out.checked = !!el.checked;
                            else out.value = (el.value || '').slice(0, 80);
                            if (el.required) out.required = true;
                            if (el.disabled) out.disabled = true;
                            if (tag === 'select') out.options = Array.from(el.options).slice(0,40).map(o => o.label || o.value);
                          }
                          return out;
                        });
                    }"""
                    # [onclick] matters as much as the semantic tags. The one
                    # element on the portal's login page carrying the word
                    # LOGIN is a <span> with a click handler, and a selector
                    # built from tag names alone could not see it at all --
                    # so the page that is "just a button you click" had no
                    # button in the snapshot.
                    selector = ("a,button,input,textarea,select,[onclick],"
                                "[contenteditable=true],"
                                "[role=button],[role=link],[role=checkbox],"
                                "[role=combobox],[role=textbox],[role=menuitem],"
                                "[role=option],[role=tab],[role=treeitem],"
                                "[role=switch],[role=slider],[role=spinbutton]")

                    def scan_frames():
                        found, blocked, offset = [], [], 0
                        for frame in page.frames:
                            kind = frame_kind(frame)
                            if kind == "blank":
                                continue
                            if kind == "captcha":
                                # Reported, never offered as something to
                                # click. It is asking for a person, and that
                                # person is Himanshu.
                                blocked.append({
                                    "kind": "captcha",
                                    "url": (frame.url or "")[:120],
                                    "note": "a human must complete this; "
                                            "VANTA cannot and should not "
                                            "click it",
                                })
                                continue
                            try:
                                items = frame.locator(selector).evaluate_all(
                                    script, {"offset": offset, "query": query})
                            except Exception:
                                continue
                            if frame is not page.main_frame:
                                for item in items:
                                    item["frame"] = (frame.url or "")[:100]
                            found.extend(items)
                            offset += len(items)
                        return found, blocked

                    refs, blockers = scan_frames()
                    if not refs:
                        # An empty page is usually one that has not finished.
                        # A bot check's iframes attach a second or two after
                        # load, so the first look sees nothing and cannot say
                        # why. Only pay for the wait when it looks empty, which
                        # is exactly when the answer matters.
                        page.wait_for_timeout(2500)
                        refs, blockers = scan_frames()

                    focused_ref = ""
                    if query and refs:
                        # Put the best visible match in view. This is a
                        # semantic scroll, not a pixel guess, and works for a
                        # control far below the fold or inside a content frame.
                        for item in refs:
                            if item.get("hidden"):
                                continue
                            for scope in content_frames(page):
                                try:
                                    candidate = scope.locator(
                                        f'[data-vanta-ref="{item["ref"]}"]')
                                    if candidate.count() == 1:
                                        candidate.scroll_into_view_if_needed(
                                            timeout=3000)
                                        focused_ref = item["ref"]
                                        break
                                except Exception:
                                    continue
                            if focused_ref:
                                break

                    try:
                        text = page.locator("body").inner_text(timeout=10_000)[:12_000]
                    except Exception:
                        text = ""

                    payload = {
                        "tab": tab_id(page), "url": page.url,
                        "title": page.title(), "elements": refs, "text": text,
                    }
                    if query:
                        payload["query"] = query
                        payload["focused_ref"] = focused_ref or None
                    try:
                        overlays = page.evaluate(HTML_OVERLAY_SCRIPT) or []
                    except Exception:
                        overlays = []
                    if overlays:
                        payload["overlays"] = overlays
                    recent_dialogs = list(dialog_history.get(page, [])[-5:])
                    if recent_dialogs:
                        payload["browser_dialogs"] = recent_dialogs
                    if blockers:
                        payload["blocked_by"] = blockers
                    if not refs:
                        payload["note"] = (
                            "No interactive element was found in any frame. "
                            + ("This page is behind a bot check: ask Himanshu "
                               "to complete it in this browser window, then "
                               "snapshot again. Do not try to click it."
                               if blockers else
                               "The page may still be loading; BROWSER_WAIT "
                               "then snapshot again."))
                    value = "[Browser Snapshot] " + json.dumps(
                        payload, ensure_ascii=False)
                elif operation in {"click", "type", "press"}:
                    page = get_page(args.get("tab"))
                    before_pages = list(context.pages)
                    source_tab = tab_id(page)
                    dialog_index = len(dialog_history.get(page, []))
                    ref = args.get("ref", "")
                    locator = find_target(page, ref,
                                          editable=operation != "click")
                    if locator is None:
                        raise ValueError(
                            f"Could not find {ref!r} on this page, by ref or "
                            f"by name, label, placeholder or type; "
                            + ref_state(page))
                    before_url = page.url
                    may_navigate = (interaction_may_navigate(locator)
                                    if operation == "click" else False)
                    if operation == "click":
                        # Playwright folds navigation waiting into click(). An
                        # SSO page with long-lived requests can therefore hold
                        # this line for the full 30-second action timeout even
                        # though the click already landed. Cap dispatch; if the
                        # URL has not moved and the native click could not be
                        # completed, use the element's own DOM click. The
                        # bounded verifier below owns navigation waiting once.
                        click_control(page, locator)
                    elif operation == "type":
                        fill_control(locator, args.get("text", ""), ref)
                    else:
                        locator.press(args.get("key", "Enter"))
                    # A click that starts a navigation needs the page it
                    # lands on, not the old one 250 milliseconds later.
                    #
                    # The college portal is one button: LOGIN, which begins an
                    # SSO redirect chain. The snapshot taken straight after
                    # caught it mid-redirect, the model saw no sign-in control
                    # and reported that the page "isn't accessible to the
                    # browser" -- asking Himanshu to log in by hand to a
                    # session he was already signed into. The submit path
                    # below already waited for networkidle for exactly this
                    # reason; the click path never learned it.
                    navigated = settle_after(page, before_url,
                                             may_navigate=may_navigate)
                    opened_tabs = adopt_popup(before_pages)
                    if opened_tabs and len(opened_tabs) == 1:
                        page = get_page()
                    value = receipt(operation, page, ref=ref,
                                    source_tab=source_tab if opened_tabs else None,
                                    opened_tabs=opened_tabs or None,
                                    clicked=True if operation == "click" else None,
                                    written=True if operation == "type" else None,
                                    verified=True if operation == "type" else None,
                                    navigated=navigated or None,
                                    came_from=before_url if navigated else None,
                                    dialogs=dialogs_since(page, dialog_index) or None)
                elif operation in {"select", "check", "upload"}:
                    page = get_page(args.get("tab"))
                    ref = args.get("ref", "")
                    # These three were left on the ref-only path when click,
                    # type, fill_form and submit moved to the resolver, so a
                    # dropdown or a file input could still only be reached from
                    # a snapshot -- the exact gap the resolver exists to close.
                    if operation == "upload":
                        details = upload_file(page, ref, args.get("path", ""))
                        page.wait_for_timeout(200)
                        value = receipt("upload", page, ref=ref, **details)
                        reply.put((True, value))
                        continue

                    locator = find_target(page, ref, editable=operation != "check")
                    if locator is None:
                        raise ValueError(
                            f"Could not find {ref!r} on this page, by ref or "
                            f"by name, label, placeholder or type; "
                            + ref_state(page))
                    if operation == "select":
                        select_control(page, locator, args.get("value", ""))
                    else:
                        set_control_checked(locator, args.get("checked", True))
                    page.wait_for_timeout(200)
                    value = receipt(operation, page, ref=ref, verified=True)
                elif operation == "fill_form":
                    # One field at a time costs a model round trip each, and a
                    # half-filled form is worse than an unfilled one.
                    page = get_page(args.get("tab"))
                    filled, verified, failed = [], [], []
                    for ref, entry in (args.get("fields") or {}).items():
                        check_deadline(args)
                        try:
                            kind = str(entry.get("kind", "fill"))
                            text = entry.get("value", "")
                            locator = (None if kind == "upload" else
                                       find_target(page, ref, editable=True))
                            if kind != "upload" and locator is None:
                                raise ValueError(
                                    "no field by that ref, name, label, "
                                    "placeholder or type")
                            # "size=Large" on a radio group is the natural way
                            # to say it, and reads as a select to the model.
                            # select_option only works on a <select>, so the
                            # whole field failed on any form using radios.
                            control = ((locator.get_attribute("type") or "").lower()
                                       if locator is not None else "")
                            if kind == "upload":
                                details = upload_file(page, ref, text)
                                if not details.get("verified"):
                                    raise ValueError(details.get("error") or
                                                     "file selection was not verified")
                            elif control == "radio":
                                group = locator.get_attribute("name") or ref
                                option = first_usable(page.locator(
                                    f'input[type="radio"][name="{group}"]'
                                    f'[value="{text}" i]'))
                                if option is None:
                                    option = first_usable(page.get_by_role(
                                        "radio", name=str(text), exact=False))
                                if option is None:
                                    raise ValueError(
                                        f"no {group!r} option called {text!r}")
                                option.check()
                            elif kind == "select":
                                select_control(page, locator, text)
                            elif kind == "check":
                                set_control_checked(locator, str(text).lower() not in {"false", "0", "no"})
                            else:
                                fill_control(locator, text, ref)
                            filled.append(ref)
                            verified.append(ref)
                        except Exception as field_error:
                            failed.append(f"{ref}: {field_error}")
                    page.wait_for_timeout(250)
                    value = receipt("fill_form", page,
                                    ok=bool(filled) and not failed,
                                    filled=filled, verified=verified, failed=failed,
                                    refs=ref_state(page) if failed else None)
                elif operation == "submit":
                    page = get_page(args.get("tab"))
                    before_pages = list(context.pages)
                    source_tab = tab_id(page)
                    dialog_index = len(dialog_history.get(page, []))
                    ref = args.get("ref", "")
                    before = page.url
                    before_signature = page_signature(page)
                    if ref:
                        locator = find_target(page, ref, editable=False)
                        if locator is None:
                            raise ValueError(
                                f"Could not find {ref!r} to submit; "
                                + ref_state(page))
                        invalid = locator.evaluate(VALIDATION_JS)
                        if invalid:
                            value = receipt('submit', page, ok=False, submitted=False,
                                            validation_errors=invalid)
                            reply.put((True, value))
                            continue
                        click_control(page, locator)
                    else:
                        invalid = page.evaluate('(' + VALIDATION_JS + ')(document.activeElement)')
                        if invalid:
                            value = receipt('submit', page, ok=False, submitted=False,
                                            validation_errors=invalid)
                            reply.put((True, value))
                            continue
                        page.keyboard.press("Enter")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    # The catalogue already tells the model that navigation is
                    # its evidence the form went through; the flag should agree
                    # with it. A single-page app can log in without changing
                    # URL, so ok=False here means "unconfirmed", and the
                    # verify note says how to settle it rather than leaving the
                    # model to assume either way.
                    navigated = page.url != before
                    changed = navigated or page_signature(page) != before_signature
                    opened_tabs = adopt_popup(before_pages)
                    if len(opened_tabs) == 1:
                        page = get_page()
                        changed = True
                    invalid = page.evaluate('(' + VALIDATION_JS + ')(null, true)')
                    changed = changed and not invalid
                    value = receipt("submit", page, ok=changed,
                                    validation_errors=invalid or None,
                                    source_tab=source_tab if opened_tabs else None,
                                    opened_tabs=opened_tabs or None,
                                    navigated=navigated, changed=changed, was=before,
                                    dialogs=dialogs_since(page, dialog_index) or None,
                                    verify=None if changed else
                                    "The URL and visible application state did not change; snapshot before retrying")
                elif operation == "wait":
                    page = get_page(args.get("tab"))
                    if args.get("text"):
                        # Unbounded, this took the context default of 30s every
                        # time the text never arrived -- half a minute of the
                        # turn spent learning nothing. ms is how long to wait
                        # for it, which is what the word means here.
                        try:
                            budget = float(args.get("ms") or 10_000)
                        except (TypeError, ValueError):
                            budget = 10_000.0
                        budget = max(500.0, min(budget, 60_000.0))
                        deadline = time.monotonic() + budget / 1000
                        seen = False
                        while time.monotonic() < deadline and not seen:
                            check_deadline(args)
                            for scope in content_frames(page):
                                try:
                                    seen = scope.get_by_text(args['text'], exact=False).first.is_visible()
                                except Exception:
                                    seen = False
                                if seen:
                                    break
                            if not seen:
                                page.wait_for_timeout(100)
                        if not seen:
                            raise TimeoutError(f'The requested text did not appear within {budget / 1000:g}s')
                        value = receipt("wait", page, found=args["text"][:80])
                    else:
                        deadline = time.monotonic() + max(0, min(int(args.get('ms', 1000)), 10_000)) / 1000
                        while time.monotonic() < deadline:
                            check_deadline(args)
                            page.wait_for_timeout(100)
                        value = receipt("wait", page)
                elif operation == "youtube_play":
                    # Playback is a user-facing surface.  A preceding portal
                    # check may have established a headless session; inheriting
                    # it made audio play in an invisible browser.  Requiring a
                    # visible context here is the generic boundary contract,
                    # independent of the requested video's words.
                    page = get_page(args.get("tab"), visible=True)
                    url = _validate_url(args["url"])
                    host = (urlparse(url).hostname or "").lower()
                    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                        raise ValueError("YOUTUBE_PLAY only accepts a youtube.com video URL")
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=DEFAULT_TIMEOUT_MS)
                    media_page = page
                    video = page.locator("video.html5-main-video").first
                    video.wait_for(state="attached", timeout=15_000)
                    paused = bool(video.evaluate("el => el.paused"))
                    methods = []
                    if paused:
                        button = page.locator("button.ytp-play-button").first
                        try:
                            button.click(timeout=10_000)
                            methods.append("player-button")
                        except Exception:
                            try:
                                button.evaluate("el => el.click()")
                                methods.append("player-button-dom")
                            except Exception:
                                pass
                        page.wait_for_timeout(900)
                    paused = bool(video.evaluate("el => el.paused"))
                    if paused:
                        # One action owns its own bounded recovery.  Sending a
                        # second YOUTUBE_PLAY through the model is correctly
                        # blocked because it could start overlapping media.
                        try:
                            video.evaluate("el => el.play()")
                            methods.append("media-play")
                            page.wait_for_timeout(900)
                        except Exception:
                            pass
                    paused = bool(video.evaluate("el => el.paused"))
                    if paused:
                        try:
                            page.keyboard.press("k")
                            methods.append("youtube-key-k")
                            page.wait_for_timeout(900)
                        except Exception:
                            pass
                    paused = bool(video.evaluate("el => el.paused"))
                    # Playback is explicitly user-facing. Restore and maximise
                    # the exact managed-browser HWND before reporting success;
                    # audible-but-invisible playback is a failed action.
                    presentation = present_page(page, maximize=True)
                    requested_video_id = youtube_video_id(url)
                    actual_video_id = youtube_video_id(page.url)
                    content_verified = bool(
                        requested_video_id
                        and actual_video_id == requested_video_id)
                    value = receipt(
                        "youtube_play", page,
                        ok=(not paused
                            and bool(presentation.get("presented"))
                            and content_verified),
                        playing=not paused,
                        video_ready_state=video.evaluate("el => el.readyState"),
                        playback_methods=methods or ["already-playing"],
                        media_owner=True,
                        requested_url=url,
                        requested_video_id=requested_video_id,
                        actual_video_id=actual_video_id,
                        content_verified=content_verified,
                        **presentation,
                    )
                elif operation == "youtube_fullscreen":
                    page = media_target(args.get("tab"))
                    host = (urlparse(page.url).hostname or "").lower()
                    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                        raise ValueError(
                            "The controlled tab is not on YouTube; open a video first")
                    wanted = bool(args.get("enabled", True))

                    def fullscreen_state():
                        return bool(page.evaluate(
                            "() => Boolean(document.fullscreenElement)"))

                    before = fullscreen_state()
                    method = "already-set"
                    if before != wanted:
                        method = "player-button"
                        try:
                            page.locator("button.ytp-fullscreen-button").first.click(
                                timeout=10_000)
                        except Exception:
                            method = "youtube-key-f"
                            page.keyboard.press("f")
                        deadline = time.monotonic() + 4.0
                        while time.monotonic() < deadline:
                            if fullscreen_state() == wanted:
                                break
                            page.wait_for_timeout(100)
                    after = fullscreen_state()
                    presentation = present_page(page)
                    value = receipt(
                        "youtube_fullscreen", page,
                        ok=after == wanted and bool(
                            presentation.get("presented")),
                        fullscreen=after, requested=wanted, method=method,
                        changed=before != after,
                        media_owner=True,
                        **presentation,
                    )
                elif operation == "close":
                    if context is None:
                        value = "[Browser Receipt] {\"ok\":true,\"operation\":\"close\"}"
                    elif args.get("all_tabs", False):
                        discard_context()
                        value = "[Browser Receipt] {\"ok\":true,\"operation\":\"close_all\"}"
                    else:
                        page = get_page(args.get("tab"))
                        closed_id = tab_id(page)
                        closed_url = page.url
                        closed_title = page.title()
                        media_stopped = "music.youtube.com" in closed_url
                        remaining = [other for other in context.pages
                                     if other is not page
                                     and not other.is_closed()]
                        if page is media_page:
                            media_page = None
                        page.close()
                        # Closing the last tab leaves a context with no window
                        # behind it, which Firefox cannot open a page from
                        # again. Tear the session down instead, so the next
                        # BROWSER_START builds a working one rather than
                        # inheriting a corpse.
                        if not remaining:
                            discard_context("its last tab was closed")
                        value = receipt(
                            "close", tab=closed_id, closed_url=closed_url,
                            closed_title=closed_title,
                            media_stopped=media_stopped,
                            session_ended=not remaining or None)
                else:
                    raise ValueError(f"Unknown browser operation: {operation}")
                reply.put((True, value))
            except InterruptedError as exc:
                reply.put((True, '[Interrupted] ' + str(exc)))
            except Exception as exc:
                reply.put((False, str(exc)))


_worker = None
_worker_lock = threading.Lock()


_real_failed = False


def _make_worker():
    """The worker for the configured engine.

    The real-Firefox worker needs Selenium and an installed Firefox. If either
    is missing the Playwright worker is used instead, said aloud in the log,
    rather than leaving browser control dead.
    """
    global _real_failed
    if engine() == REAL_ENGINE and not _real_failed:
        try:
            import browser_real
            return browser_real.RealFirefoxWorker()
        except Exception as exc:
            _real_failed = True
            print(f"[Browser] Real Firefox control is unavailable ({exc}); "
                  f"using Playwright's Firefox instead.")
    return _BrowserWorker(engine_name=(
        "firefox" if engine() == REAL_ENGINE else engine()
    ))


def _call(operation, **kwargs):
    global _worker, _real_failed
    interrupted = current_interrupt()
    if interrupted is not None:
        kwargs['_interrupt_event'] = interrupted
    with _worker_lock:
        if _worker is None or not _worker.thread.is_alive():
            _worker = _make_worker()
    result = _worker.call(operation, **kwargs)
    # Firefox itself would not start under geckodriver (missing binary, a
    # profile in use). Fall back once, loudly, so a task is not simply lost.
    if (isinstance(result, str) and "Could not start Firefox through geckodriver" in result
            and engine() == REAL_ENGINE and not _real_failed):
        _real_failed = True
        print(f"[Browser] {result[:200]} -- falling back to Playwright's Firefox.")
        with _worker_lock:
            _worker = _BrowserWorker(engine_name="firefox")
        result = _worker.call(operation, **kwargs)
    return result


def start(visible=True): return _call("start", visible=visible)
def tabs(): return _call("tabs")
def goto(url, tab=None, visible=True): return _call("goto", url=url, tab=tab, visible=visible)
def snapshot(tab=None, query=""): return _call("snapshot", tab=tab, query=query)
def click(ref, tab=None): return _call("click", ref=ref, tab=tab)
def type_text(ref, text, tab=None): return _call("type", ref=ref, text=text, tab=tab)
def press(ref, key="Enter", tab=None): return _call("press", ref=ref, key=key, tab=tab)
def select(ref, value, tab=None): return _call("select", ref=ref, value=value, tab=tab)
def check(ref, checked=True, tab=None): return _call("check", ref=ref, checked=checked, tab=tab)
def upload(ref, path, tab=None): return _call("upload", ref=ref, path=path, tab=tab)
def fill_form(fields, tab=None): return _call("fill_form", fields=fields, tab=tab)
def submit(ref=None, tab=None): return _call("submit", ref=ref, tab=tab)
def wait(text=None, ms=1000, tab=None): return _call("wait", text=text, ms=ms, tab=tab)
def dialog(mode="status", text="", tab=None):
    return _call("dialog", mode=mode, text=text, tab=tab)
def youtube_play(url, tab=None): return _call("youtube_play", url=url, tab=tab)
def youtube_fullscreen(enabled=True, tab=None):
    return _call("youtube_fullscreen", enabled=bool(enabled), tab=tab)
def close(tab=None, all_tabs=False): return _call("close", tab=tab, all_tabs=all_tabs)
def sync_login(domains=None): return _call("sync_login", domains=domains)

# Backward-compatible names used by older action branches.
get_content = snapshot
close_browser = lambda: close(all_tabs=True)
