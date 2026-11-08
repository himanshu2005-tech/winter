import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import documents
import memory


class UniversalExtractionTests(unittest.TestCase):
    def test_unknown_but_textual_extension_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "notes.custom")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("A meaningful personal note")
            self.assertIn("meaningful personal note", memory.extract_text(path))

    def test_opaque_binary_is_not_misrepresented_as_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.bin")
            with open(path, "wb") as handle:
                handle.write(b"\x00\x01\x02\xff" * 100)
            self.assertEqual(memory.extract_text(path), "")

    def test_single_file_is_idempotently_chunked_into_graph_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "profile.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Himanshu prefers concise explanations.")
            with mock.patch("memory.neo4j_store.store.upsert_memory_batch",
                            return_value=1) as write:
                result = documents.ingest_path(path)
            self.assertIn("[Success]", result)
            row = write.call_args.args[0][0]
            self.assertTrue(row["id"].startswith("file_"))
            self.assertEqual(row["source"], path)

    def test_secret_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "credentials.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"token":"secret"}')
            self.assertIn("credential/secret", documents.ingest_path(path))


if __name__ == "__main__":
    unittest.main()
