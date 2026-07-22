from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class ConversationGuardTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
