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
import prepare_job  # noqa: E402


class ImageRequestContractTests(unittest.TestCase):
    def test_chat_surface_selector_matches_current_chatgpt_contract(self) -> None:
        self.assertIn("Select chat surface", runner.CHAT_SURFACE_STATE_JS)
        self.assertIn("data-tpp-toggle-value='chatgpt'", runner.CHAT_SURFACE_STATE_JS)
        self.assertIn("data-tpp-toggle-value='work'", runner.CHAT_SURFACE_STATE_JS)

    def test_chat_surface_switches_from_work_to_chat_and_verifies(self) -> None:
        work_state = {
            "control_available": True,
            "selected_surface": "work",
            "chat_selected": False,
            "work_selected": True,
            "chat_control": {"found": True, "x": 20, "y": 30},
        }
        chat_state = {
            "control_available": True,
            "selected_surface": "chat",
            "chat_selected": True,
            "work_selected": False,
            "chat_control": {"found": True, "x": 20, "y": 30},
        }
        with (
            mock.patch.object(runner, "_eval_json", side_effect=[work_state, chat_state]),
            mock.patch.object(runner, "_dispatch_owned_tab_click", return_value=True) as click,
            mock.patch.object(runner, "_wait_ms"),
        ):
            result = runner._ensure_chat_surface(argparse.Namespace(), Path.cwd())

        click.assert_called_once()
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_surface"], "chat")
        self.assertTrue(result["clicked_chat"])
        self.assertTrue(result["verified"])

    def test_existing_conversation_requires_verified_chat_surface_record(self) -> None:
        recorded = {
            "verified": True,
            "selected_surface": "chat",
            "evidence_source": "live_selector",
        }
        with mock.patch.object(
            runner,
            "_eval_json",
            return_value={"control_available": False, "selected_surface": "unknown"},
        ):
            result = runner._ensure_chat_surface(
                argparse.Namespace(),
                Path.cwd(),
                recorded=recorded,
                timeout_s=0,
            )

        self.assertEqual(result["status"], "verified_from_session")
        self.assertEqual(result["selected_surface"], "chat")
        self.assertTrue(result["verified"])
        self.assertNotIn("recorded", result)
        self.assertEqual(result["origin_evidence_source"], "live_selector")

        with (
            mock.patch.object(
                runner,
                "_eval_json",
                return_value={"control_available": False, "selected_surface": "unknown"},
            ),
            self.assertRaises(runner.ChatSurfaceSelectionError),
        ):
            runner._ensure_chat_surface(
                argparse.Namespace(),
                Path.cwd(),
                timeout_s=0,
            )

    def test_prepare_job_preserves_reference_and_aspect_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.txt"
            reference = root / "reference.png"
            prompt.write_text("Create a new composition.", encoding="utf-8")
            reference.write_bytes(b"reference")

            job = prepare_job.build_job(
                prompt_path=prompt,
                output_dir=root / "output",
                variant_count=1,
                request_mode="single_batch",
                aspect_ratio="16:9",
                reference_base_dir=root,
                cli_references=[str(reference)],
                variant_notes=[],
                reuse_conversation_references=False,
            )

        self.assertEqual(job["image_generation_mode"], "create_image")
        self.assertEqual(job["output_aspect_ratio"], "16:9")
        self.assertEqual(job["reference_images"][0]["path"], str(reference.resolve()))
        self.assertIn("aspect ratio 16:9", job["chatgpt_batch_message"])
        self.assertIn("Reference Image 1", job["chatgpt_batch_message"])

    def test_prepare_job_rejects_invalid_aspect_ratio(self) -> None:
        with self.assertRaisesRegex(ValueError, "WIDTH:HEIGHT"):
            prepare_job.validate_aspect_ratio("wide")

    def test_create_image_mode_opens_add_menu_and_verifies_selection(self) -> None:
        states = [
            {"selected": False, "editorText": "", "hasPromptCategories": False},
            {"found": False},
            {"found": True, "x": 10, "y": 10},
            {"found": True, "label": "Create image Visualize anything", "x": 20, "y": 20},
            {"selected": True, "editorText": "Create image", "hasPromptCategories": True},
        ]
        with (
            mock.patch.object(runner, "_eval_json", side_effect=states),
            mock.patch.object(
                runner,
                "_dispatch_owned_tab_click",
                side_effect=[False, True, True],
            ),
            mock.patch.object(runner, "_wait_ms"),
        ):
            result = runner._enable_image_mode_if_available(
                argparse.Namespace(),
                Path.cwd(),
                "",
            )

        self.assertEqual(result["status"], "selected")
        self.assertTrue(result["opened_add_menu"])
        self.assertTrue(result["clicked_create_image"])
        self.assertTrue(result["verified"])

    def test_reference_upload_uses_visible_composer_attachment_as_evidence(self) -> None:
        reference = "C:/images/reference.png"
        with (
            mock.patch.object(runner, "_page_text", return_value=""),
            mock.patch.object(
                runner,
                "_image_inventory",
                return_value=[{"src": "https://chatgpt.com/backend-api/estuary/content?id=upload"}],
            ),
            mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://owned-tab"),
            mock.patch.object(
                runner,
                "_eval_json",
                return_value={
                    "removeFileCount": 1,
                    "removeFileLabels": ["Remove file 1: reference(1).png"],
                    "uploadedImagePreviewCount": 1,
                    "uploadedImagePreviewLabels": ["Open image: User uploaded image"],
                },
            ),
        ):
            observation = runner._reference_upload_observation(
                argparse.Namespace(),
                Path.cwd(),
                [reference],
            )

        self.assertTrue(observation["ready"])
        self.assertEqual(observation["attachment_count"], 1)
        self.assertTrue(observation["filename_mentions"]["reference.png"])

    def test_aspect_ratio_control_is_clicked_and_verified_when_available(self) -> None:
        states = [
            {"found": True, "selected": False, "label": "16:9", "x": 20, "y": 30},
            {"found": True, "selected": True, "label": "16:9", "x": 20, "y": 30},
        ]
        with (
            mock.patch.object(runner, "_eval_json", side_effect=states),
            mock.patch.object(runner, "_dispatch_owned_tab_click", return_value=True),
            mock.patch.object(runner, "_wait_ms"),
        ):
            result = runner._configure_aspect_ratio(
                argparse.Namespace(),
                Path.cwd(),
                "16:9",
            )

        self.assertEqual(result["delivery"], "ui_control_and_prompt_text")
        self.assertTrue(result["ui_control_available"])
        self.assertTrue(result["clicked"])
        self.assertTrue(result["verified"])

    def test_aspect_ratio_validation_detects_match_and_mismatch(self) -> None:
        matching = {
            "images": [{"image_validation": {"width": 1600, "height": 900}}],
        }
        mismatching = {
            "images": [{"image_validation": {"width": 1536, "height": 1024}}],
        }

        matching_result = runner._annotate_aspect_ratio_validation(
            {"output_aspect_ratio": "16:9"}, matching
        )
        mismatching_result = runner._annotate_aspect_ratio_validation(
            {"output_aspect_ratio": "16:9"}, mismatching
        )

        self.assertTrue(matching_result["all_match"])
        self.assertFalse(mismatching_result["all_match"])
        self.assertTrue(matching["images"][0]["aspect_ratio_validation"]["matches"])
        self.assertFalse(mismatching["images"][0]["aspect_ratio_validation"]["matches"])

    def test_submit_scripts_scope_send_button_to_composer_form(self) -> None:
        for script in (runner.COMPOSER_SUBMIT_STATE_JS, runner.COMPOSER_CLICK_SEND_JS):
            self.assertIn('editor.closest("form")', script)
            self.assertIn('button.id === "composer-submit-button"', script)
            self.assertIn('button.getAttribute("data-testid") === "send-button"', script)
            self.assertNotIn("/send|submit|发送|提交/i", script)

    def test_submission_evidence_requires_observed_prompt(self) -> None:
        state = {
            "submitted": True,
            "editorHasPromptTail": False,
            "editorTextLength": 0,
            "conversationStarted": True,
            "userMessageCount": 1,
        }

        self.assertFalse(
            runner._composer_state_has_submission_evidence(
                state,
                saw_prompt=False,
                enter_presses=0,
            )
        )
        self.assertTrue(
            runner._composer_state_has_submission_evidence(
                state,
                saw_prompt=True,
                enter_presses=0,
            )
        )


if __name__ == "__main__":
    unittest.main()
