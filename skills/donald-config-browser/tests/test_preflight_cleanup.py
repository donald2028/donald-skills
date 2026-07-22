from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "profile_config.py"
SPEC = importlib.util.spec_from_file_location("donald_config_browser_profile_config", SCRIPT_PATH)
assert SPEC and SPEC.loader
profile_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profile_config
SPEC.loader.exec_module(profile_config)


class PreflightCleanupTests(unittest.TestCase):
    def test_preflight_closes_the_chrome_process_it_launched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            user_data_dir = Path(temporary)
            (user_data_dir / "Local State").touch()
            (user_data_dir / "Profile 16").mkdir()
            config = {
                "profile": {"directory": "Profile 16", "name": "Donald", "email": ""},
                "chrome": {
                    "cdp_user_data_dir": str(user_data_dir),
                    "executable": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                },
            }
            version = {
                "Browser": "Chrome/test",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9238/devtools/browser/test",
            }
            attach = subprocess.CompletedProcess(
                args=["agent-browser"],
                returncode=0,
                stdout="about:blank\n",
            )

            with (
                mock.patch.object(profile_config, "frontmost_process_id", return_value=None),
                mock.patch.object(
                    profile_config,
                    "ensure_agent_browser",
                    return_value={"executable": "agent-browser"},
                ),
                mock.patch.object(profile_config, "_cdp_version", side_effect=[None, version]),
                mock.patch.object(profile_config, "_launch_chrome", return_value=["chrome"]),
                mock.patch.object(profile_config, "create_background_page", return_value="target"),
                mock.patch.object(
                    profile_config,
                    "wait_for_background_page_url",
                    return_value="about:blank",
                ),
                mock.patch.object(
                    profile_config,
                    "hide_browser_without_focus",
                    return_value={"status": "hidden_headed"},
                ),
                mock.patch.object(profile_config, "_run", return_value=attach),
                mock.patch.object(
                    profile_config,
                    "close_cdp_browser",
                    create=True,
                    return_value={"status": "closed", "cdp_port": 9238},
                ) as close_browser,
            ):
                result = profile_config.preflight_browser(
                    config,
                    port=9238,
                    session="test-preflight",
                    url="about:blank",
                    timeout=5,
                )

        close_browser.assert_called_once_with(config, 9238)
        self.assertEqual(result["browser_cleanup"]["status"], "closed")

    def test_preflight_closes_its_target_when_browser_was_already_running(self) -> None:
        config = {
            "profile": {"directory": "Profile 16", "name": "Donald", "email": ""},
            "chrome": {"cdp_user_data_dir": "/tmp/chrome"},
        }
        attach = subprocess.CompletedProcess(
            args=["agent-browser"],
            returncode=0,
            stdout="about:blank\n",
        )
        with (
            mock.patch.object(profile_config, "frontmost_process_id", return_value=None),
            mock.patch.object(profile_config, "ensure_agent_browser", return_value={"executable": "agent-browser"}),
            mock.patch.object(
                profile_config,
                "start_cdp_browser",
                return_value={
                    "browser": {"Browser": "Chrome/test", "webSocketDebuggerUrl": "ws://browser"},
                    "launch_command": None,
                },
            ),
            mock.patch.object(profile_config, "create_background_page", return_value="target"),
            mock.patch.object(profile_config, "wait_for_background_page_url", return_value="about:blank"),
            mock.patch.object(profile_config, "hide_browser_without_focus", return_value={"status": "hidden"}),
            mock.patch.object(profile_config, "_run", return_value=attach),
            mock.patch.object(profile_config, "close_background_page") as close_target,
        ):
            result = profile_config.preflight_browser(
                config,
                port=9238,
                session="test-preflight",
                url="about:blank",
                timeout=5,
            )

        close_target.assert_called_once_with(9238, "target")
        self.assertEqual(result["browser_cleanup"]["status"], "target_closed")


if __name__ == "__main__":
    unittest.main()
