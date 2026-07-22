from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import collect_account_articles as account_runner  # noqa: E402
import fetch_public_article_bodies as body_runner  # noqa: E402


class FocusSafetyTests(unittest.TestCase):
    def test_article_body_target_does_not_hide_or_reactivate_chrome(self) -> None:
        with (
            mock.patch.object(body_runner, "frontmost_process_id", return_value=111),
            mock.patch.object(body_runner, "create_background_page", return_value="article-target"),
            mock.patch.object(body_runner, "wait_for_background_page_url"),
            mock.patch.object(body_runner, "call_background_page"),
            mock.patch.object(
                body_runner,
                "_evaluate_background_page",
                return_value={"content_chars": 20, "ready_state": "complete"},
            ),
            mock.patch.object(
                body_runner,
                "_article_from_cdp_payload",
                return_value={"blocked_reason": "", "fetch_status": "downloaded"},
            ),
            mock.patch.object(body_runner, "close_background_page"),
            mock.patch.object(body_runner, "hide_browser_without_focus", create=True) as legacy_hide,
            mock.patch.object(body_runner, "restore_frontmost_process_if_browser_active"),
        ):
            article = body_runner.fetch_article_with_cdp(
                "https://mp.weixin.qq.com/s/example",
                "9244",
            )

        self.assertEqual(article["browser_tab_cleanup"], "closed")
        legacy_hide.assert_not_called()

    def test_article_body_reuses_the_session_target_without_opening_a_blank_placeholder(self) -> None:
        with (
            mock.patch.object(body_runner, "create_background_page") as create_target,
            mock.patch.object(body_runner, "wait_for_background_page_url"),
            mock.patch.object(body_runner, "call_background_page") as call_target,
            mock.patch.object(
                body_runner,
                "_evaluate_background_page",
                return_value={"content_chars": 20, "ready_state": "complete"},
            ),
            mock.patch.object(
                body_runner,
                "_article_from_cdp_payload",
                return_value={"blocked_reason": "", "fetch_status": "downloaded"},
            ),
            mock.patch.object(body_runner, "close_background_page") as close_target,
        ):
            article = body_runner.fetch_article_with_cdp(
                "https://mp.weixin.qq.com/s/example",
                "9244",
                target_id="session-target",
            )

        self.assertEqual(article["cdp_target_id"], "session-target")
        self.assertEqual(article["browser_tab_cleanup"], "session_target_reused")
        create_target.assert_not_called()
        close_target.assert_not_called()
        call_target.assert_any_call(
            9244,
            "session-target",
            "Page.navigate",
            {"url": "https://mp.weixin.qq.com/s/example"},
            timeout=45,
        )

    def test_opening_editor_guards_the_current_app_from_the_ui_click(self) -> None:
        connection = mock.Mock()
        with (
            mock.patch.object(account_runner, "frontmost_process_id", return_value=111),
            mock.patch.object(account_runner, "_click_exact_text") as click,
            mock.patch.object(account_runner, "_wait_for_editor_target", return_value="editor-target"),
            mock.patch.object(account_runner, "restore_frontmost_process_if_browser_active") as guard,
        ):
            target_id = account_runner._open_editor_target(
                connection,
                port=9244,
                before_ids={"home-target"},
                home_target_id="home-target",
                home_url="https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=abc",
            )

        self.assertEqual(target_id, "editor-target")
        click.assert_called_once_with(connection, "文章")
        guard.assert_called_once_with(111, 9244)


if __name__ == "__main__":
    unittest.main()
