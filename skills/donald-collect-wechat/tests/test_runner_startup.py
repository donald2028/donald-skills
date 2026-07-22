from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import collect_account_articles as account_runner  # noqa: E402
import fetch_public_article_bodies as body_runner  # noqa: E402


class RunnerStartupTests(unittest.TestCase):
    def test_account_runner_starts_configured_browser_without_cdp_argument(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["collect", "--account", "Example"]),
            mock.patch.object(account_runner, "BrowserSession") as browser_session,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            browser_session.return_value.open.side_effect = account_runner.ProfileConfigError("blocked")
            result = account_runner.main()

        self.assertEqual(result, 2)
        browser_session.assert_called_once_with(
            scope="donald-collect-wechat",
            session="donald-wechat-collect",
            url=account_runner.WECHAT_HOME,
            port=None,
        )

    def test_body_runner_starts_configured_browser_without_cdp_argument(self) -> None:
        article_url = "https://mp.weixin.qq.com/s/example"
        with (
            mock.patch.object(sys, "argv", ["fetch", "/tmp/archive"]),
            mock.patch.object(
                body_runner,
                "load_index_entries",
                return_value=(Path("/tmp/archive"), [{"url": article_url}]),
            ),
            mock.patch.object(body_runner, "BrowserSession") as browser_session,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            browser_session.return_value.open.side_effect = body_runner.ProfileConfigError("blocked")
            result = body_runner.main()

        self.assertEqual(result, 2)
        browser_session.assert_called_once_with(
            scope="donald-collect-wechat",
            session="donald-wechat-bodies",
            url=article_url,
            port=None,
        )

    def test_body_runner_does_not_open_chrome_when_nothing_was_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            with (
                mock.patch.object(sys, "argv", ["fetch", str(archive)]),
                mock.patch.object(
                    body_runner,
                    "load_index_entries",
                    return_value=(archive, []),
                ),
                mock.patch.object(body_runner, "BrowserSession") as browser_session,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = body_runner.main()

        self.assertEqual(result, 0)
        browser_session.assert_not_called()

    def test_account_ui_failure_is_structured_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            session = mock.Mock(
                port=9244,
                target_id="owned-target",
            )
            session.close.return_value = {
                "status": "closed",
                "target_id": "owned-target",
                "browser": {"status": "not_owned"},
                "errors": [],
            }
            with (
                mock.patch.object(sys, "argv", ["collect", "--account", "Example"]),
                mock.patch.object(account_runner, "BrowserSession") as browser_session,
                mock.patch.object(
                    account_runner,
                    "resolve_tool_output_root",
                    return_value=Path(temporary),
                ),
                mock.patch.object(
                    account_runner,
                    "connect_cdp_target",
                    side_effect=RuntimeError("changed UI"),
                ),
                contextlib.redirect_stdout(output),
            ):
                browser_session.return_value.open.return_value = session
                result = account_runner.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload["reason"], "wechat_ui_flow_failed")
        self.assertEqual(payload["browser_cleanup"]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
