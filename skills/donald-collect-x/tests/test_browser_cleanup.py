from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import research_post  # noqa: E402


class XBrowserCleanupTests(unittest.TestCase):
    def test_post_runner_uses_owned_target_and_closes_it(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {"X_COLLECTOR_CHROME_DATA_DIR": "", "X_COLLECTOR_CHROME_EXECUTABLE": ""},
            ),
            mock.patch.object(research_post, "BrowserSession") as session_type,
            mock.patch.object(research_post.capture_thread, "AgentBrowser") as browser_type,
            mock.patch.object(
                research_post.capture_thread,
                "capture",
                return_value={"status": "complete"},
            ),
            mock.patch.object(
                research_post.extract_thread,
                "write_thread",
                return_value={"count": 1, "posts": []},
            ),
            mock.patch.object(
                research_post.download_media,
                "download_thread",
                return_value={"count": 1, "posts": []},
            ),
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.target_id = "owned-x-target"
            session.close.return_value = {"status": "closed"}
            result = research_post.run(
                "https://x.com/example/status/123",
                Path(temporary),
                port=9333,
            )

        browser_type.assert_called_once_with(9333, target_id="owned-x-target")
        browser_type.return_value.close.assert_called_once_with()
        session.close.assert_called_once_with()
        self.assertEqual(result["browser_cleanup"]["status"], "closed")

    def test_login_wall_preserves_the_owned_target_for_human_action(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {"X_COLLECTOR_CHROME_DATA_DIR": "", "X_COLLECTOR_CHROME_EXECUTABLE": ""},
            ),
            mock.patch.object(research_post, "BrowserSession") as session_type,
            mock.patch.object(research_post.capture_thread, "AgentBrowser"),
            mock.patch.object(
                research_post.capture_thread,
                "capture",
                return_value={"status": "needs_ops", "reason": "login_wall"},
            ),
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.target_id = "owned-x-target"
            session.preserve_for_human.return_value = {"status": "activated"}
            session.close.return_value = {"status": "kept_open_for_human"}
            result = research_post.run(
                "https://x.com/example/status/123",
                Path(temporary),
                port=9333,
            )

        session.preserve_for_human.assert_called_once_with()
        self.assertEqual(result["browser_cleanup"]["status"], "kept_open_for_human")


if __name__ == "__main__":
    unittest.main()
