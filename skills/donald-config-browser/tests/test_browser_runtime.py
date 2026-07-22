from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import browser_runtime as runtime  # noqa: E402


def _config(root: Path) -> dict[str, object]:
    return {
        "scope": "donald-collect-x",
        "profile": {"directory": "Profile 1"},
        "chrome": {
            "cdp_user_data_dir": str(root),
            "default_cdp_port": 9333,
            "executable": "chrome",
        },
    }


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        frontmost = mock.patch.object(runtime, "frontmost_process_id", return_value=None)
        frontmost.start()
        self.addCleanup(frontmost.stop)

    def _open_session(self, temporary: str) -> runtime.BrowserSession:
        session = runtime.BrowserSession(
            scope="donald-collect-x",
            session="test-run",
            url="https://x.com/example",
            config=_config(Path(temporary)),
        )
        with (
            mock.patch.object(runtime, "frontmost_process_id", return_value=None),
            mock.patch.object(
                runtime,
                "start_cdp_browser",
                return_value={"launched": True, "pid": 1234},
            ),
            mock.patch.object(session, "_claim_target", return_value="owned-target"),
            mock.patch.object(session, "_verify_attach") as attach,
            mock.patch.object(
                runtime,
                "cdp_target",
                return_value={"id": "owned-target", "webSocketDebuggerUrl": "ws://owned"},
            ),
            mock.patch.object(runtime, "show_browser_without_focus"),
        ):
            attach.side_effect = lambda _own_blanks: session.owned_target_ids.add("attach-blank")
            session.open()
        return session

    def test_closes_only_owned_targets_and_the_browser_it_launched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = self._open_session(temporary)
            with (
                mock.patch.object(runtime, "cdp_target", return_value={"id": "open"}),
                mock.patch.object(runtime, "close_background_page") as close_target,
                mock.patch.object(
                    runtime,
                    "close_cdp_browser",
                    return_value={"status": "closed"},
                ) as close_browser,
            ):
                cleanup = session.close()

        self.assertEqual(cleanup["status"], "closed")
        self.assertEqual(
            {call.args[1] for call in close_target.call_args_list},
            {"owned-target", "attach-blank"},
        )
        close_browser.assert_called_once()

    def test_close_guards_cleanup_focus_without_restoring_stale_session_pid(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = self._open_session(temporary)
            session.previous_frontmost_pid = 111
            with (
                mock.patch.object(runtime, "frontmost_process_id", return_value=222),
                mock.patch.object(runtime, "cdp_target", return_value={"id": "open"}),
                mock.patch.object(runtime, "close_background_page"),
                mock.patch.object(
                    runtime,
                    "close_cdp_browser",
                    side_effect=lambda *_args: events.append("browser_close")
                    or {"status": "closed"},
                ) as close_browser,
                mock.patch.object(
                    runtime,
                    "restore_frontmost_process",
                    create=True,
                ) as restore_stale,
                mock.patch.object(
                    runtime,
                    "restore_frontmost_process_if_browser_active",
                    create=True,
                ) as guard_focus,
            ):
                guard_focus.side_effect = lambda *_args: events.append("focus_guard")
                session.close()

        guard_focus.assert_called_once_with(222, 9333)
        restore_stale.assert_not_called()
        close_browser.assert_called_once()
        self.assertEqual(events, ["focus_guard", "browser_close"])

    def test_keeps_browser_open_while_another_skill_run_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = self._open_session(temporary)
            state = json.loads(session.state_path.read_text(encoding="utf-8"))
            state["active_runs"].append(
                {"run_id": "other-run", "pid": os.getpid(), "scope": "donald-collect-wechat"}
            )
            session.state_path.write_text(json.dumps(state), encoding="utf-8")
            with (
                mock.patch.object(runtime, "cdp_target", return_value={"id": "open"}),
                mock.patch.object(runtime, "close_background_page"),
                mock.patch.object(runtime, "close_cdp_browser") as close_browser,
            ):
                session.close()

        close_browser.assert_not_called()

    def test_needs_ops_keeps_main_target_but_closes_attach_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = self._open_session(temporary)
            session.keep_open_for_human = True
            with (
                mock.patch.object(runtime, "cdp_target", return_value={"id": "open"}),
                mock.patch.object(runtime, "close_background_page") as close_target,
                mock.patch.object(runtime, "close_cdp_browser") as close_browser,
            ):
                cleanup = session.close()
            state = json.loads(session.state_path.read_text(encoding="utf-8"))

        self.assertEqual(cleanup["status"], "kept_open_for_human")
        self.assertEqual([call.args[1] for call in close_target.call_args_list], ["attach-blank"])
        self.assertEqual(state["retained_targets"][0]["target_id"], "owned-target")
        close_browser.assert_not_called()

    def test_failed_open_rolls_back_the_target_and_browser_under_the_lane_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = runtime.BrowserSession(
                scope="donald-collect-x",
                session="failed-run",
                url="https://x.com/example",
                config=_config(Path(temporary)),
            )
            with (
                mock.patch.object(runtime, "frontmost_process_id", side_effect=[111, 222]),
                mock.patch.object(
                    runtime,
                    "start_cdp_browser",
                    return_value={"launched": True, "pid": 1234},
                ),
                mock.patch.object(runtime, "create_background_page", return_value="failed-target"),
                mock.patch.object(
                    runtime,
                    "wait_for_background_page_url",
                    side_effect=runtime.ProfileConfigError("navigation failed"),
                ),
                mock.patch.object(runtime, "cdp_target", return_value={"id": "failed-target"}),
                mock.patch.object(runtime, "close_background_page") as close_target,
                mock.patch.object(
                    runtime,
                    "close_cdp_browser",
                    return_value={"status": "closed"},
                ) as close_browser,
                mock.patch.object(
                    runtime,
                    "restore_frontmost_process_if_browser_active",
                ) as guard_focus,
            ):
                with self.assertRaises(runtime.ProfileConfigError):
                    session.open()

        close_target.assert_called_once_with(9333, "failed-target")
        guard_focus.assert_called_once_with(222, 9333)
        close_browser.assert_called_once()

    def test_attach_tracks_blank_target_that_appears_after_command_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = runtime.BrowserSession(
                scope="donald-collect-x",
                session="delayed-blank",
                config=_config(Path(temporary)),
            )
            polls = 0

            def targets(_port: int) -> list[dict[str, str]]:
                nonlocal polls
                polls += 1
                rows = [{"id": "existing", "type": "page", "url": "https://example.com"}]
                if polls >= 3:
                    rows.append({"id": "delayed-blank", "type": "page", "url": "about:blank"})
                return rows

            with (
                mock.patch.object(runtime, "_target_ids", return_value={"existing"}),
                mock.patch.object(
                    runtime,
                    "ensure_agent_browser",
                    return_value={"executable": "agent-browser"},
                ),
                mock.patch.object(
                    runtime.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout="https://example.com"),
                ),
                mock.patch.object(runtime, "list_cdp_targets", side_effect=targets),
                mock.patch.object(runtime.time, "sleep"),
            ):
                session._verify_attach(own_new_blank_targets=True)

        self.assertIn("delayed-blank", session.owned_target_ids)


if __name__ == "__main__":
    unittest.main()
