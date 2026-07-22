from __future__ import annotations

import argparse
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class DownloadRecoveryTests(unittest.TestCase):
    def test_browser_download_retries_transient_failures(self) -> None:
        payload = base64.b64encode(b"image-bytes").decode("ascii")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                runner,
                "_eval_js",
                side_effect=[RuntimeError("503"), RuntimeError("503"), '{"data_url":"data:image/png;base64,' + payload + '"}'],
            ),
            mock.patch.object(runner.time, "sleep"),
        ):
            output = Path(temporary) / "image.png"
            runner._download_url_with_browser(
                argparse.Namespace(),
                Path.cwd(),
                "https://chatgpt.com/backend-api/files/example",
                output,
            )
            self.assertEqual(output.read_bytes(), b"image-bytes")

    def test_candidate_download_records_failure_without_raising(self) -> None:
        failures: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            runner,
            "_download_urls",
            side_effect=RuntimeError("authenticated download failed"),
        ):
            downloaded = runner._download_candidates(
                argparse.Namespace(),
                Path.cwd(),
                [{"src": "https://chatgpt.com/backend-api/files/example", "width": 1024, "height": 1024}],
                Path(temporary),
                "generated",
                failures,
            )

        self.assertEqual(downloaded, [])
        self.assertEqual(failures[0]["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
