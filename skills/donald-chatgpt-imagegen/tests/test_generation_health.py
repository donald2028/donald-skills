from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class GenerationHealthTests(unittest.TestCase):
    def test_recognizes_current_chatgpt_generation_tool_error(self) -> None:
        error = runner._generation_error_from_text(
            "I was unable to generate the image because the image generation tool encountered "
            "an error. Please send a new request if you'd like me to try again."
        )

        self.assertEqual(error["error_type"], "chatgpt_generation_error")
        self.assertIn("Please send a new request", error["message"])

    def test_health_check_reports_conversation_and_assistant_error(self) -> None:
        with mock.patch.object(
            runner,
            "_eval_json",
            return_value={
                "href": "https://chatgpt.com/c/canonical-id",
                "userMessageCount": 1,
                "assistantMessageCount": 1,
                "latestAssistantMessage": "Image generation tool encountered an error.",
                "hasComposer": True,
                "challengeFrame": False,
            },
        ):
            health = runner._generation_page_health(
                argparse.Namespace(),
                Path.cwd(),
                "https://chatgpt.com/c/canonical-id",
                "Image generation tool encountered an error.",
            )

        self.assertEqual(health["status"], "generation_error")
        self.assertTrue(health["conversation_ok"])
        self.assertEqual(health["assistant_message_count"], 1)


if __name__ == "__main__":
    unittest.main()
