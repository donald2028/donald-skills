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


class Clock:
    now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class GenerationHealthTests(unittest.TestCase):
    def _run_generation_wait(
        self,
        page_health_states: list[dict[str, object]],
    ) -> tuple[dict[str, object], int, float]:
        clock = Clock()
        args = argparse.Namespace(
            timeout=60,
            progress_interval=20,
            stale_generation_refresh_interval=0,
        )

        with tempfile.TemporaryDirectory() as temporary:
            job = {
                "job_name": "health-check",
                "download_dir": temporary,
                "variant_count": 1,
            }
            with (
                mock.patch.object(runner.time, "time", side_effect=clock.time),
                mock.patch.object(runner.time, "sleep", side_effect=clock.sleep),
                mock.patch.object(
                    runner,
                    "_ensure_expected_conversation",
                    return_value={
                        "current_url": "https://chatgpt.com/c/test",
                        "restored": False,
                    },
                ),
                mock.patch.object(
                    runner,
                    "_persist_canonical_conversation",
                    return_value="https://chatgpt.com/c/test",
                ),
                mock.patch.object(runner, "_page_text", return_value=""),
                mock.patch.object(
                    runner,
                    "_generation_page_health",
                    side_effect=page_health_states,
                ) as health_check,
                mock.patch.object(runner, "_image_inventory", return_value=[]),
                mock.patch.object(
                    runner,
                    "_generation_button_state",
                    return_value={
                        "checked": True,
                        "generationActive": True,
                        "readyForNextPrompt": False,
                    },
                ),
                mock.patch.object(runner, "_scroll_down"),
                mock.patch.object(runner, "_emit_progress"),
                mock.patch.object(runner, "_write_session_patch"),
                mock.patch.object(runner, "_screenshot"),
            ):
                result = runner._collect_generated_images_with_retries(
                    args,
                    job,
                    Path.cwd(),
                    Path(temporary),
                    set(),
                    [],
                    label="batch",
                    mode="single_batch_submit",
                    resumed=False,
                    max_failure_retries=0,
                    expected_conversation_url="https://chatgpt.com/c/test",
                    baseline_user_message_count=0,
                )

        return result, health_check.call_count, clock.now

    def test_recognizes_visible_something_went_wrong_error(self) -> None:
        error = runner._generation_error_from_text(
            "Something went wrong while generating your response."
        )

        self.assertEqual(error["error_type"], "chatgpt_generation_error")
        self.assertEqual(error["matched_marker"], "Something went wrong")

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

    def test_health_check_treats_visible_retry_control_as_current_generation_error(self) -> None:
        with mock.patch.object(
            runner,
            "_eval_json",
            return_value={
                "href": "https://chatgpt.com/c/canonical-id",
                "userMessageCount": 1,
                "assistantMessageCount": 0,
                "latestAssistantMessage": "",
                "latestTurnText": "",
                "errorSurfaceTexts": [],
                "retryControlLabels": ["Retry"],
                "hasComposer": True,
                "challengeFrame": False,
            },
        ):
            health = runner._generation_page_health(
                argparse.Namespace(),
                Path.cwd(),
                "https://chatgpt.com/c/canonical-id",
                "",
            )

        self.assertEqual(health["status"], "generation_error")
        self.assertEqual(health["generation_error"]["matched_marker"], "Retry")
        self.assertEqual(health["retry_control_labels"], ["Retry"])

    def test_health_check_ignores_previous_assistant_error_after_a_new_user_turn(self) -> None:
        with mock.patch.object(
            runner,
            "_eval_json",
            return_value={
                "href": "https://chatgpt.com/c/canonical-id",
                "userMessageCount": 2,
                "assistantMessageCount": 1,
                "latestAssistantMessage": "Something went wrong.",
                "latestAssistantIsCurrent": False,
                "latestTurnText": "Create a new image now.",
                "errorSurfaceTexts": [],
                "retryControlLabels": [],
                "hasComposer": True,
                "challengeFrame": False,
            },
        ):
            health = runner._generation_page_health(
                argparse.Namespace(),
                Path.cwd(),
                "https://chatgpt.com/c/canonical-id",
                "Something went wrong. Create a new image now.",
            )

        self.assertEqual(health["status"], "ok")
        self.assertNotIn("generation_error", health)
        self.assertEqual(health["latest_turn_excerpt"], "Create a new image now.")

    def test_retry_click_supports_current_retry_labels(self) -> None:
        with (
            mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://owned"),
            mock.patch.object(
                runner,
                "_try_cdp_eval_js",
                return_value=(True, "clicked"),
            ) as evaluate,
            mock.patch.object(runner, "_wait_ms"),
        ):
            clicked = runner._click_try_again(argparse.Namespace(), Path.cwd())

        self.assertTrue(clicked)
        script = evaluate.call_args.args[1]
        self.assertIn('"Retry"', script)
        self.assertIn('"重试"', script)

    def test_missing_submitted_turn_fails_after_two_heartbeats(self) -> None:
        first_health = {
            "conversation_ok": True,
            "has_composer": True,
            "challenge_frame": False,
            "user_message_count": 0,
        }
        second_health = dict(first_health)

        first_error = runner._missing_submitted_turn_error(
            first_health,
            baseline_user_message_count=0,
            candidate_count=0,
            consecutive_heartbeats=1,
        )
        second_error = runner._missing_submitted_turn_error(
            second_health,
            baseline_user_message_count=0,
            candidate_count=0,
            consecutive_heartbeats=2,
        )
        present_health = {**first_health, "user_message_count": 1}
        present_error = runner._missing_submitted_turn_error(
            present_health,
            baseline_user_message_count=0,
            candidate_count=0,
            consecutive_heartbeats=2,
        )

        self.assertIsNone(first_error)
        self.assertEqual(second_error["error_type"], "chatgpt_submitted_turn_missing")
        self.assertFalse(second_health["submitted_turn_present"])
        self.assertEqual(second_health["expected_minimum_user_message_count"], 1)
        self.assertIsNone(present_error)
        self.assertTrue(present_health["submitted_turn_present"])

    def test_generation_wait_checks_retry_health_on_progress_heartbeat(self) -> None:
        healthy = {
            "status": "ok",
            "conversation_ok": True,
            "generation_active": True,
        }
        retry_error = {
            "status": "generation_error",
            "conversation_ok": True,
            "generation_active": False,
            "retry_control_labels": ["Retry"],
            "generation_error": {
                "error_type": "chatgpt_generation_error",
                "matched_marker": "Retry",
                "message": "Retry",
            },
        }
        result, health_check_count, elapsed = self._run_generation_wait(
            [healthy, retry_error]
        )

        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["generation_error"]["matched_marker"], "Retry")
        self.assertEqual(health_check_count, 2)
        self.assertEqual(elapsed, 20.0)

    def test_generation_wait_stops_when_submitted_turn_disappears(self) -> None:
        empty_conversation = {
            "status": "ok",
            "conversation_ok": True,
            "has_composer": True,
            "challenge_frame": False,
            "user_message_count": 0,
            "generation_active": False,
        }

        result, health_check_count, elapsed = self._run_generation_wait(
            [dict(empty_conversation), dict(empty_conversation)]
        )

        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["error_type"], "chatgpt_submitted_turn_missing")
        self.assertEqual(
            result["generation_error"]["matched_marker"],
            "submitted_turn_missing",
        )
        self.assertEqual(health_check_count, 2)
        self.assertEqual(elapsed, 20.0)


if __name__ == "__main__":
    unittest.main()
