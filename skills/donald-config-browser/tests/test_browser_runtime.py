from __future__ import annotations

import contextlib
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
    def test_atexit_cleanup_respects_a_planned_takeover_stop_request(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {runtime.STATE_ROOT_ENV: temporary}):
            session = self._open_session(temporary)
            session.owner_path = Path(temporary)/'owner.json'
            (Path(temporary)/'chatgpt_worker_stop.json').write_text(json.dumps(
                {'pid':os.getpid(),'preserve_browser':True}))
            with mock.patch.object(runtime, 'cdp_target', return_value={'id':'open'}), \
                    mock.patch.object(runtime, 'close_background_page') as close_target, \
                    mock.patch.object(runtime, 'close_cdp_browser') as close_browser:
                result = session.close()
            self.assertEqual(result['status'], 'kept_open_for_human')
            self.assertNotIn(mock.call(9333,'owned-target'), close_target.call_args_list)
            close_browser.assert_not_called()

    def test_interrupt_during_open_rolls_back_instead_of_skipping_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {runtime.STATE_ROOT_ENV: temporary}):
            session = runtime.BrowserSession(session='interrupt', config=_config(Path(temporary)))
            with mock.patch.object(session, '_open_locked', side_effect=KeyboardInterrupt), \
                    mock.patch.object(session, '_cleanup_failed_open') as cleanup, self.assertRaises(KeyboardInterrupt):
                session.open()
            cleanup.assert_called_once()

    def test_stopped_worker_cleanup_protects_other_runs_and_releases_only_its_ownership(self):
        for another_run in (False, True):
            with self.subTest(another_run=another_run), tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.dict(os.environ, {runtime.STATE_ROOT_ENV: temporary}):
                session = self._open_session(temporary)
                session.owner_path = Path(temporary)/'owner.json'
                session.owner_pid = 424242
                session._save_owner()
                state = json.loads(session.state_path.read_text())
                state['active_runs'][0]['pid'] = 424242
                if another_run:
                    state['active_runs'].append({'run_id':'other','pid':os.getpid(),'target_id':'other-target'})
                session.state_path.write_text(json.dumps(state))
                with mock.patch.object(runtime, 'process_is_alive', side_effect=lambda pid:pid != 424242), \
                        mock.patch.object(runtime, 'configured_browser', return_value=session.config), \
                        mock.patch.object(runtime, 'cdp_target', return_value={'id':'open'}), \
                        mock.patch.object(runtime, 'close_background_page') as close_target, \
                        mock.patch.object(runtime, 'close_cdp_browser', return_value={'status':'closed'}) as close_browser:
                    result = runtime.cleanup_stopped_browser(session.owner_path, 424242, preserve=False)
                self.assertEqual(result['status'], 'closed')
                self.assertEqual(close_browser.call_count, 0 if another_run else 1)
                self.assertEqual({call.args[1] for call in close_target.call_args_list}, {'owned-target','attach-blank'})
                remaining = json.loads(session.state_path.read_text())['active_runs']
                self.assertEqual(len(remaining), 1 if another_run else 0)
                session.opened = False
                runtime.atexit.unregister(session._atexit_callback)

    def test_human_handoff_preserves_background_visibility_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = runtime.BrowserSession(session='handoff', config=_config(Path(temporary)))
            with (
                mock.patch.object(runtime, 'show_browser_without_focus', return_value={'status': 'visible_in_background'}) as show,
                mock.patch.object(session, 'activate') as activate,
            ):
                result = session.preserve_for_human()
            self.assertTrue(session.keep_open_for_human)
            self.assertEqual(result['status'], 'visible_in_background')
            show.assert_called_once_with(session.config, session.port)
            activate.assert_not_called()

    def test_attach_focus_guard_uses_app_active_before_the_cli_call_even_on_failure(self) -> None:
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temporary:
                session = runtime.BrowserSession(session='focus', config=_config(Path(temporary)))
                with (
                    mock.patch.object(runtime, 'frontmost_process_id', return_value=111),
                    mock.patch.object(runtime, 'ensure_agent_browser', return_value={'executable': 'agent-browser'}),
                    mock.patch.object(runtime, '_target_ids', return_value=set()),
                    mock.patch.object(runtime, 'list_cdp_targets', return_value=[]),
                    mock.patch.object(runtime, 'run_agent_browser', return_value=mock.Mock(returncode=returncode, stdout='failed')) as attach,
                    mock.patch.object(runtime, 'restore_frontmost_process_if_browser_active') as restore,
                ):
                    if returncode:
                        with self.assertRaises(runtime.ProfileConfigError):
                            session._verify_attach(False)
                    else:
                        session._verify_attach(False)
                restore.assert_called_once_with(111, 9333)
                self.assertEqual(attach.call_args.args[0][-2:], ['connect', '9333'])
                self.assertNotIn('--cdp', attach.call_args.args[0])

    def test_open_retries_disappearing_cdp_before_claiming_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {runtime.STATE_ROOT_ENV: temporary}):
            session = runtime.BrowserSession(session="startup", config=_config(Path(temporary)))
            failure = runtime.ProfileConfigError("Could not list Chrome CDP targets")
            failure.__cause__ = ConnectionRefusedError("connection refused")
            with (
                mock.patch.object(runtime, "start_cdp_browser", side_effect=[
                    {"launched": True, "pid": 1234}, {"launched": False, "pid": 1234},
                ]) as startup,
                mock.patch.object(runtime, "_target_ids", side_effect=[failure, set()]),
                mock.patch.object(session, "_claim_target", return_value="owned-target") as claim,
                mock.patch.object(runtime, "cdp_target", return_value={"webSocketDebuggerUrl": "ws://owned"}),
                mock.patch.object(session, "_verify_attach"),
                mock.patch.object(runtime, "show_browser_without_focus"),
                mock.patch.object(session, "_cleanup_failed_open") as cleanup,
                mock.patch.object(runtime.time, "sleep"),
            ):
                session.open()
            self.assertTrue(session.opened)
            self.assertTrue(session.launched)
            self.assertEqual(startup.call_count, 2)
            self.assertEqual(startup.call_args_list[0], startup.call_args_list[1])
            claim.assert_called_once()
            cleanup.assert_not_called()
            session.opened = False
            runtime.atexit.unregister(session._atexit_callback)

    def test_startup_retry_is_bounded_and_does_not_retry_owner_mismatch(self) -> None:
        for failure, expected_attempts in [(runtime.ProfileConfigError("wrong account"), 1),
                                           (runtime.ProfileConfigError("target list unavailable"), 3)]:
            if expected_attempts == 3:
                failure.__cause__ = ConnectionRefusedError("connection refused")
            with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {runtime.STATE_ROOT_ENV: temporary}):
                session = runtime.BrowserSession(session="startup", config=_config(Path(temporary)))
                with (
                    mock.patch.object(session, "_open_locked", side_effect=failure) as startup,
                    mock.patch.object(session, "_cleanup_failed_open") as cleanup,
                    mock.patch.object(runtime.time, "sleep"),
                    self.assertRaises(runtime.ProfileConfigError),
                ):
                    session.open()
                self.assertEqual(startup.call_count, expected_attempts)
                cleanup.assert_called_once()

    def setUp(self) -> None:
        frontmost = mock.patch.object(runtime, "frontmost_process_id", return_value=None)
        frontmost.start()
        self.addCleanup(frontmost.stop)
        file_lock = mock.patch.object(runtime, "_file_lock", return_value=contextlib.nullcontext())
        file_lock.start()
        self.addCleanup(file_lock.stop)

    def test_current_process_is_alive_without_signaling_it(self) -> None:
        self.assertTrue(runtime.process_is_alive(os.getpid()))

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
            mock.patch.object(runtime, "_target_ids", return_value=set()),
            mock.patch.object(runtime, "list_cdp_targets", return_value=[]),
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

    def test_launch_does_not_claim_blank_targets_it_did_not_create(self) -> None:
        for launched in (True, False):
            with self.subTest(launched=launched), tempfile.TemporaryDirectory() as temporary:
                session = runtime.BrowserSession(session="startup", config=_config(Path(temporary)))
                with (
                    mock.patch.object(runtime.sys, "platform", "darwin"),
                    mock.patch.object(runtime, "start_cdp_browser", return_value={"launched": launched, "pid": 1234}),
                    mock.patch.object(runtime, "_read_state", return_value={"active_runs": [], "retained_targets": []}),
                    mock.patch.object(runtime, "_write_state"),
                    mock.patch.object(runtime, "_target_ids", return_value=set()),
                    mock.patch.object(session, "_claim_target", return_value="owned-target"),
                    mock.patch.object(runtime, "cdp_target", return_value={}),
                    mock.patch.object(session, "_verify_attach"),
                    mock.patch.object(runtime, "show_browser_without_focus"),
                    mock.patch.object(runtime, "list_cdp_targets", return_value=[
                        {"id": "startup-blank", "type": "page", "url": "about:blank"},
                        {"id": "other-page", "type": "page", "url": "https://example.com"},
                    ]),
                ):
                    session._open_locked()
                self.assertEqual(session.owned_target_ids, {"owned-target"})

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

    def test_missing_retained_target_does_not_keep_browser_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {runtime.STATE_ROOT_ENV: temporary},
        ):
            session = self._open_session(temporary)
            state = json.loads(session.state_path.read_text())
            state["retained_targets"] = [{"target_id": "already-closed", "scope": "donald-chatgpt-imagegen"}]
            session.state_path.write_text(json.dumps(state))
            with (
                mock.patch.object(runtime, "cdp_target", side_effect=lambda _port, target:
                                  None if target == "already-closed" else {"id": target}),
                mock.patch.object(runtime, "close_background_page"),
                mock.patch.object(runtime, "close_cdp_browser", return_value={"status": "closed"}) as close_browser,
            ):
                session.close()
            state = json.loads(session.state_path.read_text())
        close_browser.assert_called_once()
        self.assertEqual(state["retained_targets"], [])

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
                    side_effect=lambda *_args, **_kwargs: events.append("browser_close")
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
            session._transport_executable = "agent-browser"
            with (
                mock.patch.object(runtime, "run_agent_browser", return_value=mock.Mock(returncode=0)) as release,
                mock.patch.object(runtime, "cdp_target", return_value={"id": "open"}),
                mock.patch.object(runtime, "close_background_page") as close_target,
                mock.patch.object(runtime, "close_cdp_browser") as close_browser,
            ):
                cleanup = session.close()
            state = json.loads(session.state_path.read_text(encoding="utf-8"))

        self.assertEqual(cleanup["status"], "kept_open_for_human")
        self.assertEqual([call.args[1] for call in close_target.call_args_list], ["attach-blank"])
        self.assertEqual(state["retained_targets"][0]["target_id"], "owned-target")
        release.assert_called_once_with(
            ["agent-browser", "--session", session.transport_session, "close"], timeout=10,
        )
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
                mock.patch.object(runtime, "frontmost_process_id", side_effect=[111, 111, 222]),
                mock.patch.object(
                    runtime,
                    "start_cdp_browser",
                    return_value={"launched": True, "pid": 1234},
                ),
                mock.patch.object(runtime, "create_background_page", return_value="failed-target"),
                mock.patch.object(runtime, "_target_ids", return_value=set()),
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
        self.assertEqual(guard_focus.call_args_list, [mock.call(111, 9333), mock.call(222, 9333)])
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
                    runtime,
                    "run_agent_browser",
                    return_value=mock.Mock(returncode=0, stdout="https://example.com"),
                ),
                mock.patch.object(runtime, "list_cdp_targets", side_effect=targets),
                mock.patch.object(runtime.time, "sleep"),
            ):
                session._verify_attach(own_new_blank_targets=True)

        self.assertIn("delayed-blank", session.owned_target_ids)


if __name__ == "__main__":
    unittest.main()
