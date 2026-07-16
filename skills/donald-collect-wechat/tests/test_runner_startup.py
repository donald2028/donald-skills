from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import collect_account_articles as account_runner  # noqa: E402
import fetch_public_article_bodies as body_runner  # noqa: E402


CONFIG = {
    "chrome": {"default_cdp_port": 9345},
    "profile": {"directory": "Profile 19"},
}


class RunnerStartupTests(unittest.TestCase):
    def test_account_runner_starts_configured_browser_without_cdp_argument(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["collect", "--account", "Example"]),
            mock.patch.object(account_runner, "configured_browser", return_value=CONFIG),
            mock.patch.object(
                account_runner,
                "preflight_browser",
                side_effect=account_runner.ProfileConfigError("blocked"),
            ) as preflight,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = account_runner.main()

        self.assertEqual(result, 2)
        preflight.assert_called_once_with(
            CONFIG,
            9345,
            "donald-wechat-collect",
            "about:blank",
            60,
        )

    def test_body_runner_starts_configured_browser_without_cdp_argument(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["fetch", "/tmp/archive"]),
            mock.patch.object(body_runner, "configured_browser", return_value=CONFIG),
            mock.patch.object(
                body_runner,
                "preflight_browser",
                side_effect=body_runner.ProfileConfigError("blocked"),
            ) as preflight,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = body_runner.main()

        self.assertEqual(result, 2)
        preflight.assert_called_once_with(
            CONFIG,
            9345,
            "donald-wechat-bodies",
            "about:blank",
            60,
        )


if __name__ == "__main__":
    unittest.main()
