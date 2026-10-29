"""Bounded process supervision for the ChatGPT workers; never operate the browser here."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from browser_runtime import _file_lock, process_is_alive, cleanup_stopped_browser
from run_health import atomic_json
from chatgpt_page_health import page_failure

DEFAULT_HEARTBEAT_TIMEOUT = 60
DEFAULT_STALL_TIMEOUT = 180
DEFAULT_TASK_TIMEOUT = 300
MAX_CONTINUATION_RUNS = 2


class WorkerStopped(BaseException):
    def __init__(self, preserve_browser: bool):
        self.preserve_browser = preserve_browser


@contextlib.contextmanager
def worker_signals(root: Path):
    previous = {}
    def stop(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        request = read_state(root / 'chatgpt_worker_stop.json')
        # An older supervisor may not write a stop request. Preserve its unresolved browser
        # rather than mistaking a planned takeover for permission to discard the live target.
        preserve = bool(request.get('preserve_browser')) if request.get('pid') == os.getpid() else True
        raise WorkerStopped(preserve)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, *([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else [])):
            previous[sig] = signal.signal(sig, stop)
        try:
            yield
        except WorkerStopped:
            pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextlib.contextmanager
def termination_as_interrupt():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    def stop(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, stop) for sig in
                (signal.SIGTERM, signal.SIGINT, *([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else []))}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class StreamWriter:
    """Keep caller stdout/stderr backpressure off the supervision loop."""
    def __init__(self, stream) -> None:
        self.stream = stream
        self.pending: queue.Queue = queue.Queue()
        try:
            self.fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            self.fd = None
        self.thread = threading.Thread(target=self._run, name='supervisor-output', daemon=True)
        self.thread.start()

    def write(self, value: str) -> None:
        self.pending.put(value)

    def json(self, value: Any) -> None:
        self.write(json.dumps(value, ensure_ascii=False) + '\n')

    def _run(self) -> None:
        try:
            while (value := self.pending.get()) is not None:
                if self.fd is None:
                    self.stream.write(value)
                    self.stream.flush()
                else:
                    # Raw writes avoid holding a TextIO lock if a pipe blocks during shutdown.
                    data = value.encode('utf-8')
                    while data:
                        data = data[os.write(self.fd, data):]
        except (OSError, ValueError):
            return  # Worker logs and the supervisor result remain available on disk.

    def close(self) -> None:
        self.pending.put(None)
        self.thread.join(timeout=1)


def add_supervisor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--stall-timeout', type=float, default=DEFAULT_STALL_TIMEOUT,
                        help='Restart a worker after this many seconds without observable progress.')
    parser.add_argument('--heartbeat-timeout', type=float, default=DEFAULT_HEARTBEAT_TIMEOUT,
                        help='Restart a worker whose heartbeat stops updating for this many seconds.')
    parser.add_argument('--task-timeout', type=float, default=DEFAULT_TASK_TIMEOUT,
                        help='Active-run budget across worker restarts, in seconds.')
    parser.add_argument('--max-worker-restarts', type=int, default=2)
    parser.add_argument('--resume-recovery', action='store_true',
                        help='Continue this task from its saved recovery checkpoint after cooldown.')
    parser.add_argument('--restart-task', action='store_true',
                        help='Request takeover of the live worker, or start a fresh bounded recovery cycle.')
    parser.add_argument('--reconcile-only', action='store_true',
                        help='Inspect/collect the original request without Send or response Retry.')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--recover-worker', action='store_true', help=argparse.SUPPRESS)


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def browser_exited(owner: dict[str, Any], worker_pid: int) -> bool:
    return bool(owner.get('pid') == worker_pid and owner.get('browser_pid')
                and (owner.get('browser_exited_before_cleanup')
                     or (owner.get('status') in {'open', 'starting'}
                         and not process_is_alive(int(owner['browser_pid'])))))


def stop_worker(child: subprocess.Popen, root: Path | None = None, *, preserve_browser: bool = True) -> None:
    # Only the Python worker belongs to this supervisor. Chrome may own unresolved requests
    # or other tasks; never kill its process group or close its targets during takeover.
    if child.poll() is None:
        if root is not None:
            atomic_json(root / 'chatgpt_worker_stop.json', {'pid': child.pid, 'preserve_browser': preserve_browser})
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    if root is not None:
        cleanup = cleanup_stopped_browser(root / 'chatgpt_browser_owner.json', child.pid, preserve=preserve_browser)
        atomic_json(root / 'chatgpt_browser_cleanup.json', cleanup)


def terminal_failure(report: Any) -> bool:
    if isinstance(report, list):
        return any(terminal_failure(item) for item in report)
    if not isinstance(report, dict):
        return False
    if report.get('handoff_requested'):
        return True
    markers = {str(report.get(key, '')) for key in ('status', 'error_type', 'reason')}
    if markers & {
        'policy_refused', 'human_attention_required', 'human_verification_required',
        'login_required', 'foreground_changed',
        'browser_profile_unconfigured', 'pillow_unavailable', 'rebuild_inputs_required',
    }:
        return True
    return any(terminal_failure(value) for value in report.values() if isinstance(value, (dict, list)))


def result_complete(report: dict[str, Any], *, interactive: bool) -> bool:
    if interactive:
        return report.get('status') == 'complete'
    variants = report.get('variants')
    if variants is not None:
        return isinstance(variants, list) and bool(variants) and all(
            isinstance(item, dict) and isinstance(item.get('downloaded'), dict)
            and item['downloaded'].get('status') == 'downloaded' for item in variants)
    return report.get('status') == 'downloaded' or (
        report.get('mode') == 'dry_upload' and not report.get('status') and not report.get('error_type'))


def reconcile_saved_images(root: Path, session_path: Path, manifest: Path | None) -> dict[str, Any] | None:
    """A worker may save its PNGs before stalling in final reporting/cleanup."""
    if not manifest:
        return None
    session, job = read_state(session_path), read_state(manifest)
    prompt = str(job.get('chatgpt_batch_message') or job.get('chatgpt_message') or '')
    if (session.get('status') != 'downloaded' or not prompt
            or session.get('prompt_sha256') != hashlib.sha256(prompt.encode()).hexdigest()
            or Path(job.get('download_dir', '')).resolve() != root.resolve()):
        return None
    try:
        expected = max(1, int(job.get('variant_count') or 1))
        if int(session.get('expected_image_count') or 0) != expected:
            return None
        paths = [Path(value).resolve() for value in session.get('outputs', [])]
        if len(set(paths)) < expected or len(set(paths)) != len(paths) or any(root.resolve() not in path.parents for path in paths):
            return None
        from PIL import Image  # Image jobs already require Pillow; text sessions do not.
        records = {str(Path(item['path']).resolve()): item for item in session.get('images', []) if item.get('path')}
        images = []
        for path in paths:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if records.get(str(path), {}).get('sha256', digest) != digest:
                return None
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'PNG':
                    return None
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                images.append({**records.get(str(path), {}), 'path': str(path), 'sha256': digest,
                               'width': image.width, 'height': image.height})
    except (ImportError, OSError, ValueError, TypeError, KeyError, SyntaxError):
        return None
    return {**session, 'status': 'downloaded', 'image_count': len(images), 'images': images,
            'completion_source': 'verified_saved_artifacts', 'business_complete': True,
            'requires_user_action': False}


def takeover_reason(heartbeat: dict[str, Any], *, now: float, started: float,
                    limits: argparse.Namespace) -> str | None:
    # Ignore a previous worker's heartbeat; its timestamps must not kill a new worker.
    fresh = float(heartbeat.get('at', 0)) >= started
    if now - (float(heartbeat['at']) if fresh else started) >= limits.heartbeat_timeout:
        return 'heartbeat_lost'
    if not fresh:
        return None
    if heartbeat.get('phase') == 'waiting_input':
        return None  # An interactive session waiting for its next instruction is intentionally idle.
    page = heartbeat.get('page') or {}
    if (heartbeat.get('phase') in {'checking_verification', 'waiting_for_human'}
            and (page.get('humanReason') or (page.get('gate') or {}).get('kind') == 'verification')):
        return None  # The worker's verification budget and the total deadline still apply.
    failure = page_failure(page, heartbeat.get('consecutive_page_failures', 0), heartbeat.get('phase', ''))
    if (failure
            and now - float(heartbeat.get('phase_started_at', started)) >= 30):
        return 'page_error' if page.get('crashReason') else 'page_unreadable'
    if now - max(started, float(heartbeat.get('last_progress_at', started))) >= limits.stall_timeout:
        return 'progress_stalled'
    return None


def supervise(script: Path, worker_args: list[str], root: Path, session_path: Path,
              limits: argparse.Namespace, *, interactive: bool = False) -> int:
    if min(limits.stall_timeout, limits.heartbeat_timeout, limits.task_timeout) <= 0 or limits.max_worker_restarts < 0:
        raise ValueError('Supervisor timeouts must be positive and restart count must be nonnegative')
    root.mkdir(parents=True, exist_ok=True)
    manifest_value = getattr(limits, 'manifest', None)
    manifest = Path(manifest_value) if manifest_value else (
        Path(worker_args[0]) if worker_args and not worker_args[0].startswith('-') else None)
    if manifest and manifest.is_file():
        manifest = manifest.resolve()
        worker_args = [str(manifest) if not value.startswith('-')
                       and Path(value).expanduser().resolve() == manifest else value for value in worker_args]
    task_key = hashlib.sha256((script.name + str(session_path.resolve())
        + (hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest and manifest.is_file() else '')).encode()).hexdigest()
    if getattr(limits, 'restart_task', False):
        try:
            with _file_lock(root / 'chatgpt_supervisor.lock', timeout=0):
                pass
        except TimeoutError:
            owner = read_state(root / 'chatgpt_supervisor.json')
            if read_state(root / 'chatgpt_recovery.json').get('task_key') != task_key:
                raise ValueError('Recovery inputs changed; preserve the original manifest and session')
            if owner.get('status') != 'running' or not process_is_alive(int(owner.get('pid') or 0)):
                raise RuntimeError('Supervisor lock is busy without a live owner; inspect the saved run')
            request = {'action': 'restart', 'supervisor_pid': owner['pid'], 'started_at': owner['started_at'],
                       'id': uuid.uuid4().hex, 'rebuild_request': bool(getattr(limits, 'rebuild_request', False))}
            atomic_json(root / 'chatgpt_supervisor_control.json', request)
            print(json.dumps({'status': 'recovery_requested', 'business_complete': False,
                              'supervisor_path': str(root / 'chatgpt_supervisor.json'), 'request': request}))
            return 0
    # Prevent two parents from restarting workers against the same task.
    with termination_as_interrupt(), _file_lock(root / 'chatgpt_supervisor.lock', timeout=0):
        recovery_path = root / 'chatgpt_recovery.json'
        recovery = read_state(recovery_path)
        if recovery:
            if recovery.get('task_key') != task_key:
                raise ValueError('Recovery inputs changed; preserve the original manifest and session')
            restart = getattr(limits, 'restart_task', False)
            if restart:
                recovery.setdefault('takeovers', []).append({'at': time.time(), 'preceding_status': recovery.get('status'),
                    'preceding_round': recovery['round']})
                recovery.update(round=recovery['round'] + 1, cycle_started_round=recovery['round'] + 1, status='running')
                atomic_json(recovery_path, recovery)
                if '--recover-worker' not in worker_args:
                    worker_args = [*worker_args, '--recover-worker']
            if recovery.get('status') == 'interrupted' and not (restart or getattr(limits, 'resume_recovery', False) or getattr(limits, 'resume', None)):
                writer = StreamWriter(sys.stdout)
                writer.json(recovery['result'])
                writer.close()
                return 130
            if recovery.get('status') in {'recovery_pending', 'recovery_blocked'}:
                reconcile = getattr(limits, 'reconcile_only', False)
                if not reconcile and not restart and (not getattr(limits, 'resume_recovery', False)
                        or recovery['round'] - recovery.get('cycle_started_round', 0) >= MAX_CONTINUATION_RUNS):
                    writer = StreamWriter(sys.stdout)
                    writer.json(recovery['result'])
                    writer.close()
                    return 2
                # A new bounded run is explicit, checkpointed, and delayed; repeated normal
                # invocations cannot silently reset either the budget or the continuation cap.
                if not restart:
                    time.sleep(max(0, recovery['not_before'] - time.time()))
                recovery.update(round=recovery['round'] + (0 if reconcile else 1), status='running')
                atomic_json(recovery_path, recovery)
                if '--recover-worker' not in worker_args:
                    worker_args = [*worker_args, '--recover-worker']
        else:
            if getattr(limits, 'resume_recovery', False):
                raise ValueError('There is no recovery checkpoint for this task')
            recovery = {'task_key': task_key, 'round': 0, 'status': 'running'}
            atomic_json(recovery_path, recovery)
        return _supervise(script, worker_args, root, session_path, limits,
                          interactive=interactive, recovery=recovery)


def _supervise(script: Path, worker_args: list[str], root: Path, session_path: Path,
               limits: argparse.Namespace, *, interactive: bool, recovery: dict[str, Any]) -> int:
    events: queue.Queue = queue.Queue()
    if interactive:
        def input_lines() -> None:
            for line in sys.stdin:
                events.put(('input', line))
            events.put(('eof', ''))
        threading.Thread(target=input_lines, daemon=True).start()
    log_root = root / 'supervisor' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    log_root.mkdir(parents=True)
    state_path = root / 'chatgpt_supervisor.json'
    state: dict[str, Any] = {'status': 'running', 'restarts': 0, 'actions': [],
                             'pid': os.getpid(), 'log_dir': str(log_root),
                             'session_path': str(session_path), 'started_at': time.time()}
    state['retain_live_target'] = read_state(state_path).get('retain_live_target', False)
    active_started = time.monotonic()
    child = None
    last_report: dict[str, Any] = {}
    eof = False
    eof_at = None
    output = StreamWriter(sys.stdout)
    diagnostics = StreamWriter(sys.stderr)

    def record(action: str, **details: Any) -> None:
        state['actions'].append({'at': time.time(), 'action': action, **details})
        atomic_json(state_path, state)
        diagnostics.json({'event': 'task_supervisor', **state['actions'][-1]})

    def interrupted(reason: str) -> int:
        result = {'status': 'interrupted', 'reason': reason, 'business_complete': False,
                  'automatic_resume_allowed': False, 'session_path': str(session_path),
                  'supervisor_path': str(state_path), 'recommended_next_action': 'resume_only_on_explicit_request'}
        state.update(status='interrupted', result=result)
        recovery.update(status='interrupted', result=result)
        atomic_json(root / 'chatgpt_recovery.json', recovery)
        record('supervisor_interrupted', reason=reason)
        output.json(result)
        return 130

    def exhausted(reason: str, report: dict[str, Any]) -> int:
        manifest_value = getattr(limits, 'manifest', None)
        completed = None if interactive else reconcile_saved_images(
            root, session_path, Path(manifest_value) if manifest_value else None)
        if completed:
            completed.update(supervisor_path=str(state_path), worker_restarts=state['restarts'])
            state.update(status='complete', result=completed)
            recovery.update(status='complete', result=completed)
            atomic_json(root / 'chatgpt_recovery.json', recovery)
            record('artifacts_reconciled', preceding_reason=reason)
            output.json(completed)
            return 0
        page = read_state(root / 'chatgpt_heartbeat.json').get('page') or {}
        if terminal_failure(report) or page.get('humanReason') or (page.get('gate') or {}).get('kind') == 'verification':
            result = {'status': 'needs_ops', 'error_type': 'human_attention_required' if page.get('humanReason')
                      or (page.get('gate') or {}).get('kind') == 'verification' else reason,
                      'last_worker_report': report, 'session_path': str(session_path),
                      'supervisor_path': str(state_path), 'requires_user_action': True, 'business_complete': False}
            recovery.update(status='needs_ops', result=result)
            atomic_json(root / 'chatgpt_recovery.json', recovery)
            state.update(status='needs_ops', reason=reason, result=result)
            record('supervision_handoff')
            output.json(result)
            return 2
        cycle_round = recovery['round'] - recovery.get('cycle_started_round', 0)
        pending = cycle_round < MAX_CONTINUATION_RUNS
        command_args = [value for value in worker_args if value not in {
            '--resume-recovery', '--recover-worker', '--reconcile-only', '--restart-task', '--rebuild-request'}]
        reconcile_args = list(command_args)
        if not interactive:
            if '--mode' in reconcile_args:
                reconcile_args[reconcile_args.index('--mode') + 1] = 'collect-current'
            else:
                reconcile_args.extend(['--mode', 'collect-current'])
        result = {'status': 'recovery_pending' if pending else 'recovery_blocked', 'error_type': reason,
                  'session_path': str(session_path), 'supervisor_path': str(state_path),
                  'worker_restarts': state['restarts'], 'last_worker_report': report,
                  'requires_user_action': False, 'business_complete': False,
                  'recovery_path': str(root / 'chatgpt_recovery.json'),
                  'recommended_next_action': 'resume_recovery_after_cooldown' if pending else 'upstream_takeover',
                  'continuation': {'round': recovery['round'], 'remaining_rounds': max(0, MAX_CONTINUATION_RUNS - cycle_round),
                     'not_before': time.time() + 30,
                     'argv': [sys.executable, str(script), *command_args, '--resume-recovery'],
                     'reconcile_argv': [sys.executable, str(script), *reconcile_args, '--reconcile-only'],
                     'takeover_argv': [sys.executable, str(script), *command_args, '--restart-task']}}
        if not interactive:
            result['continuation']['rebuild_argv'] = [sys.executable, str(script), *command_args, '--restart-task', '--rebuild-request']
        recovery.update(status=result['status'], result=result, not_before=result['continuation']['not_before'])
        atomic_json(root / 'chatgpt_recovery.json', recovery)
        state.update(status=result['status'], reason=reason, result=result)
        record('supervision_exhausted', exit_code=child.returncode if child else None)
        output.json(result)
        return 2

    def forward_output(stream, log, attempt: int) -> None:
        try:
            for line in stream:
                log.write(line)
                log.flush()
                events.put(('output', (attempt, line)))
        finally:
            stream.close()
            log.close()

    def forward_input(stream, pending: queue.Queue, attempt: int) -> None:
        try:
            while (line := pending.get()) is not None:
                stream.write(line)
                stream.flush()
        except (OSError, ValueError):
            events.put(('write_failed', attempt))
        finally:
            try:
                stream.close()
            except OSError:
                pass

    try:
        while True:
            if time.monotonic() - active_started >= limits.task_timeout:
                return exhausted('task_deadline_exceeded', last_report)
            attempt = state['restarts']
            output_path = log_root / f'worker-{attempt}.stdout.log'
            error_path = log_root / f'worker-{attempt}.stderr.log'
            command_args = list(worker_args)
            if attempt and '--recover-worker' not in command_args:
                command_args.append('--recover-worker')
            if attempt and not interactive and '--no-resume' not in command_args:
                # Proven/unknown submissions remain guarded in _run_one_request. Unsubmitted
                # preparation is replayed from scratch instead of collecting an empty draft.
                command_args.append('--no-resume')
            launched = time.time()
            with output_path.open('w', encoding='utf-8') as worker_output, error_path.open('w', encoding='utf-8') as errors:
                child = subprocess.Popen([sys.executable, '-u', str(script), *command_args, '--worker'],
                    stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                    stdout=subprocess.PIPE if interactive else worker_output, stderr=errors,
                    text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
                if interactive:
                    thread_log = output_path.open('a', encoding='utf-8')
                    output_thread = threading.Thread(target=forward_output, args=(child.stdout, thread_log, attempt), daemon=True)
                    output_thread.start()
                    worker_input: queue.Queue = queue.Queue()
                    input_thread = threading.Thread(target=forward_input, args=(child.stdin, worker_input, attempt), daemon=True)
                    input_thread.start()
                state.update(status='running', worker_pid=child.pid)
                record('worker_started', pid=child.pid, attempt=attempt)
                reason = None
                heartbeat = {}
                pending_commands: list[float] = []
                reply_ready = False
                next_report = time.monotonic() + 20
                while child.poll() is None:
                    try:
                        event, value = events.get(timeout=0.2)
                    except queue.Empty:
                        event, value = '', ''
                    if event == 'output':
                        if value[0] == attempt:
                            output.write(value[1])
                            try:
                                response = json.loads(value[1])
                            except ValueError:
                                response = {}
                            if not isinstance(response, dict):
                                response = {}
                            if response.get('event') == 'reply_ready':
                                reply_ready = True
                            if 'ok' in response and not response.get('event') and pending_commands:
                                pending_commands.pop(0)
                    elif event == 'input':
                        # Pipe writes can block when a worker stops reading. Keep them off the
                        # monitoring thread; queued or uncertain commands are never replayed.
                        worker_input.put(value)
                        pending_commands.append(time.time())
                        reply_ready = False
                    elif event == 'write_failed' and value == attempt:
                        output.json({'ok': False, 'error_type': 'worker_interrupted',
                                     'session_path': str(session_path)})
                    elif event == 'eof':
                        eof = True
                        eof_at = time.monotonic()
                        worker_input.put(None)
                    heartbeat = read_state(root / 'chatgpt_heartbeat.json')
                    owner = read_state(root / 'chatgpt_browser_owner.json')
                    if heartbeat.get('phase') == 'download':
                        state['retain_live_target'] = True
                    if heartbeat.get('phase') == 'waiting_input' and (not interactive or not reply_ready):
                        saved = read_state(session_path) if interactive else {}
                        if not interactive or saved.get('pending') or saved.get('status') not in {'ready', 'complete'}:
                            heartbeat = {**heartbeat, 'phase': 'awaiting_reply'}
                    if pending_commands and heartbeat.get('phase') == 'waiting_input':
                        # The worker may be stuck before reading a command or publishing a phase.
                        # Independent page heartbeats must not turn that outstanding command into idle time.
                        heartbeat = {**heartbeat, 'phase': 'command_unacknowledged',
                                     'last_progress_at': max(pending_commands[0], float(heartbeat.get('last_progress_at', 0)))}
                    now = time.time()
                    control = read_state(root / 'chatgpt_supervisor_control.json')
                    takeover = (control.get('action') == 'restart' and control.get('supervisor_pid') == os.getpid()
                        and control.get('started_at') == state['started_at'] and control.get('id') != state.get('last_control_id'))
                    if takeover:
                        state['last_control_id'] = control['id']
                        recovery.setdefault('takeovers', []).append({**control, 'at': now})
                        atomic_json(root / 'chatgpt_recovery.json', recovery)
                        if control.get('rebuild_request') and not interactive and '--rebuild-request' not in worker_args:
                            worker_args.append('--rebuild-request')
                        active_started = time.monotonic()
                        state['restart_cycle_start'] = state['restarts']
                        record('upstream_takeover', request_id=control['id'])
                    idle = interactive and heartbeat.get('phase') == 'waiting_input' and heartbeat.get('pid') == child.pid
                    if idle:
                        active_started = time.monotonic()
                    if takeover:
                        reason = 'upstream_restart'
                    elif browser_exited(owner, child.pid):
                        reason = 'browser_process_exited'
                    elif time.monotonic() - active_started >= limits.task_timeout:
                        reason = 'task_deadline_exceeded'
                    elif eof:
                        if time.monotonic() - eof_at >= 10:
                            reason = 'input_closed'
                    else:
                        reason = takeover_reason(heartbeat, now=now, started=launched, limits=limits)
                    if reason:
                        stop_worker(child, root, preserve_browser=reason != 'browser_process_exited')
                        break
                    if time.monotonic() >= next_report:
                        record('worker_observed', phase=heartbeat.get('phase', 'starting'),
                               last_progress_at=heartbeat.get('last_progress_at'))
                        next_report = time.monotonic() + 20
                child.wait()
                if interactive:
                    worker_input.put(None)
                    input_thread.join(timeout=2)
                    output_thread.join(timeout=2)
            # Flush final interactive results before emitting a restart or terminal event.
            if interactive:
                while True:
                    try:
                        event, value = events.get_nowait()
                    except queue.Empty:
                        break
                    if event == 'output' and value[0] == attempt:
                        output.write(value[1])
                    elif event in {'input', 'eof'}:
                        events.put((event, value))
                        break
            report = read_state(output_path) if not interactive else read_state(session_path)
            # The worker may notice the disconnect, finish cleanup and exit before our next
            # poll. Its pre-cleanup observation still prevents an unwanted browser relaunch.
            if reason is None and browser_exited(read_state(root / 'chatgpt_browser_owner.json'), child.pid):
                reason = 'browser_process_exited'
            last_report = report
            recovered = None
            if not interactive and (reason or child.returncode != 0 or not result_complete(report, interactive=False)):
                manifest_value = getattr(limits, 'manifest', None)
                recovered = reconcile_saved_images(root, session_path, Path(manifest_value) if manifest_value else None)
                if recovered:
                    report = recovered
                    record('artifacts_reconciled', preceding_reason=reason, worker_exit_code=child.returncode)
            succeeded = bool(recovered) or (child.returncode == 0 and reason is None and result_complete(report, interactive=interactive))
            if child.returncode == 130 and reason is None and not succeeded:
                return interrupted('worker_cancelled')
            if interactive and reason == 'browser_process_exited' and not succeeded:
                return interrupted(reason)
            if succeeded or eof or (reason is None and terminal_failure(report)):
                state.update(status='complete' if succeeded else 'needs_ops')
                if not interactive:
                    report.update(supervisor_path=str(state_path), worker_restarts=state['restarts'])
                    result = report
                else:
                    result = {'status': state['status'], 'reason': reason or report.get('reason'),
                              'session_path': str(session_path), 'supervisor_path': str(state_path)}
                state['result'] = result
                recovery.update(status=state['status'], result=result)
                atomic_json(root / 'chatgpt_recovery.json', recovery)
                record('worker_finished', exit_code=child.returncode)
                if not interactive or (not succeeded and not eof):
                    output.json(result)
                return 0 if succeeded else (child.returncode if child.returncode > 0 else 2)
            if reason == 'task_deadline_exceeded' or (reason != 'upstream_restart'
                    and state['restarts'] - state.get('restart_cycle_start', 0) >= limits.max_worker_restarts):
                return exhausted(reason or 'worker_restart_budget_exhausted', report)
            state['restarts'] += 1
            state['retain_live_target'] = bool(state.get('retain_live_target') or heartbeat.get('phase') == 'download')
            if reason == 'browser_process_exited':
                state['retain_live_target'] = False
            record('restart_worker', reason=reason or 'worker_exited', exit_code=child.returncode)
            if interactive:
                output.json({'event': 'worker_restarting', 'ok': False,
                             'session_path': str(session_path), 'pending': read_state(session_path).get('pending')})
            # Bounded backoff counts toward the same task deadline.
            time.sleep(min(5 * state['restarts'], max(0, limits.task_timeout - (time.monotonic() - active_started))))
    except (KeyboardInterrupt, BrokenPipeError):
        return interrupted('cancelled')
    except Exception as error:
        return exhausted('unexpected_supervisor_error', {'error_type': type(error).__name__, 'reason': str(error)})
    finally:
        if child is not None:
            stop_worker(child, root, preserve_browser=state['status'] not in {'interrupted', 'complete'})
        output.close()
        diagnostics.close()
