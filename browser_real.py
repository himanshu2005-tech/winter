# -*- coding: utf-8 -*-
"""
browser_real.py -- VANTA driving Himanshu's own, installed Firefox.

Playwright cannot do this. It speaks to Firefox through a private protocol
that only its own patched build implements; pointed at a stock Firefox it
starts and then never answers (tested: the process launches and stalls). So
the "Firefox" VANTA opened was always Playwright's build, branded Firefox
Nightly, and it arrived on Mozilla's Nightly welcome page -- which is what made
it look like a second, wrong browser.

This drives the Firefox that is actually installed, through geckodriver and
Selenium, which is the route Mozilla itself supports. It exposes exactly the
operations browser.py's Playwright worker does -- start, tabs, goto, snapshot,
click, type, press, select, check, upload, fill_form, submit, wait, close,
sync_login -- with the same receipts, so nothing above this file changes.

PROFILE
-------
A dedicated profile, vault/browser_profile_firefox_real, not his own. His
profile is locked while his Firefox is open, and driving it would also put
automation's cookies, history and crashes into the browser he lives in. The
sessions he already has are carried across instead: before launch his
cookies.sqlite is MERGED into this profile (never his saved passwords, which
live in logins.json and key4.db and are not read), keeping whichever copy of a
cookie is newer, so a CAPTCHA he cleared in VANTA's browser is not overwritten
by an older one from his own.
"""

import json
import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import browser as _b

_DEFAULT_PROFILE = _b.BASE_DIR / "vault" / "browser_profile_firefox_real"


def real_profile():
    """VANTA's own Firefox profile. WINTER_BROWSER_PROFILE overrides it, which
    the tests use so they never write into the real one."""
    override = os.getenv("WINTER_BROWSER_PROFILE", "").strip()
    return Path(override) if override else _DEFAULT_PROFILE

_CAPTCHA_HOSTS = ("google.com/recaptcha", "recaptcha.net", "hcaptcha.com",
                  "challenges.cloudflare.com", "turnstile")

_TYPE_WORDS = {
    "password": "password", "passwd": "password", "pass": "password",
    "email": "email", "e-mail": "email", "mail": "email",
    "search": "search", "query": "search", "q": "search",
    "tel": "tel", "phone": "tel", "mobile": "tel",
    "url": "url", "number": "number",
}

_SNAPSHOT_SELECTOR = ("a,button,input,textarea,select,[onclick],[contenteditable=true],"
                      "[role=button],[role=link],[role=checkbox],[role=switch],"
                      "[role=combobox],[role=textbox],[role=spinbutton],"
                      "[role=menuitem],[role=option],[role=tab],[role=treeitem],"
                      "[role=slider]")

# --------------------------------------------------------------------------- #
# JavaScript run inside the page. Kept as data so the logic that decides which
# element a name refers to is one piece of text rather than forty round trips.
# --------------------------------------------------------------------------- #
_PRELUDE = r"""
const visible = el => {
  if (!el || !el.getBoundingClientRect) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none';
};
const typeable = el => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'select') return true;
  if (tag === 'input')
    return !['button','submit','reset','image'].includes((el.getAttribute('type')||'text').toLowerCase());
  return !!el.isContentEditable || ['textbox','combobox','spinbutton'].includes(
    (el.getAttribute('role') || '').toLowerCase());
};
const usable = (list, editable) => {
  for (const el of Array.from(list).slice(0, 12)) {
    if (!visible(el) || el.disabled) continue;
    if (editable && !typeable(el)) continue;
    return el;
  }
  return null;
};
const q = sel => {
  const out = [];
  const visit = root => {
    try {
      out.push(...root.querySelectorAll(sel));
      for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
    } catch (e) {}
  };
  visit(document);
  return out;
};
"""

_FIND_JS = _PRELUDE + r"""
const key = arguments[0], editable = arguments[1];
const wanted = (arguments[2] || {});
const safe = key.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
const low = key.toLowerCase();
const exact = q('[data-vanta-ref="' + safe + '"]');
if (exact.length === 1) return exact[0];
if (/^e[0-9a-f]{12}-\d+$/.test(key)) return null;
const group = (tags, attr, op) => tags.map(t => t + '[' + attr + op + '"' + safe + '" i]').join(',');
let attempts;
if (editable) {
  const tags = ['input','textarea','select','[contenteditable]','[role=textbox]','[role=combobox]'];
  attempts = [group(tags,'name','='), group(tags,'id','='), group(tags,'placeholder','*='),
              group(tags,'aria-label','*='), 'input[autocomplete*="' + safe + '" i]'];
} else {
  const tags = ['button','a','input','[role=button]','[role=link]',
                '[role=menuitem]','[role=option]','[role=tab]','[role=treeitem]',
                '[role=checkbox]','[role=switch]','[role=slider]'];
  attempts = [group(tags,'name','='), group(tags,'id','='), group(tags,'aria-label','*='),
              group(tags,'value','*=')];
}
for (const sel of attempts) { const f = usable(q(sel), editable); if (f) return f; }
// A visible label next to the field, which is how a person identifies it.
const labels = Array.from(q('label')).filter(
  l => (l.innerText || '').toLowerCase().includes(low));
for (const l of labels) {
  let c = l.control || (l.htmlFor && document.getElementById(l.htmlFor)) || l.querySelector('input,textarea,select');
  if (c && usable([c], editable)) return c;
}
if (!editable) {
  const pool = Array.from(q('a,button,[role=button],[role=link],[role=menuitem],[role=option],[role=tab],[role=treeitem],[role=checkbox],[role=switch],[role=slider],input[type=submit],input[type=button],span,div,li,[onclick]'))
    .filter(el => visible(el) && !el.disabled &&
      ((el.innerText || el.value || '').trim().toLowerCase().includes(low)))
    .sort((a, b) => (a.getBoundingClientRect().width * a.getBoundingClientRect().height) -
                    (b.getBoundingClientRect().width * b.getBoundingClientRect().height));
  if (pool.length) return pool[0];
}
const partial = (editable ? ['input','textarea','select','[contenteditable]','[role=textbox]','[role=combobox]'] : ['button','a','[role=button]','[role=link]','[role=menuitem]','[role=option]','[role=tab]','[role=checkbox]','[role=switch]','[role=slider]'])
  .flatMap(t => ['name','id','title'].map(a => t + '[' + a + '*="' + safe + '" i]')).join(',');
let f = usable(q(partial), editable);
if (f) return f;
const typed = wanted[low];
if (editable && typed) {
  f = usable(q('input[type="' + typed + '"]'), true);
  if (f) return f;
  if (typed === 'search') {
    for (const sel of ['input[role="combobox"]','input[type="text"]','textarea','[contenteditable="true"]']) {
      f = usable(q(sel), true); if (f) return f;
    }
  }
}
return null;
"""

_SNAPSHOT_JS = _PRELUDE + _b.STABLE_REFERENCES_JS + r"""
const selector = arguments[0], offset = arguments[1], query = arguments[2] || '';
const terms = String(query).toLowerCase().split(/[^a-z0-9]+/).filter(x => x.length > 1);
const ranked = Array.from(q(selector)).map((el, order) => {
  const r = el.getBoundingClientRect();
  const isFile = el.tagName.toLowerCase() === 'input' &&
    (el.getAttribute('type') || '').toLowerCase() === 'file';
  const isVisible = visible(el);
  if (!isVisible && !isFile) return null;
  const label = el.labels && el.labels.length ? (el.labels[0].innerText || '') : '';
  const haystack = [el.innerText, el.textContent, el.id, el.name,
    el.getAttribute('aria-label'), el.getAttribute('placeholder'),
    el.getAttribute('title'), label, isFile ? 'file upload attachment resume cv' : '']
    .filter(Boolean).join(' ').toLowerCase();
  const score = terms.reduce((n, term) => n + (haystack.includes(term) ? 1 : 0), 0);
  return {el: el, order: order, score: score, visible: isVisible};
}).filter(Boolean);
const candidates = terms.length ? ranked.filter(x => x.score > 0) : ranked;
candidates.sort((a, b) => (b.score - a.score) ||
  (Number(b.visible) - Number(a.visible)) || (a.order - b.order));
return candidates.slice(0, 180).map((entry, i) => {
  const el = entry.el, isVisible = entry.visible;
  const ref = referenceFor(el);
  const tag = el.tagName.toLowerCase();
  let label = '';
  if (el.labels && el.labels.length) label = el.labels[0].innerText || '';
  if (!label && el.id) {
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) label = l.innerText || '';
  }
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
      fallback = driven ? (driven.innerText || driven.value || target[1]).trim() : target[1];
    }
  }
  const name = (el.getAttribute('aria-label') || label || el.getAttribute('placeholder') ||
    el.innerText || el.title || el.name || fallback || '').trim().replace(/\s+/g, ' ').slice(0, 160);
  const out = {ref: ref, tag: tag, role: el.getAttribute('role') || '', name: name};
  for (const attr of ['placeholder','autocomplete','aria-expanded','aria-selected',
                     'aria-checked','contenteditable']) {
    const v = el.getAttribute(attr); if (v !== null) out[attr] = v;
  }
  if (tag === 'a' && el.getAttribute('href')) out.href = el.getAttribute('href').slice(0, 200);
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    out.type = el.type || 'text';
    if (!isVisible) out.hidden = true;
    if (el.type === 'checkbox' || el.type === 'radio') out.checked = !!el.checked;
    else out.value = (el.value || '').slice(0, 80);
    if (el.required) out.required = true;
    if (el.disabled) out.disabled = true;
    if (tag === 'select') out.options = Array.from(el.options).slice(0, 40).map(o => o.label || o.value);
  }
  return out;
});
"""

_SET_VALUE_JS = r"""
const el = arguments[0], value = arguments[1];
el.focus();
const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const setter = Object.getOwnPropertyDescriptor(proto, 'value');
if (setter && setter.set) setter.set.call(el, value); else el.value = value;
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
"""


# --------------------------------------------------------------------------- #
# Finding Firefox and carrying his sessions across
# --------------------------------------------------------------------------- #
def firefox_binary():
    """The installed Firefox, or '' to let Selenium look."""
    override = os.getenv("WINTER_FIREFOX_PATH", "").strip()
    if override and os.path.exists(override):
        return override
    for base in (os.getenv("ProgramFiles", r"C:\Program Files"),
                 os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidate = os.path.join(base, "Mozilla Firefox", "firefox.exe")
        if os.path.exists(candidate):
            return candidate
    return ""


_COOKIE_COLUMNS = ("originAttributes", "name", "value", "host", "path", "expiry",
                   "lastAccessed", "creationTime", "isSecure", "isHttpOnly",
                   "inBrowserElement", "sameSite", "schemeMap")


def merge_cookies(source_db, target_db):
    """Merge `source_db` (a copy of his cookies) into `target_db`.

    New cookies are added; an existing one is replaced only if the source copy
    was used more recently. Returns (added, updated). Pure sqlite, no browser,
    so it can be tested and cannot lock anything.
    """
    target_exists = os.path.exists(target_db)
    if not target_exists:
        os.makedirs(os.path.dirname(target_db), exist_ok=True)
        shutil.copy2(source_db, target_db)
        con = sqlite3.connect(target_db)
        try:
            return con.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()[0], 0
        finally:
            con.close()
    con = sqlite3.connect(target_db)
    try:
        con.execute("ATTACH DATABASE ? AS src", (source_db,))
        have = {row[1] for row in con.execute("PRAGMA main.table_info(moz_cookies)")}
        both = {row[1] for row in con.execute("PRAGMA src.table_info(moz_cookies)")}
        columns = [c for c in _COOKIE_COLUMNS if c in have and c in both]
        names = ", ".join(columns)
        before = con.execute("SELECT COUNT(*) FROM main.moz_cookies").fetchone()[0]
        con.execute(f"INSERT OR IGNORE INTO main.moz_cookies ({names}) "
                    f"SELECT {names} FROM src.moz_cookies")
        added = con.execute("SELECT COUNT(*) FROM main.moz_cookies").fetchone()[0] - before
        updated = 0
        if "lastAccessed" in columns:
            mutable = [c for c in ("value", "expiry", "lastAccessed", "isSecure",
                                   "isHttpOnly", "sameSite") if c in columns]
            assignments = ", ".join(
                f"{c} = (SELECT s.{c} FROM src.moz_cookies s WHERE "
                f"s.name = main.moz_cookies.name AND s.host = main.moz_cookies.host "
                f"AND s.path = main.moz_cookies.path AND "
                f"s.originAttributes = main.moz_cookies.originAttributes)"
                for c in mutable)
            cursor = con.execute(
                f"UPDATE main.moz_cookies SET {assignments} WHERE EXISTS ("
                f"SELECT 1 FROM src.moz_cookies s WHERE s.name = main.moz_cookies.name "
                f"AND s.host = main.moz_cookies.host AND s.path = main.moz_cookies.path "
                f"AND s.originAttributes = main.moz_cookies.originAttributes "
                f"AND s.lastAccessed > main.moz_cookies.lastAccessed)")
            updated = max(0, cursor.rowcount)
        con.commit()
        return added, updated
    finally:
        con.close()


def seed_sessions(profile_path=None):
    """Bring his Firefox sessions into VANTA's profile. Never raises."""
    try:
        import browser_login
        found = browser_login.default_profile()
        if found is None:
            return "no Firefox profile with cookies was found; starting signed out"
        source = Path(found["path"]) / "cookies.sqlite"
        directory, copied = browser_login._snapshot(source)
        try:
            target = str((profile_path or real_profile()) / "cookies.sqlite")
            added, updated = merge_cookies(copied, target)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return (f"carried his sessions across from Firefox profile "
                f"'{Path(found['path']).name}' ({added} new cookies, {updated} "
                f"refreshed). Saved passwords were not read.")
    except Exception as exc:
        return f"could not carry sessions across ({type(exc).__name__}: {exc})"


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
def _frame_kind(url):
    url = (url or "").lower()
    if not url or url == "about:blank":
        return "blank"
    if any(host in url for host in _CAPTCHA_HOSTS):
        return "captcha"
    return "content"


class RealFirefoxWorker:
    def __init__(self):
        self.requests = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="vanta-browser-real")
        self.thread.start()

    def call(self, operation, **kwargs):
        reply = queue.Queue(maxsize=1)
        waiting = _b.call_timeout(operation, kwargs)
        kwargs['_request_deadline'] = time.monotonic() + waiting
        self.requests.put((operation, kwargs, reply))
        try:
            ok, value = _b.wait_for_reply(reply, waiting, kwargs)
        except InterruptedError as exc:
            return '[Interrupted] ' + str(exc)
        except queue.Empty:
            return "[Browser Error] " + _b.timeout_message(operation, waiting)
        return value if ok else f"[Browser Error] {value}"

    # ------------------------------------------------------------------ #
    def _run(self):
        from selenium import webdriver
        from selenium.common.exceptions import (
            ElementClickInterceptedException, ElementNotInteractableException,
            NoAlertPresentException, StaleElementReferenceException,
            TimeoutException, UnexpectedAlertPresentException,
            WebDriverException)
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.support.ui import Select

        state = {"driver": None, "visible": None, "pid": None, "active": None}
        tab_ids = {}
        counter = {"next": 1}
        # Fullscreen belongs to the exact tab selected by YOUTUBE_PLAY, not
        # whichever Firefox tab happens to be last later in the turn.
        media_handle = {"value": None}
        dialog_history = {}
        dialog_policies = {}

        def discard(why=""):
            driver = state["driver"]
            state["driver"] = None
            state["visible"] = None
            state["pid"] = None
            state['active'] = None
            media_handle["value"] = None
            tab_ids.clear()
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            if why:
                print(f"[Browser] Discarded the old session: {why}")

        def usable():
            driver = state["driver"]
            if driver is None:
                return False
            try:
                return bool(driver.window_handles)
            except Exception:
                return False

        def ensure_started(visible=None):
            """Keep an existing browser's visibility unless explicitly set.

            SNAPSHOT and later interaction steps must not turn a background
            session into a new visible, blank Firefox process.  START/GOTO
            choose visibility; subsequent operations inherit it.
            """
            if usable():
                if visible is None or state["visible"] == bool(visible):
                    return
                discard("switching browser visibility")
            if state["driver"] is not None:
                discard("the browser window is gone")
            if visible is None:
                visible = True
            real_profile().mkdir(parents=True, exist_ok=True)
            _b.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            if _b_login_enabled():
                print(f"[Browser] Sessions: {seed_sessions()}")
            options = Options()
            # Leave native JavaScript dialogs pending until BROWSER_DIALOG
            # resolves them. Firefox/geckodriver otherwise auto-dismisses an
            # alert as soon as the next WebDriver command inspects the tab;
            # the action then reports "armed" even though the dialog the user
            # meant has already vanished.
            try:
                options.set_capability("unhandledPromptBehavior", "ignore")
            except Exception:
                pass
            binary = firefox_binary()
            if binary:
                options.binary_location = binary
            if not visible:
                options.add_argument("-headless")
            options.add_argument("-profile")
            options.add_argument(str(real_profile()))
            options.page_load_strategy = "eager"
            for name, value in {
                "media.autoplay.default": 0,
                "media.autoplay.blocking_policy": 0,
                "browser.download.folderList": 2,
                "browser.download.dir": str(_b.DOWNLOAD_DIR),
                "browser.download.useDownloadDir": True,
                "browser.helperApps.neverAsk.saveToDisk":
                    "application/pdf,application/octet-stream,application/zip,"
                    "text/csv,application/vnd.ms-excel,"
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "pdfjs.disabled": True,
                "browser.startup.homepage": "about:blank",
                "browser.startup.page": 0,
                "browser.aboutwelcome.enabled": False,
                "browser.shell.checkDefaultBrowser": False,
                "datareporting.policy.dataSubmissionEnabled": False,
                "browser.tabs.warnOnClose": False,
            }.items():
                options.set_preference(name, value)
            try:
                driver = webdriver.Firefox(options=options)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not start Firefox through geckodriver: {exc}. "
                    f"Close any Firefox window using the VANTA profile and try "
                    f"again.") from exc
            driver.set_page_load_timeout(_b.DEFAULT_TIMEOUT_MS / 1000.0)
            driver.set_script_timeout(20)
            try:
                driver.set_window_size(1440, 900)
            except Exception:
                pass
            state["driver"] = driver
            state["visible"] = bool(visible)
            try:
                state["pid"] = int(
                    (driver.capabilities or {}).get("moz:processID") or 0)
            except (TypeError, ValueError):
                state["pid"] = None

        def drv():
            return state["driver"]

        def tab_id(handle):
            if handle not in tab_ids:
                tab_ids[handle] = f"t{counter['next']}"
                counter["next"] += 1
            return tab_ids[handle]

        def get_tab(requested=None, visible=None):
            ensure_started(visible)
            driver = drv()
            handles = driver.window_handles
            if requested:
                for handle in handles:
                    if tab_id(handle) == requested:
                        state['active'] = handle
                        driver.switch_to.window(handle)
                        try:
                            driver.switch_to.default_content()
                        except UnexpectedAlertPresentException:
                            # BROWSER_DIALOG must still be able to reach a tab
                            # whose JavaScript dialog is blocking its document.
                            pass
                        return handle
                raise ValueError(f"Unknown tab id: {requested}")
            handle = state.get('active')
            if handle not in handles:
                handle = handles[-1]
            state['active'] = handle
            driver.switch_to.window(handle)
            try:
                driver.switch_to.default_content()
            except UnexpectedAlertPresentException:
                pass
            return handle

        def adopt_popup(before_handles):
            opened = [h for h in drv().window_handles if h not in before_handles]
            rows = []
            original = drv().current_window_handle
            for handle in opened:
                drv().switch_to.window(handle)
                drv().switch_to.default_content()
                wait_loaded(3)
                rows.append({'tab': tab_id(handle), 'url': drv().current_url})
            target = opened[0] if len(opened) == 1 else original
            drv().switch_to.window(target)
            drv().switch_to.default_content()
            state['active'] = target
            return rows

        def media_target(requested=None):
            """Return the exact tab owned by the active YouTube playback."""
            if requested:
                handle = get_tab(requested)
                media_handle["value"] = handle
                return handle
            current = media_handle.get("value")
            if current and current in (drv().window_handles if usable() else []):
                return get_tab(tab_id(current))
            ensure_started()
            candidates = []
            for handle in drv().window_handles:
                try:
                    drv().switch_to.window(handle)
                    drv().switch_to.default_content()
                    host = (urlparse(drv().current_url).hostname or "").lower()
                    videos = drv().find_elements(
                        By.CSS_SELECTOR, "video.html5-main-video")
                    if host in {"youtube.com", "www.youtube.com",
                                "m.youtube.com"} and videos:
                        candidates.append(handle)
                except Exception:
                    continue
            if len(candidates) != 1:
                raise ValueError(
                    "No unique VANTA-owned YouTube playback tab exists; run "
                    "YOUTUBE_PLAY first or provide its tab id")
            media_handle["value"] = candidates[0]
            return get_tab(tab_id(candidates[0]))

        def present_tab(maximize=False):
            """Restore and focus this exact WebDriver-owned Firefox window."""
            if state.get("visible") is not True:
                return {"presented": False,
                        "presentation_error":
                            "the managed browser session is headless"}
            try:
                # WebDriver restoring/maximising first also makes an HWND
                # available if Firefox created it initially minimised.
                if maximize:
                    drv().maximize_window()
                drv().execute_script("window.focus();")
            except Exception:
                pass
            try:
                import browser_window
                return browser_window.present_owned_browser(
                    real_profile(), title_hint=drv().title,
                    hinted_pid=state.get("pid"), maximize=maximize)
            except Exception as exc:
                return {"presented": False,
                        "presentation_error": str(exc)[:300]}

        def pending_dialog():
            """Return the current native JavaScript dialog without closing it."""
            try:
                alert = drv().switch_to.alert
                return alert, {
                    # Selenium's W3C alert endpoint exposes the message but not
                    # whether it came from alert/confirm/prompt.
                    "type": "javascript_dialog",
                    "message": str(alert.text or "")[:1000],
                }
            except NoAlertPresentException:
                return None, None

        def resolve_dialog(mode, text=""):
            alert, info = pending_dialog()
            if alert is None:
                return None
            chosen = _b.normalize_dialog_mode(mode)
            if chosen == "status":
                return info
            if chosen == "accept":
                if text:
                    try:
                        alert.send_keys(str(text))
                    except Exception:
                        # Not every JavaScript dialog is a prompt.
                        pass
                alert.accept()
            else:
                alert.dismiss()
            info.update({"handled": chosen, "explicit": True})
            handle = drv().current_window_handle
            history = dialog_history.setdefault(handle, [])
            history.append(info)
            del history[:-10]
            return info

        def interaction_dialog(handle):
            """Resolve an armed dialog, or leave an unarmed one observable."""
            alert, info = pending_dialog()
            if alert is None:
                return None, False
            policy = dialog_policies.pop(handle, None)
            if policy and policy.get("expires", 0) >= time.monotonic():
                return resolve_dialog(policy["mode"], policy.get("text", "")), True
            # A pending Selenium alert can safely remain open: unlike the
            # Playwright event API, returning to the agent does not deadlock
            # the worker. The next BROWSER_DIALOG action can decide it.
            return info, False

        def top_info():
            driver = drv()
            driver.switch_to.default_content()
            return driver.current_window_handle, driver.current_url, driver.title

        def receipt(operation, with_page=True, **extra):
            data = {"ok": True, "operation": operation}
            if with_page:
                handle, url, title = top_info()
                data.update({"tab": tab_id(handle), "url": url, "title": title})
            data.update(extra)
            data = {k: v for k, v in data.items() if v is not None}
            return "[Browser Receipt] " + json.dumps(data, ensure_ascii=False)

        # ---------------------------------------------------------- frames
        def enter(path):
            driver = drv()
            driver.switch_to.default_content()
            for index in path:
                driver.switch_to.frame(index)

        def scopes():
            """Every frame worth looking at: [{'path','url','kind'}], main first."""
            driver = drv()
            found = []

            def walk(path, depth):
                enter(path)
                try:
                    url = driver.execute_script("return location.href") or ""
                except Exception:
                    url = ""
                kind = "main" if not path else _frame_kind(url)
                found.append({"path": path, "url": url, "kind": kind})
                if depth >= 3 or kind == "captcha":
                    return
                enter(path)
                count = len(driver.find_elements(By.CSS_SELECTOR, "iframe,frame"))
                for index in range(count):
                    try:
                        walk(path + (index,), depth + 1)
                    except Exception:
                        continue

            walk((), 0)
            return found

        def content_scopes():
            usable_scopes = [s for s in scopes() if s["kind"] in ("main", "content")]
            return usable_scopes or [{"path": (), "url": "", "kind": "main"}]

        # ---------------------------------------------------------- finding
        def find_target(key, editable=True, settle_ms=1200):
            """The element a person means by `key`, searching every frame.

            One bounded retry, because a frame's own document can still be
            loading when the action arrives, and "no such field" is then true
            but misleading.
            """
            key = str(key or "").strip()
            if not key:
                return None
            if key.startswith(('css=', 'xpath=')):
                matches = []
                kind, selector = key.split('=', 1)
                for scope in content_scopes():
                    enter(scope['path'])
                    elements = (drv().execute_script(_PRELUDE + 'return q(arguments[0]);', selector)
                                if kind == 'css' else drv().find_elements(By.XPATH, selector))
                    matches.extend((scope['path'], el) for el in elements
                                   if el.is_displayed() and el.is_enabled())
                if len(matches) != 1:
                    raise ValueError('The explicit selector must match one visible enabled control; narrow it before acting')
                enter(matches[0][0])
                return matches[0][1]
            for attempt in (0, 1):
                for scope in content_scopes():
                    try:
                        enter(scope["path"])
                        found = drv().execute_script(
                            _FIND_JS, key, bool(editable), _TYPE_WORDS)
                    except Exception:
                        continue
                    if found is not None:
                        return found       # the driver is left inside its frame
                if attempt == 0 and settle_ms:
                    time.sleep(settle_ms / 1000.0)
            return None

        def all_file_inputs():
            found = []
            for scope in content_scopes():
                try:
                    enter(scope["path"])
                    elements = drv().execute_script(
                        _PRELUDE + "return q('input[type=\"file\"]');") or []
                    found.extend((scope["path"], element)
                                 for element in elements)
                except Exception:
                    continue
            return found

        def file_identity(element):
            try:
                return drv().execute_script("""
                  const el = arguments[0];
                  let label = '';
                  if (el.labels && el.labels.length) label = el.labels[0].innerText || '';
                  return [el.getAttribute('data-vanta-ref'), el.id, el.name,
                          el.getAttribute('aria-label'), el.getAttribute('title'),
                          el.getAttribute('accept'), label,
                          'file upload attachment']
                    .filter(Boolean).join(' ').toLowerCase();
                """, element) or ""
            except Exception:
                return ""

        def find_upload_target(key):
            key = str(key or "").strip()
            terms = [word for word in __import__('re').findall(
                r"[a-z0-9]+", key.casefold()) if len(word) > 1]
            files = all_file_inputs()
            if key:
                for path, element in files:
                    enter(path)
                    if element.get_attribute("data-vanta-ref") == key:
                        return element, None, "snapshot_ref"

            if __import__('re').fullmatch(r'e[0-9a-f]{12}-\d+', key):
                trigger = find_target(key, editable=False)
                if trigger is None:
                    return None, None, ''
                try:
                    owned = drv().execute_script(
                        "const el = arguments[0]; const root = el.closest('form,section,[role=dialog]'); "
                        "const files = root ? root.querySelectorAll('input[type=file]') : []; "
                        "return files.length === 1 ? files[0] : null;", trigger)
                    return owned, trigger, 'snapshot_trigger'
                except Exception:
                    return None, trigger, 'snapshot_trigger'

            scored = []
            for path, element in files:
                enter(path)
                identity = file_identity(element)
                score = sum(1 for term in terms if term in identity)
                if score:
                    scored.append((score, path, element))
            scored.sort(key=lambda row: row[0], reverse=True)
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                enter(scored[0][1])
                return scored[0][2], None, "semantic_file_input"

            trigger = find_target(key, editable=False) if key else None
            if trigger is not None:
                # ``find_target`` may resolve the requested field directly to
                # the native file input.  A file input is already the target;
                # clicking it asks Firefox to open an OS chooser and Gecko
                # deliberately rejects that WebDriver click.  Feed the path
                # to the element with ``send_keys`` instead.
                try:
                    if (str(trigger.tag_name or "").casefold() == "input"
                            and str(trigger.get_attribute("type") or "").casefold()
                            == "file"):
                        return trigger, None, "direct_file_input"
                except Exception:
                    pass
                try:
                    owned = drv().execute_script("""
                      const trigger = arguments[0];
                      for (const selector of ['form','[role=dialog]','fieldset','section','div','tr']) {
                        const region = trigger.closest(selector);
                        if (!region) continue;
                        const files = region.querySelectorAll('input[type=file]');
                        if (files.length === 1) return files[0];
                      }
                      return null;
                    """, trigger)
                    if owned is not None:
                        return owned, trigger, "trigger_region"
                except Exception:
                    pass

            files = all_file_inputs()
            if len(files) == 1:
                enter(files[0][0])
                return files[0][1], trigger, "only_file_input"
            return None, trigger, ""

        def selected_file_names(element):
            if element is None:
                return []
            try:
                return drv().execute_script(
                    "return Array.from(arguments[0].files || []).map(f => f.name);",
                    element) or []
            except Exception:
                return []

        def upload_file(key, raw_path):
            _b.check_deadline(args)
            path = Path(str(raw_path or "")).expanduser()
            if not path.is_absolute():
                path = (_b.BASE_DIR / path).resolve()
            if not path.is_file():
                return {"ok": False, "verified": False,
                        "error": f"Local upload file does not exist: {path}",
                        "file": path.name or str(raw_path or "")}
            element, trigger, resolved_by = find_upload_target(key)
            if element is None and trigger is not None:
                click_element(trigger)
                time.sleep(0.25)
                element, _, after_click = find_upload_target(key)
                resolved_by = after_click or resolved_by
            if element is None:
                return {"ok": False, "verified": False,
                        "error": (f"Could not resolve upload control {key!r}; "
                                  "use a targeted BROWSER_SNAPSHOT query to "
                                  "inspect the form."),
                        "file": path.name}
            original_style = element.get_attribute("style")
            try:
                if not element.is_displayed():
                    drv().execute_script("""
                      const el = arguments[0];
                      el.style.display = 'block';
                      el.style.visibility = 'visible';
                      el.style.opacity = '0.01';
                      el.style.width = '1px';
                      el.style.height = '1px';
                      el.style.position = 'fixed';
                      el.style.left = '0';
                      el.style.top = '0';
                    """, element)
                _b.check_deadline(args)
                element.send_keys(str(path))
            finally:
                try:
                    drv().execute_script(
                        "arguments[0].setAttribute('style', arguments[1]);",
                        element, original_style or "")
                except Exception:
                    pass
            observed = selected_file_names(element)
            verified = path.name in observed
            return {"ok": verified, "verified": verified,
                    "file": path.name, "bytes": path.stat().st_size,
                    "observed_files": observed,
                    "resolved_by": resolved_by,
                    "error": None if verified else
                    "The browser did not report the selected file after upload."}

        def ref_state():
            try:
                total = 0
                for scope in content_scopes():
                    enter(scope["path"])
                    total += len(drv().find_elements(By.CSS_SELECTOR, "[data-vanta-ref]"))
            except Exception:
                return "no snapshot has been taken for this page, so no refs exist yet"
            if not total:
                return ("no snapshot has been taken for this page, so no refs "
                        "exist yet; refs are created by BROWSER_SNAPSHOT")
            return (f"{total} refs exist from the last snapshot of this page; "
                    "if the page changed since, they are stale")

        def wait_loaded(seconds):
            end = time.time() + seconds
            while time.time() < end:
                try:
                    if drv().execute_script("return document.readyState") in ("interactive", "complete"):
                        return
                except Exception:
                    pass
                time.sleep(0.1)

        def interaction_may_navigate(element):
            if element is None:
                return False
            try:
                tag = (element.tag_name or "").lower()
                kind = (element.get_attribute("type") or "").lower()
                role = (element.get_attribute("role") or "").lower()
                handler = element.get_attribute("onclick") or ""
                href = element.get_attribute("href") or ""
                return bool(tag in ("a", "button")
                            or kind in ("submit", "button")
                            or role in ("button", "link") or handler or href)
            except Exception:
                return True

        def settle_after(before_url, may_navigate=False):
            probe = (_b.NAVIGATION_SETTLE_MS if may_navigate
                     else _b.SHORT_SETTLE_MS)
            waited = 0
            moved = False
            while waited < probe:
                time.sleep(0.1)
                waited += 100
                try:
                    if drv().current_url != before_url:
                        moved = True
                        break
                except Exception:
                    break
            if moved:
                wait_loaded(_b.LOAD_SETTLE_MS / 1000.0)
            return moved

        def click_element(element):
            _b.check_deadline(args)
            try:
                drv().execute_script(
                    "arguments[0].scrollIntoView({block:'center',inline:'center'});", element)
            except Exception:
                pass
            baseline = drv().execute_script('return (' + _b.CLICK_OBSERVER_JS + ')(arguments[0]);', element)
            try:
                element.click()
            except (ElementClickInterceptedException, ElementNotInteractableException,
                    StaleElementReferenceException):
                # An overlay, a zero-size wrapper, a handler on a span. A
                # script click is what a person clicking the visible control
                # would have caused.
                count = drv().execute_script('return (' + _b.CLICK_COUNT_JS + ')(arguments[0]);', element)
                if count is None:
                    raise RuntimeError('Click target changed; snapshot before retrying')
                if count == baseline:
                    drv().execute_script("arguments[0].click();", element)

        def fill_element(element, text):
            _b.check_deadline(args)
            text = _b.normalize_control_value(
                text,
                control_type=element.get_attribute("type") or "",
                placeholder=element.get_attribute("placeholder") or "",
                name=element.get_attribute("name") or "",
                label=element.get_attribute("aria-label") or "",
            )
            tag = (element.tag_name or "").lower()
            if tag == "select":
                raise ValueError("this is a dropdown; use BROWSER_SELECT")
            editable = str(element.get_attribute("contenteditable") or "").lower() == "true"
            try:
                element.clear()
                element.send_keys(text)
            except Exception:
                click_element(element)
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.BACKSPACE)
                element.send_keys(text)
            current = ((element.text or "").strip() if editable
                       else str(element.get_attribute("value") or ""))
            if current != text and not editable:
                try:
                    drv().execute_script(_SET_VALUE_JS, element, text)
                except Exception:
                    pass
                current = str(element.get_attribute("value") or "")
            if current != text:
                raise ValueError("the page rejected the entered value")
            return text

        def select_option(element, wanted):
            _b.check_deadline(args)
            wanted = str(wanted)
            if (element.tag_name or "").lower() != "select":
                click_element(element)
                chosen = None
                low = wanted.casefold()
                for scope in content_scopes():
                    enter(scope["path"])
                    candidates = drv().find_elements(
                        By.CSS_SELECTOR,
                        '[role="option"],[role="menuitem"],[role="radio"],li')
                    for option in candidates:
                        try:
                            if option.is_displayed() and low == (option.text or "").strip().casefold():
                                chosen = option
                                break
                        except Exception:
                            continue
                    if chosen is not None:
                        break
                if chosen is None:
                    raise ValueError(f"no visible option called {wanted!r}")
                click_element(chosen)
                actual = str(element.get_attribute('value') or element.text or '').strip().casefold()
                selected = False
                if actual != low:
                    try:
                        selected = ((chosen.get_attribute('aria-selected') or '').lower() == 'true'
                                    or (chosen.get_attribute('aria-checked') or '').lower() == 'true')
                    except Exception:
                        pass
                if actual != low and not selected:
                    raise ValueError('the custom dropdown did not confirm its selection')
                return
            chooser = Select(element)
            try:
                chooser.select_by_visible_text(wanted)
            except Exception:
                try:
                    chooser.select_by_value(wanted)
                except Exception:
                    lowered = str(wanted).strip().lower()
                    for option in chooser.options:
                        if option.text.strip().lower() == lowered:
                            option.click()
                            break
                    else:
                        raise
            selected = chooser.first_selected_option
            if (selected.text.strip().casefold() != wanted.strip().casefold()
                    and str(selected.get_attribute('value') or '') != wanted):
                raise ValueError('the dropdown did not retain that option')

        def set_checked(element, wanted):
            _b.check_deadline(args)
            native = ((element.tag_name or "").lower() == "input" and
                      (element.get_attribute("type") or "").lower() in {"checkbox", "radio"})
            current = (bool(element.is_selected()) if native else
                       (element.get_attribute("aria-checked") or "false").lower() == "true")
            if current != bool(wanted):
                click_element(element)
            actual = (bool(element.is_selected()) if native else
                      (element.get_attribute("aria-checked") or "false").lower() == "true")
            if actual != bool(wanted):
                raise ValueError("the control did not retain its checked state")

        def page_signature():
            enter(())
            try:
                return drv().execute_script("""
                  const body = document.body;
                  const text = body ? (body.innerText || '').slice(0, 4000) : '';
                  const controls = document.querySelectorAll(
                    'input,select,textarea,button,[role],[contenteditable=true]').length;
                  return [location.href, document.title, text, controls].join('\u241f');
                """)
            except Exception:
                return f"{drv().current_url}\u241f{drv().title}"

        KEYS = {"enter": Keys.ENTER, "return": Keys.RETURN, "tab": Keys.TAB,
                "escape": Keys.ESCAPE, "esc": Keys.ESCAPE, "space": Keys.SPACE,
                "backspace": Keys.BACKSPACE, "delete": Keys.DELETE,
                "arrowdown": Keys.ARROW_DOWN, "arrowup": Keys.ARROW_UP,
                "arrowleft": Keys.ARROW_LEFT, "arrowright": Keys.ARROW_RIGHT,
                "down": Keys.ARROW_DOWN, "up": Keys.ARROW_UP,
                "pagedown": Keys.PAGE_DOWN, "pageup": Keys.PAGE_UP,
                "home": Keys.HOME, "end": Keys.END}

        def press_key(element, key):
            name = str(key or "Enter").strip()
            parts = [KEYS.get(p.strip().lower(), p.strip()) for p in name.split("+")]
            if len(parts) > 1:
                element.send_keys(*(parts[:-1] + [parts[-1]]))
            else:
                element.send_keys(parts[0])

        # ------------------------------------------------------------ loop
        while True:
            operation, args, reply = self.requests.get()
            try:
                _b.check_deadline(args)
                if operation == "start":
                    ensure_started(args.get("visible", True))
                    get_tab(visible=args.get("visible", True))
                    presentation = (present_tab()
                                    if args.get("visible", True) else
                                    {"presented": False,
                                     "background": True})
                    value = receipt("start", **presentation)
                elif operation == "sync_login":
                    ensure_started(args.get("visible", True))
                    value = sync_login_live(drv(), args.get("domains"), get_tab,
                                            tab_id, WebDriverException)
                elif operation == "tabs":
                    ensure_started()
                    driver = drv()
                    original = driver.current_window_handle
                    listing = []
                    for handle in driver.window_handles:
                        driver.switch_to.window(handle)
                        driver.switch_to.default_content()
                        listing.append({"tab": tab_id(handle),
                                        "title": driver.title,
                                        "url": driver.current_url,
                                        "media_owner": (
                                            handle == media_handle.get("value")
                                            or None)})
                    driver.switch_to.window(original)
                    driver.switch_to.default_content()
                    value = "[Browser Tabs] " + json.dumps(listing, ensure_ascii=False)
                elif operation == "goto":
                    get_tab(args.get("tab"), args.get("visible", True))
                    url = _b._validate_url(args["url"])
                    try:
                        drv().get(url)
                    except TimeoutException:
                        # The page is slow, not absent: stop it loading and
                        # report where it got to.
                        try:
                            drv().execute_script("window.stop();")
                        except Exception:
                            pass
                    presentation = (present_tab()
                                    if args.get("visible", True) else
                                    {"presented": False,
                                     "background": True})
                    value = receipt(
                        "goto",
                        ok=(bool(presentation.get("presented"))
                            if args.get("visible", True) else True),
                        **presentation)
                elif operation == "dialog":
                    handle = get_tab(args.get("tab"))
                    mode = _b.normalize_dialog_mode(args.get("mode"))
                    current = resolve_dialog(mode, args.get("text", ""))
                    if current is not None:
                        value = receipt(
                            "dialog", with_page=False, tab=tab_id(handle),
                            ok=True, dialog=current)
                    elif mode == "status":
                        recent = list(dialog_history.get(handle, [])[-5:])
                        value = receipt(
                            "dialog", with_page=False, tab=tab_id(handle),
                            ok=bool(recent), dialogs=recent,
                            note=None if recent else
                            "No JavaScript dialog is open or recently handled on this tab.")
                    else:
                        dialog_policies[handle] = {
                            "mode": mode,
                            "text": str(args.get("text") or ""),
                            "expires": time.monotonic() + 30.0,
                        }
                        value = receipt(
                            "dialog", with_page=False, tab=tab_id(handle),
                            armed_for_next_dialog=mode, expires_in_seconds=30,
                            note="Run the click or submit that opens the dialog next.")
                elif operation == "snapshot":
                    handle = get_tab(args.get("tab"))
                    query = str(args.get("query") or "").strip()
                    _alert, open_dialog = pending_dialog()
                    if open_dialog is not None:
                        payload = {
                            "tab": tab_id(handle), "browser_dialog": open_dialog,
                            "note": "The page is blocked by a JavaScript dialog. "
                                    "Use BROWSER_DIALOG with mode accept or dismiss."
                        }
                        value = "[Browser Snapshot] " + json.dumps(
                            payload, ensure_ascii=False)
                        reply.put((True, value))
                        continue

                    def scan():
                        found, blocked, offset = [], [], 0
                        for scope in scopes():
                            kind = scope["kind"]
                            if kind == "blank":
                                continue
                            if kind == "captcha":
                                blocked.append({
                                    "kind": "captcha", "url": scope["url"][:120],
                                    "note": "a human must complete this; VANTA "
                                            "cannot and should not click it"})
                                continue
                            try:
                                enter(scope["path"])
                                items = drv().execute_script(
                                    _SNAPSHOT_JS, _SNAPSHOT_SELECTOR, offset,
                                    query) or []
                            except Exception:
                                continue
                            if scope["path"]:
                                for item in items:
                                    item["frame"] = scope["url"][:100]
                            found.extend(items)
                            offset += len(items)
                        return found, blocked

                    refs, blockers = scan()
                    if not refs:
                        time.sleep(2.5)
                        refs, blockers = scan()
                    focused_ref = ""
                    if query:
                        for item in refs:
                            if item.get("hidden"):
                                continue
                            candidate = str(item.get("ref") or "")
                            if not candidate:
                                continue
                            element = find_target(candidate, editable=False,
                                                  settle_ms=0)
                            if element is None:
                                continue
                            try:
                                drv().execute_script(
                                    "arguments[0].scrollIntoView({block:'center', "
                                    "inline:'nearest'});", element)
                                focused_ref = candidate
                            except Exception:
                                pass
                            break
                    enter(())
                    try:
                        text = (drv().execute_script(
                            "return document.body ? document.body.innerText : ''") or "")[:12000]
                    except Exception:
                        text = ""
                    handle, url, title = top_info()
                    payload = {"tab": tab_id(handle), "url": url, "title": title,
                               "elements": refs, "text": text}
                    if query:
                        payload["query"] = query
                    if focused_ref:
                        payload["focused_ref"] = focused_ref
                    try:
                        overlays = drv().execute_script(
                            "return (" + _b.HTML_OVERLAY_SCRIPT + ")();") or []
                    except Exception:
                        overlays = []
                    if overlays:
                        payload["overlays"] = overlays
                    recent_dialogs = list(dialog_history.get(handle, [])[-5:])
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
                    value = "[Browser Snapshot] " + json.dumps(payload, ensure_ascii=False)
                elif operation in {"click", "type", "press"}:
                    handle = get_tab(args.get("tab"))
                    before_handles = list(drv().window_handles)
                    ref = args.get("ref", "")
                    element = find_target(ref, editable=operation != "click")
                    if element is None:
                        raise ValueError(
                            f"Could not find {ref!r} on this page, by ref or by "
                            f"name, label, placeholder or type; " + ref_state())
                    # current_url is the top-level page's, whichever frame the
                    # element is in, so the driver stays inside that frame.
                    before_url = drv().current_url
                    may_navigate = (interaction_may_navigate(element)
                                    if operation == "click" else False)
                    if operation == "click":
                        pre = element
                        click_element(element)
                    elif operation == "type":
                        pre = None
                        fill_element(element, args.get("text", ""))
                    else:
                        pre = None
                        press_key(element, args.get("key", "Enter"))
                    native_dialog, resolved = interaction_dialog(handle)
                    if native_dialog is not None and not resolved:
                        value = receipt(
                            operation, with_page=False, tab=tab_id(handle),
                            ok=False, verified=False,
                            ref=ref,
                            clicked=True if operation == "click" else None,
                            written=True if operation == "type" else None,
                            browser_dialog=native_dialog,
                            requires_action="BROWSER_DIALOG")
                        reply.put((True, value))
                        continue
                    navigated = settle_after(before_url, may_navigate)
                    opened_tabs = adopt_popup(before_handles)
                    value = receipt(operation, ref=ref,
                                    source_tab=tab_id(handle) if opened_tabs else None,
                                    opened_tabs=opened_tabs or None,
                                    clicked=True if operation == "click" else None,
                                    written=True if operation == "type" else None,
                                    verified=True if operation == "type" else None,
                                    navigated=navigated or None,
                                    came_from=before_url if navigated else None,
                                    dialog=native_dialog if resolved else None)
                elif operation in {"select", "check", "upload"}:
                    get_tab(args.get("tab"))
                    ref = args.get("ref", "")
                    if operation == "upload":
                        details = upload_file(ref, args.get("path", ""))
                        value = receipt("upload", ref=ref, **details)
                        reply.put((True, value))
                        continue
                    element = find_target(ref, editable=operation != "check")
                    if element is None:
                        raise ValueError(
                            f"Could not find {ref!r} on this page, by ref or by "
                            f"name, label, placeholder or type; " + ref_state())
                    if operation == "select":
                        select_option(element, args.get("value", ""))
                    elif operation == "check":
                        set_checked(element, args.get("checked", True))
                    time.sleep(0.2)
                    value = receipt(operation, ref=ref, verified=True)
                elif operation == "fill_form":
                    get_tab(args.get("tab"))
                    filled, verified, failed = [], [], []
                    for ref, entry in (args.get("fields") or {}).items():
                        _b.check_deadline(args)
                        try:
                            kind = str(entry.get("kind", "fill"))
                            text = entry.get("value", "")
                            if kind == "upload":
                                details = upload_file(ref, text)
                                if not details.get("verified"):
                                    raise ValueError(details.get("error") or
                                                     "file selection was not verified")
                                filled.append(ref)
                                verified.append(ref)
                                continue
                            element = find_target(ref, editable=True)
                            if element is None:
                                raise ValueError("no field by that ref, name, "
                                                 "label, placeholder or type")
                            control = (element.get_attribute("type") or "").lower()
                            if control == "radio":
                                group = element.get_attribute("name") or ref
                                options = drv().find_elements(
                                    By.CSS_SELECTOR,
                                    f'input[type="radio"][name="{group}"]')
                                chosen = None
                                for option in options:
                                    if str(option.get_attribute("value") or "").lower() == str(text).lower():
                                        chosen = option
                                        break
                                if chosen is None:
                                    raise ValueError(f"no {group!r} option called {text!r}")
                                set_checked(chosen, True)
                            elif kind == "select":
                                select_option(element, str(text))
                            elif kind == "check":
                                set_checked(element, str(text).lower() not in {"false", "0", "no"})
                            else:
                                fill_element(element, text)
                            filled.append(ref)
                            verified.append(ref)
                        except Exception as field_error:
                            failed.append(f"{ref}: {field_error}")
                    time.sleep(0.25)
                    value = receipt("fill_form", ok=bool(filled) and not failed,
                                    filled=filled, verified=verified, failed=failed,
                                    refs=ref_state() if failed else None)
                elif operation == "submit":
                    handle = get_tab(args.get("tab"))
                    before_handles = list(drv().window_handles)
                    ref = args.get("ref", "")
                    before = drv().current_url
                    before_signature = page_signature()
                    if ref:
                        element = find_target(ref, editable=False)
                        if element is None:
                            raise ValueError(
                                f"Could not find {ref!r} to submit; " + ref_state())
                        invalid = drv().execute_script('return (' + _b.VALIDATION_JS + ')(arguments[0]);', element)
                        if invalid:
                            value = receipt('submit', ok=False, submitted=False,
                                            validation_errors=invalid)
                            reply.put((True, value))
                            continue
                        click_element(element)
                    else:
                        invalid = drv().execute_script('return (' + _b.VALIDATION_JS + ')(document.activeElement);')
                        if invalid:
                            value = receipt('submit', ok=False, submitted=False,
                                            validation_errors=invalid)
                            reply.put((True, value))
                            continue
                        drv().switch_to.active_element.send_keys(Keys.ENTER)
                    native_dialog, resolved = interaction_dialog(handle)
                    if native_dialog is not None and not resolved:
                        value = receipt(
                            "submit", with_page=False, tab=tab_id(handle),
                            ok=False, browser_dialog=native_dialog,
                            requires_action="BROWSER_DIALOG")
                        reply.put((True, value))
                        continue
                    time.sleep(0.6)
                    wait_loaded(8.0)
                    enter(())
                    navigated = drv().current_url != before
                    changed = navigated or page_signature() != before_signature
                    opened_tabs = adopt_popup(before_handles)
                    changed = changed or len(opened_tabs) == 1
                    invalid = drv().execute_script('return (' + _b.VALIDATION_JS + ')(null, true);')
                    changed = changed and not invalid
                    value = receipt("submit", ok=changed, navigated=navigated,
                                    validation_errors=invalid or None,
                                    source_tab=tab_id(handle) if opened_tabs else None,
                                    opened_tabs=opened_tabs or None,
                                    changed=changed,
                                    was=before,
                                    dialog=native_dialog if resolved else None,
                                    verify=None if changed else
                                    "The URL and visible application state did not change; snapshot before retrying")
                elif operation == "wait":
                    get_tab(args.get("tab"))
                    if args.get("text"):
                        try:
                            budget = float(args.get("ms") or 10_000)
                        except (TypeError, ValueError):
                            budget = 10_000.0
                        budget = max(500.0, min(budget, 60_000.0)) / 1000.0
                        needle = str(args["text"]).lower()
                        end = time.time() + budget
                        seen = False
                        while time.time() < end and not seen:
                            _b.check_deadline(args)
                            for scope in content_scopes():
                                try:
                                    enter(scope["path"])
                                    seen = bool(drv().execute_script(
                                        "return !!document.body && document.body."
                                        "innerText.toLowerCase().includes(arguments[0])",
                                        needle))
                                except Exception:
                                    seen = False
                                if seen:
                                    break
                            if not seen:
                                time.sleep(0.25)
                        if not seen:
                            raise TimeoutError(
                                f"The text {args['text'][:60]!r} did not appear "
                                f"within {int(budget)}s")
                        value = receipt("wait", found=str(args["text"])[:80])
                    else:
                        deadline = time.monotonic() + max(0, min(int(args.get('ms', 1000)), 10_000)) / 1000
                        while time.monotonic() < deadline:
                            _b.check_deadline(args)
                            time.sleep(0.1)
                        value = receipt("wait")
                elif operation == "youtube_play":
                    # Playback is a visible user surface. If a background
                    # portal check established headless Firefox, rebuild it
                    # visibly instead of playing into an invisible session.
                    handle = get_tab(args.get("tab"), visible=True)
                    url = _b._validate_url(args["url"])
                    host = (urlparse(url).hostname or "").lower()
                    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                        raise ValueError("YOUTUBE_PLAY only accepts a youtube.com video URL")
                    try:
                        drv().get(url)
                    except TimeoutException:
                        try:
                            drv().execute_script("window.stop();")
                        except Exception:
                            pass
                    media_handle["value"] = handle
                    end = time.time() + 15.0
                    video = None
                    while time.time() < end and video is None:
                        found = drv().find_elements(By.CSS_SELECTOR,
                                                    "video.html5-main-video")
                        video = found[0] if found else None
                        if video is None:
                            time.sleep(0.2)
                    if video is None:
                        raise ValueError("YouTube loaded without a video player")
                    paused = bool(drv().execute_script(
                        "return Boolean(arguments[0].paused);", video))
                    methods = []
                    if paused:
                        buttons = drv().find_elements(
                            By.CSS_SELECTOR, "button.ytp-play-button")
                        if buttons:
                            try:
                                click_element(buttons[0])
                                methods.append("player-button")
                            except Exception:
                                try:
                                    drv().execute_script(
                                        "arguments[0].click();", buttons[0])
                                    methods.append("player-button-dom")
                                except Exception:
                                    pass
                            time.sleep(0.9)
                    paused = bool(drv().execute_script(
                        "return Boolean(arguments[0].paused);", video))
                    if paused:
                        try:
                            drv().execute_script("arguments[0].play();", video)
                            methods.append("media-play")
                            time.sleep(0.9)
                        except Exception:
                            pass
                    paused = bool(drv().execute_script(
                        "return Boolean(arguments[0].paused);", video))
                    if paused:
                        try:
                            drv().find_element(By.TAG_NAME, "body").send_keys("k")
                            methods.append("youtube-key-k")
                            time.sleep(0.9)
                        except Exception:
                            pass
                    paused = bool(drv().execute_script(
                        "return Boolean(arguments[0].paused);", video))
                    ready = drv().execute_script(
                        "return Number(arguments[0].readyState || 0);", video)
                    # Playback is explicitly user-facing. Restore and maximise
                    # the exact WebDriver-owned Firefox HWND before success.
                    presentation = present_tab(maximize=True)
                    requested_video_id = _b.youtube_video_id(url)
                    actual_video_id = _b.youtube_video_id(drv().current_url)
                    content_verified = bool(
                        requested_video_id
                        and actual_video_id == requested_video_id)
                    value = receipt("youtube_play",
                                    ok=(not paused and bool(
                                        presentation.get("presented"))
                                        and content_verified),
                                    playing=not paused,
                                    video_ready_state=ready,
                                    playback_methods=(methods or
                                                      ["already-playing"]),
                                    media_owner=True,
                                    requested_url=url,
                                    requested_video_id=requested_video_id,
                                    actual_video_id=actual_video_id,
                                    content_verified=content_verified,
                                    **presentation)
                elif operation == "youtube_fullscreen":
                    media_target(args.get("tab"))
                    host = (urlparse(drv().current_url).hostname or "").lower()
                    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                        raise ValueError(
                            "The controlled tab is not on YouTube; open a video first")
                    wanted = bool(args.get("enabled", True))

                    def fullscreen_state():
                        return bool(drv().execute_script(
                            "return Boolean(document.fullscreenElement);"))

                    before = fullscreen_state()
                    method = "already-set"
                    if before != wanted:
                        method = "player-button"
                        buttons = drv().find_elements(
                            By.CSS_SELECTOR, "button.ytp-fullscreen-button")
                        if buttons:
                            click_element(buttons[0])
                        else:
                            method = "youtube-key-f"
                            drv().find_element(By.TAG_NAME, "body").send_keys("f")
                        end = time.time() + 4.0
                        while time.time() < end and fullscreen_state() != wanted:
                            time.sleep(0.1)
                    after = fullscreen_state()
                    presentation = present_tab()
                    value = receipt(
                        "youtube_fullscreen",
                        ok=after == wanted and bool(
                            presentation.get("presented")),
                        fullscreen=after, requested=wanted, method=method,
                        changed=before != after,
                        media_owner=True,
                        **presentation,
                    )
                elif operation == "close":
                    if not usable():
                        discard()
                        value = '[Browser Receipt] {"ok":true,"operation":"close"}'
                    elif args.get("all_tabs", False):
                        discard()
                        value = '[Browser Receipt] {"ok":true,"operation":"close_all"}'
                    else:
                        handle = get_tab(args.get("tab"))
                        closed_id = tab_id(handle)
                        _h, closed_url, closed_title = top_info()
                        remaining = [h for h in drv().window_handles if h != handle]
                        if handle == media_handle.get("value"):
                            media_handle["value"] = None
                        drv().close()
                        if remaining:
                            drv().switch_to.window(remaining[-1])
                        else:
                            discard("its last tab was closed")
                        value = receipt(
                            "close", with_page=False, tab=closed_id,
                            closed_url=closed_url, closed_title=closed_title,
                            media_stopped="music.youtube.com" in closed_url,
                            session_ended=not remaining or None)
                else:
                    raise ValueError(f"Unknown browser operation: {operation}")
                reply.put((True, value))
            except InterruptedError as exc:
                reply.put((True, '[Interrupted] ' + str(exc)))
            except UnexpectedAlertPresentException:
                _alert, info = pending_dialog()
                reply.put((False, "A JavaScript dialog is blocking the page: "
                           + json.dumps(info or {}, ensure_ascii=False)
                           + ". Use BROWSER_DIALOG with mode accept or dismiss."))
            except Exception as exc:
                reply.put((False, str(exc)))


def _b_login_enabled():
    try:
        import browser_login
        return browser_login.enabled()
    except Exception:
        return False


def sync_login_live(driver, domains, get_tab, tab_id, web_error):
    """Bring more of his sessions into the running browser, via cookies.

    A cookie can only be set for the site that is currently open, so each
    domain is visited briefly. Capped, because "everything" is hundreds of
    sites and a minute of flicker.
    """
    import browser_login
    found = browser_login.default_profile()
    if found is None:
        return ("[Error] No Firefox profile with cookies was found on this "
                "machine, so there is no session to borrow.")
    cookies = browser_login.read_cookies(found["path"], domains=domains or None)
    if not cookies:
        scope = f" for {', '.join(domains)}" if domains else ""
        return (f"[Error] Himanshu's Firefox profile holds no current "
                f"cookies{scope}. Nothing was imported.")
    by_host = {}
    for cookie in cookies:
        by_host.setdefault(cookie["domain"].lstrip("."), []).append(cookie)
    landed, visited = 0, 0
    get_tab()
    original = driver.current_url
    for host, rows in list(by_host.items())[:25]:
        try:
            driver.get(f"https://{host}/")
            visited += 1
        except Exception:
            continue
        for cookie in rows:
            entry = {"name": cookie["name"], "value": cookie["value"],
                     "path": cookie["path"], "secure": cookie["secure"],
                     "httpOnly": cookie["httpOnly"]}
            if cookie["expires"] and cookie["expires"] > 0:
                entry["expiry"] = int(cookie["expires"])
            try:
                driver.add_cookie(entry)
                landed += 1
            except web_error:
                continue
    try:
        if original and original != "about:blank":
            driver.get(original)
    except Exception:
        pass
    return (f"[Success] Brought {landed} of {len(cookies)} cookies into the "
            f"browser from Firefox profile '{Path(found['path']).name}' "
            f"({visited} sites visited). Sites: "
            f"{browser_login.summarise(cookies)}. Saved passwords were not read.")
