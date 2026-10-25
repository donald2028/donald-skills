from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class BrowserCleanupTests(unittest.TestCase):
    def test_runner_and_legacy_helpers_cannot_fall_back_to_cli_tab_interaction(self) -> None:
        for args in (argparse.Namespace(_browser_session=object()), argparse.Namespace()):
            with self.subTest(registered=hasattr(args, '_browser_session')):
                with mock.patch.object(runner, 'ensure_agent_browser') as install:
                    with self.assertRaisesRegex(RuntimeError, 'Foreground'):
                        runner._agent_browser_base(args)
                install.assert_not_called()

    def test_verification_handoff_keeps_chrome_in_the_background(self) -> None:
        config = {'scope': 'donald-chatgpt-imagegen'}
        with (
            mock.patch.object(runner, 'configured_browser', return_value=config),
            mock.patch.object(runner, '_cdp_launch_port', return_value=9333),
            mock.patch.object(runner, 'show_browser_without_focus', return_value={'status': 'visible_in_background'}) as show,
            mock.patch.object(runner, 'activate_browser', create=True) as activate,
        ):
            result = runner._activate_verification_browser(argparse.Namespace())
        self.assertEqual(result['status'], 'visible_in_background')
        show.assert_called_once_with(config, 9333)
        activate.assert_not_called()

    def test_macos_legacy_launch_defers_its_window_to_background_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                cdp_port=9333, cdp=None, user_data_dir=temporary, profile="Default",
                browser_args="", launch_background=True, executable_path=runner.DEFAULT_CHROME,
            )
            with mock.patch.object(runner.sys, "platform", "darwin"), mock.patch.object(
                runner.subprocess, "Popen"
            ) as launch:
                runner._launch_chrome_for_cdp(args, Path(temporary))
        command = launch.call_args.args[0]
        self.assertIn("--no-startup-window", command)
        self.assertNotIn("-j", command)
        self.assertNotIn("about:blank", command)

    def test_replacement_claims_only_the_target_created_by_this_run(self) -> None:
        session = argparse.Namespace(owned_target_ids=set())
        args = argparse.Namespace(_browser_session=session, cdp='9333')
        targets = [
            {"id": "other-blank", "url": "about:blank", "webSocketDebuggerUrl": "ws://other-blank"},
            {"id": "other-new", "url": "https://chatgpt.com/c/other", "webSocketDebuggerUrl": "ws://other-new"},
            {"id": "created", "url": "https://chatgpt.com/c/ours", "webSocketDebuggerUrl": "ws://created"},
        ]
        with (
            mock.patch.object(runner, "_cdp_page_targets", return_value=targets),
            mock.patch.object(runner, "create_background_page", return_value="created") as create,
            mock.patch.object(runner, "_navigate_owned_cdp_tab", return_value=True) as navigate,
        ):
            self.assertTrue(runner._open_background_cdp_tab(args, "https://chatgpt.com/c/ours"))
        create.assert_called_once_with(9333, "https://chatgpt.com/c/ours")
        self.assertEqual(args._owned_tab_cdp_target_id, "created")
        self.assertEqual(session.owned_target_ids, {"created"})
        navigate.assert_not_called()

    def test_default_chrome_uses_shared_platform_discovery(self) -> None:
        with mock.patch.object(
            runner,
            "chrome_environment",
            return_value={"executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe"},
        ):
            executable = runner._default_chrome_executable()

        self.assertEqual(executable, r"C:\Program Files\Google\Chrome\Application\chrome.exe")

    def test_cdp_locks_work_with_the_current_platform_lock_implementation(self) -> None:
        args = argparse.Namespace(cdp=None, cdp_port=9333, user_data_dir="/tmp/chrome")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            runner,
            "resolve_tool_state_root",
            return_value=Path(temporary),
        ):
            with runner._cdp_command_lock(args, timeout_s=1):
                self.assertTrue(runner._command_lock_path_for_cdp(args).exists())
            with runner._cdp_state_lock(args, timeout_s=1):
                self.assertTrue(runner._lock_path_for_cdp(args).exists())

    def test_windows_cleanup_terminates_only_the_confirmed_cdp_process(self) -> None:
        args = argparse.Namespace(cdp=None, cdp_port=9333, user_data_dir=r"C:\Donald Chrome")
        with (
            mock.patch.object(runner.sys, "platform", "win32"),
            mock.patch.object(runner, "_listening_process_ids", return_value=[1234]),
            mock.patch.object(
                runner,
                "_process_command",
                return_value=r"chrome.exe --remote-debugging-port=9333 --user-data-dir=C:\Donald Chrome",
            ),
            mock.patch.object(runner, "_terminate_process") as terminate,
        ):
            runner._terminate_owned_cdp_chrome(args, Path.cwd())

        terminate.assert_called_once_with(1234, Path.cwd())

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
            mock.patch.object(runner, "_FreshImageBrowserSession") as session_type,
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
            _frontmost_pid_before_launch=42,
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
            mock.patch.object(runner, "restore_frontmost_process", create=True) as stale_restore,
        ):
            runner._cleanup_agent_browser(args, Path.cwd())

        close_browser.assert_called_once_with(args, Path.cwd())
        stale_restore.assert_not_called()

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
