# -*- coding: utf-8 -*-
import datetime
import os
import queue
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_schema
import actions
import autonomy
import life_tools
import office_docs as od

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DECK = """# Roadmap
## 2026 plan

# Goals
- Ship the **wake word**
  - test on five voices
- Finish placements
Notes: keep it short

# Numbers
```chart column Scores
categories: A, B, C
Mock: 50, 60, 70
```
- improving

# People
| Name | Role |
|---|---|
| Asha | Lead |
| Ravi | Dev |

> Build it.
"""


def dispatch(tag):
    actions.execute_actions._results = []
    with patch.object(actions, "_policy_confirmation_required", return_value=False):
        actions.execute_actions(tag, {}, queue.Queue(), BASE_DIR,
                                origin="user", turn_id="office-test")
    return list(getattr(actions.execute_actions, "_results", []))


class Paths(unittest.TestCase):
    def test_nothing_is_ever_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            first = od.resolve_output(os.path.join(folder, "a.pptx"), "t", "pptx")
            first.write_bytes(b"x")
            second = od.resolve_output(os.path.join(folder, "a.pptx"), "t", "pptx")
            self.assertEqual(second.name, "a (2).pptx")

    def test_wrong_extension_is_corrected_and_folder_gets_the_title(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(od.resolve_output(os.path.join(folder, "x.docx"), "t", "pptx").suffix,
                             ".pptx")
            self.assertEqual(od.resolve_output(folder + os.sep, "My Talk!", "pptx").name,
                             "my-talk.pptx")

    def test_default_location_is_documents_vanta(self):
        path = od.resolve_output("", "Weekly Plan", "docx")
        self.assertEqual(path.name, "weekly-plan.docx")
        self.assertIn("VANTA", str(path))


class Parser(unittest.TestCase):
    def test_blocks(self):
        kinds = [b["kind"] for b in od.parse_blocks(DECK)]
        for expected in ("heading", "bullet", "notes", "chart", "table", "quote"):
            self.assertIn(expected, kinds)

    def test_chart_numbers_survive_symbols(self):
        block = od.parse_blocks("```chart line Rev\ncategories: Q1, Q2\nA: $1,200, 15%\n```")[0]
        self.assertEqual(block["series"][0][1], [1200.0, 15.0])

    def test_inline_runs(self):
        runs = od.inline_runs("a **b** *c* `d`")
        self.assertEqual([r[0] for r in runs], ["a ", "b", " ", "c", " ", "d"])
        self.assertTrue(runs[1][1]["bold"] and runs[3][1]["italic"] and runs[5][1]["code"])

    def test_table_separator_rows_are_dropped_and_rows_padded(self):
        table = od.parse_blocks("| a | b |\n|---|---|\n| 1 |")[0]
        self.assertEqual(table["rows"], [["a", "b"], ["1", ""]])


class Presentation(unittest.TestCase):
    def build(self, markdown, **kw):
        folder = tempfile.mkdtemp()
        out = od.resolve_output(os.path.join(folder, "d.pptx"), "d", "pptx")
        info = od.build_presentation(markdown, output=out, **kw)
        from pptx import Presentation as P
        return P(str(out)), info

    def test_structure_and_notes(self):
        prs, info = self.build(DECK)
        self.assertEqual(info["slides"], 5)        # title, goals, chart, table, quote
        self.assertEqual(len(prs.slides), 5)
        notes = prs.slides[1].notes_slide.notes_text_frame.text
        self.assertIn("keep it short", notes)

    def test_nothing_is_drawn_off_the_slide(self):
        prs, _ = self.build(DECK + "\n# Long\n" + "\n".join(
            f"- point {i} " + "word " * 18 for i in range(30)))
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.left is None:
                    continue
                self.assertGreaterEqual(shape.left, 0)
                self.assertGreaterEqual(shape.top, 0)
                self.assertLessEqual(shape.left + shape.width, prs.slide_width + 10)
                self.assertLessEqual(shape.top + shape.height, prs.slide_height + 10)

    def test_long_lists_split_instead_of_shrinking_below_readable(self):
        items = "\n".join(f"- item {i} " + "word " * 18 for i in range(30))
        prs, info = self.build("# Deck\n\n# Long\n" + items)
        self.assertGreater(info["slides"], 3)
        titles = [s.shapes[1].text_frame.text for s in prs.slides if len(s.shapes) > 1]
        self.assertTrue(any("(cont.)" in t for t in titles))

    def test_balanced_continuations_have_no_orphan(self):
        items = [(0, "item " + "word " * 20) for _ in range(21)]
        chunks = od.split_to_fit(items, 11.8, 5.2)
        self.assertGreater(len(chunks), 1)
        self.assertGreaterEqual(min(len(c) for c in chunks), 2)
        self.assertEqual(sum(len(c) for c in chunks), 21)

    def test_big_tables_continue_on_the_next_slide(self):
        rows = "\n".join(f"| r{i} | v{i} |" for i in range(25))
        prs, info = self.build("# T\n\n# Data\n| a | b |\n|---|---|\n" + rows)
        self.assertGreater(info["slides"], 2)

    def test_every_theme_builds(self):
        for theme in od.THEMES:
            self.build(DECK, theme=theme)

    def test_unknown_theme_falls_back(self):
        self.build(DECK, theme="neon")

    def test_empty_outline_is_refused_before_writing(self):
        with self.assertRaises(ValueError):
            od.build_presentation("   ", output=Path(tempfile.mkdtemp()) / "x.pptx")

    def test_missing_image_fails_before_anything_is_written(self):
        folder = Path(tempfile.mkdtemp())
        out = folder / "x.pptx"
        with self.assertRaises(ValueError):
            od.build_presentation("# T\n\n# S\n![x](C:\\nope\\missing.png)", output=out)
        self.assertFalse(out.exists())

    def test_picture_slide(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        Image.new("RGB", (400, 300), (10, 100, 200)).save(folder / "p.png")
        out = folder / "p.pptx"
        info = od.build_presentation(f"# T\n\n# Pic\n![cap]({folder / 'p.png'})\n- beside", output=out)
        self.assertEqual(info["slides"], 2)


class Document(unittest.TestCase):
    def test_docx_roundtrip(self):
        out = Path(tempfile.mkdtemp()) / "r.docx"
        info = od.build_document(
            "# Report\nIntro **text**.\n\n## Part\n- a\n  - b\n1. one\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n> quote\n",
            author="Me", output=out)
        text = od.extract_text(out)
        for needle in ("Report", "Intro text.", "Part", "X | Y", "1 | 2"):
            self.assertIn(needle, text)
        self.assertGreater(info["words"], 5)


class Spreadsheet(unittest.TestCase):
    def test_types_formulas_totals_and_charts(self):
        out = Path(tempfile.mkdtemp()) / "b.xlsx"
        od.build_spreadsheet(
            "# Budget\n| Month | Revenue | Margin |\n|---|---|---|\n"
            "| Jan | $1,200 | 33% |\n| Feb | $1,500 | 40% |\n"
            "total: Revenue\nchart: column \"R\" x=Month y=Revenue\n"
            "# Dates\nName,Day\nA,2026-01-05\n", output=out)
        from openpyxl import load_workbook
        book = load_workbook(out)
        sheet = book["Budget"]
        self.assertEqual(sheet["B2"].value, 1200)
        self.assertAlmostEqual(sheet["C2"].value, 0.33)
        self.assertEqual(sheet["B4"].value, "=SUM(B2:B3)")
        self.assertEqual(len(sheet._charts), 1)
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertIsInstance(book["Dates"]["B2"].value, datetime.datetime)

    def test_sheet_names_are_made_safe_and_unique(self):
        out = Path(tempfile.mkdtemp()) / "n.xlsx"
        od.build_spreadsheet("# A/B?\n1,2\n# A/B?\n3,4\n", output=out)
        from openpyxl import load_workbook
        names = load_workbook(out).sheetnames
        self.assertEqual(len(set(n.lower() for n in names)), 2)

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            od.build_spreadsheet("nothing here", output=Path(tempfile.mkdtemp()) / "e.xlsx")

    def test_cell_typing(self):
        self.assertEqual(od.typed_cell("1,234")[0], 1234)
        self.assertEqual(od.typed_cell("12%")[0], 0.12)
        self.assertEqual(od.typed_cell("\u20b9500")[0], 500)
        self.assertEqual(od.typed_cell("007x")[0], "007x")
        self.assertEqual(od.typed_cell("=A1+1")[0], "=A1+1")
        self.assertIsNone(od.typed_cell("  ")[0])


class Pdf(unittest.TestCase):
    def test_pdf_roundtrip_and_pages(self):
        out = Path(tempfile.mkdtemp()) / "p.pdf"
        long = "\n\n".join(f"Paragraph {i} " + "text " * 60 for i in range(60))
        info = od.build_pdf("# Notes\n" + long, output=out)
        self.assertGreater(info["pages"], 1)
        self.assertIn("Paragraph 3", od.extract_text(out, 60000))


class Calculator(unittest.TestCase):
    def test_expressions(self):
        cases = {"15% of 2400": "360", "2^10": "1,024", "sqrt(144)+3": "15",
                 "(1200 + 300) * 2": "3,000", "10 / 4": "2.5", "100 \u00f7 8": "12.5",
                 "50% of 80 + 5": "45"}
        for text, expected in cases.items():
            self.assertIn(expected, life_tools.calculate(text), text)

    def test_unsafe_and_broken_input_is_refused(self):
        for text in ("__import__('os').system('x')", "open('f')", "1/0", "9**9**9",
                     "", "2 +", "a + 1", "x" * 400):
            with self.assertRaises(ValueError):
                life_tools.calculate(text)


class WorldTime(unittest.TestCase):
    def test_known_places(self):
        self.assertIn("Asia/Tokyo", life_tools.time_in("Tokyo"))
        self.assertIn("America/New_York", life_tools.time_in("new york"))
        self.assertIn("Europe/London", life_tools.time_in("UK"))

    def test_conversion(self):
        text = life_tools.time_in("Tokyo", "5pm")
        self.assertIn("17:00 here is", text)

    def test_unknown_place_and_bad_time(self):
        with self.assertRaises(ValueError):
            life_tools.time_in("Atlantis")
        with self.assertRaises(ValueError):
            life_tools.time_in("Tokyo", "25:99")


class Notes(unittest.TestCase):
    def test_add_search_delete(self):
        with tempfile.TemporaryDirectory() as base:
            added = life_tools.add_note(base, "Call the bank about the card", "money, todo")
            note_id = __import__("re").search(r"\[(n\w+)\]", added).group(1)
            life_tools.add_note(base, "Idea: wake word shortcut")
            self.assertIn("bank", life_tools.list_notes(base, "card"))
            self.assertNotIn("wake word", life_tools.list_notes(base, "card"))
            self.assertIn("#money", life_tools.list_notes(base))
            self.assertIn("No notes found", life_tools.list_notes(base, "zebra"))
            life_tools.delete_note(base, note_id)
            self.assertNotIn("bank", life_tools.list_notes(base))
            with self.assertRaises(ValueError):
                life_tools.delete_note(base, note_id)

    def test_empty_note_refused(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(ValueError):
                life_tools.add_note(base, "  ")

    def test_corrupt_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            life_tools.add_note(base, "good one")
            with open(life_tools._notes_file(base), "a", encoding="utf-8") as h:
                h.write("not json\n")
            self.assertIn("good one", life_tools.list_notes(base))


class Archives(unittest.TestCase):
    def test_zip_and_unzip_roundtrip(self):
        with tempfile.TemporaryDirectory() as base:
            src = Path(base) / "data"
            (src / "sub").mkdir(parents=True)
            (src / "a.txt").write_text("hello")
            (src / "sub" / "b.txt").write_text("world")
            zipped = life_tools.zip_path(str(src))
            self.assertIn("2 file(s)", zipped)
            out = Path(base) / "out"
            life_tools.unzip_file(str(Path(base) / "data.zip"), str(out))
            self.assertEqual((out / "data" / "sub" / "b.txt").read_text(), "world")

    def test_zip_slip_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            bad = Path(base) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("../escape.txt", "x")
            with self.assertRaises(ValueError):
                life_tools.unzip_file(str(bad), str(Path(base) / "dest"))
            self.assertFalse((Path(base) / "escape.txt").exists())

    def test_zip_never_overwrites(self):
        with tempfile.TemporaryDirectory() as base:
            f = Path(base) / "f.txt"
            f.write_text("x")
            life_tools.zip_path(str(f))
            self.assertIn("(2)", life_tools.zip_path(str(f)))


class PdfTools(unittest.TestCase):
    def make(self, folder, name, pages):
        out = Path(folder) / name
        od.build_pdf("# " + name + "\n" + "\n\n===\n\n".join(f"Page body {i}" for i in range(pages)),
                     output=out)
        return out

    def test_merge_and_extract(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b = self.make(folder, "a.pdf", 2), self.make(folder, "b.pdf", 3)
            merged = life_tools.pdf_merge(f"{a}; {b}")
            self.assertIn("Merged 2 PDFs", merged)
            out = Path(merged.split(" into ")[1].split(" (")[0])
            self.assertTrue(out.exists())
            extracted = life_tools.pdf_extract(str(b), "1-2")
            self.assertIn("2 page(s)", extracted)
            with self.assertRaises(ValueError):
                life_tools.pdf_extract(str(b), "9")
            with self.assertRaises(ValueError):
                life_tools.pdf_merge(str(a))


class Download(unittest.TestCase):
    def test_non_http_and_private_urls_are_refused(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://x/y", "http://127.0.0.1/secret",
                    "http://192.168.1.1/x"):
            with self.assertRaises(ValueError):
                life_tools.download_file(url, tempfile.gettempdir())

    def test_download_writes_and_never_overwrites(self):
        class Response:
            headers = {"Content-Type": "text/plain", "Content-Length": "5"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"hello"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as folder, \
                patch("browser._validate_url", side_effect=lambda u: u), \
                patch("requests.get", return_value=Response()):
            first = life_tools.download_file("https://example.com/a.txt", folder)
            second = life_tools.download_file("https://example.com/a.txt", folder)
            self.assertIn("5 bytes", first)
            self.assertIn("a (2).txt", second)
            self.assertFalse([p for p in Path(folder).iterdir() if p.suffix == ".part"])

    def test_oversize_is_stopped(self):
        class Big:
            headers = {"Content-Length": str(10 ** 12)}

            def raise_for_status(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("browser._validate_url", side_effect=lambda u: u), \
                patch("requests.get", return_value=Big()):
            with self.assertRaises(ValueError):
                life_tools.download_file("https://example.com/big", tempfile.gettempdir())


class ThroughTheDispatcher(unittest.TestCase):
    """The real path a model's tag takes: scan, validate, authorise, run."""

    def test_a_presentation_made_from_a_tag_body(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "talk.pptx")
            results = dispatch(
                f'<ACTION type="CREATE_PRESENTATION" title="Talk" theme="light" path="{target}">'
                f"{DECK}</ACTION>")
            self.assertTrue(results, "the dispatcher ran nothing")
            self.assertIn("5-slide presentation", results[0]["output"], results[0]["output"])
            self.assertTrue(os.path.exists(target))

    def test_a_spreadsheet_and_a_document_through_the_dispatcher(self):
        with tempfile.TemporaryDirectory() as folder:
            sheet = os.path.join(folder, "s.xlsx")
            doc = os.path.join(folder, "d.docx")
            r1 = dispatch(f'<ACTION type="CREATE_SPREADSHEET" path="{sheet}"># T\n| a | b |\n|---|---|\n| 1 | 2 |</ACTION>')
            r2 = dispatch(f'<ACTION type="CREATE_DOCUMENT" path="{doc}"># Memo\nHello.</ACTION>')
            self.assertIn("1 sheet", r1[0]["output"].replace("(s)", ""))
            self.assertIn("Word document", r2[0]["output"])

    def test_an_empty_body_says_what_is_missing(self):
        results = dispatch('<ACTION type="CREATE_PRESENTATION" title="x"></ACTION>')
        self.assertIn("no content", results[0]["output"])

    def test_calculate_and_notes_through_the_dispatcher(self):
        results = dispatch('<ACTION type="CALCULATE" expression="15% of 2400" />')
        self.assertIn("= 360", results[0]["output"])

    def test_missing_required_attributes_are_rejected_by_the_schema(self):
        results = dispatch('<ACTION type="DOWNLOAD_FILE" />')
        self.assertNotIn("[Success]", str(results))


class Wiring(unittest.TestCase):
    NEW = ("CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF",
           "EXTRACT_TEXT", "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE",
           "CALCULATE", "TIME_IN", "NOTE_ADD", "NOTES_LIST", "NOTE_DELETE",
           "PDF_MERGE", "PDF_EXTRACT")
    WRITES = ("CREATE_PRESENTATION", "CREATE_DOCUMENT", "CREATE_SPREADSHEET", "CREATE_PDF",
              "OPEN_PATH", "DOWNLOAD_FILE", "ZIP_PATH", "UNZIP_FILE", "NOTE_ADD",
              "NOTE_DELETE", "PDF_MERGE", "PDF_EXTRACT")

    def test_known_schema_checked(self):
        for name in self.NEW:
            self.assertIn(name, autonomy.KNOWN_ACTIONS, name)
            self.assertIn(name, action_schema.ACTION_SPECS, name)

    def test_writes_have_receipts_and_are_never_proactive(self):
        for name in self.WRITES:
            self.assertIn(name, actions.RECEIPT_CONTRACTS, name)
            self.assertIn(name, autonomy.PROACTIVE_BLOCKED_ACTIONS, name)

    def test_files_and_notes_are_personal_but_calculators_are_not(self):
        for name in ("CREATE_PRESENTATION", "EXTRACT_TEXT", "NOTES_LIST", "DOWNLOAD_FILE"):
            self.assertTrue(autonomy.is_personal_action(name), name)
        for name in ("CALCULATE", "TIME_IN"):
            self.assertFalse(autonomy.is_personal_action(name), name)

    def test_sleep_mode_blocks_file_creation(self):
        import mode_manager
        for name in ("CREATE_PRESENTATION", "DOWNLOAD_FILE", "ZIP_PATH"):
            self.assertIn(name, mode_manager._FILE_WRITE_ACTIONS)


if __name__ == "__main__":
    unittest.main()


class EdgeCases(unittest.TestCase):
    def test_dotted_names_are_not_mangled(self):
        with tempfile.TemporaryDirectory() as folder:
            out = od.resolve_output(os.path.join(folder, "plan v1.2"), "t", "pptx")
            self.assertEqual(out.name, "plan v1.2.pptx")

    def test_a_named_deck_that_opens_with_content_gets_a_title_slide(self):
        folder = tempfile.mkdtemp()
        out = Path(folder) / "t.pptx"
        info = od.build_presentation("# Only slide\n- a\n- b", title="My Deck", output=out)
        self.assertEqual(info["slides"], 2)
        from pptx import Presentation
        self.assertIn("My Deck", Presentation(str(out)).slides[0].shapes[0].text_frame.text
                      + " ".join(sh.text_frame.text for sh in Presentation(str(out)).slides[0].shapes
                                 if sh.has_text_frame))

    def test_binary_files_are_refused_not_dumped(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "x.ppt").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 50)
        with self.assertRaises(ValueError):
            od.extract_text(folder / "x.ppt")
        with self.assertRaises(ValueError):
            od.extract_text(folder / "missing.txt")

    def test_a_quote_after_a_table_is_not_lost(self):
        folder = Path(tempfile.mkdtemp())
        out = folder / "q.pptx"
        info = od.build_presentation("# T\n\n# S\n| a | b |\n|---|---|\n| 1 | 2 |\n> kept\n", output=out)
        self.assertEqual(info["slides"], 3)
        self.assertIn("kept", od.extract_text(out))

    def test_a_second_picture_continues_instead_of_vanishing(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        for name in ("a.png", "b.png"):
            Image.new("RGB", (100, 80), (1, 2, 3)).save(folder / name)
        out = folder / "i.pptx"
        info = od.build_presentation(f"# T\n\n# S\n![a]({folder/'a.png'})\n![b]({folder/'b.png'})\n", output=out)
        self.assertEqual(info["slides"], 3)

    def test_thousands_separators_are_not_split(self):
        self.assertEqual(od.split_values("$1,200, 15%"), ["$1,200", "15%"])
        self.assertEqual(od.split_values("10,12,14"), ["10", "12", "14"])
        self.assertEqual(od.split_values("1,200,300"), ["1,200,300"])
