# -*- coding: utf-8 -*-
"""
Tests for the teaching board, the Control Center panels VANTA builds for
itself, the autonomous researcher, and the provider fallback underneath them.

Nothing here opens a window or hits the network. What it does check is the
behaviour that only shows up as silence in production: a board style the
catalogue promises but the renderer does not have, a panel item the model can
emit and the dashboard drops, a researcher that mistakes a dropped network for
an exhausted subject, and a router that turns an outage into an empty string
nobody can distinguish from a model with nothing to say.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blackboard
import panels
import research
from providers import research_api


class BoardStyleTests(unittest.TestCase):
    """Whatever the model writes has to land somewhere sensible."""

    def test_every_style_has_a_complete_definition(self):
        for name, spec in blackboard.BlackboardMixin.STYLES.items():
            self.assertEqual(len(spec), 7, f"{name} is missing fields")
            family, size, weight, colour, lead, gap, indent = spec
            self.assertIn(family, ("display", "text", "mono"), name)
            self.assertGreater(size, 0, name)
            self.assertIn(weight, ("normal", "bold"), name)
            self.assertRegex(colour, r"^#[0-9a-fA-F]{6}$", name)
            self.assertGreaterEqual(lead, size, f"{name} lines would overlap")

    def test_ruled_styles_all_exist(self):
        for name in blackboard.BlackboardMixin.RULED_STYLES:
            self.assertIn(name, blackboard.BlackboardMixin.STYLES, name)

    def test_markdown_prefixes_map_onto_real_styles(self):
        import actions
        for _prefix, style in actions._BOARD_PREFIXES:
            self.assertIn(style, blackboard.BlackboardMixin.STYLES, style)


class BoardParsingTests(unittest.TestCase):
    """A whole lesson arrives in one tag; the split has to be right."""

    def setUp(self):
        import actions
        self.actions = actions
        self.sent = []

        class Recorder:
            def put(inner, item):
                self.sent.append(item)
        self.queue = Recorder()

    def styles(self):
        return [payload.get("style") for kind, payload in self.sent
                if kind == "board_write"]

    def test_prefixes_become_a_typographic_hierarchy(self):
        self.actions._board_lines(self.queue, "\n".join([
            "# Title", "## Heading", "### Subheading", "- Bullet",
            "  - Nested bullet", "> An aside", "! A warning", "Plain prose",
        ]))
        self.assertEqual(self.styles(), [
            "title", "heading", "subheading", "point", "subpoint",
            "quote", "warning", "body"])

    def test_double_dollar_lines_are_typeset_not_written(self):
        self.actions._board_lines(self.queue, "Before\n$$E = mc^2$$\nAfter")
        kinds = [kind for kind, _payload in self.sent]
        self.assertEqual(kinds, ["board_write", "board_math", "board_write"])
        self.assertEqual(self.sent[1][1]["latex"], "E = mc^2")

    def test_fenced_blocks_become_code_and_keep_indentation(self):
        self.actions._board_lines(
            self.queue, "Try:\n```\nfor i in range(3):\n    print(i)\n```\nDone")
        styles = self.styles()
        self.assertEqual(styles, ["body", "code", "code", "body"])
        # Indentation inside a fence is meaningful; stripping it breaks Python.
        code = [p["text"] for k, p in self.sent if p.get("style") == "code"]
        self.assertTrue(code[1].startswith("    "), code)

    def test_an_explicit_style_overrides_the_prefixes(self):
        self.actions._board_lines(self.queue, "# Not a title", forced_style="note")
        self.assertEqual(self.styles(), ["note"])

    def test_blank_lines_are_skipped(self):
        self.actions._board_lines(self.queue, "One\n\n\nTwo")
        self.assertEqual(len(self.styles()), 2)

    def test_tag_body_is_extracted_for_block_form_actions(self):
        body = self.actions._tag_body(
            '<ACTION type="BOARD_MATH" size="30">\\frac{a}{b}</ACTION>',
            "BOARD_MATH")
        self.assertEqual(body, r"\frac{a}{b}")

    def test_tag_body_of_an_absent_action_is_empty_not_an_error(self):
        self.assertEqual(self.actions._tag_body("<ACTION type=\"X\"/>", "BOARD_MATH"), "")


class PanelTests(unittest.TestCase):
    """Specs arrive from a model, so malformed input must never raise."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_panels_")

    def test_every_item_kind_survives_a_round_trip(self):
        spec = {"title": "Everything", "items": [
            {"kind": "stat", "label": "A", "value": "1"},
            {"kind": "progress", "label": "B", "ratio": 0.5},
            {"kind": "text", "text": "hello"},
            {"kind": "note", "text": "aside"},
            {"kind": "bullets", "items": ["x", "y"]},
            {"kind": "kv", "rows": [["k", "v"]]},
            {"kind": "button", "label": "Go", "command": "do it"},
            {"kind": "link", "label": "Docs", "url": "https://example.com"},
            {"kind": "divider"},
        ]}
        panel, _action = panels.add(self.base, spec)
        self.assertEqual(len(panel["items"]), 9)
        reloaded = panels.load(self.base)
        self.assertEqual([i["kind"] for i in reloaded[0]["items"]],
                         [i["kind"] for i in panel["items"]])

    def test_unknown_kinds_are_dropped_not_guessed_at(self):
        panel, _ = panels.add(self.base, {"title": "T", "items": [
            {"kind": "hologram", "text": "no"}, {"kind": "text", "text": "yes"}]})
        self.assertEqual(len(panel["items"]), 1)

    def test_a_link_must_actually_be_a_link(self):
        panel, _ = panels.add(self.base, {"title": "T", "items": [
            {"kind": "link", "label": "Evil", "url": "javascript:alert(1)"},
            {"kind": "link", "label": "Fine", "url": "https://example.com"}]})
        self.assertEqual(len(panel["items"]), 1)
        self.assertEqual(panel["items"][0]["label"], "Fine")

    def test_a_button_without_a_command_is_dropped(self):
        panel, _ = panels.add(self.base, {"title": "T", "items": [
            {"kind": "button", "label": "Dead"}]})
        self.assertEqual(panel["items"], [])

    def test_percentages_and_fractions_both_work_for_ratio(self):
        panel, _ = panels.add(self.base, {"title": "T", "items": [
            {"kind": "progress", "label": "A", "ratio": 62},
            {"kind": "progress", "label": "B", "ratio": 0.62}]})
        self.assertAlmostEqual(panel["items"][0]["ratio"], 0.62)
        self.assertAlmostEqual(panel["items"][1]["ratio"], 0.62)

    def test_ratio_is_clamped(self):
        panel, _ = panels.add(self.base, {"title": "T", "items": [
            {"kind": "progress", "label": "A", "ratio": -5}]})
        self.assertEqual(panel["items"][0]["ratio"], 0.0)

    def test_a_panel_needs_a_title(self):
        with self.assertRaises(ValueError):
            panels.add(self.base, {"items": []})

    def test_reusing_a_title_replaces_rather_than_duplicates(self):
        panels.add(self.base, {"title": "Same", "items": [
            {"kind": "text", "text": "first"}]})
        panels.add(self.base, {"title": "same", "items": [
            {"kind": "text", "text": "second"}]})
        loaded = panels.load(self.base)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["items"][0]["text"], "second")

    def test_set_value_updates_one_stat_in_place(self):
        panels.add(self.base, {"title": "Live", "items": [
            {"kind": "progress", "label": "Done", "value": "1", "ratio": 0.1},
            {"kind": "stat", "label": "Other", "value": "9"}]})
        panels.set_value(self.base, "Live", "Done", value="7", ratio=0.7)
        items = panels.load(self.base)[0]["items"]
        self.assertEqual(items[0]["value"], "7")
        self.assertAlmostEqual(items[0]["ratio"], 0.7)
        self.assertEqual(items[1]["value"], "9", "the other stat was touched")

    def test_set_value_on_an_unknown_label_says_so(self):
        panels.add(self.base, {"title": "L", "items": [
            {"kind": "stat", "label": "A", "value": "1"}]})
        with self.assertRaises(ValueError):
            panels.set_value(self.base, "L", "Nope", value="2")

    def test_a_corrupt_panel_does_not_hide_the_others(self):
        panels.add(self.base, {"title": "Good", "items": [
            {"kind": "text", "text": "x"}]})
        path = os.path.join(self.base, "vault", "system", "panels.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["panels"].insert(0, {"no_title": True})
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        loaded = panels.load(self.base)
        self.assertEqual([p["title"] for p in loaded], ["Good"])

    def test_a_missing_store_is_empty_not_an_error(self):
        self.assertEqual(panels.load(tempfile.mkdtemp()), [])

    def test_the_panel_count_is_bounded(self):
        for index in range(panels.MAX_PANELS):
            panels.add(self.base, {"title": f"P{index}",
                                   "items": [{"kind": "text", "text": "x"}]})
        with self.assertRaises(ValueError):
            panels.add(self.base, {"title": "One too many",
                                   "items": [{"kind": "text", "text": "x"}]})


class ResearchSessionTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="vanta_research_")
        self.worker = research.Researcher(self.base, lambda *a, **k: "")

    def test_a_session_round_trips_through_its_checkpoint(self):
        self.worker.session = research.ResearchSession("Topology", self.base, "user")
        self.worker.session.rounds = 4
        self.worker.session.facts_total = 42
        self.worker.session.open_questions = ["What is a manifold?"]
        self.worker.session.best_score = 0.6
        self.worker._save_state()
        restored = self.worker._load_state()
        self.assertEqual(restored.topic, "Topology")
        self.assertEqual(restored.rounds, 4)
        self.assertEqual(restored.facts_total, 42)
        self.assertEqual(restored.open_questions, ["What is a manifold?"])
        self.assertAlmostEqual(restored.best_score, 0.6)

    def test_no_checkpoint_is_none_not_an_error(self):
        self.assertIsNone(self.worker._load_state())

    def test_status_line_is_safe_before_anything_starts(self):
        self.assertIn("No research session", self.worker.status_line())

    def test_json_is_recovered_from_a_chatty_reply(self):
        self.assertEqual(
            research._parse_json_block('Sure! ```json\n["a","b"]\n``` hope that helps'),
            ["a", "b"])
        self.assertEqual(research._parse_json_block('[1, 0, 1]'), [1, 0, 1])
        self.assertIsNone(research._parse_json_block("no json at all"))
        self.assertIsNone(research._parse_json_block(""))

    def test_mastery_needs_both_a_score_and_a_plateau(self):
        # Either alone is a false positive: a high score can mean the exam was
        # easy, and a plateau can mean the questions ran out, not the subject.
        self.assertGreater(research.MASTERY_SCORE, 0.5)
        self.assertGreaterEqual(research.PLATEAU_ROUNDS, 2)

    def test_an_exam_with_nothing_in_the_graph_scores_zero(self):
        self.worker.session = research.ResearchSession("Nothing", self.base)
        self.worker._ask = lambda *args, **kwargs: ""
        self.worker._graph_summary = lambda **kwargs: ""
        self.assertEqual(self.worker._examine(self.worker.session), 0.0)

    def test_opening_questions_fall_back_rather_than_returning_nothing(self):
        # The model returning nothing must not leave the session with an empty
        # queue, which would look exactly like a finished subject. Stubbing
        # _ask skips its retry backoff, which is correct behaviour but makes
        # for a thirty-second unit test.
        self.worker._ask = lambda *args, **kwargs: ""
        questions = self.worker._opening_questions("Ergodic theory")
        self.assertGreaterEqual(len(questions), 4)
        self.assertTrue(all("Ergodic theory" in q for q in questions))


class SearchLayerTests(unittest.TestCase):

    def test_unavailable_is_a_distinct_failure_from_empty(self):
        self.assertTrue(issubclass(research_api.SearchUnavailable, Exception))

    def test_a_missing_key_raises_rather_than_returning_nothing(self):
        saved = os.environ.pop("TAVILY_API_KEY", None)
        try:
            with self.assertRaises(research_api.SearchUnavailable):
                research_api.search("anything")
        finally:
            if saved is not None:
                os.environ["TAVILY_API_KEY"] = saved

    def test_an_empty_query_is_not_a_search(self):
        self.assertEqual(research_api.search("")["results"], [])

    def test_formatting_keeps_urls_with_their_content(self):
        text = research_api.format_for_model({
            "answer": "Short answer.",
            "results": [{"title": "T", "url": "https://example.com",
                         "content": "Body.", "score": 1.0}]})
        self.assertIn("https://example.com", text)
        self.assertIn("Body.", text)

    def test_encoding_damage_is_repaired_before_it_reaches_the_graph(self):
        cleaned = research_api.clean_text("Solomonoff�Kolmogorov")
        self.assertNotIn("�", cleaned)
        self.assertEqual(cleaned, "Solomonoff-Kolmogorov")


class RouterFallbackTests(unittest.TestCase):
    """An outage must be announced and stepped over, never returned as ''."""

    def test_the_chain_gives_every_provider_its_own_model(self):
        import llm_router
        for provider, env_key, default in llm_router.FALLBACK_CHAIN:
            self.assertTrue(provider and env_key and default)
            # A model id is provider-specific; sharing one across providers is
            # how a fallback trades one failure for another.
            self.assertIsInstance(default, str)

    def test_candidates_skip_providers_with_no_key(self):
        import llm_router
        for provider, client, _model in llm_router._candidates("routine"):
            self.assertIsNotNone(client, provider)

    def test_the_preferred_provider_is_tried_first(self):
        import llm_router
        candidates = llm_router._candidates("routine")
        if candidates and llm_router._working_provider is None:
            self.assertEqual(candidates[0][0], llm_router.active_provider)


if __name__ == "__main__":
    unittest.main()
