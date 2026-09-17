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
                cli_reference_roles=["Identity portrait for the only subject"],
                cli_reference_spatial_maps=["Whole image; single subject"],
                cli_reference_uses=["Facial identity and hair only"],
                cli_reference_ignores=["Background, clothing, text, and composition"],
            )

        self.assertEqual(job["image_generation_mode"], "create_image")
        self.assertEqual(job["output_aspect_ratio"], "16:9")
        self.assertEqual(job["reference_images"][0]["path"], str(reference.resolve()))
        self.assertEqual(job["ordered_upload_paths"], [str(reference.resolve())])
        self.assertEqual(job["model_reference_map"][0]["index"], 1)
        self.assertIn("aspect ratio 16:9", job["chatgpt_batch_message"])
        self.assertIn("Reference Image 1", job["chatgpt_batch_message"])
        self.assertNotIn("reference.png", job["chatgpt_batch_message"])

    def test_prepare_job_compiles_five_distinct_references_without_filename_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["style.png", "identities.png", "meeting.png", "farewell.png", "digits.png"]
            for name in names:
                (root / name).write_bytes(b"reference")
            prompt = root / "prompt.md"
            prompt.write_text(
                """## Required Reference Images

1. `style.png`
   - Role: Page visual style anchor.
   - Spatial map: Whole image; no subject identity mapping.
   - Use: Illustration style, palette, and rendering only.
   - Ignore: People, text, digits, and original layout.
2. `identities.png`
   - Role: Identity sheet for Mia, Ryan, and Owen.
   - Spatial map: Left is Mia, center is Ryan, right is Owen.
   - Use: Facial identity, hair, and body proportions.
   - Ignore: Clothing, background, captions, and composition.
3. `meeting.png`
   - Role: A1 meeting appearance reference.
   - Spatial map: Left is Mia, center is Ryan, right is Owen.
   - Use: Ordinary clothing for the meeting scene only.
   - Ignore: Text, numbers, props, and original layout.
4. `farewell.png`
   - Role: Farewell accessory reference.
   - Spatial map: Top panel is Ryan's backpack and telescope; bottom panel is Mia's backpack.
   - Use: Those named accessories in the farewell scene only.
   - Ignore: People, clothing, captions, and panel layout.
5. `digits.png`
   - Role: B1 and B2 character appearance reference.
   - Spatial map: Left is Mia, center is Owen, right is Ryan.
   - Use: Character appearance for B1 and B2 only.
   - Ignore: Card digits, subtitles, clothing conflicts, and speaking relationships.

## Prompt

Create the requested illustrated page.
""",
                encoding="utf-8",
            )

            job = prepare_job.build_job(
                prompt_path=prompt,
                output_dir=root / "output",
                variant_count=1,
                request_mode="single_batch",
                aspect_ratio="16:9",
                reference_base_dir=root,
                cli_references=[],
                variant_notes=[],
                reuse_conversation_references=False,
            )

        self.assertEqual([entry["index"] for entry in job["model_reference_map"]], [1, 2, 3, 4, 5])
        self.assertEqual(job["ordered_upload_paths"], [str((root / name).resolve()) for name in names])
        for index in range(1, 6):
            self.assertIn(f"Reference Image {index}\n", job["compiled_model_reference_map"])
        for name in names:
            self.assertNotIn(name, job["chatgpt_batch_message"])
        runner._validate_manifest_reference_mapping(job)
        leaked_job = {
            **job,
            "chatgpt_batch_message": job["chatgpt_batch_message"] + "Use style.png.\n",
        }
        with self.assertRaisesRegex(ValueError, "local reference path or filename"):
            runner._validate_manifest_reference_mapping(leaked_job)

    def test_prepare_job_rejects_missing_or_duplicate_reference_map_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.png").write_bytes(b"one")
            (root / "two.png").write_bytes(b"two")
            prompt = root / "prompt.md"
            build = lambda: prepare_job.build_job(
                prompt_path=prompt,
                output_dir=root / "output",
                variant_count=1,
                request_mode="single_batch",
                aspect_ratio="1:1",
                reference_base_dir=root,
                cli_references=[],
                variant_notes=[],
                reuse_conversation_references=False,
            )
            prompt.write_text(
                """## Required Reference Images
1. `one.png`
   - Role: Primary identity portrait.
   - Spatial map: Whole image; single subject.
   - Use: Face and hair only.
   - Ignore: Clothing and background.
2. `two.png`
## Prompt
Create an image.
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Reference Image 2 is missing"):
                build()

            prompt.write_text(
                """## Required Reference Images
1. `one.png`
1. `two.png`
## Prompt
Create an image.
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate index 1"):
                build()

    def test_prepare_job_rejects_filename_and_reference_image_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "identity.png").write_bytes(b"identity")
            prompt = root / "prompt.md"

            def build(role: str) -> None:
                prompt.write_text(
                    f"""## Required Reference Images
1. `identity.png`
   - Role: {role}
   - Spatial map: Whole image; single subject.
   - Use: Face and hair only.
   - Ignore: Clothing and background.
## Prompt
Create an image.
""",
                    encoding="utf-8",
                )
                prepare_job.build_job(
                    prompt_path=prompt,
                    output_dir=root / "output",
                    variant_count=1,
                    request_mode="single_batch",
                    aspect_ratio="1:1",
                    reference_base_dir=root,
                    cli_references=[],
                    variant_notes=[],
                    reuse_conversation_references=False,
                )

            with self.assertRaisesRegex(ValueError, "path or filename"):
                build("identity.png")
            with self.assertRaisesRegex(ValueError, "generic placeholder"):
                build("Reference Image 1")

    def test_prepare_job_without_references_does_not_emit_reference_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.txt"
            prompt.write_text("Create an image.", encoding="utf-8")
            job = prepare_job.build_job(
                prompt_path=prompt,
                output_dir=root / "output",
                variant_count=1,
                request_mode="single_batch",
                aspect_ratio="1:1",
                reference_base_dir=root,
                cli_references=[],
                variant_notes=[],
                reuse_conversation_references=False,
            )

        self.assertEqual(job["model_reference_map"], [])
        self.assertEqual(job["compiled_model_reference_map"], "")
        self.assertNotIn("REFERENCE IMAGE MAP", job["chatgpt_batch_message"])

    def test_runner_uploads_reference_files_sequentially_and_records_order(self) -> None:
        references = ["C:/refs/one.png", "C:/refs/two.png", "C:/refs/three.png"]
        observations = [
            {
                "attachment_count": index,
                "blob_image_count": index,
                "ready": True,
                "attachment_labels": [
                    f"Remove file {position}: {Path(path).name}"
                    for position, path in enumerate(references[:index], start=1)
                ],
            }
            for index in range(1, 4)
        ]
        with (
            mock.patch.object(runner, "_upload_files") as upload,
            mock.patch.object(
                runner,
                "_wait_for_reference_uploads_ready",
                side_effect=observations,
            ),
        ):
            result = runner._upload_references_in_order(
                argparse.Namespace(),
                Path.cwd(),
                references,
            )

        self.assertEqual(
            [call.args[2] for call in upload.call_args_list],
            [[references[0]], [references[1]], [references[2]]],
        )
        self.assertTrue(result["order_verified"])
        self.assertTrue(result["attachment_label_order_verified"])
        self.assertEqual(
            result["order_verification_method"],
            "visible_attachment_labels_and_sequential_file_input",
        )
        self.assertEqual(
            [(entry["index"], entry["path"]) for entry in result["upload_sequence"]],
            list(enumerate(references, start=1)),
        )

    def test_runner_rejects_visible_attachment_order_mismatch(self) -> None:
        references = ["C:/refs/one.png", "C:/refs/two.png"]
        observations = [
            {
                "attachment_count": 1,
                "blob_image_count": 1,
                "ready": True,
                "attachment_labels": ["Remove file 1: one.png"],
            },
            {
                "attachment_count": 2,
                "blob_image_count": 2,
                "ready": True,
                "attachment_labels": [
                    "Remove file 1: two.png",
                    "Remove file 2: one.png",
                ],
            },
        ]
        with (
            mock.patch.object(runner, "_upload_files"),
            mock.patch.object(
                runner,
                "_wait_for_reference_uploads_ready",
                side_effect=observations,
            ),
            self.assertRaises(runner.ReferenceUploadError) as raised,
        ):
            runner._upload_references_in_order(
                argparse.Namespace(),
                Path.cwd(),
                references,
            )

        self.assertEqual(raised.exception.failure["error_type"], "reference_upload_order_mismatch")

    def test_run_summary_keeps_upload_and_model_reference_audits_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ordered_paths = ["C:/refs/style.png", "C:/refs/identity.png"]
            model_map = [
                {
                    "index": 1,
                    "model_role": "Visual style anchor",
                    "spatial_map": "Whole image",
                    "use": "Rendering style only",
                    "ignore": "People and text",
                },
                {
                    "index": 2,
                    "model_role": "Identity sheet",
                    "spatial_map": "Left is Mia and right is Ryan",
                    "use": "Facial identity only",
                    "ignore": "Clothing and background",
                },
            ]
            compiled = prepare_job.compile_model_reference_map(model_map)
            job = {
                "job_name": "audit",
                "prompt_card": "C:/prompts/audit.md",
                "download_dir": temporary,
                "reference_images": [
                    {"index": index, "path": path}
                    for index, path in enumerate(ordered_paths, start=1)
                ],
                "ordered_upload_paths": ordered_paths,
                "model_reference_map": model_map,
                "compiled_model_reference_map": compiled,
            }

            summary = runner._write_summary(
                job,
                request_mode="single_batch",
                variant_count=1,
                start_variant=1,
                end_variant="batch",
                variants=[],
            )

        self.assertEqual(summary["ordered_upload_paths"], ordered_paths)
        self.assertEqual(summary["model_reference_map"], model_map)
        self.assertEqual(summary["compiled_model_reference_map"], compiled)
        self.assertEqual(
            summary["reference_upload_order"],
            [
                {"index": 1, "path": ordered_paths[0]},
                {"index": 2, "path": ordered_paths[1]},
            ],
        )

    def test_prepare_job_rejects_invalid_aspect_ratio(self) -> None:
        with self.assertRaisesRegex(ValueError, "WIDTH:HEIGHT"):
            prepare_job.validate_aspect_ratio("wide")

    def test_create_image_mode_opens_add_menu_and_verifies_selection(self) -> None:
        states = [
            {"selected": False, "editorText": "", "hasPromptCategories": False},
            {"found": False},
            {
                "found": True,
                "x": 10,
                "y": 10,
                "evidenceSource": "composer_left_edge_button",
            },
            {
                "found": True,
                "label": "Create image Visualize anything",
                "role": "menuitem",
                "x": 20,
                "y": 20,
            },
            {
                "selected": True,
                "editorText": "",
                "placeholderText": "Create image",
                "hasPromptCategories": False,
            },
        ]
        with (
            mock.patch.object(runner, "_eval_json", side_effect=states),
            mock.patch.object(
                runner,
                "_dispatch_owned_tab_click",
                side_effect=[True, True],
            ) as click,
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
        self.assertEqual(click.call_count, 2)
        self.assertEqual(
            result["menu_control"]["evidenceSource"],
            "composer_left_edge_button",
        )
        self.assertEqual(result["verification"]["placeholderText"], "Create image")

    def test_create_image_ui_contract_supports_current_plus_menu_and_placeholder(self) -> None:
        self.assertIn("data-placeholder", runner.IMAGE_MODE_STATE_JS)
        self.assertIn("aria-placeholder", runner.IMAGE_MODE_STATE_JS)
        self.assertIn("selectedControlLabel", runner.IMAGE_MODE_STATE_JS)
        self.assertIn("[role='menuitem']", runner.CREATE_IMAGE_CONTROL_JS)
        self.assertIn("[role='group']", runner.CREATE_IMAGE_CONTROL_JS)
        self.assertIn(".popover", runner.CREATE_IMAGE_CONTROL_JS)
        self.assertIn("Generate", runner.CREATE_IMAGE_CONTROL_JS)
        self.assertIn("composer.*plus", runner.IMAGE_MODE_ADD_MENU_CONTROL_JS)
        self.assertIn("composer_left_edge_button", runner.IMAGE_MODE_ADD_MENU_CONTROL_JS)
        self.assertIn("atLeadingEdge", runner.IMAGE_MODE_ADD_MENU_CONTROL_JS)

    def test_missing_create_image_control_requests_pre_submit_page_recovery(self) -> None:
        with (
            mock.patch.object(
                runner,
                "_image_mode_state",
                return_value={"selected": False},
            ),
            mock.patch.object(
                runner,
                "_find_create_image_control",
                return_value={"found": False},
            ),
            mock.patch.object(
                runner,
                "_find_image_mode_add_menu_control",
                return_value={"found": True, "expanded": False, "x": 10, "y": 10},
            ),
            mock.patch.object(
                runner,
                "_poll_for_create_image_control",
                return_value={"found": False},
            ),
            mock.patch.object(
                runner,
                "_poll_for_image_mode_selection",
                return_value={"selected": False},
            ),
            mock.patch.object(runner, "_dispatch_owned_tab_click", return_value=True),
            mock.patch.object(runner, "_wait_ms"),
            self.assertRaises(runner.RecoverablePageError),
        ):
            runner._enable_image_mode_if_available(
                argparse.Namespace(),
                Path.cwd(),
                "",
            )

    def test_page_health_recognizes_subscription_and_network_errors(self) -> None:
        self.assertIn("failed to load subscription", runner.PAGE_ACCESS_STATE_JS)
        self.assertIn("chatgpt_transient_page_error", runner.PAGE_ACCESS_STATE_JS)
        self.assertIn("[data-message-author-role]", runner.PAGE_ACCESS_STATE_JS)

    def test_image_mode_failure_stays_before_upload_prompt_and_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {
                "job_name": "image-mode-gate",
                "download_dir": temporary,
                "prompt_card": str(Path(temporary) / "prompt.md"),
                "variant_count": 1,
                "output_aspect_ratio": "1:1",
                "reference_images": [{"path": str(Path(temporary) / "reference.png")}],
            }
            args = argparse.Namespace(
                page_recovery_attempts=3,
                max_failure_retries=0,
            )
            with (
                mock.patch.object(runner, "_wait_for_submit_throttle_slot", return_value={}),
                mock.patch.object(runner, "_open_new_chat"),
                mock.patch.object(runner, "_wait_for_prompt_box"),
                mock.patch.object(
                    runner,
                    "_ensure_chat_surface",
                    return_value={"selected_surface": "chat", "verified": True},
                ),
                mock.patch.object(runner, "_validate_account_lane", return_value={"ok": True}),
                mock.patch.object(runner, "_screenshot"),
                mock.patch.object(runner, "_owned_tab_cdp_url", return_value="ws://owned"),
                mock.patch.object(
                    runner,
                    "_enable_image_mode_if_available",
                    side_effect=runner.ImageModeSelectionError("not verified"),
                ),
                mock.patch.object(runner, "_upload_references_in_order") as upload,
                mock.patch.object(runner, "_paste_prompt") as paste,
                mock.patch.object(runner, "_submit_prompt") as submit,
                self.assertRaises(runner.ImageModeSelectionError),
            ):
                runner._run_one_request(
                    args,
                    job,
                    Path.cwd(),
                    label="batch",
                    message="Generate an image",
                    resume=False,
                )

        upload.assert_not_called()
        paste.assert_not_called()
        submit.assert_not_called()

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
