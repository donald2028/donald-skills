"""Bounded process supervision for the ChatGPT workers; never operate the browser here."""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from browser_runtime import _file_lock
from run_health import atomic_json
from chatgpt_page_health import page_failure

DEFAULT_HEARTBEAT_TIMEOUT = 60
DEFAULT_STALL_TIMEOUT = 180
DEFAULT_TASK_TIMEOUT = 300


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
                        help='Total active-task budget across worker restarts, in seconds.')
    parser.add_argument('--max-worker-restarts', type=int, default=2)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--recover-worker', action='store_true', help=argparse.SUPPRESS)


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def stop_worker(child: subprocess.Popen) -> None:
    # Only the Python worker belongs to this supervisor. Chrome may own unresolved requests
    # or other tasks; never kill its process group or close its targets during takeover.
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


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
        'login_required', 'foreground_changed', 'original_conversation_required',
        'browser_profile_unconfigured', 'pillow_unavailable', 'reference_upload_failed',
        'chatgpt_chat_surface_unavailable', 'chatgpt_create_image_mode_unavailable',
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
    # Prevent two parents from restarting workers against the same task.
    with _file_lock(root / 'chatgpt_supervisor.lock', timeout=0):
        return _supervise(script, worker_args, root, session_path, limits, interactive=interactive)


def _supervise(script: Path, worker_args: list[str], root: Path, session_path: Path,
               limits: argparse.Namespace, *, interactive: bool) -> int:
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

    def exhausted(reason: str, report: dict[str, Any]) -> int:
        result = {'status': 'needs_ops', 'error_type': reason,
                  'session_path': str(session_path), 'supervisor_path': str(state_path),
                  'worker_restarts': state['restarts'], 'last_worker_report': report,
                  'recommended_next_action': 'inspect_saved_session_before_resume'}
        state.update(status='needs_ops', reason=reason, result=result)
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
                    idle = interactive and heartbeat.get('phase') == 'waiting_input' and heartbeat.get('pid') == child.pid
                    if idle:
                        active_started = time.monotonic()
                    if time.monotonic() - active_started >= limits.task_timeout:
                        reason = 'task_deadline_exceeded'
                    elif eof:
                        if time.monotonic() - eof_at >= 10:
                            reason = 'input_closed'
                    else:
                        reason = takeover_reason(heartbeat, now=now, started=launched, limits=limits)
                    if reason:
                        stop_worker(child)
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
            last_report = report
            succeeded = child.returncode == 0 and reason is None and result_complete(report, interactive=interactive)
            if succeeded or eof or (reason is None and terminal_failure(report)):
                state.update(status='complete' if succeeded else 'needs_ops')
                if not interactive:
                    report.update(supervisor_path=str(state_path), worker_restarts=state['restarts'])
                    result = report
                else:
                    result = {'status': state['status'], 'reason': reason or report.get('reason'),
                              'session_path': str(session_path), 'supervisor_path': str(state_path)}
                state['result'] = result
                record('worker_finished', exit_code=child.returncode)
                if not interactive or (not succeeded and not eof):
                    output.json(result)
                return 0 if succeeded else (child.returncode if child.returncode > 0 else 2)
            if reason == 'task_deadline_exceeded' or state['restarts'] >= limits.max_worker_restarts:
                return exhausted(reason or 'worker_restart_budget_exhausted', report)
            state['restarts'] += 1
            state['retain_live_target'] = bool(state.get('retain_live_target') or heartbeat.get('phase') == 'download')
            record('restart_worker', reason=reason or 'worker_exited', exit_code=child.returncode)
            if interactive:
                output.json({'event': 'worker_restarting', 'ok': False,
                             'session_path': str(session_path), 'pending': read_state(session_path).get('pending')})
            # Bounded backoff counts toward the same task deadline.
            time.sleep(min(5 * state['restarts'], max(0, limits.task_timeout - (time.monotonic() - active_started))))
    except (KeyboardInterrupt, BrokenPipeError):
        state.update(status='interrupted')
        record('supervisor_interrupted')
        return 130
    except Exception as error:
        return exhausted('unexpected_supervisor_error', {'error_type': type(error).__name__, 'reason': str(error)})
    finally:
        if child is not None:
            stop_worker(child)
        output.close()
        diagnostics.close()
