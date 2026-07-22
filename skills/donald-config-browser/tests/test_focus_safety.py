from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "profile_config.py"
SPEC = importlib.util.spec_from_file_location("donald_config_browser_focus_profile_config", SCRIPT_PATH)
assert SPEC and SPEC.loader
profile_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profile_config
SPEC.loader.exec_module(profile_config)


def _config() -> dict[str, object]:
    return {
        "profile": {"directory": "Profile 22"},
        "chrome": {
            "cdp_user_data_dir": "/tmp/donald-wechat-cdp",
            "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        },
    }


class FocusSafetyTests(unittest.TestCase):
    def test_macos_launches_visible_in_background_instead_of_hidden_then_revealed(self) -> None:
        with mock.patch.object(profile_config.sys, "platform", "darwin"):
            command = profile_config.chrome_launch_command(_config(), 9244)

        self.assertEqual(command[:2], ["open", "-g"])
        self.assertNotIn("-j", command)

    def test_reveal_defocuses_chrome_before_visibility_changes_and_restores_conditionally(self) -> None:
        completed = subprocess.CompletedProcess(args=["osascript"], returncode=0, stdout="111\n")
        with (
            mock.patch.object(profile_config.sys, "platform", "darwin"),
            mock.patch.object(profile_config, "_cdp_version", return_value={"Browser": "Chrome/test"}),
            mock.patch.object(profile_config, "_verify_existing_cdp_owner"),
            mock.patch.object(profile_config, "_listening_process_id", return_value="222"),
            mock.patch.object(profile_config, "_run", return_value=completed) as run,
        ):
            profile_config.show_browser_without_focus(_config(), 9244)

        script = run.call_args.args[0][2]
        self.assertLess(script.index("set frontmost to false"), script.index("set visible to true"))
        self.assertIn("if visible is false then set visible to true", script)
        self.assertIn("if currentPid is 222", script)

    def test_background_target_creation_restores_focus_only_if_chrome_took_it(self) -> None:
        connection = mock.MagicMock()
        connection.__enter__.return_value.call.return_value = {"targetId": "article-target"}
        with (
            mock.patch.object(profile_config, "_cdp_version", return_value={"webSocketDebuggerUrl": "ws://browser"}),
            mock.patch.object(profile_config._CDPConnection, "connect", return_value=connection),
            mock.patch.object(profile_config, "frontmost_process_id", return_value=111),
            mock.patch.object(profile_config, "restore_frontmost_process_if_browser_active") as guard,
        ):
            target_id = profile_config.create_background_page(9244, "https://mp.weixin.qq.com/article")

        self.assertEqual(target_id, "article-target")
        guard.assert_called_once_with(111, 9244)


if __name__ == "__main__":
    unittest.main()
