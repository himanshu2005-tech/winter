import tempfile
import unittest
from pathlib import Path
from queue import Queue

import approval_isolation
from unittest.mock import MagicMock, patch

import actions
import autonomy
from providers import drive_api


class DriveProviderTests(unittest.TestCase):
    def test_search_escapes_apostrophe_and_returns_stable_ids(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "abc", "name": "Himanshu's CV.pdf",
                       "mimeType": "application/pdf", "size": "120"}]
        }
        with patch.object(drive_api, "_service", return_value=service):
            result = drive_api.search_files(".", "Himanshu's CV")
        call = service.files.return_value.list.call_args.kwargs
        self.assertIn("Himanshu\\'s CV", call["q"])
        self.assertIn("id=abc", result["text"])

    def test_relative_download_destination_is_confined_to_managed_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            target = drive_api._safe_destination(folder, "reports/a.pdf", "unused.pdf")
            expected = Path(folder).resolve() / "vault" / "downloads" / "google_drive"
            self.assertIn(expected, target.parents)

    def test_existing_download_is_not_overwritten_by_default(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "x", "name": "report.pdf", "mimeType": "application/pdf"}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.pdf"
            target.write_text("original", encoding="utf-8")
            with patch.object(drive_api, "_service", return_value=service):
                with self.assertRaises(FileExistsError):
                    drive_api.download_file(folder, "x", str(target))
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_native_document_export_uses_pdf_and_atomic_part_file(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "doc", "name": "Plan",
            "mimeType": "application/vnd.google-apps.document"}

        class Downloader:
            def __init__(self, handle, request, chunksize):
                self.handle = handle

            def next_chunk(self):
                self.handle.write(b"pdf")
                return None, True

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(drive_api, "_service", return_value=service), \
             patch.object(drive_api, "MediaIoBaseDownload", Downloader):
            result = drive_api.download_file(folder, "doc")
            saved = Path(folder) / "vault" / "downloads" / "google_drive" / "Plan.pdf"
            self.assertEqual(saved.read_bytes(), b"pdf")
            self.assertFalse(Path(str(saved) + ".part").exists())
            self.assertIn("Downloaded", result)
        service.files.return_value.export_media.assert_called_once_with(
            fileId="doc", mimeType="application/pdf")


class DrivePolicyAndDispatchTests(unittest.TestCase):
    def setUp(self):
        actions.reset_task_ledger("drive test")

    def test_unknown_drive_mutations_are_blocked_proactively(self):
        for name in ("DRIVE_UPLOAD", "DRIVE_CREATE_FOLDER", "DRIVE_RENAME",
                     "DRIVE_MOVE", "DRIVE_COPY", "DRIVE_TRASH", "DRIVE_SHARE"):
            allowed, _ = autonomy.action_allowed(name, "proactive")
            self.assertFalse(allowed, name)

    def test_upload_trash_and_share_require_real_confirmation(self):
        for name in ("DRIVE_UPLOAD", "DRIVE_TRASH", "DRIVE_SHARE"):
            self.assertTrue(autonomy.confirmation_required(name), name)

    @patch("providers.drive_api.search_files")
    def test_search_dispatches_to_provider(self, search):
        search.return_value = {"files": [], "text": "Drive search: none found."}
        queue = Queue()
        actions.execute_actions(
            '<ACTION type="DRIVE_SEARCH" query="exam notes" />', {}, queue, ".")
        search.assert_called_once()
        self.assertIn("none found", actions.execute_actions._last_output)

    @patch("providers.drive_api.trash_item")
    def test_trash_does_not_execute_without_human_approval(self, trash):
        queue = Queue()
        # Nothing is trashed because the provider is mocked -- but reaching
        # the gate used to write a real Approval node, leaving a pending
        # DRIVE_TRASH in the live graph after every run.
        with approval_isolation.no_approval_writes() as attempted:
            actions.execute_actions(
                '<ACTION type="DRIVE_TRASH" file_id="abc" />', {}, queue, ".")
        trash.assert_not_called()
        self.assertIn("Approval Required", actions.execute_actions._last_output)
        self.assertIn("DRIVE_TRASH",
                      approval_isolation.queued_action_types(attempted))


if __name__ == "__main__":
    unittest.main()
