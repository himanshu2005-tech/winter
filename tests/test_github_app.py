import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from providers import github_api


class GitHubReferenceTests(unittest.TestCase):
    def test_repository_url(self):
        self.assertEqual(
            github_api.parse_reference("https://github.com/himanshu2005-tech/tether"),
            ("himanshu2005-tech/tether", "", ""),
        )

    def test_blob_url_preserves_ref_and_path(self):
        self.assertEqual(
            github_api.parse_reference(
                "https://github.com/himanshu2005-tech/tether/blob/main/src/app.py?plain=1#L2"),
            ("himanshu2005-tech/tether", "src/app.py", "main"),
        )

    def test_non_github_url_is_rejected(self):
        with self.assertRaises(github_api.GitHubError):
            github_api.parse_reference("https://example.com/owner/repo")


class GitHubReadTests(unittest.TestCase):
    @patch("providers.github_api.request")
    def test_linked_file_is_decoded(self, request):
        request.return_value = {
            "type": "file", "encoding": "base64",
            "content": base64.b64encode(b"hello").decode(),
        }
        result = github_api.read_file(
            ".", "https://github.com/o/r/blob/dev/folder/a.txt")
        self.assertEqual(result["repo"], "o/r")
        self.assertEqual(result["path"], "folder/a.txt")
        self.assertEqual(result["ref"], "dev")
        self.assertEqual(result["content"], "hello")
        self.assertNotIn("token", str(result).lower())

    @patch("providers.github_api.requests.request")
    @patch("providers.github_api.installation_token", return_value="secret-token")
    def test_api_failure_does_not_expose_token(self, _token, request):
        response = Mock(ok=False, status_code=403)
        response.json.return_value = {"message": "Resource not accessible"}
        request.return_value = response
        with self.assertRaises(github_api.GitHubError) as caught:
            github_api.request(".", "GET", "/repos/o/r")
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
