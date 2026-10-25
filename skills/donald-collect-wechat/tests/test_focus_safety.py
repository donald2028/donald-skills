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

    def test_editor_opens_in_an_owned_background_target_without_a_foreground_popup(self) -> None:
        with (
            mock.patch.object(account_runner, "_click_exact_text") as click,
            mock.patch.object(account_runner, "create_background_page", return_value="editor-target") as create,
        ):
            target_id = account_runner._open_editor_target(
                port=9244,
                home_url="https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=abc",
            )

        self.assertEqual(target_id, "editor-target")
        click.assert_not_called()
        self.assertEqual(create.call_args.args[0], 9244)
        self.assertIn('/cgi-bin/appmsg?', create.call_args.args[1])
        self.assertIn('token=abc', create.call_args.args[1])

    def test_blocked_article_retains_background_browser_without_activation(self) -> None:
        with (
            mock.patch.object(body_runner, 'create_background_page', return_value='article'),
            mock.patch.object(body_runner, 'wait_for_background_page_url'),
            mock.patch.object(body_runner, 'call_background_page'),
            mock.patch.object(body_runner, '_evaluate_background_page', return_value={'content_chars': 20}),
            mock.patch.object(body_runner, '_article_from_cdp_payload', return_value={
                'blocked_reason': 'WeChat login page', 'fetch_status': 'blocked'}),
            mock.patch.object(body_runner, 'configured_browser', return_value={}),
            mock.patch.object(body_runner, 'show_browser_without_focus', create=True,
                              return_value={'status': 'visible_in_background'}) as show,
            mock.patch.object(body_runner, 'activate_browser', create=True) as activate,
        ):
            article = body_runner.fetch_article_with_cdp('https://mp.weixin.qq.com/s/example', '9244')
        self.assertEqual(article['browser_tab_cleanup'], 'kept_open_for_human')
        self.assertEqual(article['browser_activation']['status'], 'visible_in_background')
        show.assert_called_once_with({}, 9244)
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
