"""Read-only run observation and per-browser submission circuit state."""
from __future__ import annotations

import json
import os
import threading
import sys
import time
from pathlib import Path
from typing import Any, Callable


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


class RunHeartbeat:
    def __init__(self, root: Path, probe: Callable[[], dict[str, Any]], interval: float = 20):
        self.root, self.probe, self.interval = root, probe, interval
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.phase = 'browser_startup'
        self.finished = False
        self.phase_at = self.progress_at = time.time()
        self.detail = 'starting browser'
        self.failures = 0
        self.page: dict[str, Any] = {}
        self.page_at: float | None = None
        self.signature: str | None = None
        self.thread = threading.Thread(target=self._run, name='imagegen-heartbeat', daemon=True)

    def start(self) -> None:
        self.publish()
        self.thread.start()

    def set_phase(self, phase: str, detail: str = '') -> None:
        with self.lock:
            if phase != self.phase:
                self.phase = phase
                self.phase_at = self.progress_at = time.time()
            if detail:
                self.detail = detail
            self.publish()

    def progress(self, detail: str) -> None:
        with self.lock:
            self.progress_at = time.time()
            self.detail = detail

    def sample(self) -> None:
        try:
            page = self.probe()
        except Exception as error:
            with self.lock:
                self.failures += 1
                self.page = {'status': 'unreadable', 'error_type': type(error).__name__}
        else:
            with self.lock:
                self.failures = 0
                self.page_at = time.time()
                signature = json.dumps(page, sort_keys=True)
                if signature != self.signature:
                    self.progress_at = time.time()
                    self.signature = signature
                self.page = page
        self.publish()

    def publish(self) -> dict[str, Any]:
        with self.lock:
            value = {
                'event': 'run_heartbeat', 'at': time.time(), 'pid': os.getpid(),
                'phase': self.phase, 'phase_started_at': self.phase_at, 'finished': self.finished,
                'last_progress_at': self.progress_at, 'detail': self.detail,
                'last_page_check_at': self.page_at, 'page': self.page,
                'consecutive_page_failures': self.failures,
                'recovery_required': self.failures >= 3 or bool(self.page.get('crashReason') or self.page.get('responseError')),
            }
            atomic_json(self.root / 'chatgpt_heartbeat.json', value)
            with (self.root / 'chatgpt_heartbeat.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(value, ensure_ascii=False) + '\n')
            print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)
            return value

    def _run(self) -> None:
        while not self.stopped.is_set():
            started = time.monotonic()
            self.sample()
            self.stopped.wait(max(0, self.interval - (time.monotonic() - started)))

    def close(self, status: str) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=6)
        self.finished = True
        self.set_phase(status, 'runner finished; inspect session for recovery action')


def heartbeat_status(root: Path) -> dict[str, Any]:
    path = root / 'chatgpt_heartbeat.json'
    if not path.exists():
        return {'status': 'no_heartbeat'}
    value = json.loads(path.read_text(encoding='utf-8'))
    try:
        os.kill(int(value['pid']), 0)
        alive = True
    except (OSError, ValueError):
        alive = False
    age = max(0, time.time() - float(value['at']))
    return {**value, 'process_alive': alive, 'heartbeat_age_seconds': round(age, 1),
            'status': value['phase'] if value.get('finished') else ('stopped' if not alive else ('stale' if age > 60 else value['phase']))}


class SubmissionCircuit:
    """The caller must hold the browser-lane file lock for every state transition."""
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {'state': 'running', 'failures': []}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def failure(self, job_id: str, now: float) -> dict[str, Any]:
        state = self.read()
        failures = [f for f in state.get('failures', []) if f['at'] > now - 600 and f['job_id'] != job_id]
        failures.append({'job_id': job_id, 'at': now})
        state['failures'] = failures
        if len(failures) >= 2:
            pause = {'state': 'paused', 'reason': 'consecutive_page_failures',
                     'next_check_at': now + 300, 'failures': failures}
            if state.get('reason') == 'human_verification':
                state['prior_pause'] = pause
            else:
                state.update(pause)
            state.pop('probe', None)
        atomic_json(self.path, state)
        return state

    def verification(self, job_id: str, now: float, *, manifest: str | None = None) -> None:
        state = self.read()
        owners = state.get('verification_owners', [state['owner']] if state.get('owner') else [])
        if state['state'] == 'paused' and state.get('reason') != 'human_verification':
            state['prior_pause'] = dict(state)
        if job_id not in owners:
            owners.append(job_id)
        if manifest:
            state.setdefault('verification_manifests', {})[job_id] = manifest
        state.update(state='paused', reason='human_verification',
                     verification_owners=owners, next_check_at=now + 300)
        atomic_json(self.path, state)

    def verified(self, job_id: str) -> None:
        state = self.read()
        owners = state.get('verification_owners', [state['owner']] if state.get('owner') else [])
        if state.get('reason') != 'human_verification' or job_id not in owners:
            return
        owners.remove(job_id)
        state.get('verification_manifests', {}).pop(job_id, None)
        if owners:
            state['verification_owners'] = owners
        else:
            state = state.get('prior_pause') or {'state': 'running', 'failures': state.get('failures', [])}
        atomic_json(self.path, state)

    def admit(self, owner: str, now: float, *, allow_probe: bool) -> str:
        state = self.read()
        if state['state'] == 'running':
            return 'allow'
        # A challenge never expires into permission to submit.
        if state.get('reason') == 'human_verification':
            return 'paused'
        probe = state.get('probe') or {}
        if now < state.get('next_check_at', 0) or now < probe.get('until', 0):
            return 'paused'
        if probe.get('pid'):
            try:
                os.kill(int(probe['pid']), 0)
            except OSError:
                pass
            else:
                return 'paused'  # A slow but live probe must not overlap a second probe.
        if not allow_probe:
            return 'paused'
        state['probe'] = {'owner': owner, 'until': now + 120, 'pid': os.getpid()}
        atomic_json(self.path, state)
        return 'probe'

    def probe_finished(self, owner: str, now: float, healthy: bool) -> None:
        state = self.read()
        if (state.get('probe') or {}).get('owner') != owner or state.get('reason') == 'human_verification':
            return
        if healthy:
            state = {'state': 'running', 'failures': []}
        else:
            state.pop('probe', None)
            state.update(state='paused', next_check_at=now + 300)
        atomic_json(self.path, state)

    def submitted(self) -> None:
        state = self.read()
        if state['state'] == 'running':
            atomic_json(self.path, {'state': 'running', 'failures': []})
