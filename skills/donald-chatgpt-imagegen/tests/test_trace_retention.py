from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402


class TraceRetentionTests(unittest.TestCase):
    def test_success_prunes_routine_screenshots_but_keeps_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            trace_dir = output_dir / "agent_browser_trace_submit"
            trace_dir.mkdir()
            routine = trace_dir / "01_open.png"
            failure = trace_dir / "05_download_failed.png"
            report_path = trace_dir / "submit_report.json"
            routine.write_bytes(b"routine")
            failure.write_bytes(b"failure")
            report_path.write_text("{}\n", encoding="utf-8")
            report = {
                "variants": [
                    {
                        "variant_index": "batch",
                        "report_path": str(report_path),
                        "downloaded": {
                            "status": "downloaded",
                            "image_count": 1,
                            "expected_image_count": 1,
                        },
                    }
                ]
            }

            retention = runner._prune_success_trace_screenshots(
                {"download_dir": str(output_dir), "request_mode": "single_batch"},
                report,
                mode="single-batch-submit",
                start_variant=1,
                end_variant=None,
                keep_success_trace_screenshots=False,
            )

            self.assertFalse(routine.exists())
            self.assertTrue(failure.exists())
            self.assertTrue(report_path.exists())
            self.assertEqual(retention["removed_count"], 1)
            self.assertGreater(retention["reclaimed_bytes"], 0)

    def test_partial_result_preserves_routine_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            trace_dir = output_dir / "agent_browser_trace_submit"
            trace_dir.mkdir()
            routine = trace_dir / "04_after_submit.png"
            report_path = trace_dir / "submit_report.json"
            routine.write_bytes(b"routine")
            report_path.write_text("{}\n", encoding="utf-8")
            report = {
                "variants": [
                    {
                        "variant_index": "batch",
                        "report_path": str(report_path),
                        "downloaded": {
                            "status": "partial_downloaded",
                            "image_count": 1,
                            "expected_image_count": 2,
                        },
                    }
                ]
            }

            retention = runner._prune_success_trace_screenshots(
                {"download_dir": str(output_dir), "request_mode": "single_batch"},
                report,
                mode="single-batch-submit",
                start_variant=1,
                end_variant=None,
                keep_success_trace_screenshots=False,
            )

            self.assertTrue(routine.exists())
            self.assertEqual(retention["removed_count"], 0)

    def test_keep_flag_preserves_success_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            trace_dir = output_dir / "agent_browser_trace_collect"
            trace_dir.mkdir()
            routine = trace_dir / "01_current_page.png"
            routine.write_bytes(b"routine")
            report = {
                "trace_dir": str(trace_dir),
                "status": "downloaded",
                "image_count": 1,
                "expected_image_count": 1,
            }

            retention = runner._prune_success_trace_screenshots(
                {"download_dir": str(output_dir), "request_mode": "single_batch"},
                report,
                mode="collect-current",
                start_variant=1,
                end_variant=None,
                keep_success_trace_screenshots=True,
            )

            self.assertTrue(routine.exists())
            self.assertEqual(retention["policy"], "all")
            self.assertEqual(retention["removed_count"], 0)

    def test_independent_variant_cleanup_only_touches_current_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            current_trace = output_dir / "agent_browser_trace_variant_02"
            previous_trace = output_dir / "agent_browser_trace_variant_01"
            current_trace.mkdir()
            previous_trace.mkdir()
            current_screenshot = current_trace / "05_after_generated.png"
            previous_screenshot = previous_trace / "05_after_generated.png"
            current_screenshot.write_bytes(b"current")
            previous_screenshot.write_bytes(b"previous")
            current_report = current_trace / "variant_02_submit_report.json"
            previous_report = previous_trace / "variant_01_submit_report.json"
            current_report.write_text("{}\n", encoding="utf-8")
            previous_report.write_text("{}\n", encoding="utf-8")
            downloaded = {
                "status": "downloaded",
                "image_count": 1,
                "expected_image_count": 1,
            }
            report = {
                "variants": [
                    {
                        "variant_index": 1,
                        "report_path": str(previous_report),
                        "downloaded": downloaded,
                    },
                    {
                        "variant_index": 2,
                        "report_path": str(current_report),
                        "downloaded": downloaded,
                    },
                ]
            }

            runner._prune_success_trace_screenshots(
                {
                    "download_dir": str(output_dir),
                    "request_mode": "independent_variants",
                    "variant_count": 2,
                },
                report,
                mode="single-batch-submit",
                start_variant=2,
                end_variant=2,
                keep_success_trace_screenshots=False,
            )

            self.assertFalse(current_screenshot.exists())
            self.assertTrue(previous_screenshot.exists())


if __name__ == "__main__":
    unittest.main()
