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
