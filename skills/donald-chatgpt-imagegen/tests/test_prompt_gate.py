from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
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

    def test_needs_ops_report_preserves_committed_state_for_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "challenge", "prompt_card": "prompt.md", "download_dir": temporary}
            manifest = Path(temporary) / "job.json"
            manifest.write_text(json.dumps(job))
            url = "https://chatgpt.com/c/local-chatgpt%3Atest"
            runner._write_session_patch(job, {"submission_committed": True, "conversation_url": url})
            output = io.StringIO()
            with (
                mock.patch.object(sys, "argv", ["runner", str(manifest), "--mode", "collect-current"]),
                mock.patch.object(runner, "_resolve_browser_profile"),
                mock.patch.object(runner, "_prepare_browser_lane"),
                mock.patch.object(runner, "_validate_profile"),
                mock.patch.object(runner, "run_collect_current", side_effect=self.attention),
                mock.patch.object(runner, "_cleanup_agent_browser"),
                contextlib.redirect_stdout(output),
            ):
                result = runner.main()
            session = json.loads((Path(temporary) / "chatgpt_session.json").read_text())
        report = json.loads(output.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(report["status"], "needs_ops")
        self.assertFalse(report["retryable"])
        self.assertTrue(report["submission_committed"])
        self.assertEqual(session["status"], "needs_ops")
        self.assertEqual(session["conversation_url"], url)
        self.assertEqual(report["initial_committed_conversation_url"], url)
        self.assertEqual(report["recommended_next_action"], "resolve_verification_then_inspect_or_collect_current")

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
