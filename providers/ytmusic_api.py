"""YouTube Music for VANTA, driven through the managed browser.

Spotify's Web API cannot start playback unless a Spotify client is already
running and signed in to Premium — `sp.devices()` returns an empty list on a
machine where nothing is open, so "play something" fails for a reason Himanshu
cannot act on. YouTube Music has no such requirement: the page *is* the player.

Two deliberate choices keep this from being brittle.

Transport control uses the operating system's media keys rather than clicking
page elements. Windows routes them to whatever currently owns media focus, so
pause and skip keep working after YouTube Music restyles its player, and they
work whether the tab is focused or buried.

Now-playing is read from the browser tab title, which YouTube Music maintains
as "Song - Artist - YouTube Music". A tab title is a stable contract; a CSS
selector into someone else's player is not.
"""

import json
import re
import time

_HOME = "https://music.youtube.com"


def _receipt_payload(text):
    """Browser helpers return a '[Browser ...] {json}' string."""
    try:
        return json.loads(str(text).split("] ", 1)[1])
    except Exception:
        return {}


def _music_tab():
    """The tab id already showing YouTube Music, if there is one."""
    import browser
    payload = _receipt_payload(browser.tabs())
    tabs = payload if isinstance(payload, list) else []
    for tab in tabs:
        if "music.youtube.com" in str(tab.get("url", "")):
            return tab.get("tab")
    return None


def _title_now_playing(title):
    """Track name out of the tab title.

    YouTube Music writes "Weightless | YouTube Music" — a pipe, not the dash
    the rest of YouTube uses. Matching only the dash form made every playing
    track report as nothing playing.
    """
    raw = re.sub(r"^\(\d+\)\s*", "", str(title or "")).strip()
    raw = re.sub(r"\s*[|\-–]\s*YouTube Music\s*$", "", raw).strip()
    if not raw or raw.lower() in {"youtube music", "search results", "music"}:
        return ""
    return raw


# Controls YouTube Music puts on every page. Without this the results read
# "Guide, Initiate search, Clear, Like" — the page's furniture, not its music.
_CHROME = {
    "guide", "initiate search", "clear", "like", "dislike", "search",
    "sign in", "home", "explore", "library", "upgrade", "settings",
    "next", "previous", "play", "pause", "shuffle", "repeat", "more",
    "back", "forward", "skip navigation", "mute", "expand", "collapse",
}


def _result_names(elements, limit):
    """Song and album titles, separated from the player's own controls."""
    songs, fallback, seen = [], [], set()
    for element in elements:
        name = " ".join(str(element.get("name", "")).split())
        name = re.sub(r"^Play\s+", "", name).strip()
        if not name or len(name) < 3:
            continue
        key = name.lower()
        if key in _CHROME or key in seen:
            continue
        seen.add(key)
        # A link into /watch or /playlist is a real result; everything else is
        # navigation until proven otherwise.
        href = str(element.get("href", ""))
        if "/watch" in href or "/playlist" in href or "list=" in href:
            songs.append(name)
        else:
            fallback.append(name)
        if len(songs) >= int(limit):
            break
    chosen = songs or fallback
    return chosen[:int(limit)]


def _player_is_playing(tab):
    """Use the player's accessible Pause control as live playback evidence."""
    import browser
    snapshot = _receipt_payload(browser.snapshot(tab=tab))
    for element in snapshot.get("elements", []):
        name = " ".join(str(element.get("name", "")).split()).lower()
        if name == "pause" or name.startswith("pause "):
            return True
    return False


def _play_receipt(query, playing):
    """Classify observed player state; explanatory text is not success."""
    if str(playing or "").startswith("Now playing:"):
        return f"[Success] Playing on YouTube Music. {playing}"
    return ("[Error] YouTube Music opened the requested results, but playback "
            "did not start. No track is currently playing.")


def play(query=""):
    """Search and start playing. With no query, resume what is loaded."""
    import browser
    tab = _music_tab()

    if not str(query or "").strip():
        if tab is None:
            return ("[Error] Nothing is loaded in YouTube Music yet. Give me "
                    "something to play.")
        from desktop import media_key
        media_key("playpause")
        time.sleep(1.0)
        return f"[Success] Resumed. {now_playing()}"

    url = f"{_HOME}/search?q={requests_quote(query)}"
    result = browser.goto(url, tab=tab)
    if "[Browser" not in result:
        return f"[Error] Could not open YouTube Music: {result}"
    tab = tab or _receipt_payload(result).get("tab")

    # Let the results render, then start the first song.
    time.sleep(2.5)
    snapshot = _receipt_payload(browser.snapshot(tab=tab))
    elements = snapshot.get("elements", [])

    # "Play <song>" is the accessible name YouTube Music gives its play
    # buttons; falling back to the first song row keeps this working if that
    # label changes.
    candidates = [e for e in elements
                  if e.get("name", "").lower().startswith("play ")]
    if not candidates:
        candidates = [e for e in elements
                      if e.get("role") == "link" and e.get("name")
                      and "watch" not in e.get("name", "").lower()]
    if not candidates:
        return ("[Error] YouTube Music opened but I could not find a play "
                "button. Take a BROWSER_SNAPSHOT and click a result yourself.")

    chosen_name = re.sub(r"^Play\s+", "", candidates[0].get("name", "")).strip()
    click_result = browser.click(candidates[0]["ref"], tab=tab)
    if "[Browser" not in str(click_result):
        return f"[Error] YouTube Music play control failed: {click_result}"

    # The tab title often lags behind audible playback. Poll briefly, then use
    # the player's accessible Pause control as stronger evidence that audio is
    # already running. Never click another result merely because the title is
    # stale; that is what started three overlapping tracks.
    for _ in range(6):
        time.sleep(1.0)
        playing = now_playing()
        if playing.startswith("Now playing:"):
            return _play_receipt(query, playing)
        if _player_is_playing(tab):
            title = chosen_name or str(query)
            return _play_receipt(query, f"Now playing: {title}")
    return _play_receipt(query, now_playing())


def play_default():
    """Resume a paused VANTA player, or create one with a safe default.

    A generic ``play music`` used to send Windows' play/pause key and report
    ``Done`` even when no media session existed.  Observe the owned tab first:
    if Himanshu closed it, create a fresh background player; if it is already
    playing, do not toggle it off; otherwise resume and verify.
    """
    tab = _music_tab()
    if tab is None:
        return play("relaxing music mix")
    if _player_is_playing(tab):
        playing = now_playing()
        return _play_receipt("music", playing)

    from desktop import media_key
    media_key("playpause")
    for _ in range(4):
        time.sleep(0.75)
        playing = now_playing()
        if playing.startswith("Now playing:") and _player_is_playing(tab):
            return _play_receipt("music", playing)
    return _play_receipt("music", now_playing())


def requests_quote(text):
    from urllib.parse import quote_plus
    return quote_plus(str(text))


def pause():
    from desktop import media_key
    if _music_tab() is None:
        return "[Error] YouTube Music is not open."
    media_key("playpause")
    return "[Success] Toggled play/pause."


def skip(direction="next"):
    from desktop import media_key
    if _music_tab() is None:
        return "[Error] YouTube Music is not open."
    media_key("next" if direction != "previous" else "previous")
    time.sleep(1.5)
    return f"[Success] Skipped to {direction} track. {now_playing()}"


def now_playing():
    import browser
    payload = _receipt_payload(browser.tabs())
    tabs = payload if isinstance(payload, list) else []
    for tab in tabs:
        if "music.youtube.com" in str(tab.get("url", "")):
            track = _title_now_playing(tab.get("title", ""))
            if track:
                return f"Now playing: {track}"
            # The title can remain the generic "YouTube Music" while audio is
            # already running. The accessible Pause control is stronger live
            # evidence: a paused player exposes Play instead.
            tab_id = tab.get("tab")
            if tab_id is not None and _player_is_playing(tab_id):
                return "Now playing: audio in YouTube Music"
            return "YouTube Music is open but nothing is playing."
    return "YouTube Music is not open."


def search(query, limit=5):
    """List results without starting anything, so a choice can be offered."""
    import browser
    if not str(query or "").strip():
        return "[Error] A search query is required."
    tab = _music_tab()
    browser.goto(f"{_HOME}/search?q={requests_quote(query)}", tab=tab)
    time.sleep(2.5)
    snapshot = _receipt_payload(browser.snapshot(tab=tab or _music_tab()))
    names = _result_names(snapshot.get("elements", []), limit)
    if not names:
        return f"No results found for '{query}'."
    return (f"YouTube Music results for '{query}':\n"
            + "\n".join(f"- {n}" for n in names)
            + "\nAsk me to play one by name.")
