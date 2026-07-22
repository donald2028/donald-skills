from __future__ import annotations

import argparse
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class BrowserCleanupTests(unittest.TestCase):
    def test_runner_prepares_the_shared_browser_runtime(self) -> None:
        args = argparse.Namespace(
            session="chatgpt-test",
            cdp="9333",
            cdp_port="9333",
            profile="Profile 1",
            user_data_dir="/tmp/chrome",
            executable_path="chrome",
            no_launch_browser=False,
            lock_timeout=30,
            _uses_shared_browser_profile=True,
        )
        config = {
            "scope": "donald-chatgpt-imagegen",
            "profile": {"directory": "Profile 1"},
            "chrome": {"default_cdp_port": 9333, "cdp_user_data_dir": "/tmp/chrome"},
        }
        with (
            mock.patch.object(runner, "configured_browser", return_value=config),
            mock.patch.object(runner, "BrowserSession") as session_type,
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.transport_session = "shared-transport"
            session.run_id = "run-id"
            session.target_id = "target-id"
            session.target_url = "ws://target"
            session.launched = True
            session.previous_frontmost_pid = 42
            session.port = 9333
            runner._prepare_browser_lane(args, Path.cwd())

        self.assertIs(args._browser_session, session)
        self.assertEqual(args._owned_tab_cdp_target_id, "target-id")
        self.assertEqual(args._agent_browser_transport_session, "shared-transport")

    def test_runner_cleanup_delegates_to_shared_runtime(self) -> None:
        browser_session = mock.Mock()
        browser_session.close.return_value = {"status": "closed"}
        args = argparse.Namespace(
            keep_browser_open=False,
            _browser_session=browser_session,
        )

        runner._cleanup_agent_browser(args, Path.cwd())

        browser_session.close.assert_called_once_with(preserve=False)
        self.assertEqual(args._browser_cleanup["status"], "closed")

    def test_runner_closes_idle_browser_when_no_page_targets_remain(self) -> None:
        args = argparse.Namespace(
            keep_browser_open=False,
            _active_run_id="current-run",
            _frontmost_pid_before_launch=None,
        )
        state = {
            "schema_version": 1,
            "active_runs": [{"run_id": "current-run"}],
            "launched_by_runner": False,
        }

        with (
            mock.patch.object(runner, "_close_owned_tab"),
            mock.patch.object(
                runner,
                "_close_about_blank_tabs",
                return_value={"listed": True, "about_blank_tab_ids": [], "non_blank_count": 0},
            ),
            mock.patch.object(runner, "_cdp_state_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(runner, "_read_cdp_state_locked", return_value=state),
            mock.patch.object(runner, "_write_cdp_state_locked"),
            mock.patch.object(runner, "_close_cdp_browser") as close_browser,
        ):
            runner._cleanup_agent_browser(args, Path.cwd())

        close_browser.assert_called_once_with(args, Path.cwd())

    def test_runner_keeps_browser_while_another_run_is_active(self) -> None:
        args = argparse.Namespace(
            keep_browser_open=False,
            _active_run_id="current-run",
            _frontmost_pid_before_launch=None,
        )
        state = {
            "schema_version": 1,
            "active_runs": [
                {"run_id": "current-run"},
                {"run_id": "other-run"},
            ],
            "launched_by_runner": False,
        }

        with (
            mock.patch.object(runner, "_close_owned_tab"),
            mock.patch.object(
                runner,
                "_close_about_blank_tabs",
                return_value={"listed": True, "about_blank_tab_ids": [], "non_blank_count": 0},
            ),
            mock.patch.object(runner, "_cdp_state_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(runner, "_read_cdp_state_locked", return_value=state),
            mock.patch.object(runner, "_write_cdp_state_locked"),
            mock.patch.object(runner, "_close_cdp_browser") as close_browser,
        ):
            runner._cleanup_agent_browser(args, Path.cwd())

        close_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
