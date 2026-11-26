"""Interactive reports must collect real answers, validate them and queue once."""
import copy
import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

import report_forms
import report_html
from report_window import ReportServer


FORM = {'type': 'questionnaire', 'title': 'Plan your learning', 'questions': [
    {'id': 'pace', 'type': 'single_choice', 'label': 'Choose your pace', 'options': [
        {'id': 'guided', 'label': 'Guided practice'}, {'id': 'deep', 'label': 'Deep dive'}]},
    {'id': 'topics', 'type': 'multiple_choice', 'label': 'Choose topics', 'required': False, 'options': [
        {'id': 'ai', 'label': 'AI'}, {'id': 'web', 'label': 'Web'}]},
    {'id': 'hours', 'type': 'rating', 'label': 'Hours available', 'min': 1, 'max': 5},
    {'id': 'notes', 'type': 'textarea', 'label': 'Anything to add?', 'required': False},
]}


def rendered(payload=FORM):
    text, _ = report_html.blocks_from_payload({'blocks': [payload]})
    return text, report_html.render_report(text)


def test_form_is_a_real_visual_block_with_stable_schema_identity():
    source, report = rendered()
    assert not report_html.validate_scenes(source)
    assert len(report['forms']) == 1 and 'questionnaire reveal' in report['html']
    spec = next(iter(report['forms'].values()))
    assert report_forms.normalize(FORM)['id'] == spec['id']
    assert not any('checked' in part for part in report['html'].split('<input')[1:])


@pytest.mark.parametrize('change', [
    {'questions': []}, {'layout': 'javascript'},
    {'questions': [{'id': 'bad id', 'label': 'Bad'}]},
    {'questions': [{'id': 'q', 'label': 'Bad', 'required': 'false'}]},
    {'questions': [{'id': 'q', 'label': 'Bad', 'type': 'single_choice', 'options': []}]},
])
def test_invalid_forms_fail_preflight(change):
    source, _ = rendered(dict(FORM, **change))
    assert report_html.validate_scenes(source)


def test_answers_validate_choices_ranges_dates_and_unknown_fields():
    spec = report_forms.normalize(FORM)
    _, errors = report_forms.validate_answers(spec, {})
    assert set(errors) == {'pace', 'hours'}
    _, errors = report_forms.validate_answers(spec, {'pace': 'unknown', 'hours': True})
    assert set(errors) == {'pace', 'hours'}
    answers, errors = report_forms.validate_answers(spec, {'pace': 'guided', 'hours': 4, 'topics': ['web', 'ai']})
    assert not errors and answers['topics'] == ['ai', 'web']
    with pytest.raises(ValueError):
        report_forms.validate_answers(spec, {'invented': 'anything'})
    date = report_forms.normalize({'questions': [{'id': 'date', 'type': 'date', 'label': 'Day?'}]})
    assert report_forms.validate_answers(date, {'date': '2026-02-30'})[1]
    assert not report_forms.validate_answers(date, {'date': '2026-10-31'})[1]


def test_quiz_scoring_uses_answer_key_and_does_not_grade_open_text():
    payload = {'mode': 'quiz', 'questions': [
        {'id': 'sum', 'type': 'number', 'label': 'Two plus two?', 'min': 0, 'max': 10, 'correct': 4},
        {'id': 'why', 'type': 'text', 'label': 'Explain your reasoning', 'required': False}]}
    spec = report_forms.normalize(payload)
    score = report_forms.feedback(spec, {'sum': 4, 'why': 'An explanation'})
    assert score['correct'] == score['total'] == 1


def test_markup_and_review_content_do_not_execute_html():
    payload = copy.deepcopy(FORM)
    payload['title'] = '<script>alert(1)</script>'
    markup = report_forms.render(payload)
    assert '<script>' not in markup and '&lt;script&gt;' in markup


@pytest.fixture
def server():
    sent = []
    instance = ReportServer(sent.append, token='synthetic-questionnaire-test')
    _, report = rendered()
    instance.publish(report)
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance, sent
    instance.shutdown()
    instance.server_close()


def post(server, payload, token=True):
    request = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/questionnaire',
                                     data=json.dumps(payload).encode(), method='POST')
    if token:
        request.add_header('X-Report-Token', server.token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def body():
    return {'form_id': report_forms.normalize(FORM)['id'], 'submission_id': 'test-submission-1',
            'answers': {'pace': 'guided', 'hours': 4}}


def test_submission_auth_validation_and_no_client_supplied_schema(server):
    service, sent = server
    assert post(service, body(), token=False)[0] == 403
    invalid = body()
    invalid['answers']['pace'] = 'invented'
    assert post(service, invalid)[0] == 422
    invalid['form_id'] = 'not-active'
    assert post(service, invalid)[0] == 409
    assert not sent


def test_repeated_concurrent_submissions_queue_exactly_once_and_recover_lost_ack(server):
    service, sent = server
    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(lambda _: post(service, body())[0], range(3)))
    assert statuses == [200, 200, 200] and len(sent) == 1
    assert 'Guided practice' in sent[0] and 'approval tokens' in sent[0]
    service.publish(report_html.render_report('New answer report'))
    assert post(service, body())[0] == 200  # retry after the form was replaced
    changed = body()
    changed['answers']['hours'] = 3
    assert post(service, changed)[0] == 409
    changed['submission_id'] = 'new-submission-id'
    assert post(service, changed)[0] == 409  # a new submission needs a current form
    assert len(sent) == 1


def test_failed_delivery_is_retryable_without_losing_answers(server):
    service, sent = server
    def broken(command):
        raise RuntimeError('queue is unavailable')
    service.command_sink = broken
    assert post(service, body())[0] == 503
    service.command_sink = sent.append
    assert post(service, body())[0] == 200 and len(sent) == 1


def test_quiz_submission_returns_grounded_score(server):
    service, sent = server
    quiz = {'type': 'questionnaire', 'mode': 'quiz', 'questions': [{'id': 'sum', 'type': 'number', 'label': 'Two plus two?',
                                         'min': 0, 'max': 10, 'correct': 4}]}
    _, report = rendered(quiz)
    service.publish(report)
    form_id = next(iter(report['forms']))
    status, receipt = post(service, {'form_id': form_id, 'submission_id': 'quiz-submission', 'answers': {'sum': 4}})
    assert status == 200 and receipt['score']['correct'] == receipt['score']['total'] == 1
    assert len(sent) == 1


def test_real_browser_wizard_review_drafts_and_submit(server):
    from playwright.sync_api import sync_playwright
    service, sent = server
    source, report = rendered()
    report.update(stream_id='form-stream', phase='writing')
    service.publish(report)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 900, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{service.port}/?t={service.token}&static=1')
            form = page.locator('.questionnaire')
            form.wait_for()
            assert form.locator('fieldset:not([hidden])').count() == 1
            page.get_by_role('button', name='Next', exact=True).click()
            assert 'required' in form.locator('[data-question=pace] .q-error').inner_text()
            page.get_by_label('Guided practice', exact=True).check()
            page.evaluate('window.originalForm=document.querySelector(".questionnaire")')
            extended = report_html.render_report(source+'\n\nMore context is arriving.')
            extended.update(stream_id='form-stream', phase='writing')
            service.publish(extended)
            page.get_by_text('More context is arriving.', exact=True).wait_for()
            assert page.evaluate('window.originalForm===document.querySelector(".questionnaire")')
            assert page.get_by_label('Guided practice', exact=True).is_checked()
            page.get_by_role('button', name='Next', exact=True).click()
            page.get_by_label('AI', exact=True).check()
            page.get_by_role('button', name='Next', exact=True).click()
            page.get_by_role('spinbutton', name='Hours available', exact=True).fill('4')
            page.reload()
            page.get_by_role('spinbutton', name='Hours available', exact=True).wait_for()
            assert page.get_by_role('spinbutton', name='Hours available', exact=True).input_value() == '4'
            page.get_by_role('button', name='Next', exact=True).click()
            page.get_by_role('textbox', name='Anything to add?', exact=True).fill('Teach with examples. <img src=x onerror=alert(1)>')
            page.get_by_role('button', name='Review answers', exact=True).click()
            assert 'Guided practice' in form.locator('.q-review').inner_text()
            assert form.locator('.q-review img').count() == 0
            assert not sent
            with page.expect_response(lambda response: response.url.endswith('/api/questionnaire')) as result:
                page.get_by_role('button', name='Submit answers', exact=True).click()
            assert result.value.status == 200
            page.wait_for_function('() => document.querySelector(".q-status").textContent.includes("submitted")')
            assert len(sent) == 1 and 'Teach with examples.' in sent[0]
            assert form.locator('input:not([disabled]),textarea:not([disabled])').count() == 0
            assert not errors
        finally:
            browser.close()
