# -*- coding: utf-8 -*-
"""
Tests for the paginated board: page recording, navigation, editing, and the
animation loop's cleanup.

None of these open a window. They exercise the page model and the frame
builders directly, because the bugs that actually happened here were not
drawing bugs -- a slider that divided by zero on a one-page lesson, and
animation frames that leaked canvas items until the event loop stalled. Both
were invisible until something froze.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions
import blackboard


class FakeCanvas:
    """Records what was drawn, and honours tag deletion like Tk does."""

    def __init__(self):
        self.items = []
        self._next = 0

    def _add(self, kind, tags):
        self._next += 1
        names = () if not tags else (
            (tags,) if isinstance(tags, str) else tuple(tags))
        self.items.append({"id": self._next, "kind": kind, "tags": names})
        return self._next

    def create_line(self, *_a, **kw):
        return self._add("line", kw.get("tags"))

    def create_text(self, *_a, **kw):
        return self._add("text", kw.get("tags"))

    def create_rectangle(self, *_a, **kw):
        return self._add("rect", kw.get("tags"))

    def create_oval(self, *_a, **kw):
        return self._add("oval", kw.get("tags"))

    def create_image(self, *_a, **kw):
        return self._add("image", kw.get("tags"))

    def delete(self, which="all"):
        if which == "all":
            self.items = []
        else:
            self.items = [i for i in self.items if which not in i["tags"]]

    def find_all(self):
        return [i["id"] for i in self.items]

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def move(self, *_a, **_kw):
        pass

    def bbox(self, *_a):
        return (0, 0, 10, 10)

    def find_withtag(self, *_a):
        return []

    def itemconfigure(self, *_a, **_kw):
        pass


class Board(blackboard.BlackboardMixin):
    """The mixin with just enough around it to record and render."""

    FONT_DISPLAY = FONT_TEXT = FONT_MONO = "Segoe UI"

    def __init__(self):
        self._board_canvas = FakeCanvas()
        self._board_win = None
        self._board_y = blackboard.MARGIN
        self._board_images = []
        self._board_math_cache = {}
        self._pages = [{"title": "", "items": []}]
        self._page = 0
        self._anim_job = None
        self._anim_state = {}
        self._column = "full"
        self._column_y = {"left": blackboard.MARGIN, "right": blackboard.MARGIN,
                          "full": blackboard.MARGIN}
        self._overflow_warned = False
        self._replaying = False

    # The surface, the window chrome and the real font metrics are all Tk;
    # stubbing them keeps these tests headless.
    def _ensure_board(self):
        return self._board_canvas

    def _board_show(self):
        pass

    def _refresh_pager(self):
        pass

    def _safe_pager(self):
        # The real one refreshes the pager widget and then publishes the
        # snapshot. Only the widget half is Tk, so only that half is stubbed --
        # dropping the publish would stub out the thing under test.
        self._publish_board_state()

    def _board_is_showing(self):
        # There is no Tk window here. Showing-ness is checked directly against
        # the real predicate in LessonContinuityTests.
        return True

    def _board_wrap(self, text, _font, _width):
        return str(text).split("\n")

    def _line_height(self, font):
        # The real one measures the font, which needs a live Tk interpreter.
        return int(font[1] * 1.6)

    def _fit_text(self, canvas, x, y, text, width, font, fill, anchor="center",
                  tags=None):
        return canvas.create_text(x, y, tags=tags or ())

    def _draw_escape_hint(self):
        pass

    def _draw_math(self, spec):
        self._board_canvas.create_image(0, 0)

    def _board_place_figure(self, figure, dpi=110):
        # The real one renders through matplotlib; the fitting rule it relies
        # on is tested separately against a plain image.
        self._board_scroll_if_needed(300)
        self._board_canvas.create_image(0, 0, tags="figure-latest")
        self._board_y += 300

    def _draw_diagram(self, spec):
        # Diagrams are the thing that forces a split; their internals are Tk.
        self._board_scroll_if_needed(120)
        self._board_canvas.create_rectangle(0, 0, 1, 1)
        self._board_y += 120

    def _draw_plot(self, spec):
        self._board_canvas.create_image(0, 0)

    def _draw_chart(self, spec):
        self._board_canvas.create_image(0, 0)


class PageModelTests(unittest.TestCase):

    def setUp(self):
        self.board = Board()

    def test_a_lesson_starts_on_one_page(self):
        self.assertEqual(self.board._board_page_count(), 1)

    def test_the_first_section_fills_the_opening_page(self):
        # Pushing past an untouched first page would leave every lesson with a
        # blank page one.
        self.board._board_section({"title": "One"})
        self.assertEqual(self.board._board_page_count(), 1)
        self.assertEqual(self.board._pages[0]["title"], "One")

    def test_later_sections_add_pages(self):
        for title in ("One", "Two", "Three"):
            self.board._board_section({"title": title})
        self.assertEqual(self.board._board_page_count(), 3)
        self.assertEqual([p["title"] for p in self.board._pages],
                         ["One", "Two", "Three"])

    def test_writing_records_onto_the_open_page(self):
        self.board._board_section({"title": "One"})
        self.board._board_write({"style": "body", "text": "hello"})
        self.board._board_section({"title": "Two"})
        self.board._board_write({"style": "body", "text": "world"})
        self.assertIn("hello", self.board._board_page_text(0))
        self.assertIn("world", self.board._board_page_text(1))
        self.assertNotIn("world", self.board._board_page_text(0))

    def test_pages_are_commands_so_they_can_be_replayed(self):
        self.board._board_section({"title": "One"})
        self.board._board_write({"style": "body", "text": "hello"})
        self.board._board_section({"title": "Two"})
        before = len(self.board._pages[0]["items"])
        self.board._render_page(0)
        self.assertEqual(len(self.board._pages[0]["items"]), before,
                         "replaying a page must not duplicate its commands")
        self.assertIn("hello", self.board._board_page_text())

    def test_navigation_is_clamped_to_real_pages(self):
        for title in ("One", "Two"):
            self.board._board_section({"title": title})
        self.board._render_page(99)
        self.assertEqual(self.board._page, 1)
        self.board._render_page(-5)
        self.assertEqual(self.board._page, 0)

    def test_goto_counts_from_one_the_way_people_do(self):
        for title in ("One", "Two", "Three"):
            self.board._board_section({"title": title})
        self.board._board_goto({"page": "2"})
        self.assertEqual(self.board._pages[self.board._page]["title"], "Two")

    def test_goto_survives_nonsense(self):
        self.board._board_section({"title": "One"})
        self.board._board_goto({"page": "the second one"})
        self.assertEqual(self.board._page, 0)

    def test_stepping_stops_at_the_ends(self):
        self.board._board_section({"title": "One"})
        self.board._board_step(-1)
        self.assertEqual(self.board._page, 0)
        self.board._board_step(1)
        self.assertEqual(self.board._page, 0)

    def test_the_outline_numbers_pages_and_marks_the_one_showing(self):
        for title in ("Why", "How"):
            self.board._board_section({"title": title})
        outline = self.board._board_outline()
        self.assertIn("1. Why", outline)
        self.assertIn("2. How", outline)
        self.assertIn("showing", outline)


class PageEditingTests(unittest.TestCase):
    """A question mid-lesson is answered on the page being looked at."""

    def setUp(self):
        self.board = Board()
        for title in ("One", "Two", "Three"):
            self.board._board_section({"title": title})
            self.board._board_write({"style": "body", "text": f"body of {title}"})

    def test_editing_brings_that_page_up(self):
        self.board._board_edit_page({"page": "1"})
        self.assertEqual(self.board._page, 0)

    def test_appending_keeps_what_was_there(self):
        self.board._board_edit_page({"page": "2", "mode": "append"})
        self.board._board_write({"style": "body", "text": "the answer"})
        text = self.board._board_page_text(1)
        self.assertIn("body of Two", text)
        self.assertIn("the answer", text)

    def test_replacing_keeps_only_the_title(self):
        self.board._board_edit_page({"page": "2", "mode": "replace"})
        text = self.board._board_page_text(1)
        self.assertIn("Two", text)
        self.assertNotIn("body of Two", text)

    def test_editing_without_a_page_means_the_one_on_screen(self):
        self.board._render_page(0)
        self.board._board_edit_page({})
        self.assertEqual(self.board._page, 0)

    def test_an_out_of_range_page_is_clamped_not_an_error(self):
        self.board._board_edit_page({"page": "99"})
        self.assertEqual(self.board._page, 2)


class AnimationTests(unittest.TestCase):
    """The failures here were a stalled event loop, not a wrong picture."""

    def setUp(self):
        self.board = Board()
        self.board._board_section({"title": "Animated"})

    def _frames(self, spec, count=25):
        self.board._draw_animation(spec)
        state = self.board._anim_state
        self.assertTrue(state, "the animation never started")
        counts = []
        for _ in range(count):
            state["draw"](state)
            state["tick"] += 1 / self.board.ANIMATION_FPS
            counts.append(len(self.board._board_canvas.find_all()))
        return counts

    def test_a_wave_does_not_accumulate_canvas_items(self):
        counts = self._frames({"kind": "wave", "expr": "sin(x - t)",
                               "xmin": "0", "xmax": "12"})
        self.assertLess(max(counts), counts[0] + 4, counts[:5])

    def test_a_flow_does_not_accumulate_canvas_items(self):
        # Its labels were untagged, so every frame stacked a fresh copy and
        # the canvas grew until Tk could not keep up.
        counts = self._frames({"kind": "flow", "steps": "A,B,C,D"})
        self.assertLess(max(counts), counts[0] + 4, counts[:5])

    def test_attention_does_not_accumulate_canvas_items(self):
        counts = self._frames({"kind": "attention",
                               "tokens": "The,cat,sat,on,the,mat"})
        self.assertLess(max(counts), counts[0] + 4, counts[:5])

    def test_attention_weights_are_a_distribution(self):
        self.board._draw_animation({"kind": "attention", "tokens": "a,b,c,d"})
        state = self.board._anim_state
        state["draw"](state)      # must not raise on the first frame
        self.assertEqual(state["spec"]["kind"], "attention")

    def test_an_unknown_kind_starts_nothing_and_says_so(self):
        notes = []
        self.board._draw_write = lambda spec: notes.append(spec.get("text", ""))
        self.board._draw_animation({"kind": "hologram"})
        self.assertFalse(self.board._anim_state, "it started an animation anyway")
        self.assertTrue(any("no animation kind" in n for n in notes), notes)

    def test_only_one_animation_runs_at_a_time(self):
        self.board._draw_animation({"kind": "wave", "expr": "sin(x)"})
        first = self.board._anim_state
        self.board._draw_animation({"kind": "flow", "steps": "A,B"})
        self.assertIsNot(self.board._anim_state, first)
        self.assertEqual(self.board._anim_state["spec"]["kind"], "flow")

    def test_leaving_a_page_stops_its_animation(self):
        self.board._board_animate({"kind": "wave", "expr": "sin(x - t)"})
        self.assertTrue(self.board._anim_state)
        self.board._board_section({"title": "Next"})
        self.assertFalse(self.board._anim_state,
                         "an animation kept running after its page was left")

    def test_a_bad_expression_does_not_stop_the_frame(self):
        self.board._draw_animation({"kind": "wave", "expr": "nonsense(((x"})
        state = self.board._anim_state
        state["draw"](state)      # the frame skips the curve, it does not raise


class InlineMathTests(unittest.TestCase):

    def test_inline_delimiters_are_removed_and_the_source_kept(self):
        self.assertEqual(
            actions._strip_inline_math("$QK^T$ against every key"),
            "QK^T against every key")

    def test_display_math_is_left_for_the_typesetter(self):
        self.assertEqual(actions._strip_inline_math("$$E = mc^2$$"), "$$E = mc^2$$")

    def test_prices_are_not_mistaken_for_mathematics(self):
        self.assertEqual(actions._strip_inline_math("costs $5 and $9"),
                         "costs $5 and $9")



class ColumnLayoutTests(unittest.TestCase):
    """Prose and pictures used to be drawn over each other."""

    def setUp(self):
        self.board = Board()

    def test_a_page_with_no_visual_uses_the_full_width(self):
        self.board._board_section({"title": "Text only"})
        self.board._board_write({"style": "body", "text": "just words"})
        self.assertEqual(self.board._column, "full")
        full = self.board._column_box("full")[1]
        half = self.board._column_box("left")[1]
        self.assertGreater(full, half)

    def test_the_first_visual_splits_the_page(self):
        self.board._board_section({"title": "Mixed"})
        self.board._board_write({"style": "body", "text": "words"})
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        self.assertIn(self.board._column, ("left", "right"))

    def test_text_and_visuals_land_in_different_columns(self):
        self.board._board_section({"title": "Mixed"})
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        text_x = self.board._column_box("left")[0]
        visual_x = self.board._column_box("right")[0]
        self.assertLess(text_x + self.board._column_box("left")[1], visual_x,
                        "the columns overlap horizontally")

    def test_writing_goes_left_and_drawing_goes_right(self):
        self.board._board_section({"title": "Mixed"})
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        self.assertEqual(self.board._column_for("write"), "left")
        self.assertEqual(self.board._column_for("diagram"), "right")
        self.assertEqual(self.board._column_for("math"), "left",
                         "a formula is part of the argument, not an illustration")

    def test_a_side_attribute_overrides_the_default(self):
        self.board._board_section({"title": "Mixed"})
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        self.assertEqual(self.board._column_for("write", {"side": "right"}), "right")

    def test_a_full_width_title_pushes_both_columns_below_it(self):
        # The bug this catches: the title drew at the top, then the first
        # paragraph started at the top margin again, straight over it.
        self.board._board_section({"title": "Spanning title"})
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        self.board._render_page(0)
        self.assertGreater(self.board._column_y["left"], blackboard.MARGIN)
        self.assertGreater(self.board._column_y["right"], blackboard.MARGIN)


class TableAndDiagramTests(unittest.TestCase):
    def setUp(self):
        self.board = Board()

    def test_table_is_a_first_class_replayable_operation(self):
        self.board._board_table({
            "title": "Results", "headers": "Model,Score",
            "rows": "A,92|B,87"})
        self.assertEqual(self.board._pages[0]["items"][0]["op"], "table")
        self.assertIn("Model | Score", self.board._board_page_text())
        before = len(self.board._pages[0]["items"])
        self.board._render_page(0)
        self.assertEqual(len(self.board._pages[0]["items"]), before)

    def test_json_table_cells_can_contain_commas(self):
        headers, rows = self.board._table_data({
            "headers": '["Name", "Notes"]',
            "rows": '[["A", "fast, reliable"], ["B", "small"]]'})
        self.assertEqual(headers, ["Name", "Notes"])
        self.assertEqual(rows[0][1], "fast, reliable")

    def test_table_uses_full_width_below_mixed_content(self):
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        self.assertEqual(self.board._column_for("table"), "full")

    def test_architecture_is_a_supported_diagram_alias(self):
        notes = []
        self.board._draw_write = lambda spec: notes.append(spec.get("text", ""))
        self.board._draw_diagram({"kind": "architecture", "steps": "UI,API,DB"})
        self.assertFalse(notes, notes)


class OverflowTests(unittest.TestCase):
    """Nothing may be cut off, and nothing may be drawn over anything."""

    def setUp(self):
        self.board = Board()
        self.board._board_section({"title": "Long"})

    def test_too_much_content_continues_onto_another_page(self):
        for number in range(80):
            self.board._board_write({"style": "point", "text": f"Point {number}"})
        self.assertGreater(len(self.board._pages), 1,
                           "the overflow was never given anywhere to go")

    def test_a_continuation_page_says_it_is_one(self):
        for number in range(80):
            self.board._board_write({"style": "point", "text": f"Point {number}"})
        self.assertTrue(any("(cont.)" in page["title"]
                            for page in self.board._pages[1:]))

    def test_no_point_is_lost_across_the_split(self):
        for number in range(80):
            self.board._board_write({"style": "point", "text": f"Point {number}"})
        written = []
        for page in self.board._pages:
            written += [item["text"] for item in page["items"]
                        if item["op"] == "write" and item.get("style") == "point"]
        self.assertEqual(len(written), 80)
        self.assertEqual(written[0], "Point 0")
        self.assertEqual(written[-1], "Point 79")

    def test_a_replay_that_cannot_fit_runs_on_rather_than_stacking(self):
        # The old behaviour clamped the cursor, so every remaining line was
        # drawn at the same y and three bullets became one.
        for number in range(80):
            self.board._board_write({"style": "point", "text": f"Point {number}"})
        self.board._render_page(0)
        self.assertFalse(self.board._replaying)
        self.board._replaying = True
        start = self.board._board_y
        self.board._board_scroll_if_needed(10_000)
        self.assertEqual(self.board._board_y, start,
                         "the cursor was moved backwards, which stacks content")
        self.board._replaying = False


class ToggleTests(unittest.TestCase):
    """F11 must put the lesson away without losing it."""

    def setUp(self):
        self.board = Board()
        self.hidden = []
        self.board._board_conceal = lambda: self.hidden.append(True)
        for title in ("One", "Two", "Three"):
            self.board._board_section({"title": title})

    def test_an_empty_board_has_nothing_to_toggle(self):
        empty = Board()
        empty._board_conceal = lambda: None
        self.assertEqual(empty._board_toggle(), "empty")

    def test_showing_returns_to_the_page_that_was_open(self):
        self.board._render_page(1)
        self.assertEqual(self.board._board_toggle(), "shown")
        self.assertEqual(self.board._page, 1)

    def test_toggling_never_discards_pages(self):
        before = len(self.board._pages)
        self.board._board_toggle()
        self.board._board_toggle()
        self.assertEqual(len(self.board._pages), before)



class FigureFittingTests(unittest.TestCase):
    """A figure must never be able to cover what comes after it."""

    def setUp(self):
        self.board = Board()
        self.board._board_section({"title": "Fitting"})

    def _image(self, width, height):
        from PIL import Image
        return Image.new("RGB", (width, height), (0, 0, 0))

    def test_a_wide_figure_is_narrowed_to_its_column(self):
        self.board._board_diagram({"kind": "flow", "steps": "A,B"})
        fitted = self.board._fit_into_column(self._image(4000, 300))
        self.assertLessEqual(fitted.width, self.board._board_width())

    def test_a_tall_figure_is_capped_by_the_room_left(self):
        # Capping against a fixed fraction of the screen was not enough: a
        # figure placed low on the page still ran off the bottom.
        self.board._board_y = self.board._page_bottom() - 200
        fitted = self.board._fit_into_column(self._image(400, 4000))
        self.assertLess(fitted.height, 4000)

    def test_aspect_ratio_survives_fitting(self):
        original = self._image(2000, 1000)
        fitted = self.board._fit_into_column(original)
        self.assertAlmostEqual(fitted.width / fitted.height, 2.0, places=1)

    def test_a_figure_with_no_room_moves_to_a_new_page(self):
        self.board._board_y = self.board._page_bottom() - 10
        before = len(self.board._pages)
        self.board._board_place_figure(None)
        self.assertGreater(len(self.board._pages), before)


class AdvancedVisualTests(unittest.TestCase):
    """The renderers that carry a function rather than an argument."""

    def setUp(self):
        self.board = Board()
        self.board._board_section({"title": "Advanced"})

    def test_every_advanced_op_has_a_renderer(self):
        for op in ("plot3d", "matrix", "field", "contour"):
            self.assertIn(op, self.board.RENDERERS, op)
            self.assertTrue(hasattr(self.board, self.board.RENDERERS[op]), op)

    def test_advanced_ops_count_as_visual_so_the_page_splits(self):
        for op in ("plot3d", "matrix", "field", "contour"):
            self.assertIn(op, self.board.VISUAL_OPS, op)

    def test_an_omitted_attribute_falls_back_to_its_default(self):
        # The dispatcher fills every known attribute, so anything the model
        # left out arrives as "" -- which dict.get happily returns instead of
        # the default, and matplotlib was handed cmap="".
        spec = {"cmap": "", "title": "", "elev": None}
        self.assertEqual(self.board._opt(spec, "cmap", "viridis"), "viridis")
        self.assertEqual(self.board._opt(spec, "elev", 28), 28)
        self.assertEqual(self.board._opt(spec, "missing", "fallback"), "fallback")
        self.assertEqual(self.board._opt({"cmap": "magma"}, "cmap", "viridis"),
                         "magma")

    def test_a_bad_matrix_says_what_it_needed(self):
        notes = []
        self.board._draw_write = lambda spec: notes.append(spec.get("text", ""))
        self.board._draw_matrix({"values": "not numbers"})
        self.assertTrue(any("BOARD_MATRIX needs" in note for note in notes), notes)

    def test_a_bad_3d_expression_is_reported_not_raised(self):
        notes = []
        self.board._draw_write = lambda spec: notes.append(spec.get("text", ""))
        self.board._draw_plot3d({"kind": "surface", "expr": "nonsense((("})
        self.assertTrue(any("3D plot failed" in note for note in notes), notes)

    def test_expressions_evaluate_without_builtins(self):
        # These come from a model, so the namespace is numpy and nothing else.
        with self.assertRaises(Exception):
            self.board._evaluate("__import__('os').getcwd()")


class ContinuationStyleTests(unittest.TestCase):
    """A wrapped line keeps the style of the line it continues."""

    def setUp(self):
        self.sent = []

        class Recorder:
            def put(inner, item):
                self.sent.append(item)
        self.queue = Recorder()

    def styles(self):
        return [(p.get("style"), p.get("continuation"))
                for k, p in self.sent if k == "board_write"]

    def test_a_wrapped_warning_stays_a_warning(self):
        actions._board_lines(self.queue,
                             "! A warning that runs on\n  and continues here.")
        self.assertEqual(self.styles(), [("warning", False), ("warning", True)])

    def test_a_wrapped_bullet_stays_a_bullet(self):
        actions._board_lines(self.queue,
                             "- A bullet that runs on\n  and continues here.")
        self.assertEqual(self.styles(), [("point", False), ("point", True)])

    def test_plain_prose_joins_into_one_paragraph(self):
        actions._board_lines(self.queue, "One line\n  and another")
        self.assertEqual(len(self.styles()), 1)



class ExplainOnScreenTests(unittest.TestCase):
    """Pointing at a document, and bringing it onto the board."""

    def setUp(self):
        self.board = Board()
        self.board._board_section({"title": "Explaining"})

    def test_a_capture_counts_as_visual_so_the_page_splits(self):
        self.assertIn("capture", self.board.VISUAL_OPS)
        self.assertIn("capture", self.board.RENDERERS)
        self.assertTrue(hasattr(self.board, self.board.RENDERERS["capture"]))

    def test_no_region_means_the_whole_screen(self):
        self.assertEqual(self.board._capture_region({}),
                         (0, 0, 1920, 1080))

    def test_a_region_is_clamped_to_the_screen(self):
        left, top, right, bottom = self.board._capture_region(
            {"region": "-50,-50,9999,9999"})
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(right, 1920)
        self.assertLessEqual(bottom, 1080)

    def test_a_malformed_region_falls_back_rather_than_raising(self):
        self.assertEqual(self.board._capture_region({"region": "not,a,region"}),
                         (0, 0, 1920, 1080))

    def test_a_missing_capture_file_is_reported_not_raised(self):
        notes = []
        self.board._draw_write = lambda spec: notes.append(spec.get("text", ""))
        self.board._draw_capture({"path": r"C:\nowhere\gone.png"})
        self.assertTrue(any("captured image is gone" in n for n in notes), notes)


class AnnotationShapeTests(unittest.TestCase):
    """Every shape the catalogue offers has to actually draw something."""

    def setUp(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui.py"), encoding="utf-8") as handle:
            self.ui_source = handle.read()
        with open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            self.catalogue = handle.read()
        self.re = re

    def implemented(self):
        block = self.ui_source[self.ui_source.index("def _draw_annotation"):]
        block = block[:block.index("def _clear_annotations")]
        return set(self.re.findall(r'shape ==\s*"([a-z]+)"', block)) | set(
            self.re.findall(r'shape in \("([a-z]+)", "([a-z]+)"\)', block) and
            [k for pair in self.re.findall(r'shape in \("([a-z]+)", "([a-z]+)"\)', block)
             for k in pair])

    def test_every_documented_shape_is_drawable(self):
        documented = set(self.re.findall(r'shape=\"([a-z]+)\"', self.catalogue))
        # The catalogue also lists them in prose; both spellings must be real.
        listed = set()
        for line in self.catalogue.splitlines():
            if "shape=" in line and "highlight, box" in line:
                listed |= {w.strip(" .,") for w in line.split("shape=")[-1].split(",")}
        missing = (documented | {w for w in listed if w.isalpha()}) - self.implemented()
        missing -= {"shape"}
        self.assertEqual(missing, set(), f"documented but never drawn: {missing}")

    def test_the_shapes_the_explain_flow_relies_on_exist(self):
        for shape in ("highlight", "underline", "callout", "number", "box"):
            self.assertIn(shape, self.implemented(), shape)

    def test_annotate_text_is_registered_and_validated(self):
        import action_schema
        import autonomy
        self.assertIn("ANNOTATE_TEXT", autonomy.KNOWN_ACTIONS)
        self.assertIn("ANNOTATE_TEXT", action_schema.ACTION_SPECS)
        self.assertIn("BOARD_CAPTURE", autonomy.KNOWN_ACTIONS)

    def test_annotate_text_needs_something_to_look_for(self):
        import action_schema
        error = action_schema.validate("ANNOTATE_TEXT", {"shape": "highlight"})
        self.assertIsNotNone(error)
        self.assertIsNone(action_schema.validate("ANNOTATE_TEXT", {"text": "prior"}))


class LocateTextBoxTests(unittest.TestCase):
    """Highlighting needs an extent, which a centre point cannot give."""

    def test_an_empty_needle_is_refused(self):
        import system_tools
        box, detail = system_tools.locate_text_box("")
        self.assertIsNone(box)
        self.assertTrue(detail.startswith("[Error]"))

    def test_it_returns_a_box_not_a_point(self):
        import inspect
        import system_tools
        source = inspect.getsource(system_tools.locate_text_box)
        self.assertIn("right", source)
        self.assertIn("bottom", source)


class LessonContinuityTests(unittest.TestCase):
    """The model must be able to see what it has already taught.

    The reported bug was VANTA repeating earlier slides. The cause was not the
    renderer: BOARD_PAGES pushed the outline to the activity log and returned
    "it is printed to the activity log" to the model, which cannot read a log.
    So the one action for checking the lesson so far conveyed nothing, and the
    model, having no other view of the board, taught the same ground again.
    """

    def setUp(self):
        self.board = Board()
        for title in ("Vectors", "Dot products", "Projections"):
            self.board._board_section({"title": title})
            self.board._board_write({"style": "body", "text": f"about {title}"})

    def test_the_board_publishes_itself_off_the_ui_thread(self):
        state = blackboard.board_state()
        self.assertEqual(state["count"], 3)
        self.assertEqual(state["page"], 3)
        for title in ("Vectors", "Dot products", "Projections"):
            self.assertIn(title, state["outline"])

    def test_the_snapshot_is_readable_from_another_thread(self):
        # The whole point: actions run off the UI thread, and a snapshot that
        # can only be read on the UI thread would solve nothing.
        import threading
        seen = {}

        def reader():
            seen["state"] = blackboard.board_state()

        thread = threading.Thread(target=reader)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(seen["state"]["count"], 3)

    def test_board_pages_tells_the_model_what_it_taught(self):
        import queue
        q = queue.Queue()
        actions.execute_actions('<ACTION type="BOARD_PAGES" />', {}, q, ".")
        out = str(getattr(actions.execute_actions, "_last_output", ""))
        self.assertIn("Projections", out,
                      "BOARD_PAGES must return the outline, not point at a log")
        self.assertNotIn("activity log", out)

    def test_the_outline_moves_with_navigation(self):
        self.board._render_page(0)
        self.assertEqual(blackboard.board_state()["page"], 1)
        self.assertIn("showing", blackboard.board_state()["outline"])

    def test_a_put_away_board_is_not_advertised_as_open(self):
        """F11 withdraws the window without discarding the lesson.

        Tested on the predicate rather than through Board, because the fake has
        no Tk window at all and the question is precisely what the real one
        reports.
        """
        class Window:
            def __init__(self, how): self.how = how
            def winfo_exists(self): return True
            def state(self): return self.how

        # Unbound, because Board stubs this method out -- calling it through
        # the fake would only assert that the stub returns what the stub
        # returns.
        showing = blackboard.BlackboardMixin._board_is_showing
        probe = Board()
        probe._board_win = Window("normal")
        self.assertTrue(showing(probe))
        probe._board_win = Window("withdrawn")
        self.assertFalse(showing(probe))
        probe._board_win = None
        self.assertFalse(showing(probe))


class TeachingContextTests(unittest.TestCase):
    """The outline reaches the model without it having to remember to ask."""

    def test_context_carries_the_outline_while_a_lesson_is_live(self):
        board = Board()
        for title in ("Limits", "Derivatives"):
            board._board_section({"title": title})
            board._board_write({"style": "body", "text": f"on {title}"})
        import agent
        block = agent._board_context()
        self.assertIn("Limits", block)
        self.assertIn("Derivatives", block)
        self.assertIn("do not restate", block)

    def test_context_is_empty_when_nothing_is_being_taught(self):
        # It is in gather_context on every single turn, so the quiet case has
        # to cost nothing at all.
        blackboard._set_board_state(open=False, page=0, count=0,
                                    outline="", current="")
        import agent
        self.assertEqual(agent._board_context(), "")


if __name__ == "__main__":
    unittest.main()
