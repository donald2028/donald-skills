from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class ConversationGuardTests(unittest.TestCase):
    def test_encoded_local_conversation_id_is_temporary(self) -> None:
        self.assertTrue(runner._temporary_conversation_id("local-chatgpt%3Ab368818b-bc96-4a14-a5f9-94a2a91bc613"))

    def test_committed_session_retains_conversation_when_page_becomes_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "challenge", "prompt_card": "prompt.md", "download_dir": temporary}
            url = "https://chatgpt.com/c/local-chatgpt%3Atest"
            runner._write_session_patch(job, {"submission_committed": True, "conversation_url": url})
            session = runner._write_session_patch(job, {
                "conversation_url": "https://chatgpt.com/", "conversation_id": None,
            })
        self.assertTrue(session["submission_committed"])
        self.assertEqual(session["conversation_url"], url)
        self.assertEqual(session["initial_committed_conversation_url"], url)
        self.assertEqual(session["observed_page_url"], "https://chatgpt.com/")

    def test_accepts_verified_temporary_to_canonical_url_migration(self) -> None:
        with (
            mock.patch.object(
                runner,
                "_current_url",
                return_value="https://chatgpt.com/c/6a6070a4-4550-83e8-96d8-a0d5835c6d50",
            ),
            mock.patch.object(
                runner,
                "_conversation_message_counts",
                return_value={"user_message_count": 1, "assistant_message_count": 1},
            ),
            mock.patch.object(runner, "_open_conversation") as reopen,
        ):
            event = runner._ensure_expected_conversation(
                argparse.Namespace(),
                Path.cwd(),
                "https://chatgpt.com/c/WEB:temporary-id",
                baseline_user_message_count=0,
            )

        self.assertTrue(event["canonicalized"])
        self.assertFalse(event["restored"])
        reopen.assert_not_called()

    def test_does_not_accept_unverified_conversation_change(self) -> None:
        with (
            mock.patch.object(runner, "_page_access_state", return_value={}),
            mock.patch.object(
                runner,
                "_current_url",
                return_value="https://chatgpt.com/c/unrelated",
            ),
            mock.patch.object(
                runner,
                "_conversation_message_counts",
                return_value={"user_message_count": 0, "assistant_message_count": 0},
            ),
            mock.patch.object(runner, "_open_conversation", return_value="https://chatgpt.com/"),
        ):
            with self.assertRaisesRegex(RuntimeError, "conversation changed"):
                runner._ensure_expected_conversation(
                    argparse.Namespace(),
                    Path.cwd(),
                    "https://chatgpt.com/c/WEB:temporary-id",
                    baseline_user_message_count=0,
                )

    def test_verification_blocks_conversation_reopen(self) -> None:
        with (
            mock.patch.object(runner, "_current_url", return_value="https://chatgpt.com/"),
            mock.patch.object(runner, "_page_access_state",
                              return_value={"humanReason": "anti_automation_verification"}),
            mock.patch.object(runner, "_activate_for_human_attention",
                              side_effect=runner.HumanAttentionRequired("anti_automation_verification", {})),
            mock.patch.object(runner, "_open_conversation") as reopen,
            self.assertRaises(runner.HumanAttentionRequired),
        ):
            runner._ensure_expected_conversation(argparse.Namespace(), Path.cwd(),
                                                  "https://chatgpt.com/c/local-chatgpt%3Atest")
        reopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
