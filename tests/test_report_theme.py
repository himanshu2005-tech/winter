"""Render the equations from the reported contrast defect in both actual themes."""
import io
import threading
import json

from PIL import Image
from playwright.sync_api import sync_playwright

import report_html
from report_window import ReportServer


SOURCE = r"""# Drone basics
## Key equations
Hover condition — thrust must equal weight.

$$T = m \cdot g$$

Thrust-to-weight ratio:

$$\text{TWR} = \frac{T_{\max}}{m \cdot g}$$

Explore [[thrust]] and [[weight]].
"""


def test_formula_does_not_expose_duplicate_source_caption():
    markup = report_html.render_math(r'T=m\cdot g')
    assert 'data:image/svg+xml;base64,' in markup
    assert '<figcaption>' not in markup
    assert 'alt="T=m\\cdot g"' in markup


def test_real_browser_formula_transparency_dark_light_and_print(tmp_path):
    image_path = tmp_path / 'blue.png'
    Image.new('RGB', (20, 20), (0, 90, 220)).save(image_path)
    source = SOURCE + '\n```image\n{"path":' + json.dumps(str(image_path)) + '}\n```'
    server = ReportServer(token='synthetic-theme-check')
    server.publish(report_html.render_report(source))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1000, 'height': 1000})
                page.goto(f'http://127.0.0.1:{server.port}/?t={server.token}&static=1')
                page.wait_for_function('() => document.querySelectorAll(".formula img").length===2 && [...document.querySelectorAll("img")].every(i=>i.complete && i.naturalWidth>0)')
                assert page.locator('.formula-source').count() == 0
                formula = page.locator('.formula img').first
                # Check the SVG bitmap itself: its corner is transparent, not
                # the white rectangle that hid the light text in the screenshot.
                assert formula.evaluate('''i=>{let c=document.createElement('canvas');c.width=i.naturalWidth;c.height=i.naturalHeight;
                  let x=c.getContext('2d');x.drawImage(i,0,0);return x.getImageData(0,0,1,1).data[3]}''') == 0
                assert formula.evaluate('i=>getComputedStyle(i).filter') == 'invert(1)'
                dark = Image.open(io.BytesIO(formula.screenshot())).convert('RGB')
                assert sum(min(dark.getpixel((x, y)))>180 for x in range(dark.width) for y in range(dark.height)) > 30
                assert page.locator('.report-image img').evaluate('i=>getComputedStyle(i).filter') == 'none'
                page.get_by_role('button', name='Theme', exact=True).click()
                assert formula.evaluate('i=>getComputedStyle(i).filter') == 'none'
                light = Image.open(io.BytesIO(formula.screenshot())).convert('RGB')
                assert sum(max(light.getpixel((x, y)))<80 for x in range(light.width) for y in range(light.height)) > 30
                assert page.locator('.explain-term').first.evaluate('e=>getComputedStyle(e).color') == 'rgb(36, 106, 195)'
                page.reload()
                page.wait_for_function('() => document.querySelector(".formula img")?.complete')
                assert page.locator('html').get_attribute('data-theme') == 'light'
                page.get_by_role('button', name='Theme', exact=True).click()
                page.emulate_media(media='print')
                assert formula.evaluate('i=>getComputedStyle(i).filter') == 'none'
                assert page.locator('.formula').first.evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(255, 255, 255)'
                assert page.locator('#body p').first.evaluate('e=>getComputedStyle(e).color') == 'rgb(28, 28, 30)'
                pdf = page.pdf(print_background=True)
                assert pdf.startswith(b'%PDF')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
