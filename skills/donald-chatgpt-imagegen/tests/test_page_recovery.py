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


class PageRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(page_recovery_attempts=3)
        self.cwd = Path(__file__).resolve().parent

    def test_transient_post_submit_read_failures_do_not_refresh(self) -> None:
        operation = mock.Mock(
            side_effect=[RuntimeError("heartbeat 1"), RuntimeError("heartbeat 2"), "healthy"]
        )
        with (
            mock.patch.object(runner.time, "sleep"),
            mock.patch.object(runner, "_recover_chatgpt_page") as recover,
        ):
            result, events = runner._run_with_page_recovery(
                self.args,
                self.cwd,
                operation,
                phase="post_submit_monitor:heartbeat",
                target_url="https://chatgpt.com/c/test",
                failure_confirmations=3,
            )

        self.assertEqual(result, "healthy")
        self.assertEqual(events, [])
        self.assertEqual(operation.call_count, 3)
        recover.assert_not_called()

    def test_recovery_is_bounded_and_reports_terminal_failure(self) -> None:
        operation = mock.Mock(side_effect=RuntimeError("page unavailable"))
        recovery_event = {
            "phase": "pre_submit_prepare",
            "attempt": 1,
            "status": "page_recovered",
            "target_url": "https://chatgpt.com/",
        }
        with (
            mock.patch.object(runner, "_recover_chatgpt_page", return_value=recovery_event) as recover,
            mock.patch.object(runner, "_record_page_recovery"),
            self.assertRaises(runner.PageRecoveryExhaustedError) as raised,
        ):
            runner._run_with_page_recovery(
                self.args,
                self.cwd,
                operation,
                phase="pre_submit_prepare",
                target_url="https://chatgpt.com/",
            )

        report = raised.exception.report
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(operation.call_count, 4)
        self.assertEqual(report["error_type"], "chatgpt_page_recovery_exhausted")
        self.assertEqual(report["recovery_attempt_count"], 3)
        self.assertTrue(report["terminal"])
        self.assertFalse(report["submission_committed"])
        self.assertEqual(report["recommended_next_action"], "rerun_same_job")

    def test_post_submit_exhaustion_never_recommends_resubmission(self) -> None:
        args = argparse.Namespace(page_recovery_attempts=1)
        with (
            mock.patch.object(
                runner,
                "_recover_chatgpt_page",
                side_effect=runner.RecoverablePageError("still crashed"),
            ),
            mock.patch.object(runner, "_record_page_recovery"),
            self.assertRaises(runner.PageRecoveryExhaustedError) as raised,
        ):
            runner._run_with_page_recovery(
                args,
                self.cwd,
                mock.Mock(side_effect=RuntimeError("heartbeat unavailable")),
                phase="post_submit_monitor:heartbeat",
                target_url="https://chatgpt.com/c/test",
                failure_confirmations=1,
            )

        report = raised.exception.report
        self.assertTrue(report["submission_committed"])
        self.assertTrue(report["should_collect_current_first"])
        self.assertEqual(
            report["recommended_next_action"],
            "collect_current_or_inspect_conversation",
        )

    def test_human_verification_is_never_refreshed(self) -> None:
        attention = runner.HumanAttentionRequired(
            "anti_automation_verification",
            {"status": "active"},
        )
        with (
            mock.patch.object(runner, "_recover_chatgpt_page") as recover,
            self.assertRaises(runner.HumanAttentionRequired),
        ):
            runner._run_with_page_recovery(
                self.args,
                self.cwd,
                mock.Mock(side_effect=attention),
                phase="pre_submit_prepare",
                target_url="https://chatgpt.com/",
            )

        recover.assert_not_called()

    def test_selector_contract_failure_is_not_treated_as_page_crash(self) -> None:
        with (
            mock.patch.object(runner, "_recover_chatgpt_page") as recover,
            self.assertRaises(runner.ImageModeSelectionError),
        ):
            runner._run_with_page_recovery(
                self.args,
                self.cwd,
                mock.Mock(side_effect=runner.ImageModeSelectionError("selector changed")),
                phase="pre_submit_prepare",
                target_url="https://chatgpt.com/",
            )

        recover.assert_not_called()

    def test_prompt_gate_fails_fast_on_browser_error_page(self) -> None:
        with mock.patch.object(
            runner,
            "_page_access_state",
            return_value={
                "hasPrompt": False,
                "humanReason": "",
                "crashReason": "chrome_error_page",
                "href": "chrome-error://chromewebdata/",
            },
        ):
            with self.assertRaisesRegex(runner.RecoverablePageError, "chrome_error_page"):
                runner._wait_for_prompt_box(self.args, self.cwd, timeout_s=60)

    def test_prompt_gate_stops_for_human_verification_without_refresh(self) -> None:
        attention = runner.HumanAttentionRequired(
            "anti_automation_verification",
            {"status": "active"},
        )
        with (
            mock.patch.object(
                runner,
                "_page_access_state",
                return_value={
                    "hasPrompt": False,
                    "humanReason": "anti_automation_verification",
                    "crashReason": "",
                },
            ),
            mock.patch.object(
                runner,
                "_activate_for_human_attention",
                side_effect=attention,
            ) as activate,
            self.assertRaises(runner.HumanAttentionRequired),
        ):
            runner._wait_for_prompt_box(self.args, self.cwd, timeout_s=60)

        activate.assert_called_once_with(self.args, "anti_automation_verification")

    def test_pre_submit_refresh_replays_mode_ratio_and_reference_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = str(Path(temporary) / "reference.png")
            job = {
                "job_name": "recovery-check",
                "download_dir": temporary,
                "prompt_card": str(Path(temporary) / "prompt.md"),
                "output_aspect_ratio": "16:9",
                "reference_images": [{"path": reference}],
            }
            upload_ready = {
                "reference_count": 1,
                "blob_image_count": 1,
                "filename_mentions": {"reference.png": True},
                "ready": True,
            }
            recovery_event = {
                "phase": "dry_upload_prepare",
                "attempt": 1,
                "status": "page_recovered",
                "target_url": "https://chatgpt.com/",
            }
            with (
                mock.patch.object(runner, "_open_new_chat"),
                mock.patch.object(runner, "_wait_for_prompt_box"),
                mock.patch.object(
                    runner,
                    "_ensure_chat_surface",
                    return_value={"selected_surface": "chat", "verified": True},
                ),
                mock.patch.object(runner, "_validate_account_lane", return_value={"ok": True}),
                mock.patch.object(runner, "_screenshot"),
                mock.patch.object(
                    runner,
                    "_enable_image_mode_if_available",
                    return_value={"status": "selected", "verified": True},
                ) as image_mode,
                mock.patch.object(
                    runner,
                    "_configure_aspect_ratio",
                    return_value={"requested": "16:9", "verified": True},
                ) as ratio,
                mock.patch.object(runner, "_upload_files") as upload,
                mock.patch.object(
                    runner,
                    "_wait_for_reference_uploads_ready",
                    side_effect=[runner.RecoverablePageError("manual refresh"), upload_ready],
                ),
                mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://owned"),
                mock.patch.object(runner, "_page_text", return_value="reference.png"),
                mock.patch.object(runner, "_agent_browser_session_record", return_value={}),
                mock.patch.object(runner, "_recover_chatgpt_page", return_value=recovery_event),
                mock.patch.object(runner, "_record_page_recovery"),
                mock.patch.object(runner, "_write_session_patch"),
            ):
                report = runner.run_dry_upload(self.args, job, self.cwd)

        self.assertEqual(image_mode.call_count, 2)
        self.assertEqual(ratio.call_count, 2)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(len(report["page_recoveries"]), 1)

    def test_missing_attachment_evidence_fails_before_full_upload_timeout(self) -> None:
        clock = {"now": 0.0}

        def now() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        empty_observation = {
            "failure": None,
            "reference_count": 1,
            "attachment_count": 0,
            "blob_image_count": 0,
            "ready": False,
        }
        with (
            mock.patch.object(runner.time, "time", side_effect=now),
            mock.patch.object(runner.time, "sleep", side_effect=sleep),
            mock.patch.object(
                runner,
                "_page_access_state",
                return_value={"humanReason": "", "crashReason": ""},
            ),
            mock.patch.object(
                runner,
                "_reference_upload_observation",
                return_value=empty_observation,
            ),
            self.assertRaisesRegex(runner.RecoverablePageError, "no attachment evidence"),
        ):
            runner._wait_for_reference_uploads_ready(
                self.args,
                self.cwd,
                ["reference.png"],
                timeout_s=90,
            )

        self.assertEqual(clock["now"], runner.REFERENCE_UPLOAD_NO_EVIDENCE_RECOVERY_SECONDS)

    def test_normal_generation_has_no_time_based_refresh_by_default(self) -> None:
        self.assertEqual(runner.DEFAULT_STALE_GENERATION_REFRESH_SECONDS, 0)

    def test_submit_boundary_failure_is_reported_without_resubmitting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {
                "job_name": "submit-boundary",
                "download_dir": temporary,
                "prompt_card": str(Path(temporary) / "prompt.md"),
                "variant_count": 1,
                "reference_images": [],
            }
            prepared = {
                "chat_surface": {"selected_surface": "chat", "verified": True},
                "account_guard": {"ok": True},
                "image_mode": {"status": "selected", "verified": True},
                "aspect_ratio_delivery": {"requested": "1:1"},
                "upload_observation": {"reference_count": 0},
                "after_upload": "",
                "baseline": set(),
                "baseline_message_counts": {
                    "user_message_count": 0,
                    "assistant_message_count": 0,
                },
            }
            with (
                mock.patch.object(runner, "_wait_for_submit_throttle_slot", return_value={}),
                mock.patch.object(
                    runner,
                    "_run_with_page_recovery",
                    return_value=(prepared, []),
                ) as recoverable_prepare,
                mock.patch.object(
                    runner,
                    "_submit_prompt",
                    side_effect=RuntimeError("connection dropped during click"),
                ) as submit,
                mock.patch.object(runner, "_write_session_patch") as write_session,
                self.assertRaises(runner.SubmissionStateUnknownError) as raised,
            ):
                runner._run_one_request(
                    self.args,
                    job,
                    self.cwd,
                    label="batch",
                    message="Create an image",
                    resume=False,
                )

        self.assertEqual(submit.call_count, 1)
        self.assertEqual(recoverable_prepare.call_count, 1)
        self.assertEqual(raised.exception.report["submission_committed"], "unknown")
        self.assertEqual(
            raised.exception.report["recommended_next_action"],
            "inspect_or_collect_current_before_resubmitting",
        )
        write_session.assert_called_once()


if __name__ == "__main__":
    unittest.main()
