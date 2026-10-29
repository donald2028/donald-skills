from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_browser_runner as runner  # noqa: E402


def native_png() -> bytes:
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("native-asset", "keep these original bytes")
    Image.new("RGB", (300, 420), "coral").save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


class AssetRecoveryTests(unittest.TestCase):
    def test_worker_takeover_reopens_generation_but_preserves_live_download_target(self):
        url = 'https://chatgpt.com/c/original'
        for keep_assets in (False, True):
            with self.subTest(keep_assets=keep_assets), tempfile.TemporaryDirectory() as temporary:
                job = {'job_name': 'takeover', 'prompt_card': 'fixture.md', 'download_dir': temporary}
                runner._write_session_patch(job, {'submission_committed': 'unknown', 'conversation_url': url})
                args = argparse.Namespace(recover_worker=True, _retain_generated_assets=keep_assets)
                with (
                    mock.patch.object(runner, '_require_original_conversation', return_value=url),
                    mock.patch.object(runner, '_current_url', return_value=url),
                    mock.patch.object(runner, '_validate_session_reference_mapping'),
                    mock.patch.object(runner, '_open_conversation') as navigate,
                    mock.patch.object(runner, '_wait_until_idle') as wait,
                    mock.patch.object(runner, '_ensure_chat_surface', side_effect=RuntimeError('checkpoint')),
                    mock.patch.object(runner, '_submit_prompt') as send,
                    self.assertRaisesRegex(RuntimeError, 'checkpoint'),
                ):
                    runner._run_one_request(args, job, Path.cwd(), label='batch', message='Draw a fox', resume=True)
                self.assertEqual(navigate.call_count, 0 if keep_assets else 1)
                self.assertEqual(wait.call_count, 0 if keep_assets else 1)
                send.assert_not_called()

    def test_claims_saved_live_target_even_without_a_retained_state_entry(self):
        browser = object.__new__(runner._ResumeImageBrowserSession)
        browser.original_target, browser.port = "original", 1
        browser.require_target = True
        browser.scope, browser.owned_target_ids = "donald-chatgpt-imagegen", set()
        state = {"active_runs": [], "retained_targets": []}
        with mock.patch.object(runner, "cdp_target", return_value={"id": "original"}), mock.patch.object(browser, "_navigate") as navigate:
            self.assertEqual(browser._claim_target(state), "original")
            state["active_runs"] = [{"target_id": "original"}]
            with self.assertRaises(runner.OriginalConversationRequired):
                browser._claim_target(state)
        navigate.assert_not_called()

    def test_same_url_failed_copy_yields_to_prompt_verified_asset_target(self):
        url = "https://chatgpt.com/c/original"
        args = argparse.Namespace(_submitted_prompt="Draw a fox", _owned_tab_cdp_url="ws://failed",
                                  _owned_tab_cdp_target_id="failed")
        targets = [{"id": name, "url": url, "webSocketDebuggerUrl": "ws://" + name}
                   for name in ["failed", "unrelated", "original"]]
        def inventory(probe, _cwd):
            if probe._owned_tab_cdp_target_id == "failed":
                raise RuntimeError("Could not load this ChatGPT conversation")
            return [{"src": "blob:asset", "width": 300, "height": 420, "inAssistant": True,
                     "submittedPromptChecked": True,
                     "inSubmittedTurn": probe._owned_tab_cdp_target_id == "original"}]
        with (
            mock.patch.object(runner, "_cdp_page_targets", return_value=targets),
            mock.patch.object(runner, "_image_inventory", side_effect=inventory),
            mock.patch.object(runner, "_open_conversation") as navigate,
            mock.patch.object(runner, "_close_owned_tab") as close,
            mock.patch.object(runner, "_submit_prompt") as submit,
        ):
            runner._recover_generated_asset_target(args, Path.cwd(), {"conversation_url": url})
        self.assertEqual(args._owned_tab_cdp_target_id, "original")
        navigate.assert_not_called(); close.assert_not_called(); submit.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "browser-state.json"
            state_path.write_text(json.dumps({"active_runs": [{"run_id": "other", "target_id": "original"}]}))
            args._browser_session = argparse.Namespace(state_path=state_path, run_id="collector")
            args._owned_tab_cdp_target_id, args._owned_tab_cdp_url = "failed", "ws://failed"
            with (mock.patch.object(runner, "_cdp_page_targets", return_value=targets),
                  mock.patch.object(runner, "_image_inventory", side_effect=inventory)):
                runner._recover_generated_asset_target(args, Path.cwd(), {"conversation_url": url})
            self.assertEqual(args._owned_tab_cdp_target_id, "failed")

    def test_page_loading_retry_is_not_a_generation_failure(self):
        with mock.patch.object(runner, "_eval_json", return_value={
            "href": "https://chatgpt.com/c/test", "pageLoadingError": True,
            "latestTurnText": "Could not load this ChatGPT conversation", "retryControlLabels": ["Try again"],
        }):
            health = runner._generation_page_health(argparse.Namespace(), Path.cwd(), None, "Try again")
        self.assertEqual(health["status"], "page_error")
        self.assertNotIn("generation_error", health)

    def test_visible_asset_without_a_download_button_enters_collection(self):
        self.assertEqual(runner._should_download_candidates(candidate_count=1, expected_count=1,
            busy=False, download_ready=False, first_seen_at=0, last_growth_at=0, now=1), (True, "complete"))

    def test_blob_download_preserves_native_png_bytes(self):
        data = native_png()
        payload = json.dumps({"data_url": "data:image/png;base64," + base64.b64encode(data).decode()})
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(runner, "_eval_js", return_value=payload):
            downloaded = runner._download_urls(argparse.Namespace(), Path.cwd(), ["blob:https://chatgpt.com/image"], Path(temporary), "native")
            self.assertEqual(Path(downloaded[0]["path"]).read_bytes(), data)
            self.assertTrue(downloaded[0]["image_validation"]["native_bytes_preserved"])

    def test_blob_download_failure_never_uses_unauthenticated_http(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(runner, "_download_url_with_browser", side_effect=RuntimeError("expired Blob")),
            mock.patch.object(runner.urllib.request, "urlopen") as http,
            self.assertRaises(RuntimeError),
        ):
            runner._download_urls(argparse.Namespace(), Path.cwd(), ["blob:https://chatgpt.com/image"], Path(temporary), "native")
        http.assert_not_called()

    def test_timeout_with_assets_collects_before_acting_on_stale_retry(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        url = "https://chatgpt.com/c/original"
        candidate = {"src": "blob:original", "width": 300, "height": 420, "inAssistant": True}
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "collect", "prompt_card": "fixture.md", "download_dir": temporary, "variant_count": 1}
            image = Path(temporary) / "native.png"
            image.write_bytes(native_png())
            runner._write_session_patch(job, {"generation_error": {"matched_marker": "old Retry"}})
            with (
                mock.patch.object(runner.time, "time", side_effect=lambda: clock[0]),
                mock.patch.object(runner.time, "sleep", side_effect=sleep),
                mock.patch.multiple(runner,
                    _ensure_expected_conversation=mock.Mock(return_value={"current_url": url}),
                    _persist_canonical_conversation=mock.Mock(return_value=url),
                    _page_text=mock.Mock(return_value=""), _image_inventory=mock.Mock(return_value=[candidate]),
                    _generation_button_state=mock.Mock(return_value={"checked": True, "generationActive": True}),
                    _generation_page_health=mock.Mock(return_value={"generation_error": {"matched_marker": "Try again"}}),
                    _download_candidates=mock.Mock(return_value=[{"path": str(image)}]),
                    _emit_progress=mock.Mock(), _screenshot=mock.Mock(), _scroll_down=mock.Mock(),
                    _write_timing_sample=mock.Mock()),
                mock.patch.object(runner, "_submit_prompt") as submit,
                mock.patch.object(runner, "_click_try_again") as retry,
            ):
                result = runner._collect_generated_images_with_retries(argparse.Namespace(timeout=5), job,
                    Path.cwd(), Path(temporary), set(), [], label="batch", mode="collect_current", resumed=True,
                    max_failure_retries=0, expected_conversation_url=url)
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(result["recommended_next_action"], "none")
            self.assertNotIn("generation_error", runner._read_json_if_exists(runner._session_path(job)))
            submit.assert_not_called(); retry.assert_not_called()

    def test_failed_download_preserves_blob_target_and_blocks_reload(self):
        browser = mock.Mock()
        args = argparse.Namespace(_browser_session=browser, keep_browser_open=False,
                                  _retain_generated_assets=True, _health_job={"download_dir": "/unused"})
        runner._cleanup_agent_browser(args, Path.cwd())
        browser.close.assert_called_once_with(preserve=True)
        with (
            mock.patch.object(runner, "_page_access_state", return_value={}),
            mock.patch.object(runner, "_open_url") as reopen,
            mock.patch.object(runner, "_navigate_owned_cdp_tab") as navigate,
            self.assertRaises(runner.OriginalConversationRequired),
        ):
            runner._recover_chatgpt_page(args, Path.cwd(), phase="post_submit_monitor:download",
                                         target_url="https://chatgpt.com/c/original", attempt=1, reason="download failed")
        reopen.assert_not_called(); navigate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
