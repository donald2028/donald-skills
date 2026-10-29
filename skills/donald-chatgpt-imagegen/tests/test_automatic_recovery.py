from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_browser_runner as runner
from run_health import SubmissionCircuit


class Clock:
    now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class AutomaticRecoveryTests(unittest.TestCase):
    def test_upstream_rebuild_collects_an_existing_image_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'collect-first', 'prompt_card': 'fixture.md', 'download_dir': temporary}
            runner._write_session_patch(job, {'submission_committed': 'unknown'})
            args = argparse.Namespace(mode='single-batch-submit', rebuild_request=True)
            operation = mock.Mock()
            with (mock.patch.object(runner, 'run_collect_current', return_value={
                    'status': 'downloaded', 'image_count': 1, 'expected_image_count': 1}) as collect,
                  mock.patch.object(runner, '_rebuild_unreachable_request') as rebuild):
                self.assertEqual(runner._execute_with_automatic_recovery(args, job, Path.cwd(), operation)['status'], 'downloaded')
            collect.assert_called_once(); rebuild.assert_not_called(); operation.assert_not_called()

    def test_rebuild_preserves_inherited_reference_request_until_local_inputs_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'references', 'prompt_card': 'fixture.md', 'download_dir': temporary,
                   'reuse_conversation_references': True}
            runner._write_session_patch(job, {'submission_committed': True})
            args = argparse.Namespace(mode='conversation-followup')
            result = runner._rebuild_unreachable_request(args, job, Path.cwd(),
                runner.OriginalConversationRequired('original unavailable'), mock.Mock())
            self.assertEqual(result['error_type'], 'rebuild_inputs_required')
            self.assertTrue(runner.terminal_failure(result))
            self.assertTrue(runner._read_json_if_exists(runner._session_path(job))['submission_committed'])

    def test_gallery_repair_preserves_live_generation_loaded_assets_and_temporary_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'gallery', 'prompt_card': 'fixture.md', 'download_dir': temporary}
            args = argparse.Namespace()
            idle = {'checked': True, 'readyForNextPrompt': True}
            gallery = {'galleryCount': 1, 'placeholderCount': 1, 'loadedGeneratedImages': 0}
            url = 'https://chatgpt.com/c/original'
            with (mock.patch.object(runner, '_pending_gallery_state', return_value=gallery),
                  mock.patch.object(runner, '_page_access_state', return_value={}),
                  mock.patch.object(runner, '_current_url', return_value=url),
                  mock.patch.object(runner, '_refresh_expected_conversation') as reload):
                for buttons, candidates, target, loaded in [
                    ({**idle, 'generationActive': True}, [], url, 0),
                    (idle, [{'src': 'blob:loaded'}], url, 0),
                    (idle, [], url, 1),
                    (idle, [], 'https://chatgpt.com/c/local-chatgpt%3Aoriginal', 0),
                ]:
                    gallery['loadedGeneratedImages'] = loaded
                    runner._write_session_patch(job, {'pending_asset_recovery': {'first_seen_at': runner.time.time() - 40, 'reloads': 0}})
                    self.assertFalse(runner._recover_pending_gallery(args, job, Path.cwd(), 'batch', target, buttons, candidates))
                reload.assert_not_called()
                runner._write_session_patch(job, {'pending_asset_recovery': {'first_seen_at': 1, 'last_reload_at': 1, 'reloads': 2}})
                with self.assertRaises(runner.OriginalConversationRequired):
                    runner._recover_pending_gallery(args, job, Path.cwd(), 'batch', url, idle, [])
                reload.assert_not_called()

    def test_upstream_rebuild_does_not_retry_policy_refusal_or_override_reconcile_only(self):
        args = argparse.Namespace(mode='single-batch-submit', rebuild_request=True)
        operation = mock.Mock(return_value={'status': 'policy_refused'})
        with mock.patch.object(runner, 'run_collect_current') as collect:
            self.assertEqual(runner._execute_with_automatic_recovery(args, {}, Path.cwd(), operation)['status'], 'policy_refused')
            collect.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            args.reconcile_only = True
            job = {'download_dir': temporary}
            with self.assertRaises(runner.OriginalConversationRequired):
                runner._rebuild_unreachable_request(args, job, Path.cwd(), runner.OriginalConversationRequired('missing'), operation)

    def test_replacement_cap_retains_history_and_accepts_one_explicit_takeover_per_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'cap', 'prompt_card': 'fixture.md', 'download_dir': temporary, 'chatgpt_batch_message': 'Draw a tree.'}
            old = {'submission_committed': 'unknown', 'response_retry_count': 2, 'request_rebuild_count': 2}
            runner._write_session_patch(job, old)
            args = argparse.Namespace(mode='single-batch-submit', _automatic_recovery_actions=[], _health_job=job)
            operation = mock.Mock(return_value={'status': 'downloaded'})
            error = runner.OriginalConversationRequired('Original target and history unavailable')
            with self.assertRaises(runner.OriginalConversationRequired):
                runner._rebuild_unreachable_request(args, job, Path.cwd(), error, operation)
            args.rebuild_request = True
            with (mock.patch.object(runner, '_page_access_state', return_value={}),
                  mock.patch.object(runner, '_checked_generation_button_state', return_value={'checked': True, 'readyForNextPrompt': True}),
                  mock.patch.object(runner, '_image_inventory', return_value=[]),
                  mock.patch.object(runner, '_pending_gallery_state', return_value={}),
                  mock.patch.object(runner, '_replace_unsubmitted_tab')):
                result = runner._rebuild_unreachable_request(args, job, Path.cwd(), error, operation)
                self.assertEqual(result['status'], 'downloaded')
                saved = runner._read_json_if_exists(runner._session_path(job))
                self.assertEqual(json.loads(Path(saved['replacement_of']).read_text())['response_retry_count'], 2)
                self.assertEqual(saved['request_rebuild_count'], 3)
                with self.assertRaises(runner.OriginalConversationRequired):
                    runner._rebuild_unreachable_request(args, job, Path.cwd(), error, operation)
            operation.assert_called_once()

    def test_empty_automatic_collection_exits_nonzero_and_stops_caller_retry_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "empty", "prompt_card": "prompt.md", "download_dir": temporary}
            manifest = Path(temporary) / "job.json"
            manifest.write_text(json.dumps(job))
            runner._write_session_patch(job, {"submission_committed": True})
            output = io.StringIO()
            def profile(args):
                args.user_data_dir, args.profile = "", ""
            with (
                mock.patch.object(sys, "argv", ["runner", str(manifest), "--mode", "single-batch-submit"]),
                mock.patch.multiple(runner,
                    _resolve_browser_profile=mock.Mock(side_effect=profile),
                    _prepare_browser_lane=mock.Mock(), _validate_profile=mock.Mock(),
                    _validate_manifest_reference_mapping=mock.Mock(), _cleanup_agent_browser=mock.Mock(),
                    _heartbeat_probe=mock.Mock(return_value={"status": "browser_startup"}),
                    run_submit=mock.Mock(side_effect=runner.SubmissionStateUnknownError({"submission_committed": True})),
                    run_collect_current=mock.Mock(return_value={
                        "status": "collect_current_no_new_images", "image_count": 0, "expected_image_count": 1,
                        "should_collect_current_first": True, "recommended_next_action": "collect_current_first",
                    }),
                ),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(runner.main(), 2)
            report = json.loads(output.getvalue())
            self.assertTrue(report['automatic_recovery_exhausted'])
            self.assertFalse(report['should_collect_current_first'])
            self.assertFalse(report['retryable'])
            self.assertEqual(report['recommended_next_action'], 'inspect_original_conversation_before_manual_recovery')
            session = json.loads(runner._session_path(job).read_text())
            for key in ('automatic_recovery_exhausted', 'should_collect_current_first', 'retryable', 'recommended_next_action'):
                self.assertEqual(session[key], report[key])

    def test_verification_during_startup_preserves_its_browser_on_exit(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            manifest = Path(temporary) / "job.json"
            manifest.write_text(json.dumps({"job_name": "gate", "prompt_card": "prompt.md", "download_dir": temporary}))
            browser = mock.Mock()
            def profile(args):
                args.user_data_dir, args.profile = "", ""
            with (
                mock.patch.object(sys, "argv", ["runner", str(manifest), "--mode", "single-batch-submit"]),
                mock.patch.object(runner, "_resolve_browser_profile", side_effect=profile),
                mock.patch.object(runner, "_validate_manifest_reference_mapping"),
                mock.patch.object(runner, "_admit_job_with_recovery", return_value="probe"),
                mock.patch.object(runner, "_prepare_browser_lane", side_effect=lambda args, cwd: setattr(args, "_browser_session", browser)),
                mock.patch.object(runner, "_validate_profile"),
                mock.patch.object(runner, "_heartbeat_probe", return_value={"status": "browser_startup"}),
                mock.patch.object(runner, "_queue_probe", side_effect=runner.HumanAttentionRequired("anti_automation_verification", {})),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(runner.main(), 2)
            browser.close.assert_called_once_with(preserve=True)

    def test_normal_cli_finishes_its_recovery_without_a_second_caller_command(self):
        for committed in (False, "unknown"):
            with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
                root = Path(temporary)
                job = {"job_name": "cli", "prompt_card": "prompt.md", "download_dir": temporary}
                manifest = root / "job.json"
                manifest.write_text(json.dumps(job))
                runner._write_session_patch(job, {"submission_committed": committed})
                failure = (runner.PageRecoveryExhaustedError({"submission_committed": False})
                           if committed is False else runner.SubmissionStateUnknownError({"submission_committed": "unknown"}))
                output = io.StringIO()
                def profile(args):
                    args.user_data_dir, args.profile = "", ""
                with (
                    mock.patch.object(sys, "argv", ["runner", str(manifest), "--mode", "single-batch-submit"]),
                    mock.patch.object(runner, "_resolve_browser_profile", side_effect=profile),
                    mock.patch.object(runner, "_prepare_browser_lane"),
                    mock.patch.object(runner, "_validate_profile"),
                    mock.patch.object(runner, "_validate_manifest_reference_mapping"),
                    mock.patch.object(runner, "_heartbeat_probe", return_value={"status": "browser_startup"}),
                    mock.patch.object(runner, "run_submit", side_effect=[failure, {"status": "downloaded", "image_count": 1, "expected_image_count": 1}]) as submit,
                    mock.patch.object(runner, "run_collect_current", return_value={"status": "downloaded", "image_count": 1, "expected_image_count": 1}) as collect,
                    mock.patch.object(runner, "_cleanup_agent_browser"),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(runner.main(), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["status"], "downloaded")
                self.assertFalse(report["automatic_recovery_exhausted"])
                self.assertEqual(submit.call_count, 2 if committed is False else 1)
                self.assertEqual(collect.call_count, 0 if committed is False else 1)

    def test_uncertain_send_is_collected_inside_the_same_run_without_another_submission(self):
        for committed in (True, "unknown"):
            with tempfile.TemporaryDirectory() as temporary:
                job = {"job_name": "recover", "prompt_card": "prompt.md", "download_dir": temporary}
                runner._write_session_patch(job, {"submission_committed": committed})
                args = argparse.Namespace(mode="single-batch-submit", _job_id="mine")
                operation = mock.Mock(side_effect=runner.SubmissionStateUnknownError({"submission_committed": "unknown"}))
                with mock.patch.object(runner, "run_collect_current", return_value={"status": "downloaded"}) as collect:
                    result = runner._execute_with_automatic_recovery(args, job, Path.cwd(), operation)
                self.assertEqual(result["status"], "downloaded")
                operation.assert_called_once()
                collect.assert_called_once()

    def test_confirmed_unsubmitted_failure_is_retried_once_and_persistent_failure_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "recover", "prompt_card": "prompt.md", "download_dir": temporary}
            runner._write_session_patch(job, {"submission_committed": False})
            args = argparse.Namespace(mode="single-batch-submit", _job_id="mine")
            failure = runner.PageRecoveryExhaustedError({"submission_committed": False})
            for succeeds in (True, False):
                operation = mock.Mock(side_effect=[failure, {"status": "downloaded"} if succeeds else failure])
                if succeeds:
                    self.assertEqual(runner._execute_with_automatic_recovery(args, job, Path.cwd(), operation)["status"], "downloaded")
                else:
                    with self.assertRaises(runner.PageRecoveryExhaustedError):
                        runner._execute_with_automatic_recovery(args, job, Path.cwd(), operation)
                self.assertEqual(operation.call_count, 2)

    def test_missing_rendered_turn_collects_original_without_retrying_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "recover", "prompt_card": "prompt.md", "download_dir": temporary}
            runner._write_session_patch(job, {"submission_committed": True})
            args = argparse.Namespace(mode="conversation-followup", _job_id="mine")
            operation = mock.Mock(return_value={
                "status": "submitted", "variants": [{"downloaded": {
                    "status": "generation_failed", "error_type": "chatgpt_submitted_turn_missing",
                }}],
            })
            with mock.patch.object(runner, "run_collect_current", return_value={"status": "downloaded"}) as collect:
                result = runner._execute_with_automatic_recovery(args, job, Path.cwd(), operation)
            self.assertEqual(result["status"], "downloaded")
            operation.assert_called_once()
            collect.assert_called_once()

    def test_next_job_recovers_paused_owner_after_page_error_without_changing_owner_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_job = {"job_name": "B", "prompt_card": "B.md", "download_dir": str(root / "B")}
            new_job = {"job_name": "A", "prompt_card": "A.md", "download_dir": str(root / "A")}
            manifest = root / "B.json"
            manifest.write_text(json.dumps(old_job))
            runner._write_session_patch(old_job, {"submission_committed": True, "owned_target_id": "B-original"})
            before = runner._session_path(old_job).read_bytes()
            owner = hashlib.sha256(str(runner._session_path(old_job).resolve()).encode()).hexdigest()
            args = argparse.Namespace(_queue_path=root / "queue.json", _job_id="A", _queue_owner="lease", timeout=60)
            gate = SubmissionCircuit(args._queue_path)
            gate.verification(owner, 0, manifest=str(manifest))
            probe = argparse.Namespace(**vars(args))
            clock = Clock()
            clock.now = 1000
            with (
                mock.patch.object(runner.time, "monotonic", side_effect=lambda: clock.now),
                mock.patch.object(runner.time, "time", side_effect=lambda: clock.now),
                mock.patch.object(runner.time, "sleep", side_effect=clock.sleep),
                mock.patch.object(runner, "_separate_recovery_tab", return_value=contextlib.nullcontext(probe)),
                mock.patch.object(runner, "_open_url"),
                mock.patch.object(runner, "_click_page_error_retry", return_value=False),
                mock.patch.object(runner, "_page_access_state", side_effect=[
                    {"crashReason": "chatgpt_transient_page_error"},
                    {"hasPrompt": True, "readyState": "complete"},
                    {"hasPrompt": True, "readyState": "complete"},
                ]),
                mock.patch.object(runner, "_submit_prompt") as submit,
            ):
                self.assertEqual(runner._admit_job_with_recovery(args, new_job, Path.cwd()), "allow")
            self.assertEqual(gate.read()["state"], "running")
            self.assertEqual(runner._session_path(old_job).read_bytes(), before)
            submit.assert_not_called()

    def test_service_pause_waits_for_cooldown_and_obtains_only_a_probe_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock = Clock()
            args = argparse.Namespace(_queue_path=Path(temporary) / "queue.json", _queue_owner="lease", timeout=300)
            gate = SubmissionCircuit(args._queue_path)
            gate.failure("a", 0)
            gate.failure("b", 0)
            with (
                mock.patch.object(runner.time, "monotonic", side_effect=lambda: clock.now),
                mock.patch.object(runner.time, "time", side_effect=lambda: clock.now),
                mock.patch.object(runner.time, "sleep", side_effect=clock.sleep),
            ):
                self.assertEqual(runner._admit_job_with_recovery(args, {}, Path.cwd()), "probe")
            self.assertEqual(clock.now, 300)
            self.assertEqual(gate.read()["state"], "paused")

    def test_original_tab_history_recovers_only_one_prompt_verified_canonical_conversation(self):
        for ambiguous in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                job = {"job_name": "history", "prompt_card": "prompt.md", "download_dir": temporary}
                args = argparse.Namespace(_health_job=job, _submitted_prompt="draw a fox", _owned_tab_cdp_target_id="original")
                session = runner._write_session_patch(job, {"submission_committed": "unknown", "dispatch_started": True,
                    "owned_target_id": "original", "conversation_url": "https://chatgpt.com/c/local-chatgpt%3Atest"})
                entries = [{"id": 1, "url": session["conversation_url"]}, {"id": 2, "url": "https://chatgpt.com/c/new"}]
                if ambiguous:
                    entries.append({"id": 3, "url": "https://chatgpt.com/c/duplicate"})
                probe = argparse.Namespace()
                with (
                    mock.patch.object(runner, "_conversation_history", return_value=entries),
                    mock.patch.object(runner, "_separate_recovery_tab", return_value=contextlib.nullcontext(probe)),
                    mock.patch.object(runner, "_open_url", side_effect=lambda target, cwd, url: setattr(target, "_test_url", url)),
                    mock.patch.object(runner, "_current_url", side_effect=lambda target, cwd: target._test_url),
                    mock.patch.object(runner, "_page_access_state", return_value={"hasPrompt": True}),
                    mock.patch.object(runner, "_eval_json", return_value={"text": "draw a fox", "userCount": 1}),
                ):
                    recovered = runner._recover_original_from_history(args, Path.cwd(), session)
                self.assertEqual(recovered, None if ambiguous else "https://chatgpt.com/c/new")
                saved = json.loads(runner._session_path(job).read_text())
                self.assertEqual(saved["submission_committed"], "unknown")
                self.assertEqual(saved.get("canonical_conversation_url"), None if ambiguous else recovered)


if __name__ == "__main__":
    unittest.main()
