from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib.util

# Load post-coverage-status.py as a module despite the hyphen in the filename.
script_path = Path(__file__).parent / "post-coverage-status.py"
spec = importlib.util.spec_from_file_location("post_coverage_status", script_path)
assert spec and spec.loader
post_coverage_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post_coverage_status)


class PostCoverageStatusTests(unittest.TestCase):
    def test_description_format(self) -> None:
        summary = {
            "cumulative_percent": 99.85,
            "cumulative_covered": 1234,
            "cumulative_total": 1236,
            "measured_impls": 4,
        }
        description = post_coverage_status._description(summary)
        self.assertEqual(
            description, "99.85% cumulative (1234/1236 pooled across 4 impls)"
        )

    def test_skips_when_env_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = post_coverage_status.main(["post-coverage-status.py"])
            self.assertEqual(code, 0)
            self.assertIn("skipping (not in CI)", stdout.getvalue())

    @patch("urllib.request.urlopen")
    def test_posts_error_when_summary_missing(self, mock_urlopen: MagicMock) -> None:
        env = {
            "GITHUB_SERVER_URL": "https://gitea.fleet.sticktoitive.net",
            "GITHUB_REPOSITORY": "schoen/agent-walker",
            "GITHUB_SHA": "abc1234567890",
            "GITHUB_RUN_ID": "42",
            "GITHUB_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, env, clear=True):
            code = post_coverage_status.main(
                ["post-coverage-status.py", "--summary", "/nonexistent/summary.json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(mock_urlopen.call_count, 1)
            request = mock_urlopen.call_args[0][0]
            self.assertEqual(
                request.full_url,
                "https://gitea.fleet.sticktoitive.net/api/v1/repos/schoen/agent-walker/statuses/abc1234567890",
            )
            self.assertEqual(request.get_header("Authorization"), "token secret-token")
            data = json.loads(request.data.decode("utf-8"))
            self.assertEqual(data["context"], "pr-crew/coverage")
            self.assertEqual(data["state"], "error")
            self.assertEqual(data["description"], "coverage summary unreadable")

    @patch("urllib.request.urlopen")
    def test_posts_success_when_summary_valid(self, mock_urlopen: MagicMock) -> None:
        env = {
            "GITHUB_SERVER_URL": "https://gitea.fleet.sticktoitive.net",
            "GITHUB_REPOSITORY": "schoen/agent-walker",
            "GITHUB_SHA": "abc1234567890",
            "GITHUB_RUN_ID": "42",
            "GITHUB_TOKEN": "secret-token",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_path = Path(tmpdir) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "cumulative_percent": 99.85,
                        "cumulative_covered": 1234,
                        "cumulative_total": 1236,
                        "measured_impls": 4,
                    }
                )
            )
            with patch.dict(os.environ, env, clear=True):
                code = post_coverage_status.main(
                    ["post-coverage-status.py", "--summary", str(summary_path)]
                )
                self.assertEqual(code, 0)
                self.assertEqual(mock_urlopen.call_count, 1)
                request = mock_urlopen.call_args[0][0]
                data = json.loads(request.data.decode("utf-8"))
                self.assertEqual(data["context"], "pr-crew/coverage")
                self.assertEqual(data["state"], "success")
                self.assertEqual(
                    data["description"],
                    "99.85% cumulative (1234/1236 pooled across 4 impls)",
                )

    @patch("urllib.request.urlopen")
    def test_posts_with_github_api_url(self, mock_urlopen: MagicMock) -> None:
        env = {
            "GITHUB_SERVER_URL": "https://gitea.fleet.sticktoitive.net",
            "GITHUB_API_URL": "https://gitea.fleet.sticktoitive.net/api/v1",
            "GITHUB_REPOSITORY": "schoen/agent-walker",
            "GITHUB_SHA": "abc1234567890",
            "GITHUB_RUN_ID": "42",
            "GITHUB_TOKEN": "secret-token",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_path = Path(tmpdir) / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "cumulative_percent": 100.0,
                        "cumulative_covered": 500,
                        "cumulative_total": 500,
                        "measured_impls": 2,
                    }
                )
            )
            with patch.dict(os.environ, env, clear=True):
                code = post_coverage_status.main(
                    ["post-coverage-status.py", "--summary", str(summary_path)]
                )
                self.assertEqual(code, 0)
                self.assertEqual(mock_urlopen.call_count, 1)
                request = mock_urlopen.call_args[0][0]
                self.assertEqual(
                    request.full_url,
                    "https://gitea.fleet.sticktoitive.net/api/v1/repos/schoen/agent-walker/statuses/abc1234567890",
                )


if __name__ == "__main__":
    unittest.main()
