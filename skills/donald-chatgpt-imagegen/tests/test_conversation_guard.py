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
    def test_unknown_submission_can_observe_verified_turn_without_a_saved_url(self) -> None:
        args = argparse.Namespace(_submitted_prompt="Create a fox", _owned_tab_cdp_target_id="mine")
        session = {"submission_committed": "unknown", "owned_target_id": "mine", "baseline_user_message_count": 0}
        with (
            mock.patch.object(runner, "_page_access_state", return_value={"hasPrompt": True}),
            mock.patch.object(runner, "_current_url", return_value="https://chatgpt.com/"),
            mock.patch.object(runner, "_eval_json", side_effect=lambda args, cwd, script, **kw:
                {'verified': '"Create a fox"' in script and 'length > 0' in script, 'url': 'https://chatgpt.com/'}),
            mock.patch.object(runner, "_open_conversation") as navigate,
        ):
            self.assertEqual(runner._require_original_conversation(args, Path.cwd(), session), "https://chatgpt.com/")
            with self.assertRaises(runner.OriginalConversationRequired):
                runner._require_original_conversation(args, Path.cwd(), {**session, "owned_target_id": "other"})
            with self.assertRaises(runner.OriginalConversationRequired):
                runner._require_original_conversation(args, Path.cwd(), {**session, "baseline_user_message_count": 1})
        navigate.assert_not_called()

    def test_temporary_post_submit_recovery_never_reloads_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "temporary", "prompt_card": "prompt.md", "download_dir": temporary}
            args = argparse.Namespace(_health_job=job, _owned_tab_cdp_target_id="mine")
            with (
                mock.patch.object(runner, "_page_access_state", return_value={}),
                mock.patch.object(runner, "_require_original_conversation", return_value="https://chatgpt.com/c/local-chatgpt%3Atest"),
                mock.patch.object(runner, "_wait_for_prompt_box"),
                mock.patch.object(runner, "_navigate_owned_cdp_tab") as navigate,
                mock.patch.object(runner, "_open_url") as reopen,
            ):
                event = runner._recover_chatgpt_page(args, Path.cwd(), phase="post_submit_confirm",
                    target_url="https://chatgpt.com/", attempt=1, reason="temporary URL")
            self.assertEqual(event["method"], "observe_original_target")
            navigate.assert_not_called()
            reopen.assert_not_called()

    def test_unknown_session_retains_discovered_url_on_page_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "unknown", "prompt_card": "prompt.md", "download_dir": temporary}
            url = "https://chatgpt.com/c/canonical"
            runner._write_session_patch(job, {"submission_committed": "unknown", "conversation_url": url})
            session = runner._write_session_patch(job, {"conversation_url": "https://chatgpt.com/"})
        self.assertEqual(session["conversation_url"], url)

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
            mock.patch.object(runner, "_eval_json", return_value={"latestUserText": "Create a fox"}),
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
                argparse.Namespace(_submitted_prompt="Create a fox"),
                Path.cwd(),
                "https://chatgpt.com/c/WEB:temporary-id",
                baseline_user_message_count=0,
            )

        self.assertTrue(event["canonicalized"])
        self.assertFalse(event["restored"])
        reopen.assert_not_called()

    def test_local_url_migration_requires_same_submitted_prompt(self) -> None:
        for observed_text, accepted in [("@Create image Create a fox", True), ("Create a bear", False)]:
            with (
                self.subTest(observed_text=observed_text),
                mock.patch.object(runner, "_current_url", return_value="https://chatgpt.com/c/canonical"),
                mock.patch.object(runner, "_conversation_message_counts", return_value={"user_message_count": 1}),
                mock.patch.object(runner, "_eval_json", return_value={"latestUserText": observed_text}),
                mock.patch.object(runner, "_page_access_state", return_value={}),
                mock.patch.object(runner, "_open_conversation", return_value="https://chatgpt.com/") as reopen,
            ):
                args = argparse.Namespace(_submitted_prompt="Create a fox")
                if accepted:
                    event = runner._ensure_expected_conversation(args, Path.cwd(),
                        "https://chatgpt.com/c/local-chatgpt%3A2583e128-6356-4f31-955d-aa1c79b9f858", 0)
                    self.assertTrue(event["canonicalized"])
                    self.assertEqual(event["canonicalization_evidence"], "submitted_prompt_matches_latest_user_turn")
                    reopen.assert_not_called()
                else:
                    with self.assertRaisesRegex(RuntimeError, "conversation changed"):
                        runner._ensure_expected_conversation(args, Path.cwd(),
                            "https://chatgpt.com/c/local-chatgpt%3Atest", 0)

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
