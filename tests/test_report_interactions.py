"""Streaming, linked concepts and animation: synthetic data, real local renderer."""
import json
import threading
import urllib.error
import urllib.request
from unittest import mock

import pytest

import report_html
import report_media
import report_window
from report_stream import ReportStream, preview_markdown, report_prefix


def test_streaming_chunks_keep_code_progress_and_wait_for_complete_visuals():
    events, now = [], [0.]
    reader = ReportStream(events.append, clock=lambda: now[0])
    for chunk in ('<REP', 'ORT>\n## Example\nUseful details arrive first.\n',
                  '\n```python\nprint(', '"hello")\n```\n',
                  '\n```scene3d\n{"objects":', '[]}\n```\n</REPORT>'):
        reader.feed(chunk)
        now[0] += .5
    assert any('print(' in e[1]['content'] and 'hello' not in e[1]['content'] for e in events)
    assert not any('"objects":' in e[1]['content'] and '[]}\n```' not in e[1]['content'] for e in events)
    assert all(e[0] == 'report_stream' and e[1]['phase'] == 'writing' for e in events)
    assert len({e[1]['stream_id'] for e in events}) == 1


@pytest.mark.parametrize('text', [
    '<think><REPORT>Private reasoning</REPORT></think>',
    '<ACTION type="X"><REPORT>Tool source</REPORT></ACTION>',
    '```xml\n<REPORT>Example only</REPORT>\n```',
    'Use `<REPORT>` for a report.',
    '<SAY><REPORT>Not a report</REPORT></SAY>',
])
def test_only_top_level_report_can_stream(text):
    assert report_prefix(text) == ('', False)


def test_abort_and_provider_reset_do_not_mix_drafts():
    events, cancel = [], [False]
    reader = ReportStream(events.append, cancelled=lambda: cancel[0])
    reader.feed('<REPORT>First provider draft details.')
    reader.reset()
    first_id = events[0][1]['stream_id']
    reader.feed('<REPORT>A fallback writes different details.')
    assert events[-1][1]['stream_id'] != first_id
    assert 'First provider' not in events[-1][1]['content']
    cancel[0] = True
    count = len(events)
    reader.feed('Ignore this aborted text.')
    assert len(events) == count
    reader.finish('<REPORT>A fallback writes different details.</REPORT>')
    assert events[-1][1]['phase'] == 'interrupted'


def test_preview_does_not_publish_ready_or_empty_headings():
    for text in ('<REPORT>Ready</REPORT>', '<REPORT>## Title\n'):
        events = []
        ReportStream(events.append).feed(text)
        assert not events


def test_streaming_is_throttled_and_final_is_explicitly_a_draft():
    events, now = [], [0.]
    reader = ReportStream(events.append, clock=lambda: now[0])
    reader.feed('<REPORT>Some useful content.')
    for _ in range(100):
        reader.feed(' More')
    assert len(events) == 1
    reader.finish('<REPORT>Some useful content. Final text.</REPORT>')
    assert events[-1][1]['phase'] == 'awaiting_review'
    assert 'Final text.' in events[-1][1]['content']


def test_partial_formula_is_not_compiled_but_code_dollars_are_preserved():
    assert preview_markdown('Useful text\n$$\nx^2') == 'Useful text'
    assert 'print' in preview_markdown('```python\nprint("$$")')


def test_concepts_are_escaped_and_not_created_inside_code():
    body = report_html.inline('Understand [[attention]] and `[[code]]` and [[<script>]].')
    assert 'data-explain="attention"' in body
    assert 'data-explain="code"' not in body
    assert '<script>' not in body


@pytest.fixture
def report_server():
    sent = []
    server = report_window.ReportServer(sent.append, token='synthetic-interaction-test')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, sent
    server.shutdown()
    server.server_close()


def post(server, body, token=True):
    request = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/explain',
                                     data=json.dumps(body).encode(), method='POST')
    if token:
        request.add_header('X-Report-Token', server.token)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_explanation_requires_auth_and_an_offered_term(report_server):
    server, sent = report_server
    server.publish(report_html.render_report('Understand [[attention]] with a query.', title='Transformers'))
    assert post(server, {'term': 'attention'}, token=False) == 403
    assert post(server, {'term': 'delete files'}) == 403
    assert not sent
    assert post(server, {'term': 'attention'}) == 200
    assert len(sent) == 1 and 'attention' in sent[0] and 'Transformers' in sent[0]
    assert 'focused REPORT' in sent[0]
    server.publish(report_html.render_report('Different report'))
    assert post(server, {'term': 'attention'}) == 403


def test_render_cache_is_scoped_and_reuses_expensive_visuals():
    cache = {}
    source = '```scene3d\n{"objects":[{"type":"box"}]}\n```'
    with mock.patch('report_media.render_scene', wraps=report_media.render_scene) as render:
        report_html.render_report(source, block_cache=cache)
        report_html.render_report(source + '\n\nMore text.', block_cache=cache)
        assert render.call_count == 1
        report_html.render_report(source)
        assert render.call_count == 2


ANIMATED = {'type': 'scene3d', 'objects': [{'type': 'box', 'animation': {
    'property': 'rotation', 'keyframes': [{'time': 0, 'value': [0, 0, 0]},
                                        {'time': 2, 'value': [0, 180, 0]}]}}]}
SEQUENCE = {'type': 'animation', 'title': 'Learning steps', 'frames': [
    {'caption': 'Observe', 'duration': .5, 'blocks': [{'type': 'markdown', 'text': 'Read the [[input]].'}]},
    {'caption': 'Compute', 'duration': .5, 'blocks': [{'type': 'diagram', 'source': 'graph LR\n A[Input] --> B[Result]'}]},
]}


@pytest.mark.parametrize('track', [
    {'property': 'javascript', 'keyframes': []},
    {'property': 'rotation', 'keyframes': [{'time': 1, 'value': [0, 0, 0]}, {'time': 0, 'value': [0, 1, 0]}]},
    {'property': 'scale', 'keyframes': [{'time': 0, 'value': [1, 1, 1]}, {'time': 1, 'value': [0, 1, 1]}]},
    {'property': 'opacity', 'keyframes': [{'time': 0, 'value': 1}, {'time': float('nan'), 'value': 0}]},
])
def test_bad_animation_is_a_visible_error(track):
    payload = {'objects': [{'type': 'box', 'animation': track}]}
    assert 'Could not render media' in report_media.render_media('scene3d', json.dumps(payload))


def test_nested_animation_is_not_unbounded():
    payload = {'frames': [{'blocks': [{'type': 'animation', 'frames': []}]}]}
    assert 'Could not render media' in report_media.render_media('animation', json.dumps(payload))


def test_markdown_cannot_bypass_animation_nesting_limit():
    nested = '```animation\n' + json.dumps(SEQUENCE) + '\n```'
    payload = {'frames': [{'blocks': [{'type': 'markdown', 'text': nested}]}]}
    assert 'nested animation is unsupported' in report_media.render_media('animation', json.dumps(payload))


def test_result_window_markdown_streams_but_other_actions_do_not():
    text = '<ACTION type="RESULT_WINDOW" action="show">Useful text\n```python\nprint(1)'
    assert 'print(1)' in report_prefix(text)[0]
    nested = '<ACTION type="RESULT_WINDOW"><REPORT>Useful content</REPORT></ACTION>'
    assert report_prefix(nested) == ('Useful content', True)
    assert report_prefix('<ACTION type="RUN_COMMAND" cmd="anything">')[0] == ''
    assert report_prefix('<ACTION type="RESULT_WINDOW" action="hide">')[0] == ''
    structured = '<ACTION type="RESULT_WINDOW">' + json.dumps({'blocks': [{'type': 'markdown', 'text': 'Hello report'}]})
    assert report_prefix(structured)[0] == 'Hello report'


def test_visual_only_documents_are_allowed_through_ui_content_gate():
    import ast
    from pathlib import Path
    from result_presenter import display_worthy
    source = ast.parse(Path('ui.py').read_text(encoding='utf-8'))
    function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'rich_blocks_have_content')
    namespace = {'display_worthy_text': display_worthy}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'ui.py', 'exec'), namespace)
    accepts = namespace['rich_blocks_have_content']
    assert accepts([ANIMATED]) and accepts([SEQUENCE])
    assert accepts([{'type': 'image', 'path': 'actual-image.png'}])
    assert not accepts([{'type': 'scene3d', 'objects': []}, {'type': 'animation', 'frames': []}])


def test_stream_backend_forwards_draft_metadata_without_launching_a_browser(monkeypatch):
    captured = []
    monkeypatch.setattr(report_window, 'show', lambda report, sink, launch, activate=True: captured.append(report))
    payload = {'stream_id': 'synthetic', 'phase': 'writing', 'content': 'Useful draft content.'}
    report_window.stream(payload, launch=False)
    assert captured[0]['phase'] == 'writing' and captured[0]['stream_id'] == 'synthetic'
    monkeypatch.setenv('VANTA_REPORT_WINDOW', '0')
    report_window.stream(payload)
    assert len(captured) == 1


def test_real_browser_animation_links_and_live_code(report_server):
    from playwright.sync_api import sync_playwright
    server, sent = report_server
    markdown, _ = report_html.blocks_from_payload({'blocks': [ANIMATED, SEQUENCE]})
    initial = 'Read about [[attention]].\n\n' + markdown + '\n\n```python\nprint('
    report = report_html.render_report(initial)
    report.update(stream_id='draft-one', phase='writing')
    server.publish(report)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1000, 'height': 1500})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
            canvas = page.locator('canvas[data-rendered=true]')
            canvas.wait_for()
            assert 'draft' in page.locator('#stream-status').inner_text()
            page.evaluate('window.originalCanvas=document.querySelector("canvas");window.originalCode=document.querySelector("pre code")')
            scene = page.locator('.scene3d')
            slider = scene.locator('input[type=range]')
            before = canvas.evaluate('c=>c.toDataURL()')
            slider.evaluate("s=>{s.value=1;s.dispatchEvent(new Event('input'))}")
            assert float(scene.get_attribute('data-time')) == 1
            assert canvas.evaluate('c=>c.toDataURL()') != before
            scene.get_by_role('button', name='Play', exact=True).click()
            page.wait_for_timeout(160)
            scene.get_by_role('button', name='Pause', exact=True).click()
            paused = scene.get_attribute('data-time')
            page.wait_for_timeout(100)
            assert scene.get_attribute('data-time') == paused
            scene.get_by_role('button', name='Restart', exact=True).click()
            assert float(scene.get_attribute('data-time')) == 0
            sequence = page.locator('.report-animation')
            sequence.locator('input').evaluate("s=>{s.value=.75;s.dispatchEvent(new Event('input'))}")
            assert sequence.get_attribute('data-frame') == '1'
            assert sequence.locator('.animation-frame:not([hidden]) svg').count() == 1
            with page.expect_response(lambda response: response.url.endswith('/api/explain')) as explanation:
                page.get_by_role('button', name='attention', exact=True).click()
            assert explanation.value.status == 200
            assert len(sent) == 1 and 'attention' in sent[0]
            page.set_viewport_size({'width': 1000, 'height': 400})
            page.wait_for_function('() => document.querySelector("#main").clientHeight===400')
            page.locator('#main').evaluate('e=>{e.style.scrollBehavior="auto";e.scrollTop=Math.floor((e.scrollHeight-e.clientHeight)/3)}')
            old_scroll = page.locator('#main').evaluate('e=>e.scrollTop')
            final = initial + '"hello")\n```'
            report = report_html.render_report(final)
            report.update(stream_id='draft-one', phase='awaiting_review')
            server.publish(report)
            page.wait_for_function('() => document.querySelector("pre code").textContent.includes("hello")')
            assert page.evaluate('window.originalCanvas===document.querySelector("canvas")')
            assert page.evaluate('window.originalCode===document.querySelector("pre code")')
            assert page.locator('#main').evaluate('e=>e.scrollTop') == old_scroll
            server.publish(report_html.render_report(final))
            page.wait_for_function('() => document.querySelector("#stream-status").textContent===""')
            assert page.evaluate('window.originalCanvas===document.querySelector("canvas")')
            server.publish(report_html.render_report('A replacement report.'))
            page.wait_for_function('() => !document.querySelector("canvas")')
            assert not errors
        finally:
            browser.close()
