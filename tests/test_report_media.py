"""Real report renderer + isolated local browser, with no external data or accounts."""
import json
import threading

import pytest
from PIL import Image

import report_html
import report_media
from report_window import ReportServer


SCENE = {"title": "Network", "objects": [
    {"type": "box", "label": "Input", "position": [-1, 0, 0]},
    {"type": "sphere", "label": "Output", "position": [1, 0, 0]},
    {"type": "line", "points": [[-1, 0, 0], [1, 0, 0]]},
]}


def test_structured_media_and_existing_formats_together(tmp_path):
    path = tmp_path / 'sample.png'
    Image.new('RGB', (40, 30), 'red').save(path)
    payload = {"blocks": [
        {"type": "image", "path": str(path), "caption": '<b>Sample</b>'},
        dict(SCENE, type='scene3d'),
        {"type": "table", "headers": ["Layer", "Units"], "rows": [["Input", 4]]},
        {"type": "math", "latex": r'E=mc^2'},
        {"type": "diagram", "nodes": [{"id": "a", "label": "Input"}, {"id": "b", "label": "Output"}],
         "edges": [{"from": "a", "to": "b"}]},
    ]}
    markdown, _ = report_html.blocks_from_payload(payload)
    body = report_html.render_report(markdown)['html']
    for expected in ('data:image/png;base64,', 'data-scene=', '<canvas', '<table', 'class="diagram', 'data:image/svg+xml;base64,'):
        assert expected in body
    assert '&lt;b&gt;Sample&lt;/b&gt;' in body
    assert '<b>Sample</b>' not in body


def test_standalone_markdown_image(tmp_path):
    path = tmp_path / 'space (1).jpg'
    Image.new('RGB', (8, 8)).save(path)
    body = report_html.render_report(f'Before\n![Photo]({path})\nAfter')['html']
    assert 'report-image' in body and 'alt="Photo"' in body
    assert 'Before' in body and 'After' in body


def test_missing_image_is_visible_error():
    body = report_media.render_media('image', '{"path":"missing-file.png"}')
    assert 'Could not render media' in body and '<img' not in body


@pytest.mark.parametrize('source', ['javascript:alert(1)', 'http://localhost/private',
                                   'data:image/svg+xml;base64,PHN2Zz4=',
                                   'https://user:password@example.com/p.png'])
def test_unsafe_image_source_refused(source):
    assert 'Could not render media' in report_media.render_media('image', json.dumps({'src': source}))


def test_https_image_is_escaped_and_not_fetched_by_server():
    body = report_media.render_image({'src': 'https://example.com/a.png?a=1&b=2', 'caption': '"<script>'})
    assert 'a=1&amp;b=2' in body and 'referrerpolicy="no-referrer"' in body
    assert '<script>' not in body


def test_obj_mesh_and_negative_indices(tmp_path):
    obj = tmp_path / 'triangle.obj'
    obj.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n')
    scene = report_media.scene_data({'objects': [{'type': 'mesh', 'path': str(obj)}]})
    assert scene['objects'][0]['faces'] == [[0, 1, 2]]


@pytest.mark.parametrize('payload', [
    {'objects': []}, {'objects': [{'type': 'unknown'}]},
    {'objects': [{'type': 'box', 'position': [float('nan'), 0, 0]}]},
    {'objects': [{'type': 'box', 'size': [-1, 1, 1]}]},
    {'objects': [{'type': 'sphere', 'radius': 0}]},
    {'objects': [{'type': 'box', 'color': 'red;alert(1)'}]},
    {'objects': [{'type': 'mesh', 'vertices': [[0, 0, 0]], 'faces': [[0, 1, 2]]}]},
    {'objects': [{'type': 'mesh', 'vertices': [[0, 0, 0]]}]},
    {'objects': [{'type': 'mesh', 'vertices': [[0, 0, 0]] * 8001}]},
])
def test_invalid_scene_has_honest_error(payload):
    assert 'Could not render media' in report_media.render_media('scene3d', json.dumps(payload))


def test_labels_cannot_inject_markup():
    body = report_media.render_scene({'objects': [{'type': 'box', 'label': '</figure><script>alert(1)</script>'}]})
    assert '<script>' not in body and '&lt;script&gt;' in body


def test_real_browser_draws_and_interacts(tmp_path):
    from playwright.sync_api import sync_playwright
    image = tmp_path / 'local.png'
    Image.new('RGB', (50, 30), 'blue').save(image)
    markdown, _ = report_html.blocks_from_payload({'blocks': [
        dict(SCENE, type='scene3d'), {'type': 'image', 'path': str(image)},
        {'type': 'diagram', 'source': 'graph LR\n A[Input] --> B[Output]'},
    ]})
    server = ReportServer(token='synthetic-report-test')
    server.publish(report_html.render_report(markdown))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 900, 'height': 950})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
            canvas = page.locator('canvas[data-rendered=true]')
            canvas.wait_for()
            page.wait_for_function("() => document.querySelector('.report-image img').naturalWidth > 0")
            assert page.locator('.diagram svg').count() == 1
            # Verify actual painted pixels, not just a claimed render flag.
            assert canvas.evaluate("c=>{let d=c.getContext('2d').getImageData(c.width/2,c.height/2,1,1).data;return d[0]!==12||d[1]!==16||d[2]!==22}")
            initial = canvas.get_attribute('data-yaw')
            canvas.focus()
            canvas.press('ArrowRight')
            assert canvas.get_attribute('data-yaw') != initial
            box = canvas.bounding_box()
            page.mouse.move(box['x'] + 80, box['y'] + 80)
            page.mouse.down()
            page.mouse.move(box['x'] + 140, box['y'] + 100)
            page.mouse.up()
            assert canvas.get_attribute('data-yaw') != initial
            page.mouse.wheel(0, -100)
            page.wait_for_timeout(100)
            assert float(canvas.get_attribute('data-zoom')) > 1
            canvas.press('+')
            assert float(canvas.get_attribute('data-zoom')) > 1
            page.get_by_role('button', name='Wireframe', exact=True).click()
            assert page.get_by_role('button', name='Wireframe').get_attribute('aria-pressed') == 'true'
            page.get_by_role('button', name='Reset view').click()
            assert canvas.get_attribute('data-yaw') == initial
            assert canvas.get_attribute('data-zoom') == '1'
            page.set_viewport_size({'width': 430, 'height': 850})
            page.wait_for_timeout(150)
            assert canvas.bounding_box()['width'] < 430
            # A broken remote source must leave a readable error, never an empty figure.
            page.route('https://images.example.invalid/**', lambda route: route.abort())
            server.publish(report_html.render_report(
                '```image\n{"src":"https://images.example.invalid/missing.png"}\n```'))
            page.locator('.image-error:not([hidden])').wait_for()
            server.publish(report_html.render_report('## Replacement\nNo leftover viewers.'))
            page.wait_for_function("() => !document.querySelector('canvas')")
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
