from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agent_browser_runner as runner
from run_health import RunHeartbeat, SubmissionCircuit, heartbeat_status


class Clock:
    now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RunResilienceTests(unittest.TestCase):
    def test_event_flood_cannot_extend_cdp_deadline(self):
        conn = runner._CDPConnection(mock.Mock(), timeout=1)
        conn._buffer = runner._encode_ws_text_frame(b'{"method":"Page.event"}') * 100
        ticks = itertools.count(0, 0.1)
        with mock.patch.object(runner.time, 'monotonic', side_effect=lambda: next(ticks)):
            with self.assertRaises(TimeoutError):
                conn.call('Runtime.evaluate')
        conn._sock.recv.assert_not_called()

    def test_partial_frames_cannot_extend_cdp_deadline(self):
        sock = mock.Mock()
        sock.recv.return_value = b'x'
        conn = runner._CDPConnection(sock, timeout=1)
        conn._buffer = b'\x81\x7f' + (100000).to_bytes(8, 'big')
        ticks = itertools.count(0, 0.1)
        with mock.patch.object(runner.time, 'monotonic', side_effect=lambda: next(ticks)):
            with self.assertRaises(TimeoutError):
                conn.call('Runtime.evaluate')
        self.assertLess(sock.recv.call_count, 12)

    def test_independent_heartbeat_survives_busy_worker_and_requests_recovery_on_failed_reads(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            samples = threading.Event()
            fail_reads = threading.Event()
            recovery_ready = threading.Event()
            count = 0
            failed_reads = 0

            def probe():
                nonlocal count, failed_reads
                if fail_reads.is_set():
                    failed_reads += 1
                    if failed_reads >= 4:
                        recovery_ready.set()
                    raise TimeoutError('page read blocked')
                count += 1
                if count >= 3:
                    samples.set()
                return {'hasPrompt': True}

            health = RunHeartbeat(root, probe, interval=0.02)
            health.start()
            try:
                self.assertTrue(samples.wait(2))  # Main/worker does no heartbeat work here.
                first = json.loads((root / 'chatgpt_heartbeat.json').read_text())
                self.assertEqual(first['phase'], 'browser_startup')
                self.assertGreater(count, 1)
                progress = first['last_progress_at']
                health.sample()
                self.assertEqual(health.progress_at, progress)
                fail_reads.set()
                self.assertTrue(recovery_ready.wait(2))
                with self.assertRaises(runner.RecoverablePageError):
                    runner._check_run_health(argparse.Namespace(_health=health))
            finally:
                health.close('downloaded')
            self.assertEqual(heartbeat_status(root)['status'], 'downloaded')

    def test_two_distinct_failures_pause_new_jobs_and_only_one_probe_is_admitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'queue.json'
            gate = SubmissionCircuit(path)
            gate.failure('job-a', 100)
            gate.failure('job-a', 101)
            self.assertEqual(gate.read()['state'], 'running')
            gate.failure('job-b', 102)
            self.assertEqual(gate.admit('new', 200, allow_probe=True), 'paused')
            results = []
            barrier = threading.Barrier(2)

            def admit(owner):
                args = argparse.Namespace(_queue_path=path, _queue_owner=owner)
                barrier.wait()
                try:
                    results.append(runner._queue_admit(args, allow_probe=True))
                except runner.QueuePausedError:
                    results.append('paused')

            threads = [threading.Thread(target=admit, args=(str(i),)) for i in range(2)]
            with mock.patch.object(runner.time, 'time', return_value=403):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertCountEqual(results, ['probe', 'paused'])
            owner = gate.read()['probe']['owner']
            gate.probe_finished(owner, 404, False)
            self.assertEqual(gate.admit('next', 500, allow_probe=True), 'paused')
            self.assertEqual(gate.admit('next', 705, allow_probe=True), 'probe')
            gate.probe_finished('next', 706, True)
            self.assertEqual(gate.admit('new', 707, allow_probe=False), 'allow')
            gate.failure('job-c', 800)
            gate.failure('job-d', 801)
            self.assertEqual(gate.admit('probe', 1102, allow_probe=True), 'probe')
            gate.verification('collecting-job', 1103)
            gate.probe_finished('probe', 1104, True)
            self.assertEqual(gate.read()['reason'], 'human_verification')
            self.assertEqual(gate.admit('new', 1105, allow_probe=False), 'paused')

    def test_runner_stops_third_job_before_browser_startup_after_two_exhausted_jobs(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            for index in range(3):
                folder = root / str(index)
                folder.mkdir()
                job = {'job_name': str(index), 'prompt_card': 'prompt.md', 'download_dir': str(folder)}
                manifest = folder / 'job.json'
                manifest.write_text(json.dumps(job))
                failure = runner.PageRecoveryExhaustedError({
                    'status': 'generation_failed', 'error_type': 'chatgpt_page_recovery_exhausted',
                    'submission_committed': False, 'phase': 'pre_submit_prepare',
                })

                def profile(args):
                    args.user_data_dir = str(root / 'profile')
                    args.profile = 'Default'

                output = io.StringIO()
                with (
                    mock.patch.object(sys, 'argv', ['runner', str(manifest), '--mode', 'single-batch-submit']),
                    mock.patch.object(runner, '_resolve_browser_profile', side_effect=profile),
                    mock.patch.object(runner, 'resolve_tool_state_root', return_value=root / 'state'),
                    mock.patch.object(runner, '_validate_manifest_reference_mapping'),
                    mock.patch.object(runner, '_prepare_browser_lane') as browser,
                    mock.patch.object(runner, '_validate_profile'),
                    mock.patch.object(runner, '_heartbeat_probe', return_value={'hasPrompt': True}),
                    mock.patch.object(runner, 'run_submit', side_effect=failure) as submit,
                    mock.patch.object(runner, '_cleanup_agent_browser'),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(runner.main(), 2)
                report = json.loads(output.getvalue())
                if index == 2:
                    self.assertEqual(report['status'], 'queue_paused')
                    browser.assert_not_called()
                    submit.assert_not_called()
                else:
                    browser.assert_called_once()
                    submit.assert_called_once()

    def test_old_failures_expire_and_existing_download_cannot_clear_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate = SubmissionCircuit(Path(temporary) / 'queue.json')
            gate.failure('old', 0)
            gate.failure('new', 601)
            self.assertEqual(gate.read()['state'], 'running')
            gate.failure('second', 602)
            gate.submitted()
            self.assertEqual(gate.read()['state'], 'paused')
            gate.verification('human-job', 603)
            self.assertEqual(gate.admit('other', 9999, allow_probe=True), 'paused')
            gate.verified('wrong-job')
            self.assertEqual(gate.read()['state'], 'paused')
            gate.verified('human-job')
            self.assertEqual(gate.read()['state'], 'paused')
            self.assertEqual(gate.read()['reason'], 'consecutive_page_failures')

    def test_health_check_cannot_clear_an_active_challenge_or_service_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'health', 'prompt_card': 'prompt.md', 'download_dir': temporary}
            args = argparse.Namespace(_queue_path=Path(temporary) / 'queue.json', _job_id='old-job')
            gate = SubmissionCircuit(args._queue_path)
            gate.failure('a', 0)
            gate.failure('b', 1)
            gate.verification('old-job', 2)
            with (
                mock.patch.object(runner, '_open_new_chat'),
                mock.patch.object(runner, '_page_access_state', return_value={'humanReason': 'anti_automation_verification'}),
            ):
                result = runner.run_health_check(args, job, Path.cwd())
            self.assertEqual(result['status'], 'needs_ops')
            self.assertEqual(gate.read()['reason'], 'human_verification')
            with (
                mock.patch.object(runner, '_open_new_chat'),
                mock.patch.object(runner, '_page_access_state', return_value={'hasPrompt': True}),
                mock.patch.object(runner.time, 'sleep'),
            ):
                result = runner.run_health_check(args, job, Path.cwd())
            self.assertEqual(result['status'], 'queue_paused')
            self.assertEqual(gate.read()['reason'], 'consecutive_page_failures')

    def test_health_check_cli_does_not_resume_or_submit_an_unknown_request(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            job = {'job_name': 'health', 'prompt_card': 'prompt.md', 'download_dir': temporary}
            manifest = root / 'job.json'
            manifest.write_text(json.dumps(job))
            runner._write_session_patch(job, {'submission_committed': 'unknown',
                'owned_target_id': 'lost', 'reason': 'anti_automation_verification'})
            before = runner._session_path(job).read_bytes()
            def profile(args):
                args.user_data_dir, args.profile = str(root / 'profile'), 'Default'
            lane = runner.hashlib.sha256(f"{(root / 'profile').resolve()}:Default".encode()).hexdigest()[:20]
            gate = SubmissionCircuit(root / 'state' / f'queue-{lane}.json')
            gate.verification(runner.hashlib.sha256(str(runner._session_path(job).resolve()).encode()).hexdigest(), 0)
            with (
                mock.patch.object(sys, 'argv', ['runner', str(manifest), '--mode', 'health-check']),
                mock.patch.object(runner, '_resolve_browser_profile', side_effect=profile),
                mock.patch.object(runner, 'resolve_tool_state_root', return_value=root / 'state'),
                mock.patch.object(runner, '_prepare_browser_lane'),
                mock.patch.object(runner, '_validate_profile'),
                mock.patch.object(runner, '_heartbeat_probe', return_value={'hasPrompt': True}),
                mock.patch.object(runner, '_open_new_chat'),
                mock.patch.object(runner, '_page_access_state', return_value={'hasPrompt': True}),
                mock.patch.object(runner.time, 'sleep'),
                mock.patch.object(runner, '_activate_for_human_attention') as resume,
                mock.patch.object(runner, '_submit_prompt') as submit,
                mock.patch.object(runner, '_cleanup_agent_browser'),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(runner._session_path(job).read_bytes(), before)
            self.assertEqual(gate.read()['state'], 'running')
            resume.assert_not_called()
            submit.assert_not_called()

    def test_verification_timeout_before_dispatch_preserves_unsubmitted_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'pre-send', 'prompt_card': 'prompt.md', 'download_dir': temporary}
            args = argparse.Namespace(_health_job=job, human_wait_timeout=0)
            with (
                mock.patch.object(runner, '_page_access_state', return_value={'humanReason': 'anti_automation_verification'}),
                mock.patch.object(runner, 'activate_browser', return_value={'status': 'active'}),
                mock.patch.object(runner, 'configured_browser', return_value={}),
                mock.patch.object(runner, '_cdp_launch_port', return_value=1),
                mock.patch.object(runner, '_click_composer_send_button') as click,
                self.assertRaises(runner.HumanAttentionRequired),
            ):
                runner._submit_prompt(args, Path.cwd(), 'draw')
            saved = json.loads((Path(temporary) / 'chatgpt_session.json').read_text())
            self.assertFalse(saved['submission_committed'])
            self.assertFalse(saved['dispatch_started'])
            click.assert_not_called()

    def test_uncertain_send_is_never_repeated_even_if_composer_stays_ready(self):
        clock = Clock()
        with (
            mock.patch.object(runner.time, 'monotonic', side_effect=clock.time),
            mock.patch.object(runner.time, 'sleep', side_effect=clock.sleep),
            mock.patch.object(runner, '_page_access_state', return_value={'hasPrompt': True}),
            mock.patch.object(runner, '_composer_submit_state', return_value={'readyToSubmit': True, 'editorHasPromptTail': True}),
            mock.patch.object(runner, '_click_composer_send_button', return_value={'clicked': True}) as click,
            mock.patch.object(runner, '_press_composer_enter') as enter,
            self.assertRaises(RuntimeError),
        ):
            runner._submit_prompt(argparse.Namespace(), Path.cwd(), 'draw', timeout_s=2)
        click.assert_called_once()
        enter.assert_not_called()

    def test_temporary_url_without_original_target_never_navigates_or_resubmits(self):
        args = argparse.Namespace(_submitted_prompt='draw')
        with (
            mock.patch.object(runner, '_page_access_state', return_value={'hasPrompt': True}),
            mock.patch.object(runner, '_current_url', return_value='https://chatgpt.com/'),
            mock.patch.object(runner, '_open_conversation') as navigate,
            self.assertRaises(runner.OriginalConversationRequired),
        ):
            runner._require_original_conversation(args, Path.cwd(), {
                'submission_committed': True, 'conversation_url': 'https://chatgpt.com/c/local-chatgpt%3Atest',
            })
        navigate.assert_not_called()

    def test_restart_claims_only_its_retained_target_without_reloading(self):
        browser = object.__new__(runner._ResumeImageBrowserSession)
        browser.original_target = 'mine'
        browser.require_target = True
        browser.scope = 'donald-chatgpt-imagegen'
        browser.port = 1
        browser.owned_target_ids = set()
        state = {'active_runs': [], 'retained_targets': [
            {'target_id': 'mine', 'scope': browser.scope}, {'target_id': 'other', 'scope': browser.scope},
        ]}
        with mock.patch.object(runner, 'cdp_target', return_value={'id': 'mine'}), mock.patch.object(browser, '_navigate') as nav:
            self.assertEqual(browser._claim_target(state), 'mine')
        nav.assert_not_called()
        self.assertEqual(state['retained_targets'][0]['target_id'], 'other')

    def test_committed_resume_collects_original_even_when_new_submissions_are_paused(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'resume', 'prompt_card': 'prompt.md', 'download_dir': temporary}
            url = 'https://chatgpt.com/c/original'
            runner._write_session_patch(job, {
                'submission_committed': True, 'canonical_conversation_url': url, 'conversation_url': url,
                'baseline_user_message_count': 0, 'chat_surface': {'verified': True, 'selected_surface': 'chat'},
            })
            with (
                mock.patch.object(runner, '_page_access_state', return_value={'hasPrompt': True}),
                mock.patch.object(runner, '_current_url', return_value=url),
                mock.patch.object(runner, '_validate_session_reference_mapping'),
                mock.patch.object(runner, '_wait_until_idle'),
                mock.patch.object(runner, '_ensure_chat_surface', return_value={'verified': True, 'selected_surface': 'chat'}),
                mock.patch.object(runner, '_validate_account_lane', return_value={'ok': True}),
                mock.patch.object(runner, '_screenshot'),
                mock.patch.object(runner, '_rename_current_conversation', return_value={}),
                mock.patch.object(runner, '_retry_conversation_rename', return_value={}),
                mock.patch.object(runner, '_agent_browser_session_record', return_value={}),
                mock.patch.object(runner, '_collect_generated_images_with_retries', return_value={'status': 'downloaded', 'images': []}) as collect,
                mock.patch.object(runner, '_queue_admit', side_effect=runner.QueuePausedError({'state': 'paused'})) as gate,
                mock.patch.object(runner, '_submit_prompt') as submit,
                mock.patch.object(runner, '_upload_files') as upload,
            ):
                args = argparse.Namespace(profile='Default', max_failure_retries=2)
                result = runner._run_one_request(args, job, Path.cwd(), label='batch', message='draw', resume=True)
            self.assertEqual(result['status'], 'downloaded')
            self.assertTrue(result['resumed'])
            collect.assert_called_once()
            gate.assert_not_called()
            upload.assert_not_called()
            submit.assert_not_called()

    def test_timestamp_suffix_and_aliases_keep_exact_bytes_and_order(self):
        self.assertTrue(runner._attachment_name_matches('/refs/photo.png', 'photo(20261018-080148).png'))
        self.assertFalse(runner._attachment_name_matches('/refs/photo.png', 'other(20261018-080148).png'))
        self.assertFalse(runner._attachment_name_matches('/refs/photo.png', 'photo(unexpected).png'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = []
            for directory, data in [('a', b'first'), ('b', b'second')]:
                folder = root / directory
                folder.mkdir()
                path = folder / 'same.png'
                path.write_bytes(data)
                refs.append(str(path))
            args = argparse.Namespace(_health_job={'download_dir': str(root)})
            effective = []

            def upload(_args, _cwd, paths):
                effective.extend(paths)

            def ready(_args, _cwd, paths, **kwargs):
                return {'attachment_count': len(paths), 'ready': not kwargs.get('require_complete') is False,
                        'attachment_filenames': [Path(path).stem + '(20261018-080148).png' for path in paths]}

            with mock.patch.object(runner, '_upload_files', side_effect=upload), mock.patch.object(runner, '_wait_for_reference_uploads_ready', side_effect=ready):
                result = runner._upload_references_in_order(args, root, refs)
            self.assertNotEqual(Path(effective[0]).name, Path(effective[1]).name)
            for source, alias in zip(refs, effective):
                self.assertEqual(Path(source).read_bytes(), Path(alias).read_bytes())
            runner._require_verified_reference_upload_order(refs, result)
            self.assertTrue(result['order_verified'])
