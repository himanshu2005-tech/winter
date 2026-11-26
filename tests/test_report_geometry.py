"""Generic geometry and a compound drone example; no drone-specific runtime code."""
import json
import math
import threading

import pytest

import report_geometry
import report_html
import report_media
from report_window import ReportServer


def drone_scene():
    parts = [{'type': 'ellipsoid', 'radii': [.65, .22, .45], 'color': '#53657d', 'label': 'Body'},
             {'type': 'sphere', 'radius': .11, 'position': [0, -.22, -.4], 'color': '#75baff'}]
    for x, z in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
        parts.extend([
            {'type': 'box', 'size': [1.3, .08, .12], 'rotation': [0, -z*x*45, 0],
             'position': [x*.48, 0, z*.48], 'color': '#72849b'},
            {'type': 'cylinder', 'radius': .14, 'height': .22, 'position': [x, .1, z], 'color': '#4e5d72'},
            {'type': 'torus', 'radius': .36, 'tube': .025, 'position': [x, .25, z], 'color': '#75baff'},
            {'type': 'box', 'size': [.68, .025, .08], 'position': [x, .25, z], 'color': '#bed3e8',
             'animation': {'property': 'rotation', 'keyframes': [
                 {'time': 0, 'value': [0, 0, 0]}, {'time': 4, 'value': [0, 1440, 0]}]}},
        ])
    return {'title': 'Sample drone', 'objects': [
        {'type': 'group', 'rotation': [0, 15, 0], 'children': parts}]}


@pytest.mark.parametrize('kind,options', [
    ('cylinder', {}), ('cone', {}), ('torus', {}), ('ellipsoid', {}), ('plane', {}),
    ('CylinderGeometry', {'radiusTop': .2, 'radiusBottom': .4}),
    ('cube', {'width': 2, 'height': .3, 'depth': 1}),
])
def test_primitives_have_finite_vertices_and_valid_faces(kind, options):
    data = report_media.scene_data({'objects': [dict(type=kind, **options)]})
    mesh = data['objects'][0]
    assert mesh['faces'] and all(math.isfinite(v) for p in mesh['vertices'] for v in p)
    assert all(0 <= i < len(mesh['vertices']) for face in mesh['faces'] for i in face)


def test_nested_transforms_apply_child_then_parent():
    data = report_media.scene_data({'objects': [{'type': 'group', 'position': [10, 0, 0],
            'rotation': [0, 0, 90], 'children': [{'type': 'line', 'position': [1, 0, 0],
                'scale': [2, 1, 1], 'points': [[0, 0, 0], [1, 0, 0]]}]}]})
    a, b = data['objects'][0]['vertices']
    assert a == pytest.approx([10, 1, 0])
    assert b == pytest.approx([10, 3, 0])


@pytest.mark.parametrize('obj', [
    {'type': 'cylinder', 'height': -1}, {'type': 'cylinder', 'segments': 10000},
    {'type': 'cone', 'radiusTop': -1}, {'type': 'torus', 'radius': .1, 'tube': .2},
    {'type': 'plane', 'width': float('nan')}, {'type': 'ellipsoid', 'radii': [1, 0, 1]},
    {'type': 'group', 'children': []}, {'type': 'box', 'scale': [0, 1, 1]},
])
def test_bad_dimensions_and_groups_are_refused(obj):
    assert 'Could not render media' in report_media.render_media('scene3d', json.dumps({'objects': [obj]}))


def test_drone_is_built_from_generic_parts_and_fits_limits():
    data = report_media.scene_data(drone_scene())
    assert len(data['objects']) == 18
    assert sum(len(o['vertices']) for o in data['objects']) < 8000
    assert data['duration'] == 4
    markdown = '```scene3d\n' + json.dumps(drone_scene()) + '\n```'
    assert not report_html.validate_scenes(markdown)
    assert 'Could not render media' not in report_html.render_report(markdown)['html']


def test_unknown_object_type_returns_a_repairable_contract_error():
    text = '```scene3d\n{"objects":[{"type":"spaceship"}]}\n```'
    errors = report_html.validate_scenes(text)
    assert len(errors) == 1 and 'spaceship' in errors[0]
    assert 'label, not a primitive type' in errors[0]
    assert all(kind in report_geometry.contract() for kind in report_geometry.TYPES)


def test_group_does_not_bypass_scene_limits():
    payload = {'objects': [{'type': 'group', 'children': [{'type': 'box'}]*128}]}
    with pytest.raises(ValueError, match='128 objects'):
        report_media.scene_data(payload)


def test_real_browser_renders_drone_and_propeller_animation():
    from playwright.sync_api import sync_playwright
    source = '```scene3d\n' + json.dumps(drone_scene()) + '\n```'
    server = ReportServer(token='synthetic-drone-check')
    server.publish(report_html.render_report(source))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1000, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
                canvas = page.locator('canvas[data-rendered=true]')
                canvas.wait_for()
                assert 'Could not render media' not in page.locator('#body').inner_text()
                assert page.locator('.scene3d').evaluate('f=>JSON.parse(f.dataset.scene).objects.length') == 18
                before = canvas.evaluate('c=>c.toDataURL()')
                page.locator('.scene3d input').evaluate("s=>{s.value=.4;s.dispatchEvent(new Event('input'))}")
                assert canvas.evaluate('c=>c.toDataURL()') != before
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
