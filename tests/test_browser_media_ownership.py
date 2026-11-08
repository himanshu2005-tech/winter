"""Media stays attached to, and visibly presented from, VANTA's own tab."""

import inspect
from unittest import mock

import browser
import browser_real
import browser_window


def test_window_choice_never_uses_an_unowned_same_title_browser():
    rows = [
        (10, 100, "Modern Warfare - YouTube — Mozilla Firefox"),
        (20, 200, "Modern Warfare - YouTube — Mozilla Firefox"),
    ]
    assert browser_window._pick_owned_window(
        rows, {200}, "Modern Warfare - YouTube — Mozilla Firefox") == rows[1]


def test_no_owned_process_means_no_title_only_fallback():
    rows = [(10, 100, "Modern Warfare - YouTube — Mozilla Firefox")]
    assert browser_window._pick_owned_window(rows, set(), "Modern Warfare") is None


@mock.patch.object(browser_window, "_activate_hwnd", return_value=True)
@mock.patch.object(browser_window, "_windows", return_value=[
    (30, 300, "Modern Warfare - YouTube — Mozilla Firefox"),
    (40, 400, "Unrelated - YouTube — Mozilla Firefox"),
])
@mock.patch.object(browser_window, "_owned_browser_pids", return_value={300})
def test_present_receipt_is_verified_against_owned_hwnd(pids, windows, activate):
    result = browser_window.present_owned_browser(
        "D:/winter/vault/browser_profile_firefox_real",
        title_hint="Modern Warfare - YouTube — Mozilla Firefox",
        hinted_pid=300)
    assert result["presented"] is True
    assert result["foreground_verified"] is True
    assert result["window_pid"] == 300
    activate.assert_called_once_with(30, maximize=False)


def test_both_backends_promote_playback_out_of_a_headless_session():
    playwright = inspect.getsource(browser._BrowserWorker._run)
    selenium = inspect.getsource(browser_real.RealFirefoxWorker._run)
    assert 'page = get_page(args.get("tab"), visible=True)' in playwright
    assert 'handle = get_tab(args.get("tab"), visible=True)' in selenium
    assert 'present_page(page, maximize=True)' in playwright
    assert 'present_tab(maximize=True)' in selenium
    for source in (playwright, selenium):
        assert '"presented": False' in source
        assert "presentation.get(\"presented\")" in source
        assert "media_owner=True" in source
        assert "content_verified=content_verified" in source


def test_youtube_content_identity_uses_video_id_not_a_lookalike_title():
    assert browser.youtube_video_id(
        "https://www.youtube.com/watch?v=abc_123&list=xyz") == "abc_123"
    assert browser.youtube_video_id("https://youtu.be/abc_123?t=4") == "abc_123"
    assert browser.youtube_video_id(
        "https://www.youtube.com/watch?v=different") != "abc_123"


def test_fullscreen_targets_the_owned_media_tab_not_the_last_tab():
    playwright = inspect.getsource(browser._BrowserWorker._run)
    selenium = inspect.getsource(browser_real.RealFirefoxWorker._run)
    assert 'page = media_target(args.get("tab"))' in playwright
    assert 'media_target(args.get("tab"))' in selenium
    assert 'page = get_page(args.get("tab"))\n                    host =' not in playwright
    assert 'get_tab(args.get("tab"))\n                    host =' not in selenium


def test_one_play_action_owns_bounded_playback_recovery():
    for module, marker in ((browser, 'page.keyboard.press("k")'),
                           (browser_real, '.send_keys("k")')):
        source = inspect.getsource(
            module._BrowserWorker._run if module is browser
            else module.RealFirefoxWorker._run)
        assert '"player-button"' in source
        assert '"media-play"' in source
        assert marker in source
