from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_browser_runner as runner  # noqa: E402


class ConversationPersistenceTests(unittest.TestCase):
    def test_waits_past_local_placeholder_for_real_conversation(self) -> None:
        temporary = "https://chatgpt.com/c/local-chatgpt%3Atest"
        canonical = "https://chatgpt.com/c/canonical"
        with (
            mock.patch.object(runner, "_current_url", side_effect=[temporary, temporary, canonical]),
            mock.patch.object(runner, "_page_access_state", return_value={}),
            mock.patch.object(runner.time, "sleep"),
        ):
            self.assertEqual(runner._wait_for_conversation_url(argparse.Namespace(), Path.cwd()), canonical)

    def test_placeholder_timeout_preserves_original_target_url(self) -> None:
        temporary = "https://chatgpt.com/c/local-chatgpt%3Atest"
        with (
            mock.patch.object(runner, "_current_url", return_value=temporary),
            mock.patch.object(runner.time, "time", side_effect=[0, 2]),
        ):
            self.assertEqual(runner._wait_for_conversation_url(argparse.Namespace(), Path.cwd(), 1), temporary)

    def test_canonical_url_cannot_regress_to_a_local_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "persist", "prompt_card": "prompt.md", "download_dir": temporary}
            canonical = "https://chatgpt.com/c/canonical"
            local = "https://chatgpt.com/c/local-chatgpt%3Atest"
            runner._write_session_patch(job, {"submission_committed": True,
                "conversation_url": canonical, "canonical_conversation_url": canonical})
            result = runner._write_session_patch(job, {"conversation_url": local})
            self.assertEqual(result["conversation_url"], canonical)
            self.assertEqual(result["canonical_conversation_url"], canonical)
            self.assertEqual(result["observed_page_url"], local)


if __name__ == "__main__":
    unittest.main()
