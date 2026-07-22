from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import capture_thread  # noqa: E402
import capture_user_timeline  # noqa: E402
import research_post  # noqa: E402
import research_user  # noqa: E402


def _post(status_id: str, section: str = "posts") -> dict[str, object]:
    return {
        "status_id": status_id,
        "created_at": f"2026-07-{int(status_id):02d}",
        "source_section": section,
        "full_text": status_id,
    }


def _tweet(status_id: str) -> dict[str, object]:
    return {
        "status_id": status_id,
        "handle": "example",
        "created_at": f"2026-07-{int(status_id):02d}",
        "type": "tweet",
        "source_section": "posts",
        "metrics": {},
    }


class _CaptureBrowser:
    def open(self, _url: str) -> None:
        return None

    def page_text(self) -> str:
        return ""

    def enter_page(self) -> None:
        return None

    def list_request_ids(self, _filter_name: str) -> list[str]:
        return ["current-request"]

    def save_response(self, _request_id: str, path: Path) -> None:
        path.write_text("{}", encoding="utf-8")

    def scroll(self, _pixels: int) -> None:
        return None

    def click_tab(self, _label: str) -> None:
        return None


class ResearchUserContractTests(unittest.TestCase):
    def test_rejects_handle_directory_as_output_root_before_browser_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "OpenAI"
            with mock.patch.object(research_user, "BrowserSession") as session_type:
                result = research_user.run("OpenAI", output_root, port=9244)

        session_type.assert_not_called()
        self.assertEqual(result["status"], "needs_ops")
        self.assertEqual(result["reason"], "invalid_output_root")
        self.assertEqual(result["output_root"], str(output_root.resolve()))
        self.assertEqual(
            result["canonical_user_dir"],
            str((output_root / "OpenAI" / "_user").resolve()),
        )
        self.assertEqual(result["suggested_output_root"], str(output_root.parent.resolve()))

    def test_progress_timeline_never_applies_capture_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            post_dir = Path(temporary)
            posts = [_post(str(index)) for index in range(1, 11)]
            timeline = capture_user_timeline.write_progress_timeline(
                "example",
                post_dir,
                posts,
            )

            persisted = [
                json.loads(line)
                for line in (post_dir / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(timeline["count"], 10)
        self.assertEqual(len(persisted), 10)

    def test_head_overlap_is_computed_from_current_responses_not_all_runs(self) -> None:
        existing = [_tweet("1"), _tweet("2")]
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                capture_user_timeline,
                "load_tweets_from_runs",
                return_value=existing,
            ),
            mock.patch.object(
                capture_user_timeline,
                "_saved_response_has_tweets",
                return_value=True,
            ),
            mock.patch.object(
                capture_user_timeline,
                "_load_response_tweets",
                return_value=[_tweet("3")],
            ),
        ):
            result = capture_user_timeline.capture(
                "example",
                Path(temporary),
                browser=_CaptureBrowser(),
                sleep=lambda: None,
                max_scrolls=1,
                overlap_ids={"1", "2"},
                overlap_k=2,
                include_articles=False,
            )

        self.assertEqual(result["stop_reason"], "max_scrolls_reached")
        self.assertEqual(result["capture_new_posts"], 1)
        self.assertEqual(result["capture_known_overlap_posts"], 0)

    def test_articles_phase_has_an_independent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            post_dir = Path(temporary)
            times = iter((0.0, 91.0))
            block = capture_user_timeline._capture_articles(
                _CaptureBrowser(),
                post_dir,
                post_dir / "runs",
                set(),
                lambda: None,
                max_scrolls=20,
                timeout_seconds=90,
                monotonic=lambda: next(times),
            )
            debug = [
                json.loads(line)
                for line in (post_dir / "capture_debug.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertIsNone(block)
        self.assertEqual(debug[-1]["stop_reason"], "article_timeout")

    def test_max_posts_is_passed_to_capture_but_not_final_rebuild(self) -> None:
        timeline = {
            "count": 12,
            "section_counts": {"posts": 10, "articles": 2},
            "stop_reason": "exhausted",
            "posts": [_post(str(index)) for index in range(1, 11)],
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(research_user, "BrowserSession") as session_type,
            mock.patch.object(research_user.capture_user_timeline, "AgentBrowser"),
            mock.patch.object(
                research_user.capture_user_timeline,
                "capture",
                return_value={
                    "status": "complete",
                    "stop_reason": "max_posts",
                    "capture_new_posts": 5,
                    "capture_known_overlap_posts": 2,
                    "known_before_posts": 10,
                },
            ) as capture,
            mock.patch.object(
                research_user.extract_user_timeline,
                "write_timeline",
                return_value=timeline,
            ) as write_timeline,
            mock.patch.object(research_user, "append_manifest"),
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.target_id = "owned-target"
            session.close.return_value = {"status": "closed"}
            result = research_user.run(
                "example",
                Path(temporary),
                port=9244,
                max_posts=5,
                download_media_files=False,
            )

        self.assertEqual(capture.call_args.kwargs["max_posts"], 5)
        write_timeline.assert_called_once_with("example", Path(temporary).resolve())
        self.assertEqual(result["posts"], 10)
        self.assertEqual(result["articles"], 2)
        self.assertEqual(result["capture_new_posts"], 5)
        self.assertEqual(result["capture_known_overlap_posts"], 2)

    def test_head_skips_articles_unless_explicitly_included(self) -> None:
        timeline = {
            "count": 1,
            "section_counts": {"posts": 1},
            "stop_reason": "exhausted",
            "posts": [_post("1")],
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(research_user, "BrowserSession") as session_type,
            mock.patch.object(research_user.capture_user_timeline, "AgentBrowser"),
            mock.patch.object(
                research_user.capture_user_timeline,
                "capture",
                return_value={"status": "complete", "stop_reason": "overlap_with_manifest"},
            ) as capture,
            mock.patch.object(
                research_user.extract_user_timeline,
                "write_timeline",
                return_value=timeline,
            ),
            mock.patch.object(research_user, "append_manifest"),
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.target_id = "owned-target"
            session.close.return_value = {"status": "closed"}
            research_user.run(
                "example",
                Path(temporary),
                port=9244,
                mode="head",
                download_media_files=False,
            )
            self.assertFalse(capture.call_args.kwargs["include_articles"])

            research_user.run(
                "example",
                Path(temporary),
                port=9244,
                mode="head",
                include_articles=True,
                download_media_files=False,
            )
            self.assertTrue(capture.call_args.kwargs["include_articles"])

    def test_interrupt_updates_meta_and_releases_account_lock(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(research_user, "BrowserSession") as session_type,
            mock.patch.object(research_user.capture_user_timeline, "AgentBrowser"),
            mock.patch.object(
                research_user.capture_user_timeline,
                "capture",
                side_effect=KeyboardInterrupt,
            ),
        ):
            session = session_type.return_value
            session.open.return_value = session
            session.target_id = "owned-target"
            session.close.return_value = {"status": "closed"}
            output_root = Path(temporary)
            user_dir = output_root / "example" / "_user"
            result = research_user.run("example", output_root, port=9244)
            meta = json.loads((user_dir / "timeline.meta.json").read_text(encoding="utf-8"))

            self.assertFalse((user_dir / ".collect.lock").exists())

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(meta["stop_reason"], "interrupted")
        self.assertEqual(meta["status"], "interrupted")
        self.assertIn("Rerun", meta["recovery_hint"])
        session.close.assert_called_once_with()

    def test_browser_configuration_failure_has_stable_status_fields(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                research_user,
                "resolve_cdp_port",
                side_effect=research_user.ProfileConfigError("not configured"),
            ),
        ):
            result = research_user.run("example", Path(temporary))

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["reason"], "browser_profile_unconfigured")
        self.assertEqual(result["stop_reason"], "browser_profile_unconfigured")
        for key in (
            "output_root",
            "canonical_user_dir",
            "posts",
            "articles",
            "capture_new_posts",
            "capture_known_overlap_posts",
            "known_before_posts",
        ):
            self.assertIn(key, result)


class BrowserStateTests(unittest.TestCase):
    def test_captcha_is_a_machine_readable_block_reason(self) -> None:
        self.assertEqual(capture_thread.detect_block("Please verify you are human"), "captcha")

    def test_configured_cdp_port_is_default_and_explicit_port_is_override(self) -> None:
        with mock.patch.object(
            research_post,
            "configured_browser",
            return_value={"chrome": {"default_cdp_port": 9244}},
        ):
            self.assertEqual(research_post.resolve_cdp_port(), 9244)
        self.assertEqual(research_post.resolve_cdp_port(9333), 9333)


if __name__ == "__main__":
    unittest.main()
