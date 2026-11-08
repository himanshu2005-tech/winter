# -*- coding: utf-8 -*-
"""
Interface consistency tests: the mark, the palette, the Control Center map,
and the guards on machine controls.

None of this opens a window. These are the checks that catch the failures the
UI cannot report on its own -- a rail entry with no builder crashes on open, a
mode missing from the page is simply unreachable and says nothing, and a
destructive control that stopped asking first says nothing either until it has
already happened. All three have occurred here.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import control_center
import system_controls
import theme
import ui
import winter_mark


class MarkTests(unittest.TestCase):
    """The identity has to survive being drawn at every size."""

    def test_geometry_is_a_hexagon_and_a_two_stroke_v(self):
        hexagon, letter = winter_mark.geometry()
        self.assertEqual(len(hexagon), 6)
        self.assertEqual(len(letter), 2)
        self.assertEqual(letter[0][1], letter[1][0])
        self.assertGreater(letter[0][1][1], letter[0][0][1])

    def test_every_point_stays_inside_the_unit_circle(self):
        # Points outside the unit circle would be clipped by the canvas or the
        # icon padding, silently cropping the mark at some sizes and not others.
        for segments in winter_mark.geometry():
            for start, end in segments:
                for x, y in (start, end):
                    self.assertLessEqual((x * x + y * y) ** 0.5, 1.0001,
                                         f"({x}, {y}) falls outside the mark")

    def test_hexagon_rotation_is_seamless_at_sixty_degrees(self):
        # Six-fold symmetry is what lets the orb spin without a visible seam.
        start = sorted(tuple(round(v, 6) for v in p)
                       for segment in winter_mark.geometry(0)[0] for p in segment)
        turned = sorted(tuple(round(v, 6) for v in p)
                        for segment in winter_mark.geometry(60)[0] for p in segment)
        self.assertEqual(start, turned)

    def test_stroke_weight_is_optically_sized(self):
        # A single ratio cannot serve 16px and 256px, so small sizes must get
        # proportionally heavier strokes or the icon vanishes in the taskbar.
        small = winter_mark.stroke_width(10) / 10
        large = winter_mark.stroke_width(100) / 100
        self.assertGreater(small, large)
        self.assertGreaterEqual(winter_mark.stroke_width(4), 1.0)

    def test_renders_at_every_icon_size(self):
        for size in winter_mark.ICON_SIZES:
            image = winter_mark.render_png(size)
            self.assertEqual(image.size, (size, size))
            # A blank render would mean the geometry fell outside the canvas.
            self.assertTrue(any(pixel[3] > 0 for pixel in image.convert("RGBA").getdata()),
                            f"mark rendered empty at {size}px")

    def test_ring_colour_never_touches_the_letterform(self):
        plain = winter_mark.render_png(64)
        tinted = winter_mark.render_png(64, ring_color="#0a84ff")
        self.assertNotEqual(list(plain.getdata()), list(tinted.getdata()))


class ThemeTests(unittest.TestCase):

    def test_every_type_role_resolves_to_a_known_family(self):
        for role, (family, size, weight) in theme.TYPE_SCALE.items():
            self.assertIn(family, ("display", "text", "mono"), role)
            self.assertGreater(size, 0, role)
            self.assertIn(weight, ("normal", "bold"), role)

    def test_every_state_has_an_accent_and_a_label(self):
        states = (theme.STATE_IDLE, theme.STATE_LOADING, theme.STATE_LISTENING,
                  theme.STATE_THINKING, theme.STATE_SPEAKING)
        for state in states:
            self.assertIn(state, theme.ACCENTS)
            self.assertIn(state, theme.STATE_LABELS)

    def test_every_palette_token_is_a_valid_hex_colour(self):
        for name in dir(theme):
            value = getattr(theme, name)
            if name.isupper() and isinstance(value, str) and value.startswith("#"):
                self.assertRegex(value, r"^#[0-9a-fA-F]{6}$", name)

    def test_blend_and_dim_stay_in_range(self):
        self.assertEqual(theme.blend("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(theme.blend("#000000", "#ffffff", 1.0), "#ffffff")
        # dim pulls toward the surface, so it must not brighten an accent.
        dimmed = theme.hex_to_rgb(theme.dim(theme.BLUE, 0.7))
        self.assertLess(dimmed[2], theme.hex_to_rgb(theme.BLUE)[2])

    def test_load_colour_escalates_only_when_there_is_trouble(self):
        self.assertEqual(theme.load_ratio_color(0.10), theme.BLUE)
        self.assertEqual(theme.load_ratio_color(0.80), theme.ORANGE)
        self.assertEqual(theme.load_ratio_color(0.95), theme.RED)
        self.assertEqual(theme.load_ratio_color(0.95, charging=True), theme.GREEN)


class DynamicNotchTests(unittest.TestCase):

    def test_notch_text_is_flattened_and_bounded(self):
        clean = ui.WinterUI._notch_clean_text("  one\n two   three  ", 40)
        self.assertEqual(clean, "one two three")
        clipped = ui.WinterUI._notch_clean_text("x" * 100, 20)
        self.assertEqual(len(clipped), 20)
        self.assertTrue(clipped.endswith("..."))

    def test_notch_floats_clear_of_the_top_edge(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._set_notch_geometry)
        self.assertIn("y = 10", source)
        self.assertIn('geometry(f"{w}x{h}+{x}+{y}")', source)

    def test_idle_notch_is_a_compact_circle(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._build_notch)
        self.assertIn("self.NOTCH_IDLE_W,   self.NOTCH_IDLE_H   = 54, 54", source)
        self.assertIn("rely=0.5", source)

    def test_idle_orb_is_the_soul(self):
        # The hairline orbit gave way to a living light (soul.py). State
        # colours live in the soul's palettes, not in the drawing code, and a
        # machine without numpy/PIL still gets a plain dot.
        import inspect
        source = inspect.getsource(ui.WinterUI._draw_mark)
        for colour in ("#4f8cff", "#bf5af2", "#30d5c8"):
            self.assertNotIn(colour, source)
        self.assertIn("soul", source)
        self.assertIn("mind.step(dt)", source)
        self.assertIn("create_oval", source)

    def test_recognized_input_stays_inside_the_compact_notch(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._set_notch_question)
        self.assertIn("self._update_notch()", source)
        self.assertNotIn("self._show_notch_question()", source)

    def test_hands_free_window_uses_the_listening_blue_border(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._update_notch)
        self.assertIn("self._conversation_until", source)
        self.assertIn("ACCENTS[STATE_LISTENING]", source)

    def test_conversation_window_expires_without_an_agent_callback(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._animate)
        self.assertIn("conversation_active", source)
        self.assertIn("self._update_notch()", source)

    def test_completed_question_is_cleared_from_the_notch_context(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._poll)
        self.assertIn('s == "notch_question_clear"', source)
        self.assertIn('self._notch_question = ""', source)

    def test_expired_answer_clears_its_question_too(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._animate)
        expiry = source[source.index("self._notch_answer_until"):]
        self.assertIn('self._notch_question = ""', expiry)

    def test_background_work_cannot_reopen_a_previous_question(self):
        import inspect
        import agent
        source = inspect.getsource(agent.run_agent_loop)
        proactive = source[source.index('elif origin == "proactive"'):]
        self.assertIn('ui_queue.put(("notch_question_clear", ""))', proactive)

    def test_listening_has_a_dedicated_waveform(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._draw_voice_wave)
        self.assertIn("STATE_LISTENING", source)
        self.assertIn("create_line", source)

    def test_local_speech_uses_the_same_notch_not_a_second_box(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._update_notch)
        self.assertIn('detail == "Speaking"', source)
        question = inspect.getsource(ui.WinterUI._set_notch_question)
        self.assertIn("self._clear_step_line()", question)
        self.assertNotIn("self._show_notch_question()", question)


class ResearchDetailsTests(unittest.TestCase):

    def test_result_window_accepts_every_model_answer_and_rich_blocks(self):
        import inspect
        route = inspect.getsource(ui.WinterUI._show_result)
        rich = inspect.getsource(ui.WinterUI._show_rich_result)
        render = inspect.getsource(ui.WinterUI._render_report_section)
        poll = inspect.getsource(ui.WinterUI._poll)
        self.assertIn("force=True", route)
        self.assertIn("blocks", rich)
        self.assertIn('kind == "table"', render)
        self.assertIn('kind == "diagram"', render)
        self.assertIn('kind == "actions"', render)
        self.assertIn('elif s == "show_result"', poll)

    def test_interactive_result_buttons_reenter_normal_command_pipeline(self):
        import inspect
        source = inspect.getsource(ui.WinterUI._result_command)
        self.assertIn('self._in_q.put(("text", command))', source)

    def test_minimize_preserves_result_and_exposes_restore_control(self):
        import inspect
        minimize = inspect.getsource(ui.WinterUI._minimize_details)
        restore = inspect.getsource(ui.WinterUI._restore_details)
        close = inspect.getsource(ui.WinterUI._close_details)
        self.assertIn("self.info_win.iconify()", minimize)
        self.assertNotIn("self.info_restore_win.deiconify()", minimize)
        self.assertNotIn("_artifact_visible = False", minimize)
        self.assertIn("self.info_win.deiconify()", restore)
        self.assertIn("_artifact_visible = False", close)

    def test_result_surface_uses_native_windows_controls(self):
        import inspect
        build = inspect.getsource(ui.WinterUI._build_info_window)
        register = inspect.getsource(ui.WinterUI._register_winter_hwnds)
        self.assertIn('self.info_win.title("VANTA")', build)
        self.assertIn("self.info_win.overrideredirect(False)", build)
        self.assertIn('self.info_win.protocol("WM_DELETE_WINDOW"', build)
        self.assertNotIn("<B1-Motion>", build)
        self.assertNotIn('header, text="×"', build)
        self.assertNotIn('header, text="—"', build)
        self.assertIn('(screen_w - w) // 2', build)
        self.assertIn('int(screen_h * 0.10)', build)
        self.assertIn("self.info_restore_win", register)

    def test_placeholder_status_is_not_display_worthy(self):
        for value in ("", "Ready", "working on it...", "SPEAKING"):
            self.assertFalse(ui.display_worthy_text(value))
        for value in (
                '[Browser Snapshot] {"url":"https://example.com","elements":[]}',
                'RESULT\n[Browser Snapshot] {"url":"https://example.com"}',
                '[Browser Receipt] {"ok":true,"operation":"click"}',
                '[Browser Tabs] [{"tab":"t1"}]'):
            self.assertFalse(ui.display_worthy_text(value))
        self.assertTrue(ui.display_worthy_text(
            "Your calendar has two overlapping events at 4 PM."))

    def test_empty_rich_envelope_does_not_open_a_window(self):
        self.assertFalse(ui.rich_blocks_have_content([]))
        self.assertFalse(ui.rich_blocks_have_content([
            {"type": "markdown", "content": "Ready"},
            {"type": "table", "headers": [], "rows": []},
        ]))
        self.assertTrue(ui.rich_blocks_have_content([
            {"type": "table", "headers": ["Event"], "rows": []},
        ]))

    def test_research_results_use_cards_instead_of_a_terminal_dump(self):
        import inspect
        build = inspect.getsource(ui.WinterUI._build_info_window)
        render = inspect.getsource(ui.WinterUI._show_research_cards)
        self.assertIn("self.info_source_list", build)
        self.assertIn("WHAT MATTERS", build)
        self.assertIn("domain or 'Open source'", render)
        self.assertIn('text_color="#64d2ff"', render)
        self.assertIn('size=10', render)
        self.assertIn("webbrowser.open", render)

    def test_multi_section_reports_render_as_cards(self):
        import inspect
        route = inspect.getsource(ui.WinterUI._show_report_if_structured)
        render = inspect.getsource(ui.WinterUI._render_report_section)
        poll = inspect.getsource(ui.WinterUI._poll)
        self.assertIn("report_view.parse", route)
        self.assertIn('tally.get("heading", 0) < 2', route)
        self.assertIn("self._render_report_section", route)
        self.assertIn("corner_radius=8", render)
        self.assertIn('elif s == "show_report"', poll)
        self.assertIn("self._show_result(t)", poll)
        self.assertIn("user_activity_text(t)", poll)
        self.assertNotIn("self._show_report_if_structured(t)", poll)

    def test_activity_feed_drops_diagnostics_and_normalises_real_events(self):
        self.assertEqual(control_center.user_activity_text(
            "Memory grounded: 0 graph facts, 0 semantic memories."), "")
        self.assertEqual(control_center.user_activity_text(
            "[Router] turn -> airouter"), "")
        self.assertEqual(control_center.user_activity_text(
            "  Calendar event   created  "), "Calendar event created")


class ControlCenterMapTests(unittest.TestCase):
    """The mismatches here are all silent at runtime."""

    def test_every_rail_entry_has_a_builder(self):
        listed = [name for _section, names in control_center.NAV_SECTIONS
                  for name in names]
        for name in listed:
            self.assertIn(name, control_center.PAGE_BUILDERS,
                          f"{name} is in the rail with no page builder")

    def test_a_page_off_the_rail_is_still_reachable(self):
        """The rail is a shortlist, not the whole surface.

        The invariant that matters is the other direction, which the test above
        covers: a rail entry with no builder is a dead button. A builder with no
        rail entry is a page you open by asking -- CONTROL_CENTER page="Graph"
        does exactly that -- and forcing every page into the sidebar is how the
        window ended up with eight of them.
        """
        listed = {name for _section, names in control_center.NAV_SECTIONS
                  for name in names}
        off_rail = set(control_center.PAGE_BUILDERS) - listed
        # Whatever is off the rail must still have a working builder, or it is
        # unreachable rather than merely tucked away.
        for name in off_rail:
            method = control_center.PAGE_BUILDERS[name]
            self.assertTrue(
                hasattr(control_center.ControlCenterMixin, method),
                f"{name} is off the rail and has no builder either")

    def test_the_rail_stays_short(self):
        # Eight entries under three headings was the complaint. This is the
        # guard against it creeping back one useful page at a time.
        listed = [name for _section, names in control_center.NAV_SECTIONS
                  for name in names]
        self.assertLessEqual(len(listed), 7,
                             f"the rail is back up to {len(listed)} entries")

    def test_every_builder_method_actually_exists(self):
        for name, method in control_center.PAGE_BUILDERS.items():
            self.assertTrue(
                hasattr(control_center.ControlCenterMixin, method),
                f"{name} names {method}, which is not defined")

    def test_rail_entries_are_unique(self):
        listed = [name for _section, names in control_center.NAV_SECTIONS
                  for name in names]
        self.assertEqual(len(listed), len(set(listed)))

    def test_every_operating_mode_is_offered(self):
        # The hardcoded list had drifted and left `tutor` with no way to reach
        # it from the window, which is indistinguishable from it not existing.
        import mode_manager
        for mode in mode_manager.POLICIES:
            self.assertIn(mode.value, control_center.MODE_NAMES,
                          f"{mode.value} mode cannot be selected from the UI")

    def test_every_offered_mode_is_real(self):
        import mode_manager
        known = {mode.value for mode in mode_manager.POLICIES}
        for mode in control_center.MODE_NAMES:
            self.assertIn(mode, known, f"{mode} is offered but does not exist")

    def test_every_mode_explains_itself(self):
        for mode in control_center.MODE_NAMES:
            self.assertIn(mode, control_center.MODE_BLURBS,
                          f"{mode} is offered with no description")


class MachineControlGuardTests(unittest.TestCase):
    """Destructive controls must refuse until explicitly confirmed."""

    def test_shutdown_refuses_without_confirmation(self):
        self.assertTrue(system_controls.shutdown().startswith("[Approval Required]"))
        self.assertTrue(
            system_controls.shutdown(restart=True).startswith("[Approval Required]"))

    def test_sign_out_refuses_without_confirmation(self):
        self.assertTrue(system_controls.sign_out().startswith("[Approval Required]"))

    def test_ending_a_process_refuses_without_confirmation(self):
        result = system_controls.end_process(name="explorer.exe")
        self.assertTrue(result.startswith("[Approval Required]"))

    def test_emptying_the_bin_refuses_without_confirmation(self):
        self.assertTrue(
            system_controls.empty_recycle_bin().startswith("[Approval Required]"))

    def test_confirmed_end_process_still_needs_a_target(self):
        # Confirmation must not turn into "kill everything".
        self.assertTrue(
            system_controls.end_process(confirmed=True).startswith("[Error]"))

    def test_unknown_media_action_is_rejected(self):
        self.assertTrue(system_controls.media("rewind").startswith("[Error]"))

    def test_brightness_rejects_nonsense(self):
        self.assertTrue(system_controls.set_brightness("bright").startswith("[Error]"))


class TelemetryShapeTests(unittest.TestCase):
    """The dashboard reads these keys directly; a rename would KeyError."""

    def test_snapshot_carries_every_field_the_dashboard_reads(self):
        data = system_controls.snapshot()
        for key in ("cpu", "ram", "ram_used_gb", "ram_total_gb", "disk",
                    "battery", "net_up", "net_down", "uptime"):
            self.assertIn(key, data, f"snapshot lost '{key}'")

    def test_process_rows_carry_every_field_the_card_reads(self):
        rows = system_controls.top_processes(3)
        self.assertTrue(rows)
        for row in rows:
            for key in ("name", "memory", "count", "pid"):
                self.assertIn(key, row)

    def test_human_bytes_and_duration_stay_short(self):
        self.assertEqual(system_controls.human_bytes(0), "0 B")
        self.assertTrue(system_controls.human_bytes(5 * 1024 ** 3).endswith("GB"))
        self.assertEqual(system_controls.human_duration(0), "0m")
        self.assertEqual(system_controls.human_duration(3600 * 25), "1d 1h")


class AdvertisedPageTests(unittest.TestCase):
    """What the prompt offers has to be what the window can build.

    Cutting the eight modes down to two deleted the Modes page but left the
    catalogue still offering it, so CONTROL_CENTER page="Modes" was a promise
    to the model that nothing could keep -- and Code, which does exist, was not
    offered at all. Neither showed up as a failure anywhere, which is the whole
    reason for this test.
    """

    def _advertised(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "agent.py"), encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r"Pages: ([A-Za-z,\s]+?)\.", source)
        self.assertIsNotNone(match, "the catalogue no longer lists the pages")
        return [name.strip() for name in
                match.group(1).split(",") if name.strip()]

    def test_every_advertised_page_can_be_built(self):
        for name in self._advertised():
            self.assertIn(name, control_center.PAGE_BUILDERS,
                          f"the prompt offers {name} but nothing builds it")

    def test_every_buildable_page_is_advertised(self):
        advertised = self._advertised()
        for name in control_center.PAGE_BUILDERS:
            self.assertIn(name, advertised,
                          f"{name} exists but the model is never told about it")


class OnDemandPageTests(unittest.TestCase):
    """A message for a page that has not been built must not kill the queue.

    Taking Graph off the sidebar meant its widgets are created only when it is
    opened -- but the contacts sync still requests a snapshot at boot, so
    _apply_graph_snapshot ran against a label that did not exist and took down
    the whole UI queue handler on every single launch:

        AttributeError: 'WinterUI' object has no attribute 'graph_status'

    Its two sibling handlers already guarded for this; only the snapshot one
    did not.
    """

    def _ui_source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "ui.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_every_handler_touching_graph_widgets_checks_first(self):
        source = self._ui_source()
        for line_no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("self.graph_status.configure"):
                continue
            # The guard must be on this line or shortly above it.
            window = source.splitlines()[max(0, line_no - 4):line_no]
            self.assertTrue(
                any("hasattr" in w or "getattr" in w for w in window),
                f"ui.py:{line_no} configures graph_status unguarded")

    def test_the_snapshot_handler_tolerates_an_unbuilt_page(self):
        source = self._ui_source()
        start = source.index("def _apply_graph_snapshot")
        body = source[start:start + 1200]
        self.assertIn('getattr(self, "graph_status", None)', body)
        self.assertIn('getattr(self, "graph_canvas", None)', body)

    def test_a_page_off_the_rail_still_has_a_builder(self):
        # Off the sidebar is not the same as gone: CONTROL_CENTER page="Graph"
        # must still work.
        self.assertIn("Graph", control_center.PAGE_BUILDERS)


class VoiceWarmUpTests(unittest.TestCase):
    """The warm-up must not be able to break the thing it protects.

    Warming with a lone "." synthesises no audio, so edge-tts raises
    NoAudioReceived -- and because any failure marked the voice offline for a
    minute, the warm-up guaranteed the greeting used the offline voice. Observed
    on a real boot as:

        [Voice] Online voice unavailable (NoAudioReceived after 25s)
    """

    def test_the_warm_up_uses_real_words(self):
        import inspect
        import voice
        source = inspect.getsource(voice.warm)
        self.assertNotIn('_edge_tts(".", 1.0)', source)
        self.assertIn("Ready.", source)

    def test_a_warm_up_failure_cannot_mark_the_voice_offline(self):
        import inspect
        import voice
        self.assertIn("note_failures", inspect.signature(voice._edge_tts).parameters)
        self.assertIn("note_failures=False", inspect.getsource(voice.warm))

    def test_a_real_failure_still_marks_it_offline(self):
        # The memo is what stops every sentence re-paying the timeout, so
        # suppressing it for warm-ups must not suppress it generally.
        import inspect
        import voice
        source = inspect.getsource(voice._edge_tts)
        self.assertIn("if note_failures:", source)
        self.assertIn("_note_offline(", source)

    def test_the_offline_memo_is_not_set_by_a_silent_clip(self):
        import time
        import voice
        voice._offline_until = 0.0
        # A period produces no audio from any voice, online or not.
        voice._edge_tts(".", 1.0, note_failures=False)
        self.assertLessEqual(voice._offline_until, time.time(),
                             "a warm-up failure must not push the voice offline")


if __name__ == "__main__":
    unittest.main()
