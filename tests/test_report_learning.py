"""Composable visual learning, with genuine parameter changes in a local browser."""
import copy
import json
import threading

import pytest
from playwright.sync_api import sync_playwright

import report_html
import report_learning as learning
import report_media
from report_window import ReportServer


EXPLORER = {'type': 'explorer', 'title': 'Change the curve',
    'parameters': [{'id': 'a', 'label': 'Curvature a', 'min': 0, 'max': 4, 'step': .1, 'value': 1}],
    'plot': {'x_range': [-3, 3], 'y_range': [0, 12], 'x_label': 'x', 'y_label': 'f(x)'},
    'curves': [{'label': 'f(x) = ax²', 'expression': 'a*x**2'}],
    'shapes': [{'type': 'arrow', 'x': 0, 'y': 0, 'x2': 1, 'y2': 'a', 'label': 'At x = 1'}],
    'outputs': [{'label': 'Second derivative', 'expression': '2*a'}]}
WALK = {'type': 'walkthrough', 'title': 'Follow the running sum',
    'code': 'total = 0\nfor n in [1, 2]:\n    total += n', 'steps': [
        {'title': 'Initialize', 'explanation': 'Start at zero.', 'lines': [1], 'variables': {'total': 0}},
        {'title': 'Add the first value', 'explanation': 'Add one.', 'lines': [2, 3], 'variables': {'n': 1, 'total': 1}},
        {'title': 'Add the second value', 'lines': [3], 'variables': {'n': 2, 'total': 3}}]}
FLASH = {'type': 'lesson', 'layout': 'flashcards', 'title': 'Recall the idea', 'sections': [
    {'title': 'What changes when a grows?', 'blocks': [{'type': 'markdown', 'text': 'The [[curvature]] increases.'}]}]}
LESSON = {'type': 'lesson', 'title': 'Learn by exploring', 'sections': [
    {'title': 'Intuition', 'blocks': [{'type': 'markdown', 'text': 'Change a parameter and observe the effect.'},
                                  {'type': 'math', 'latex': 'f(x)=ax^2'}]},
    {'title': 'Explore', 'blocks': [EXPLORER]}, {'title': 'Code', 'blocks': [WALK]},
    {'title': 'Recall', 'blocks': [FLASH]},
    {'title': 'Check yourself', 'blocks': [{'type': 'questionnaire', 'title': 'Reflect', 'questions': [
        {'id': 'idea', 'type': 'text', 'label': 'What did you notice?'}]}]}]}


def document(payload):
    text, _ = report_html.blocks_from_payload({'blocks': [payload]})
    return text, report_html.render_report(text)


@pytest.mark.parametrize('source', ['__import__("os")', 'x.__class__', '[x for x in range(4)]',
                                    '(lambda: 1)()', 'globals()', 'x[0]', 'open("secret")',
                                    'a + unknown', 'sin(x, 2)', 'True', '"string"'])
def test_expression_grammar_refuses_executable_or_unknown_constructs(source):
    with pytest.raises(ValueError):
        learning.expression(source, {'a','x','t','pi','e'})


@pytest.mark.parametrize('source', ['max(0, x)', 'sin(x-pi)*a', 'x if x>0 else 0',
                                    'sqrt(abs(x))', 'exp(-x**2)', 'a*x + 2', 'x//2', 'x%2',
                                    '1/(1+exp(-x))', 'exp(-x**2/2)/sqrt(2*pi)',
                                    '(1-a)**100', 'exp(a)/(exp(a)+exp(x))'])
def test_documented_mathematics_compiles_to_data(source):
    tree = learning.expression(source, {'a','x','t','pi','e'})
    assert isinstance(tree, list)


def test_composed_lesson_preflights_and_keeps_nested_quiz_active():
    source, report = document(LESSON)
    assert not report_html.validate_scenes(source)
    assert len(report['forms']) == 1
    for feature in ('report-explorer', 'report-walkthrough', 'data-layout="flashcards"', 'questionnaire'):
        assert feature in report['html']
    assert 'Could not render media' not in report['html']


@pytest.mark.parametrize('payload', [
    {'type':'explorer', 'curves':[{'expression':'x'}], 'parameters':[{'id':'x'}]},
    {'type':'explorer', 'curves':[{'expression':'x'}], 'parameters':[{'id':'__proto__'}]},
    {'type':'explorer', 'curves':[{'expression':'x'}], 'plot':{'x_range':[1,1]}},
    {'type':'explorer', 'curves':[]},
    {'type':'lesson', 'sections':[]},
    {'type':'lesson', 'sections':[{'title':'Bad','blocks':[{'type':'unsupported'}]}]},
    {'type':'walkthrough','code':'x=1','steps':[{'lines':[9]}]},
])
def test_invalid_learning_content_produces_repairable_validation(payload):
    source, _ = document(payload)
    assert report_html.validate_scenes(source)


def test_deep_nested_lesson_is_bounded():
    payload = {'type':'markdown','text':'End'}
    for i in range(8):
        payload = {'type':'lesson','sections':[{'title':str(i),'blocks':[payload]}]}
    source, _ = document(payload)
    assert report_html.validate_scenes(source)


def test_real_browser_learning_changes_values_and_explains_step_by_step():
    source, report = document(LESSON)
    report.update(stream_id='learning-test', phase='writing')
    server = ReportServer(token='synthetic-learning-test')
    server.publish(report)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width':1000,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
                page.get_by_role('tab',name='Explore',exact=True).click()
                canvas = page.locator('.report-explorer canvas[data-rendered=true]')
                canvas.wait_for()
                old = canvas.evaluate('c=>c.toDataURL()')
                page.get_by_role('slider',name='Curvature a',exact=True).evaluate("s=>{s.value=2;s.dispatchEvent(new Event('input'))}")
                assert page.locator('.explorer-outputs strong').inner_text() == '4'
                assert canvas.evaluate('c=>c.toDataURL()') != old
                # The same interpreter handles piecewise curves and omits
                # mathematically undefined values rather than making them up.
                assert page.evaluate('(n)=>reportEvaluate(n,{x:-2})',learning.expression('x if x>0 else 0',{'x'})) == 0
                assert page.evaluate('(n)=>reportEvaluate(n,{x:-2})',learning.expression('sqrt(x)',{'x'})) is None
                assert page.evaluate('(n)=>reportEvaluate(n,{x:-3})',learning.expression('x%2',{'x'})) == 1
                assert page.evaluate('(n)=>reportEvaluate(n,{a:0.1})',learning.expression('(1-a)**100',{'a'})) == pytest.approx(.9**100)
                page.get_by_role('tab',name='Code',exact=True).click()
                assert page.locator('.walk-code li.active').count() == 1
                page.get_by_role('button',name='Next step',exact=True).click()
                assert page.locator('.walk-code li.active').count() == 2
                assert 'total' in page.locator('.walk-step:not([hidden])').inner_text()
                page.get_by_role('button',name='Next step',exact=True).click()
                assert page.locator('.walk-step:not([hidden]) .walk-state').inner_text().endswith('total\t3')
                page.get_by_role('tab',name='Recall',exact=True).click()
                card=page.locator('.report-lesson[data-layout=flashcards] details')
                assert card.get_attribute('open') is None
                card.locator('summary').click()
                assert card.locator('.lesson-content').is_visible()
                page.get_by_role('tab',name='Intuition',exact=True).focus()
                page.keyboard.press('ArrowRight')
                assert page.get_by_role('tab',name='Explore',exact=True).get_attribute('aria-selected')=='true'
                page.evaluate('window.learningCanvas=document.querySelector(".report-explorer canvas")')
                update=report_html.render_report(source+'\n\nAn additional explanation.')
                update.update(stream_id='learning-test',phase='writing')
                server.publish(update)
                page.get_by_text('An additional explanation.',exact=True).wait_for()
                assert page.evaluate('window.learningCanvas===document.querySelector(".report-explorer canvas")')
                assert page.get_by_role('slider',name='Curvature a',exact=True).input_value()=='2'
                page.get_by_role('button',name='Theme',exact=True).click()
                page.wait_for_function('() => document.documentElement.dataset.theme==="light"')
                assert page.locator('.report-explorer canvas').evaluate('c=>JSON.parse(c.dataset.parameters).a') == 2
                server.publish(report_html.render_report('Replacement content.'))
                page.wait_for_function('() => !document.querySelector(".report-explorer")')
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown();server.server_close()


def test_animated_explorer_uses_time_and_reports_domain_errors():
    payload={'type':'explorer','duration':2,'curves':[{'label':'Wave','expression':'sin(x-t)'}],
             'outputs':[{'label':'Time','expression':'t'},{'label':'Undefined','expression':'1/0'}]}
    _,report=document(payload)
    server=ReportServer(token='synthetic-wave-test');server.publish(report)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page();page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
                page.locator('.report-explorer canvas[data-rendered=true]').wait_for()
                assert 'undefined' in page.locator('.explorer-status').inner_text()
                page.get_by_role('slider',name='Animation time',exact=True).evaluate("s=>{s.value=1;s.dispatchEvent(new Event('input'))}")
                assert page.locator('.explorer-outputs strong').first.inner_text()=='1'
            finally:
                browser.close()
    finally:
        server.shutdown();server.server_close()
