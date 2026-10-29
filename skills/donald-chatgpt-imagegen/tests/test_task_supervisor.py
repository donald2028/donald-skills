from __future__ import annotations

import contextlib
import hashlib
import io
import json
import queue
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import task_supervisor as supervisor
from browser_runtime import process_is_alive


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / 'worker.py'
        self.session = self.root / 'session.json'
        self.limits = SimpleNamespace(stall_timeout=0.3, heartbeat_timeout=1,
                                     task_timeout=5, max_worker_restarts=1)

    def worker(self, body):
        self.script.write_text(textwrap.dedent('''\
            import json, os, sys, time
            from pathlib import Path
            root = Path(__file__).parent
            counter = root / 'counter'
            attempt = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(attempt))
            def save(name, value):
                path = root / name
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(value))
                temporary.replace(path)
            started = time.time()
            def beat(phase='generation_wait', progress=None):
                save('chatgpt_heartbeat.json', {'at': time.time(), 'pid': os.getpid(),
                     'phase': phase, 'last_progress_at': started if progress is None else progress})
        ''') + textwrap.dedent(body))

    def run_worker(self, *, interactive=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(supervisor.time, 'sleep'):
            code = supervisor.supervise(self.script, [], self.root, self.session,
                                        self.limits, interactive=interactive)
        return code, output.getvalue(), supervisor.read_state(self.root / 'chatgpt_supervisor.json')

    def test_fresh_heartbeat_does_not_hide_stalled_progress(self):
        heartbeat = {'at': 200, 'last_progress_at': 100, 'phase': 'generation_wait'}
        self.assertEqual(supervisor.takeover_reason(heartbeat, now=200, started=90, limits=self.limits), 'progress_stalled')
        heartbeat['phase'] = 'waiting_input'
        self.assertIsNone(supervisor.takeover_reason(heartbeat, now=200, started=90, limits=self.limits))
        self.assertEqual(supervisor.takeover_reason(heartbeat, now=202, started=90, limits=self.limits), 'heartbeat_lost')

    def test_loading_error_requests_takeover_even_when_heartbeat_and_progress_are_fresh(self):
        heartbeat = {'at': 200, 'last_progress_at': 200, 'phase_started_at': 100,
                     'phase': 'conversation_loading', 'consecutive_page_failures': 0,
                     'page': {'crashReason': 'chatgpt_transient_page_error'}}
        self.assertEqual(supervisor.takeover_reason(heartbeat, now=200, started=90, limits=self.limits), 'page_error')
        heartbeat['page']['humanReason'] = 'anti_automation_verification'
        heartbeat['phase'] = 'waiting_for_human'
        self.assertIsNone(supervisor.takeover_reason(heartbeat, now=200, started=90, limits=self.limits))

    def test_stalled_worker_is_reaped_and_unknown_submission_survives_restart(self):
        self.worker('''\
            if attempt == 1:
                save('session.json', {'submission_committed': 'unknown', 'prompt': 'original'})
                (root / 'send-count').write_text('1')
                while True:
                    beat()
                    time.sleep(0.03)
            saved = json.loads((root / 'session.json').read_text())
            assert saved['submission_committed'] == 'unknown'
            assert saved['prompt'] == 'original'
            assert '--recover-worker' in sys.argv
            assert '--no-resume' in sys.argv
            print(json.dumps({'status': 'downloaded', 'image_count': 1}))
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'downloaded')
        self.assertEqual(state['restarts'], 1)
        self.assertEqual((self.root / 'send-count').read_text(), '1')
        restart = next(action for action in state['actions'] if action['action'] == 'restart_worker')
        self.assertEqual(restart['reason'], 'progress_stalled')

    def test_repeated_process_crash_exhausts_restart_budget(self):
        self.worker('sys.exit(3)\n')
        code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(state['restarts'], 1)
        self.assertEqual((self.root / 'counter').read_text(), '2')
        self.assertEqual(json.loads(output)['error_type'], 'worker_restart_budget_exhausted')

    def test_original_request_error_is_retried_without_human_handoff(self):
        self.worker('''\
            if attempt == 1:
                print(json.dumps({'status': 'needs_ops', 'error_type': 'original_conversation_required'}))
                sys.exit(2)
            print(json.dumps({'status': 'downloaded', 'image_count': 1}))
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(state['restarts'], 1)

    def test_budget_exhaustion_has_a_bounded_executable_continuation_and_collection_path(self):
        self.limits.max_worker_restarts = 0
        self.worker('sys.exit(2)\n')
        for round_number in range(3):
            self.limits.resume_recovery = round_number > 0
            code, output, state = self.run_worker()
            result = json.loads(output)
            self.assertEqual(result['continuation']['round'], round_number)
            self.assertFalse(result['requires_user_action'])
            self.assertFalse(result['business_complete'])
            self.assertIn('--resume-recovery', result['continuation']['argv'])
            self.assertIn('--reconcile-only', result['continuation']['reconcile_argv'])
            self.assertEqual(result['status'], 'recovery_pending' if round_number < 2 else 'recovery_blocked')
        count = (self.root / 'counter').read_text()
        self.run_worker()
        self.assertEqual((self.root / 'counter').read_text(), count)
        # An exhausted retry cap still permits collecting a late original result, without Send.
        self.limits.reconcile_only = True
        self.worker('''\
            assert '--recover-worker' in sys.argv
            print(json.dumps({'status': 'downloaded', 'image_count': 1}))
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'downloaded')

    def test_normal_reinvocation_cannot_reset_an_exhausted_round(self):
        self.limits.max_worker_restarts = 0
        self.worker('sys.exit(2)\n')
        self.run_worker()
        self.run_worker()
        self.assertEqual((self.root / 'counter').read_text(), '1')

    def test_changed_manifest_cannot_resume_the_original_checkpoint(self):
        self.limits.manifest = self.root / 'job.json'
        self.limits.manifest.write_text('{"prompt":"original"}')
        self.limits.max_worker_restarts = 0
        self.worker('sys.exit(2)\n')
        self.run_worker()
        self.limits.manifest.write_text('{"prompt":"different"}')
        self.limits.resume_recovery = True
        with self.assertRaisesRegex(ValueError, 'Recovery inputs changed'):
            self.run_worker()
        self.assertEqual((self.root / 'counter').read_text(), '1')

    def test_checkpoint_argv_reenters_supervision_in_a_new_process_without_resending(self):
        scripts = str(Path(supervisor.__file__).parent)
        self.script.write_text(textwrap.dedent(f'''\
            import argparse, json, sys
            from pathlib import Path
            sys.path.insert(0, {scripts!r})
            from task_supervisor import add_supervisor_arguments, supervise
            root = Path(__file__).parent
            parser = argparse.ArgumentParser()
            add_supervisor_arguments(parser)
            args = parser.parse_args()
            if not args.worker:
                raise SystemExit(supervise(Path(__file__), sys.argv[1:], root, root / 'session.json', args))
            saved = root / 'session.json'
            if not saved.exists():
                saved.write_text(json.dumps({{'submission_committed':'unknown','prompt':'original'}}))
                (root / 'send-count').write_text('1')
                print(json.dumps({{'status':'needs_ops','error_type':'original_conversation_required'}}))
                raise SystemExit(2)
            assert json.loads(saved.read_text())['prompt'] == 'original'
            assert '--recover-worker' in sys.argv
            print(json.dumps({{'status':'downloaded','image_count':1}}))
        '''))
        first = subprocess.run([sys.executable, str(self.script), '--task-timeout', '2',
                                '--max-worker-restarts', '0'], capture_output=True, text=True, timeout=5)
        result = json.loads(first.stdout)
        self.assertEqual(result['status'], 'recovery_pending')
        receipt_path = self.root / 'chatgpt_recovery.json'
        receipt = supervisor.read_state(receipt_path)
        receipt['not_before'] = 0  # The test's service/cooldown has now recovered.
        receipt_path.write_text(json.dumps(receipt))
        resumed = subprocess.run(result['continuation']['argv'], capture_output=True, text=True, timeout=5)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)['status'], 'downloaded')
        self.assertEqual((self.root / 'send-count').read_text(), '1')
        self.assertEqual(supervisor.read_state(receipt_path)['round'], 1)

    def test_transient_failure_recovers_without_an_operator_even_with_a_manual_recommendation(self):
        self.worker('''\
            if attempt == 1:
                print(json.dumps({'status': 'needs_ops', 'error_type': 'network_error',
                                  'recommended_next_action': 'inspect_session_before_manual_recovery'}))
                sys.exit(2)
            print(json.dumps({'status': 'downloaded', 'image_count': 1}))
        ''')
        with mock.patch('builtins.input', side_effect=AssertionError('Recovery must not ask for approval')), \
                mock.patch.object(sys, 'stdin', None):
            code, output, state = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'downloaded')
        self.assertEqual(state['restarts'], 1)

    def test_invalid_success_output_cannot_finish_the_task(self):
        for value in (None, [], {}, {'status': 'needs_ops'}):
            with self.subTest(value=value):
                (self.root / 'chatgpt_recovery.json').unlink(missing_ok=True)
                self.worker('print(' + repr(json.dumps(value)) + ')\n')
                code, output, state = self.run_worker()
                self.assertEqual(code, 2)
                self.assertEqual(state['status'], 'recovery_pending')
                self.assertEqual(json.loads(output)['error_type'], 'worker_restart_budget_exhausted')

    def test_unknown_supervision_error_stops_the_worker_and_returns_a_saved_result(self):
        self.worker('''\
            while True:
                beat()
                time.sleep(0.03)
        ''')
        with mock.patch.object(supervisor, 'takeover_reason', side_effect=LookupError('unexpected browser status')):
            code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)['error_type'], 'unexpected_supervisor_error')
        self.assertEqual(state['status'], 'recovery_pending')
        self.assertFalse(process_is_alive(state['worker_pid']))

    def test_blocked_caller_stdout_cannot_freeze_supervision_or_shutdown(self):
        self.limits.max_worker_restarts = 0
        self.worker('''\
            save('session.json', {'status': 'awaiting_reply', 'pending': {'submission_committed': 'unknown'}})
            print(json.dumps({'payload': 'x' * 1000000}), flush=True)
            while True:
                beat()
                time.sleep(0.03)
        ''')
        harness = self.root / 'supervisor_main.py'
        harness.write_text(textwrap.dedent(f'''\
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            sys.path.insert(0, {str(Path(supervisor.__file__).parent)!r})
            from task_supervisor import supervise
            root = Path({str(self.root)!r})
            limits = SimpleNamespace(stall_timeout=0.3, heartbeat_timeout=1, task_timeout=3, max_worker_restarts=0)
            raise SystemExit(supervise(root / 'worker.py', [], root, root / 'session.json', limits, interactive=True))
        '''))
        child = subprocess.Popen([sys.executable, '-u', str(harness)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            # Intentionally do not read stdout: its pipe cannot hold the response.
            self.assertEqual(child.wait(timeout=5), 2)
            state = supervisor.read_state(self.root / 'chatgpt_supervisor.json')
            self.assertEqual(state['reason'], 'progress_stalled')
            self.assertEqual(state['result']['status'], 'recovery_pending')
            self.assertEqual(state['result']['error_type'], 'progress_stalled')
            self.assertFalse(process_is_alive(state['worker_pid']))
            log = Path(state['log_dir']) / 'worker-0.stdout.log'
            self.assertGreater(log.stat().st_size, 1000000)
        finally:
            supervisor.stop_worker(child)
            child.communicate(timeout=2)

    def test_real_verification_handoff_does_not_restart_or_reset_its_wait_budget(self):
        self.worker('''\
            print(json.dumps({'status': 'needs_ops', 'error_type': 'human_attention_required'}))
            sys.exit(2)
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(state['restarts'], 0)
        self.assertEqual(json.loads(output)['error_type'], 'human_attention_required')

    def test_deadline_during_visible_verification_does_not_queue_automatic_continuation(self):
        self.limits.task_timeout = 0.4
        self.worker('''\
            while True:
                save('chatgpt_heartbeat.json', {'at': time.time(), 'pid': os.getpid(),
                    'phase': 'waiting_for_human', 'page': {'humanReason': 'anti_automation_verification'}})
                time.sleep(0.03)
        ''')
        code, output, state = self.run_worker()
        result = json.loads(output)
        self.assertEqual(result['status'], 'needs_ops')
        self.assertTrue(result['requires_user_action'])
        self.assertNotIn('continuation', result)
        self.assertEqual(state['restarts'], 0)

    def test_explicit_handoff_stops_even_without_a_known_error_code(self):
        self.worker('''\
            print(json.dumps({'status': 'needs_ops', 'reason': 'review requested', 'handoff_requested': True}))
            sys.exit(2)
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(state['restarts'], 0)

    def test_total_deadline_stops_even_a_worker_that_keeps_reporting_progress(self):
        self.limits.task_timeout = 0.4
        self.worker('''\
            while True:
                beat(progress=time.time())
                time.sleep(0.03)
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(state['restarts'], 0)
        self.assertEqual(json.loads(output)['error_type'], 'task_deadline_exceeded')

    def test_deadline_after_png_commit_reconciles_success_even_when_worker_stdout_is_empty(self):
        self.limits.manifest = self.root / 'job.json'
        self.limits.manifest.write_text(json.dumps({'download_dir': str(self.root), 'variant_count': 1,
                                                  'chatgpt_batch_message': 'original'}))
        output_path = self.root / 'generated.png'
        Image.new('RGB', (300, 420), 'coral').save(output_path)
        digest = hashlib.sha256(b'original').hexdigest()
        self.worker(f'''\
            save('session.json', {{'status':'downloaded','prompt_sha256':{digest!r},
                'expected_image_count':1,'outputs':[{str(output_path)!r}]}})
            while True:
                beat('download', time.time())
                time.sleep(0.03)
        ''')
        self.limits.task_timeout = 0.4
        code, output, state = self.run_worker()
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(result['completion_source'], 'verified_saved_artifacts')
        self.assertEqual(result['image_count'], 1)
        self.assertEqual(state['restarts'], 0)
        self.assertFalse(process_is_alive(state['worker_pid']))
        action = next(a for a in state['actions'] if a['action'] == 'artifacts_reconciled')
        self.assertEqual(action['preceding_reason'], 'task_deadline_exceeded')

    def test_downloaded_label_does_not_complete_corrupt_partial_or_different_prompt_artifacts(self):
        manifest = self.root / 'job.json'
        manifest.write_text(json.dumps({'download_dir': str(self.root), 'variant_count': 1,
                                       'chatgpt_batch_message': 'original'}))
        image = self.root / 'output.png'
        state = {'status': 'downloaded', 'expected_image_count': 1, 'outputs': [str(image)],
                 'prompt_sha256': hashlib.sha256(b'original').hexdigest()}
        image.write_bytes(b'\x89PNG\r\n\x1a\ntruncated')
        self.session.write_text(json.dumps(state))
        self.assertIsNone(supervisor.reconcile_saved_images(self.root, self.session, manifest))
        Image.new('RGB', (300, 420), 'coral').save(image)
        for patch in ({'outputs': []}, {'prompt_sha256': 'different'}, {'expected_image_count': 2},
                      {'images': [{'path': str(image), 'sha256': 'wrong'}]}):
            with self.subTest(patch=patch):
                self.session.write_text(json.dumps({**state, **patch}))
                self.assertIsNone(supervisor.reconcile_saved_images(self.root, self.session, manifest))

    def test_deadline_during_backoff_cannot_launch_another_worker(self):
        self.limits.task_timeout = 0.4
        self.worker('sys.exit(3)\n')
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = supervisor.supervise(self.script, [], self.root, self.session, self.limits)
        self.assertEqual(code, 2)
        self.assertEqual((self.root / 'counter').read_text(), '1')
        self.assertEqual(json.loads(output.getvalue())['error_type'], 'task_deadline_exceeded')

    def test_interactive_restart_keeps_protocol_and_does_not_replay_send(self):
        self.worker('''\
            beat('waiting_input')
            if attempt == 1:
                request = json.loads(input())
                assert request['op'] == 'send'
                save('session.json', {'pending': {'submission_committed': 'unknown'}})
                (root / 'send-count').write_text('1')
                sys.exit(3)
            assert json.loads((root / 'session.json').read_text())['pending']['submission_committed'] == 'unknown'
            print(json.dumps({'ready': 'resumed'}), flush=True)
            request = json.loads(input())
            assert request['op'] == 'finish'
            save('session.json', {'status': 'complete'})
            print(json.dumps({'ok': True, 'result': {'status': 'complete'}}), flush=True)
        ''')
        code, output = self.run_interactive_worker('{"op":"send"}\n')
        self.assertEqual(code, 0)
        self.assertIn('worker_restarting', output)
        self.assertIn('"status": "complete"', output)
        self.assertEqual((self.root / 'send-count').read_text(), '1')

    def test_blocked_stdin_and_live_idle_heartbeat_cannot_block_takeover(self):
        self.worker('''\
            if attempt == 1:
                while True:
                    beat('waiting_input')
                    time.sleep(0.03)
            print(json.dumps({'ready': 'resumed'}), flush=True)
            assert json.loads(input())['op'] == 'finish'
            save('session.json', {'status': 'complete'})
        ''')
        # Larger than a pipe buffer: a synchronous write would freeze the supervisor.
        code, output = self.run_interactive_worker(json.dumps({'op': 'fill', 'text': 'x' * 1000000}) + '\n')
        self.assertEqual(code, 0)
        state = supervisor.read_state(self.root / 'chatgpt_supervisor.json')
        restart = next(action for action in state['actions'] if action['action'] == 'restart_worker')
        self.assertEqual(restart['reason'], 'progress_stalled')

    def test_pending_reply_cannot_hide_behind_an_idle_heartbeat_after_command_ack(self):
        self.worker('''\
            if attempt == 1:
                assert json.loads(input())['op'] == 'send'
                save('session.json', {'status': 'awaiting_reply', 'pending': {'submission_committed': 'unknown'}})
                print(json.dumps({'ok': True, 'result': {'clicked': True}}), flush=True)
                while True:
                    beat('waiting_input')
                    time.sleep(0.03)
            print(json.dumps({'ready': 'resumed'}), flush=True)
            assert json.loads(input())['op'] == 'finish'
            save('session.json', {'status': 'complete'})
        ''')
        code, output = self.run_interactive_worker('{"op":"send"}\n')
        self.assertEqual(code, 0)
        self.assertIn('worker_restarting', output)
        state = supervisor.read_state(self.root / 'chatgpt_supervisor.json')
        restart = next(action for action in state['actions'] if action['action'] == 'restart_worker')
        self.assertEqual(restart['reason'], 'progress_stalled')

    def test_batch_job_cannot_claim_interactive_idle_time(self):
        self.limits.max_worker_restarts = 0
        self.worker('''\
            while True:
                beat('waiting_input')
                time.sleep(0.03)
        ''')
        code, output, state = self.run_worker()
        self.assertEqual(code, 2)
        self.assertEqual(state['reason'], 'progress_stalled')

    def test_unexpected_json_lines_do_not_crash_the_interactive_supervisor(self):
        self.worker('''\
            assert json.loads(input())['op'] == 'inspect'
            for value in (None, [], 'unexpected', 42):
                print(json.dumps(value), flush=True)
            print(json.dumps({'ready': 'resumed'}), flush=True)
            assert json.loads(input())['op'] == 'finish'
            save('session.json', {'status': 'complete'})
        ''')
        code, output = self.run_interactive_worker('{"op":"inspect"}\n')
        self.assertEqual(code, 0)
        self.assertNotIn('unexpected_supervisor_error', output)

    def run_interactive_worker(self, first_line):
        lines = queue.Queue()
        lines.put(first_line)
        class Input:
            def __iter__(self):
                while (line := lines.get()) is not None:
                    yield line
        class Output(io.StringIO):
            sent_finish = False
            def write(self, value):
                result = super().write(value)
                if '"ready": "resumed"' in value and not self.sent_finish:
                    self.sent_finish = True
                    lines.put('{"op":"finish"}\n')
                return result
        output = Output()
        try:
            with mock.patch.object(sys, 'stdin', Input()), contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()), mock.patch.object(supervisor.time, 'sleep'):
                code = supervisor.supervise(self.script, [], self.root, self.session, self.limits, interactive=True)
        finally:
            lines.put(None)
        return code, output.getvalue()


if __name__ == '__main__':
    unittest.main()
