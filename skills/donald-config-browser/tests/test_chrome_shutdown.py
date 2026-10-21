from __future__ import annotations

import json
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import profile_config as config  # noqa: E402


class ChromeShutdownTests(unittest.TestCase):
    def test_cdp_events_cannot_extend_the_close_response_deadline(self) -> None:
        sock = mock.Mock()
        sock.gettimeout.return_value = 1
        event = json.dumps({"method": "Target.targetInfoChanged"}).encode()
        sock.recv.return_value = bytes([0x81, len(event)]) + event
        connection = config._CDPConnection(sock)
        with (
            mock.patch.object(config.time, "monotonic", side_effect=[0, .25, .5, 1.25]),
            self.assertRaisesRegex(TimeoutError, "deadline"),
        ):
            connection.call("Browser.close")
        self.assertEqual(sock.recv.call_count, 1)

    def _close(self, commands: list[str]) -> tuple[dict, mock.Mock]:
        binding = {"chrome": {"cdp_user_data_dir": "/tmp/Donald Chrome"},
                   "profile": {"directory": "Profile 1"}}
        with (
            mock.patch.object(config.sys, "platform", "darwin"),
            mock.patch.object(config, "_cdp_version", return_value=None),
            mock.patch.object(config, "_listening_process_id", return_value=""),
            mock.patch.object(config, "_process_command", side_effect=commands),
            mock.patch.object(config.os, "kill") as kill,
        ):
            result = config.close_cdp_browser(binding, 9333, timeout=0, expected_pid=1234)
        return result, kill

    command = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
               "--remote-debugging-port=9333 --user-data-dir=/tmp/Donald Chrome "
               "--profile-directory=Profile 1 --no-startup-window")

    def test_unresponsive_cdp_does_not_hide_a_live_chrome_process(self) -> None:
        result, kill = self._close([self.command] * 3 + [""])
        kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual(result["method"], "SIGTERM")

    def test_stuck_chrome_escalates_only_after_graceful_exit_fails(self) -> None:
        result, kill = self._close([self.command] * 5 + [""])
        self.assertEqual(kill.call_args_list, [mock.call(1234, signal.SIGTERM),
                                               mock.call(1234, signal.SIGKILL)])
        self.assertEqual(result["method"], "SIGKILL")

    def test_a_reused_pid_is_never_signaled(self) -> None:
        result, kill = self._close([self.command, "/Applications/Other.app/Other"])
        kill.assert_not_called()
        self.assertEqual(result["status"], "closed")

    def test_another_profile_or_path_prefix_is_never_closed(self) -> None:
        for command in [self.command.replace("Profile 1", "Profile 16"),
                        self.command.replace("Donald Chrome", "Donald Chrome Other")]:
            with self.subTest(command=command), self.assertRaises(config.ProfileConfigError):
                self._close([command])


if __name__ == "__main__":
    unittest.main()
