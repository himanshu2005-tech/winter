# -*- coding: utf-8 -*-
import json
import threading
import unittest
import urllib.error
import urllib.request

import report_html as rh
import report_window as rw
import screen_border as sb


class Inline(unittest.TestCase):
    def test_marks(self):
        out = rh.inline("**b** *i* `c` ==m== ~~s~~")
        for tag in ("<strong>b</strong>", "<em>i</em>", "<code>c</code>", "<mark>m</mark>", "<del>s</del>"):
            self.assertIn(tag, out)

    def test_html_is_never_passed_through(self):
        out = rh.inline('<script>alert(1)</script> <img src=x onerror=alert(1)> & "q"')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_only_http_links_become_anchors(self):
        out = rh.inline("[x](javascript:alert(1)) [ok](https://a.example/p?q=1&r=2)")
        self.assertNotIn('href="javascript', out)
        self.assertIn('href="https://a.example/p?q=1&amp;r=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_code_spans_protect_their_contents(self):
        self.assertIn("<code>**not bold**</code>", rh.inline("`**not bold**`"))

    def test_long_urls_are_shortened_but_complete_in_the_href(self):
        url = "https://example.com/" + "a" * 80
        out = rh.inline(url)
        self.assertIn(f'href="{url}"', out)
        self.assertIn("...", out)


class Parsing(unittest.TestCase):
    def kinds(self, text):
        return [b["kind"] for b in rh.parse(text)]

    def test_structure(self):
        text = "# T\n\n## H\npara\n\n- a\n- b\n\n| x | y |\n|---|--:|\n| 1 | 2 |\n\n> [!warning] careful\n"
        self.assertEqual(self.kinds(text), ["heading", "heading", "para", "list", "table", "callout"])

    def test_table_alignment_and_numbers(self):
        block = rh.parse("| a | b |\n|:--|--:|\n| x | 1 |")[0]
        self.assertEqual(block["aligns"], ["left", "right"])
        self.assertIn('class="right"', rh.render_table(block))

    def test_number_columns_right_align_by_content(self):
        html = rh.render_table(rh.parse("| a | n |\n|---|---|\n| x | 10 |\n| y | 2.5 |")[0])
        self.assertIn('<td class="right">10</td>', html)

    def test_status_cells_become_badges(self):
        html = rh.render_table(rh.parse("| a | s |\n|---|---|\n| x | Pending |\n| y | Done |")[0])
        self.assertIn("badge warn", html)
        self.assertIn("badge ok", html)

    def test_labelled_numbers_become_tiles_and_text_becomes_a_list(self):
        tiles = rh.render_facts(rh.parse("Applications: 12\nInterviews: 3\nStreak: 14 days")[0])
        self.assertIn('class="tiles', tiles)
        self.assertIn('data-count="14 days"', tiles)
        facts = rh.render_facts(rh.parse("Role: Backend engineer\nCity: Chennai")[0])
        self.assertIn("<dl", facts)

    def test_todo_items_and_nesting(self):
        html = rh.render_list(rh.parse("- [x] done\n- [ ] open\n  - [ ] nested")[0])
        self.assertIn("check on", html)
        self.assertIn("todo-item done", html)
        self.assertEqual(html.count("<ul"), 2)

    def test_uppercase_line_is_a_heading_but_a_sentence_is_not(self):
        self.assertEqual(self.kinds("TODAY\n- a")[0], "heading")
        self.assertEqual(self.kinds("Chennai is clear today.\nmore")[0], "para")

    def test_bold_only_line_is_a_heading(self):
        self.assertEqual(self.kinds("**Email**\n- a")[0], "heading")

    def test_progress(self):
        html, _ = rh.render_blocks(rh.parse("[progress 72] Readiness"))
        self.assertIn("--w:72%", html)

    def test_empty_input(self):
        self.assertEqual(rh.parse(""), [])
        self.assertIn("Nothing to show", rh.render_report("")["html"])

    def test_latex_is_rendered_locally_as_formula_not_raw_prose(self):
        blocks = rh.parse("$$\\frac{x^2}{2}$$")
        self.assertEqual(blocks[0]["kind"], "math")
        rendered = rh.render_math(blocks[0]["text"])
        self.assertIn('class="formula', rendered)
        self.assertTrue("data:image/svg+xml;base64," in rendered
                        or "formula-source" in rendered)

    def test_multiline_and_fenced_latex(self):
        self.assertEqual(rh.parse("$$\na+b=c\n$$")[0]["text"], "a+b=c")
        self.assertEqual(rh.parse("```math\nx+y\n```")[0]["kind"], "math")


class Charts(unittest.TestCase):
    def test_columns_lines_pie(self):
        body = "categories: A, B, C\nS: 1, 2, 3"
        for kind in ("column", "line", "area", "bar", "pie", "donut"):
            html = rh.render_chart(kind + " Title", body)
            self.assertIn("<svg", html, kind)
            self.assertIn("Title", html)

    def test_thousands_separators_stay_whole(self):
        _, _, _, series = rh.parse_chart("column", "categories: A, B\nRev: $1,200, 1,500")
        self.assertEqual(series[0][1], [1200.0, 1500.0])

    def test_empty_chart_falls_back_to_code(self):
        block = rh.parse("```chart column\nnothing\n```")[0]
        self.assertIn('class="code', rh.render_fence(block))

    def test_zero_values_do_not_divide_by_zero(self):
        self.assertIn("<svg", rh.render_chart("column", "categories: A, B\nS: 0, 0"))
        self.assertIn("<svg", rh.render_chart("pie", "categories: A\nS: 0") or "<svg")


class Diagrams(unittest.TestCase):
    FLOW = "graph LR\n A[Apply] --> B{OK?}\n B -->|yes| C[Test]\n B -->|no| D((Archive))\n C --> E([Offer])"

    def test_flow_parses_nodes_edges_labels(self):
        direction, nodes, edges = rh.parse_flow(self.FLOW)
        self.assertEqual(direction, "LR")
        self.assertEqual(set(nodes), {"A", "B", "C", "D", "E"})
        self.assertIn(("B", "C", "yes", "solid"), edges)
        self.assertEqual(nodes["B"][1], "diamond")

    def test_chains(self):
        _, nodes, edges = rh.parse_flow("graph TD\n A --> B --> C")
        self.assertEqual([(a, b) for a, b, *_ in edges], [("A", "B"), ("B", "C")])

    def test_cycles_do_not_hang(self):
        html = rh.render_flow("graph TD\n A --> B\n B --> C\n C --> A")
        self.assertIn("<svg", html)

    def test_node_boxes_do_not_overlap(self):
        _, nodes, edges = rh.parse_flow("graph TD\n A --> B\n A --> C\n A --> D\n B --> E")
        positions, w, h = rh._layout(nodes, edges, "TD")
        boxes = list(positions.values())
        for i, (x1, y1, w1, h1) in enumerate(boxes):
            for x2, y2, w2, h2 in boxes[i + 1:]:
                overlap = x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1
                self.assertFalse(overlap)

    def test_labels_are_escaped(self):
        html = rh.render_flow('graph TD\n A["<img src=x onerror=alert(1)>"] --> B')
        self.assertNotIn("<img", html)

    def test_sequence_and_mermaid_pie(self):
        seq = rh.render_sequence("sequenceDiagram\n participant A\n participant B\n A->>B: hi\n B-->>A: yo")
        self.assertIn("hi", seq)
        pie = rh.render_mermaid_pie('pie title Split\n "A": 60\n "B": 40')
        self.assertIn("<svg", pie)

    def test_unknown_mermaid_shows_as_code_not_nothing(self):
        block = rh.parse("```mermaid\ngantt\n title x\n```")[0]
        self.assertIn('class="code', rh.render_fence(block))

    def test_structured_diagram_payload(self):
        text, _ = rh.blocks_from_payload({"blocks": [{"type": "diagram",
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "edges": [{"from": "a", "to": "b", "label": "go"}]}]})
        self.assertIn("<svg", rh.render_report(text)["html"])

    def test_oversized_graphs_fall_back(self):
        source = "graph TD\n" + "\n".join(f"N{i} --> N{i + 1}" for i in range(60))
        self.assertIsNone(rh.render_flow(source))


class Payloads(unittest.TestCase):
    def test_rich_blocks_and_action_buttons(self):
        text, actions = rh.blocks_from_payload({"blocks": [
            {"type": "markdown", "text": "# Hi"},
            {"type": "table", "headers": ["a", "b"], "rows": [[1, 2]]},
            {"type": "code", "language": "py", "text": "x=1"},
            {"type": "actions", "items": [{"label": "Go", "command": "open it"}, {"label": "no command"}]}]})
        self.assertIn("| a | b |", text)
        self.assertEqual(actions, [{"label": "Go", "command": "open it"}])
        page = rh.render_report(text, "T", actions)
        self.assertIn('data-command="open it"', page["html"])

    def test_leading_h1_becomes_the_title_not_a_duplicate_heading(self):
        page = rh.render_report("# Quarterly Plan\n## Goals\ntext", "VANTA Result")
        self.assertEqual(page["title"], "Quarterly Plan")
        self.assertNotIn("<h2 id=\"quarterly-plan\"", page["html"])

    def test_toc_lists_only_top_levels(self):
        page = rh.render_report("## A\nx\n### B\ny\n#### C\nz")
        self.assertEqual([t["text"] for t in page["toc"]], ["A", "B"])

    def test_rich_formula_payload_joins_tables_and_diagrams(self):
        text, _ = rh.blocks_from_payload({"blocks": [
            {"type": "math", "latex": r"E=mc^2"},
            {"type": "table", "headers": ["A"], "rows": [["B"]]},
            {"type": "diagram", "source": "graph LR\nA --> B"},
        ]})
        kinds = [block["kind"] for block in rh.parse(text)]
        self.assertEqual(kinds, ["math", "table", "fence"])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sent = []
        cls.server = rw.ReportServer(cls.sent.append)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.port}"
        cls.server.publish(rh.render_report("# T\nhello", "T",
                                            [{"label": "Go", "command": "open it"}]))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path, token=True, host=None, method="GET", body=None, header_token=None):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
        if token:
            request.add_header("X-Report-Token", header_token or self.server.token)
        if host:
            request.add_header("Host", host)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_page_needs_the_token(self):
        self.assertEqual(self.req("/", token=False)[0], 403)
        status, body, headers = self.req(f"/?t={self.server.token}", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"VANTA", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_data_needs_the_header_and_a_clean_host(self):
        self.assertEqual(self.req("/api/report", token=False)[0], 403)
        self.assertEqual(self.req(f"/api/report?t={self.server.token}", token=False)[0], 403)
        self.assertEqual(self.req("/api/report", host="evil.example")[0], 421)

    def test_report_and_version(self):
        status, body, _ = self.req("/api/report")
        self.assertEqual(json.loads(body)["title"], "T")
        before = json.loads(self.req("/api/version")[1])["version"]
        self.server.publish(rh.render_report("x", "U"))
        self.assertGreater(json.loads(self.req("/api/version")[1])["version"], before)
        self.server.publish(rh.render_report("# T\nhello", "T", [{"label": "Go", "command": "open it"}]))

    def test_only_an_offered_command_can_be_sent(self):
        self.assertEqual(self.req("/api/command", method="POST", body={"command": "open it"})[0], 200)
        self.assertEqual(self.sent, ["open it"])
        self.assertEqual(self.req("/api/command", method="POST", body={"command": "delete everything"})[0], 403)
        self.assertEqual(self.req("/api/command", method="POST", token=False, body={"command": "open it"})[0], 403)
        self.assertEqual(self.sent, ["open it"])

    def test_other_methods_are_refused(self):
        self.assertEqual(self.req("/api/report", method="DELETE")[0], 405)

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class Window(unittest.TestCase):
    def test_geometry_is_on_screen_for_common_sizes(self):
        for sw, sh in ((1920, 1080), (1366, 768), (2560, 1440), (1280, 720)):
            for position in ("right", "center", "left"):
                w, h, x, y = rw.window_geometry(sw, sh, position)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, sw, (sw, sh, position))
                self.assertLessEqual(y + h, sh, (sw, sh, position))

    def test_default_sits_on_the_right(self):
        w, h, x, y = rw.window_geometry(1920, 1080, "right")
        self.assertGreater(x, 1920 // 2)

    def test_showing_without_launching_publishes(self):
        info = rw.present("# A\nb", "A", launch=False)
        self.assertFalse(info["launched"])
        self.assertIn("127.0.0.1", info["url"])
        rw.shutdown()


class Border(unittest.TestCase):
    def test_priorities(self):
        now = 100.0
        self.assertEqual(sb.choose("thinking", now, error_until=now + 1), "error")
        self.assertEqual(sb.choose("thinking", now), "thinking")
        self.assertEqual(sb.choose("thinking", now, action_until=now + 1), "acting")
        self.assertEqual(sb.choose("listening", now), "listening")
        self.assertEqual(sb.choose("speaking", now), "speaking")
        self.assertEqual(sb.choose("idle", now, action_until=now + 1), "acting")

    def test_idle_has_no_border_but_a_protocol_does(self):
        self.assertIsNone(sb.choose("idle", 1.0))
        self.assertEqual(sb.choose("idle", 1.0, protocol="lockdown"), "lockdown")
        self.assertEqual(sb.choose("idle", 1.0, protocol="owner_override"), "owner_override")
        self.assertIsNone(sb.choose("idle", 1.0, protocol="medical_emergency"))

    def test_a_resting_screen_stays_dark(self):
        self.assertIsNone(sb.choose("thinking", 1.0, resting=True))

    def test_every_mode_has_a_colour(self):
        for mode in ("listening", "thinking", "acting", "speaking", "error",
                     "owner_override", "public_space", "lockdown"):
            self.assertRegex(sb.COLORS[mode], r"^#[0-9a-f]{6}$")
        self.assertEqual(len(set(sb.COLORS.values())), len(sb.COLORS))

    def test_expiry(self):
        self.assertEqual(sb.choose("idle", 10.0, action_until=11.0), "acting")
        self.assertIsNone(sb.choose("idle", 12.0, action_until=11.0))

    def test_perimeter_points_stay_on_the_edge_and_wrap(self):
        for p in (0.0, 0.1, 0.25, 0.5, 0.75, 0.99, 1.3, -0.2):
            x, y = sb.perimeter_point(p, 1920, 1080)
            on_edge = x in (2, 1918) or y in (2, 1078) or \
                (abs(x - 2) < 1e-6 or abs(x - 1918) < 1e-6 or abs(y - 2) < 1e-6 or abs(y - 1078) < 1e-6)
            self.assertTrue(on_edge, (p, x, y))
            self.assertTrue(0 <= x <= 1920 and 0 <= y <= 1080)
        self.assertEqual(sb.perimeter_point(0.0, 100, 100), sb.perimeter_point(1.0, 100, 100))

    def test_comet_runs_tail_to_head(self):
        points = sb.comet(0.5, 1920, 1080)
        self.assertEqual(points[-1], sb.perimeter_point(0.5, 1920, 1080))
        self.assertGreater(len(points), 10)


class Wiring(unittest.TestCase):
    def test_ui_uses_the_web_report_with_a_fallback_and_the_new_border(self):
        import inspect
        import ui
        source = inspect.getsource(ui.WinterUI)
        self.assertIn("_web_report(payload)", source)
        self.assertIn("using the built-in window", source)
        self.assertIn("self._draw_state_border()", source)
        self.assertIn("self._action_until", source)
        self.assertIn("self._error_until", source)

    def test_the_report_window_can_be_switched_off(self):
        import inspect
        import ui
        self.assertIn("VANTA_REPORT_WINDOW", inspect.getsource(ui.WinterUI._web_report))


if __name__ == "__main__":
    unittest.main()
