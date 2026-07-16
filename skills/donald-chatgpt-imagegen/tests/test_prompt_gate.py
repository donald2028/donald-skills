from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class PromptGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace()
        self.cwd = Path(__file__).resolve().parent
        self.attention = runner.HumanAttentionRequired(
            "login_required",
            {"status": "active"},
        )

    def test_cdp_login_blocks_even_when_composer_is_visible(self) -> None:
        with (
            mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://example"),
            mock.patch.object(
                runner,
                "_eval_json",
                return_value={"hasPrompt": True, "humanReason": "login_required"},
            ),
            mock.patch.object(
                runner,
                "_activate_for_human_attention",
                side_effect=self.attention,
            ) as activate,
        ):
            with self.assertRaises(runner.HumanAttentionRequired):
                runner._wait_for_prompt_box(self.args, self.cwd, timeout_s=1)

        activate.assert_called_once_with(self.args, "login_required")

    def test_snapshot_login_blocks_even_when_composer_is_visible(self) -> None:
        with (
            mock.patch.object(runner, "_owned_tab_cdp_url", return_value=""),
            mock.patch.object(runner, "_snapshot", return_value='role="textbox"\nLog in'),
            mock.patch.object(
                runner,
                "_activate_for_human_attention",
                side_effect=self.attention,
            ) as activate,
        ):
            with self.assertRaises(runner.HumanAttentionRequired):
                runner._wait_for_prompt_box(self.args, self.cwd, timeout_s=1)

        activate.assert_called_once_with(self.args, "login_required")

    def test_ready_composer_without_blocker_is_allowed(self) -> None:
        with (
            mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://example"),
            mock.patch.object(
                runner,
                "_eval_json",
                return_value={"hasPrompt": True, "humanReason": ""},
            ),
            mock.patch.object(runner, "_activate_for_human_attention") as activate,
        ):
            runner._wait_for_prompt_box(self.args, self.cwd, timeout_s=1)

        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
