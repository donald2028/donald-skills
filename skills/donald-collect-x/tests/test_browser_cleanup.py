from __future__ import annotations

import contextlib
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
    def test_x_navigation_uses_the_shared_focus_guard_on_its_owned_target(self) -> None:
        browser = research_post.capture_thread.AgentBrowser(9333, target_id='owned')
        connection = mock.Mock()
        with (
            mock.patch.object(browser, '_x_target', return_value={'id': 'owned'}),
            mock.patch.object(browser, '_drain_network_events'),
            mock.patch.object(research_post.capture_thread.cdp_input, 'connect_to_target', return_value=connection),
            mock.patch.object(research_post.capture_thread, 'preserve_foreground', return_value=contextlib.nullcontext()) as guard,
            mock.patch.object(research_post.capture_thread.time, 'sleep'),
        ):
            browser.open('https://x.com/example/status/123')
        guard.assert_called_once_with(9333)
        connection.call.assert_any_call('Page.navigate', {'url': 'https://x.com/example/status/123'})
        browser.close()

    def test_x_tab_creation_delegates_to_the_shared_background_creator(self) -> None:
        cdp = research_post.capture_thread.cdp_input
        with (
            mock.patch.object(cdp, 'create_background_page', return_value='owned') as create,
            mock.patch.object(cdp, 'list_page_targets', return_value=[{'id': 'owned'}]),
        ):
            self.assertEqual(cdp.create_page_target(9333, 'https://x.com/example'), {'id': 'owned'})
        create.assert_called_once_with(9333, 'https://x.com/example')

    def test_direct_error_handoff_keeps_chrome_in_the_background(self) -> None:
        with (
            mock.patch.object(research_post, 'configured_browser', return_value={}),
            mock.patch.object(research_post, 'show_browser_without_focus', create=True,
                              return_value={'status': 'visible_in_background'}) as show,
            mock.patch.object(research_post, 'activate_browser', create=True) as activate,
        ):
            result = research_post.activate_for_human_attention({'status': 'needs_ops', 'reason': 'captcha'}, 9333)
        self.assertEqual(result['browser_activation']['status'], 'visible_in_background')
        show.assert_called_once_with({}, 9333)
        activate.assert_not_called()

    def test_legacy_chrome_lookup_uses_shared_platform_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "Google Chrome"
            executable.touch()
            with (
                mock.patch.dict(os.environ, {"X_COLLECTOR_CHROME_EXECUTABLE": ""}),
                mock.patch.object(
                    research_post,
                    "chrome_environment",
                    return_value={"executable": str(executable)},
                ) as discovery,
            ):
                self.assertEqual(research_post.chrome_executable(), str(executable))
            discovery.assert_called_once_with()

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
            session.preserve_for_human.return_value = {"status": "visible_in_background"}
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
