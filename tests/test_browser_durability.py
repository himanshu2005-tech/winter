"""Real local multi-step workflows, isolated from personal profiles and accounts."""
import functools
import http.server
import json
import queue
import threading
import time
from unittest import mock

import pytest

import browser
import browser_real
from browser_contracts import check_deadline


PAGE = """<!doctype html><title>Durability fixture</title>
<form id="application" onsubmit="event.preventDefault();result.textContent='Application recorded'">
<label>Name<input name="applicant" required></label>
<label>Email<input name="email" type="email" required></label>
<label>Resume<input type="file" name="resume"></label>
<button type="submit">Apply</button></form><div id="result"></div>
<form><input required name="unrelated"><input type="file" name="other_doc" aria-label="Other document"></form>
<button id="first" onclick="window.clicks=(window.clicks||0)+1;result.textContent='First selected '+window.clicks">First</button>
<button id="second" onclick="result.textContent='Second selected'">Second</button>
<button id="rerender" onclick="second.outerHTML=second.outerHTML">Re-render second</button>
<a target="_blank" href="child.html">Open child</a>
<button onclick="confirm('Synthetic confirmation')">Confirm application</button>
<button onclick="document.querySelector('[name=email]').setAttribute('aria-invalid','true');result.textContent='Check email'">Reject email</button>
<div id="picker" role="combobox" aria-label="Theme" aria-expanded="false"
 onclick="this.setAttribute('aria-expanded','true');choices.hidden=false">Choose</div>
<div id="choices" hidden>
 <div role="option" onclick="picker.textContent=this.textContent;choices.hidden=true">Minimal Plus</div>
 <div role="option" onclick="picker.textContent=this.textContent;choices.hidden=true">Minimal</div>
</div>
<div id="broken" role="combobox" aria-label="Broken theme" aria-expanded="false"
 onclick="badchoices.hidden=false">Choose theme</div>
<div id="badchoices" hidden><div role="option" onclick="badchoices.hidden=true">Ignored choice</div></div>
<label>Reverting choice<select name="reverting" onchange="this.selectedIndex=0">
<option value="old">Old</option><option value="new">New</option></select></label>
"""


@pytest.fixture(scope='module', params=['chromium', 'firefox-real'])
def session(request, tmp_path_factory):
    root = tmp_path_factory.mktemp('browser-durability')
    (root / 'index.html').write_text(PAGE, encoding='utf-8')
    (root / 'child.html').write_text(
        '<title>Child workspace</title><label>Child note<input name="child_note"></label>', encoding='utf-8')

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        'WINTER_BROWSER_ALLOW_PRIVATE': '1',
        'WINTER_BROWSER_PROFILE': str(root / 'profile'),
        'WINTER_BROWSER_IMPORT_COOKIES': '0',
    }
    with mock.patch.dict('os.environ', env):
        worker = (browser_real.RealFirefoxWorker() if request.param == 'firefox-real'
                  else browser._BrowserWorker(engine_name='chromium'))
        try:
            result = worker.call('start', visible=False)
            if '[Browser Error]' in result:
                pytest.skip(result)
            yield worker, f'http://127.0.0.1:{server.server_port}/index.html', root
        finally:
            worker.call('close', all_tabs=True)
            server.shutdown()
            server.server_close()


def unpack(raw):
    assert not raw.startswith('[Browser Error]'), raw
    return json.loads(raw.split('] ', 1)[1])


def reset(session):
    worker, url, _ = session
    return worker, unpack(worker.call('goto', url=url, visible=False))


def by_name(snapshot, name):
    return next(item['ref'] for item in snapshot['elements'] if item['name'] == name)


def test_targeted_snapshot_cannot_reassign_existing_references(session):
    worker, _ = reset(session)
    full = unpack(worker.call('snapshot'))
    first = by_name(full, 'First')
    second = by_name(full, 'Second')
    targeted = unpack(worker.call('snapshot', query='Second'))
    assert by_name(targeted, 'Second') == second
    assert first != second
    unpack(worker.call('click', ref=first))
    assert 'First selected' in unpack(worker.call('snapshot'))['text']


def test_replaced_and_previous_document_refs_never_click_a_new_element(session):
    worker, _ = reset(session)
    old = by_name(unpack(worker.call('snapshot')), 'Second')
    unpack(worker.call('click', ref='rerender'))
    new = by_name(unpack(worker.call('snapshot')), 'Second')
    assert old != new
    assert worker.call('click', ref=old).startswith('[Browser Error]')
    reset(session)
    fresh = by_name(unpack(worker.call('snapshot')), 'Second')
    assert fresh not in {old, new}
    assert worker.call('click', ref=new).startswith('[Browser Error]')


def test_popup_is_reported_and_explicit_tab_survives_tab_listing(session):
    worker, parent = reset(session)
    result = unpack(worker.call('click', ref='Open child'))
    assert len(result['opened_tabs']) == 1
    child = result['opened_tabs'][0]['tab']
    assert result['source_tab'] == parent['tab']
    assert result['tab'] == child
    assert unpack(worker.call('snapshot'))['title'] == 'Child workspace'
    unpack(worker.call('snapshot', tab=parent['tab']))
    worker.call('tabs')
    assert unpack(worker.call('snapshot'))['tab'] == parent['tab']
    unpack(worker.call('snapshot', tab=child))
    worker.call('tabs')
    assert unpack(worker.call('snapshot'))['tab'] == child
    unpack(worker.call('close', tab=child))


def test_multistep_application_checks_fields_upload_and_submit(session):
    worker, _ = reset(session)
    rejected = unpack(worker.call('submit', ref='Apply'))
    assert not rejected['ok'] and not rejected['submitted']
    assert {item['field'] for item in rejected['validation_errors']} == {'applicant', 'email'}
    path = session[2] / 'synthetic-resume.txt'
    path.write_text('Synthetic resume fixture', encoding='utf-8')
    filled = unpack(worker.call('fill_form', fields={
        'applicant': {'value': 'Test Person'},
        'email': {'value': 'test@example.org'},
        'resume': {'kind': 'upload', 'value': str(path)},
    }))
    assert filled['ok'] and len(filled['verified']) == 3
    result = unpack(worker.call('submit', ref='Apply'))
    assert result['ok'], result
    assert 'Application recorded' in unpack(worker.call('snapshot'))['text']


def test_changed_page_with_invalid_fields_is_not_successful_submission(session):
    worker, _ = reset(session)
    unpack(worker.call('fill_form', fields={
        'applicant': {'value': 'Test'}, 'email': {'value': 'test@example.org'}}))
    result = unpack(worker.call('submit', ref='Reject email'))
    assert not result['ok']
    assert result['validation_errors'][0]['field'] == 'email'


def test_expired_queued_click_is_never_executed_later(session):
    worker, _ = reset(session)
    # Place an already-expired request in the actual queue, as happens when its
    # caller times out while another operation occupies the serial worker.
    reply = queue.Queue()
    worker.requests.put(('click', {'ref': 'First', '_request_deadline': time.monotonic() - 1}, reply))
    ok, message = reply.get(timeout=5)
    assert not ok and 'expired' in message
    assert 'First selected' not in unpack(worker.call('snapshot'))['text']


def test_dropdown_chooses_exact_option_and_requires_retained_selection(session):
    worker, _ = reset(session)
    assert unpack(worker.call('select', ref='Theme', value='Minimal'))['verified']
    snapshot = unpack(worker.call('snapshot'))
    picker = next(el for el in snapshot['elements'] if el.get('name') == 'Theme')
    assert 'Minimal Plus' not in snapshot['text']  # closed popup, exact option retained
    assert picker['aria-expanded'] == 'true'  # receipt must rely on value, not closed state
    assert worker.call('select', ref='Broken theme', value='Ignored choice').startswith('[Browser Error]')
    assert worker.call('select', ref='reverting', value='New').startswith('[Browser Error]')


def test_native_click_ack_loss_cannot_dispatch_a_second_click(session):
    worker, _ = reset(session)
    if isinstance(worker, browser_real.RealFirefoxWorker):
        from selenium.webdriver.remote.webelement import WebElement
        from selenium.common.exceptions import ElementClickInterceptedException
        original = WebElement.click

        def lost_ack(el, *args, **kwargs):
            first = el.get_attribute('id') == 'first'
            result = original(el, *args, **kwargs)
            if first:
                raise ElementClickInterceptedException('synthetic acknowledgement loss')
            return result

        patcher = mock.patch.object(WebElement, 'click', lost_ack)
    else:
        from playwright.sync_api import Locator
        original = Locator.click

        def lost_ack(el, *args, **kwargs):
            first = el.get_attribute('id') == 'first'
            result = original(el, *args, **kwargs)
            if first:
                raise TimeoutError('synthetic acknowledgement loss')
            return result

        patcher = mock.patch.object(Locator, 'click', lost_ack)
    with patcher:
        unpack(worker.call('click', ref='first'))
    assert 'First selected 1' in unpack(worker.call('snapshot'))['text']


@pytest.mark.parametrize('wait_text', [None, 'never-appears-in-this-fixture'])
def test_abort_interrupts_wait_and_worker_remains_usable(session, wait_text):
    worker, _ = reset(session)
    abort = threading.Event()
    timer = threading.Timer(0.25, abort.set)
    started = time.monotonic()
    timer.start()
    try:
        result = worker.call('wait', text=wait_text, ms=9000, _interrupt_event=abort)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2
    assert result.startswith('[Interrupted]')
    assert unpack(worker.call('snapshot'))['title'] == 'Durability fixture'


def test_stale_upload_ref_does_not_choose_another_file_input(session):
    worker, _ = reset(session)
    snapshot = unpack(worker.call('snapshot'))
    old = next(el['ref'] for el in snapshot['elements'] if el.get('type') == 'file')
    reset(session)
    path = session[2] / 'synthetic-upload.txt'
    path.write_text('synthetic', encoding='utf-8')
    result = unpack(worker.call('upload', ref=old, path=str(path)))
    assert not result['ok'] and not result['verified']


def test_explicit_selectors_work_and_ambiguous_selectors_do_not_guess(session):
    worker, _ = reset(session)
    unpack(worker.call('click', ref='css=#first'))
    assert 'First selected 1' in unpack(worker.call('snapshot'))['text']
    unpack(worker.call('type', ref='xpath=//input[@name="applicant"]', text='Exact target'))
    result = worker.call('click', ref='css=button')
    assert result.startswith('[Browser Error]')
    assert 'First selected 1' in unpack(worker.call('snapshot'))['text']


def test_unresolved_native_dialog_is_not_a_completed_action(session):
    worker, _ = reset(session)
    result = unpack(worker.call('click', ref='Confirm application'))
    if result.get('browser_dialog'):
        try:
            assert not result['ok'] and not result['verified']
            assert result['requires_action'] == 'BROWSER_DIALOG'
        finally:
            worker.call('dialog', mode='dismiss')
    else:
        assert result['dialogs'][-1]['handled'] == 'dismiss'


def test_deadline_and_browser_errors_are_failures():
    import actions
    import execution_reliability
    import outcomes
    assert not actions._classify_output('[Browser Error] worker timed out')
    assert execution_reliability._receipt_state(
        {'ok': True, 'output': '[Browser Error] expired'}) != outcomes.SUCCEEDED
    with pytest.raises(TimeoutError):
        check_deadline({'_request_deadline': time.monotonic() - 1})


def test_action_cancellation_context_reaches_browser_calls():
    from browser_contracts import execution_scope, current_interrupt
    abort = threading.Event()
    worker = mock.Mock()
    worker.thread.is_alive.return_value = True
    worker.call.return_value = '[Browser Receipt] {"ok":true}'
    with mock.patch.object(browser, '_worker', worker):
        with execution_scope(abort):
            browser.snapshot()
    assert worker.call.call_args.kwargs['_interrupt_event'] is abort
    assert current_interrupt() is None
