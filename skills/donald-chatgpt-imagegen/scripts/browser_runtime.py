#!/usr/bin/env python3
"""Shared Chrome/CDP lifecycle for Donald browser business skills.

This file is canonical in ``donald-config-browser`` and is vendored into each
browser business skill by ``npm run build``. Keep it runtime-self-contained:
consumers import the local vendored ``profile_config`` module, never a sibling
skill directory.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from profile_config import (
    ProfileConfigError,
    activate_browser,
    call_background_page,
    cdp_target,
    close_background_page,
    close_cdp_browser,
    configured_browser,
    create_background_page,
    ensure_agent_browser,
    frontmost_process_id,
    list_cdp_targets,
    restore_frontmost_process_if_browser_active,
    show_browser_without_focus,
    start_cdp_browser,
    wait_for_background_page_url,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
STATE_ROOT_ENV = "DONALD_AGENT_BROWSER_STATE_DIR"


def default_browser_state_root(
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    override = os.environ.get(STATE_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        return home_value / "Library" / "Application Support" / "Donald Skills" / "state" / "agent-browser"
    if platform_value == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home_value / "AppData" / "Local")))
        return base / "Donald Skills" / "state" / "agent-browser"
    base = Path(os.environ.get("XDG_STATE_HOME", str(home_value / ".local" / "state")))
    return base / "donald-skills" / "agent-browser"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _target_ids(port: int) -> set[str]:
    return {
        str(target.get("id") or "")
        for target in list_cdp_targets(port)
        if target.get("type") == "page" and target.get("id")
    }


def _safe_session(value: str, run_id: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "donald-browser"
    suffix = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8]
    return f"{prefix[:29]}-{suffix}"[:40]


@contextlib.contextmanager
def _file_lock(path: Path, timeout: int) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    with path.open("a+b") as handle:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for browser lifecycle lock: {path}")
                time.sleep(0.2)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows fallback
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("schema_version") != SCHEMA_VERSION:
        state = {}
    active_runs = []
    for item in state.get("active_runs") or []:
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        if _pid_alive(pid):
            active_runs.append(item)
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "active_runs": active_runs,
            "retained_targets": list(state.get("retained_targets") or []),
            "launched_by_runtime": bool(state.get("launched_by_runtime")),
        }
    )
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class BrowserSession:
    """Own one business tab and the Chrome process lifecycle around it."""

    def __init__(
        self,
        *,
        session: str,
        url: str = "about:blank",
        scope: str | None = None,
        port: int | None = None,
        config: dict[str, Any] | None = None,
        timeout: int = 60,
        lock_timeout: int = 180,
        allow_launch: bool = True,
        verify_agent_browser: bool = True,
        require_initialized_profile: bool = True,
    ) -> None:
        self.config = config or configured_browser(scope=scope)
        self.scope = str(scope or self.config.get("scope") or "donald-browser")
        self.port = int(port or self.config["chrome"]["default_cdp_port"])
        self.session = session
        self.url = url
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.allow_launch = allow_launch
        self.verify_agent_browser = verify_agent_browser
        self.require_initialized_profile = require_initialized_profile
        self.run_id = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.transport_session = _safe_session(session, self.run_id)
        user_data_dir = str(Path(self.config["chrome"]["cdp_user_data_dir"]).expanduser().resolve())
        key = hashlib.sha1(f"{self.port}:{user_data_dir}".encode("utf-8")).hexdigest()[:16]
        state_root = default_browser_state_root()
        self.state_path = state_root / f"cdp-{key}.json"
        self.lock_path = state_root / f"cdp-{key}.lock"
        self.target_id = ""
        self.target_url = ""
        self.owned_target_ids: set[str] = set()
        self.previous_frontmost_pid: int | None = None
        self.launched = False
        self.browser_pid: int | None = None
        self.keep_open_for_human = False
        self.opened = False
        self.cleanup: dict[str, Any] = {"status": "not_opened"}
        self._atexit_callback: Any | None = None

    def _navigate(self, target_id: str, url: str) -> None:
        call_background_page(
            self.port,
            target_id,
            "Page.navigate",
            {"url": url},
            timeout=self.timeout,
        )
        wait_for_background_page_url(
            self.port,
            target_id,
            url,
            timeout=min(self.timeout, 15),
        )

    def _claim_target(self, state: dict[str, Any]) -> str:
        retained = []
        reusable = ""
        stale_same_scope: list[str] = []
        for item in state.get("retained_targets") or []:
            target_id = str(item.get("target_id") or "")
            if not target_id or cdp_target(self.port, target_id) is None:
                continue
            if item.get("scope") == self.scope and not reusable:
                reusable = target_id
                continue
            if item.get("scope") == self.scope:
                stale_same_scope.append(target_id)
                continue
            retained.append(item)
        state["retained_targets"] = retained
        for target_id in stale_same_scope:
            try:
                close_background_page(self.port, target_id)
            except ProfileConfigError:
                pass
        if reusable:
            self.owned_target_ids.add(reusable)
            self._navigate(reusable, self.url)
            return reusable
        target_id = create_background_page(self.port, self.url)
        self.owned_target_ids.add(target_id)
        wait_for_background_page_url(
            self.port,
            target_id,
            self.url,
            timeout=min(self.timeout, 15),
        )
        return target_id

    def _verify_attach(self, own_new_blank_targets: bool) -> None:
        executable = ensure_agent_browser(auto_install=True)["executable"]
        before = _target_ids(self.port)
        result = subprocess.run(
            [
                executable,
                "--session",
                self.transport_session,
                "--cdp",
                str(self.port),
                "get",
                "url",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        poll_count = 20 if own_new_blank_targets else 1
        for poll_index in range(poll_count):
            after_targets = {
                str(target.get("id") or ""): target
                for target in list_cdp_targets(self.port)
                if target.get("type") == "page" and target.get("id")
            }
            if own_new_blank_targets:
                self.owned_target_ids.update(
                    target_id
                    for target_id, target in after_targets.items()
                    if target_id not in before and target.get("url") == "about:blank"
                )
            if poll_index + 1 < poll_count:
                time.sleep(0.1)
        if result.returncode != 0:
            raise ProfileConfigError(
                "Chrome CDP is reachable, but agent-browser could not attach: "
                f"{result.stdout.strip()}"
            )

    def _open_locked(self) -> None:
        state = _read_state(self.state_path)
        startup = start_cdp_browser(
            self.config,
            self.port,
            self.timeout,
            allow_launch=self.allow_launch,
            require_initialized_profile=self.require_initialized_profile,
        )
        self.launched = bool(startup.get("launched"))
        self.browser_pid = startup.get("pid")
        previous_runtime_pid = state.get("browser_pid")
        if previous_runtime_pid and self.browser_pid and int(previous_runtime_pid) != int(self.browser_pid):
            state["launched_by_runtime"] = False
            state["retained_targets"] = []
        if self.launched:
            state["launched_by_runtime"] = True
            state["browser_pid"] = self.browser_pid

        self.target_id = self._claim_target(state)
        self.owned_target_ids.add(self.target_id)
        target = cdp_target(self.port, self.target_id) or {}
        self.target_url = str(target.get("webSocketDebuggerUrl") or "")
        if self.verify_agent_browser:
            self._verify_attach(not state["active_runs"])
        show_browser_without_focus(
            self.config,
            self.port,
        )
        state["active_runs"].append(
            {
                "run_id": self.run_id,
                "pid": os.getpid(),
                "scope": self.scope,
                "session": self.session,
                "transport_session": self.transport_session,
                "target_id": self.target_id,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        _write_state(self.state_path, state)

    def open(self) -> "BrowserSession":
        if self.opened:
            return self
        self.previous_frontmost_pid = frontmost_process_id()
        with _file_lock(self.lock_path, self.lock_timeout):
            try:
                self._open_locked()
            except Exception:
                self._cleanup_failed_open()
                raise
        self.opened = True
        self._atexit_callback = self.close
        atexit.register(self._atexit_callback)
        return self

    def _cleanup_failed_open(self) -> None:
        cleanup_frontmost_pid: int | None = None
        try:
            cleanup_frontmost_pid = frontmost_process_id()
        except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError):
            pass
        for target_id in list(self.owned_target_ids):
            try:
                if cdp_target(self.port, target_id) is not None:
                    close_background_page(self.port, target_id)
            except ProfileConfigError:
                pass
        if cleanup_frontmost_pid:
            try:
                restore_frontmost_process_if_browser_active(
                    cleanup_frontmost_pid,
                    self.port,
                )
            except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError):
                pass
        if self.launched:
            try:
                close_cdp_browser(self.config, self.port)
            except ProfileConfigError:
                pass
        self.owned_target_ids.clear()
        self.launched = False

    def preserve_for_human(self) -> dict[str, Any]:
        self.keep_open_for_human = True
        return self.activate()

    def retain_target_for_human(self, target_id: str) -> None:
        target = cdp_target(self.port, target_id)
        if target is None:
            raise ProfileConfigError(f"Cannot retain missing CDP target {target_id}")
        self.owned_target_ids.add(target_id)
        self.target_id = target_id
        self.target_url = str(target.get("webSocketDebuggerUrl") or "")
        self.keep_open_for_human = True

    def activate(self) -> dict[str, Any]:
        return activate_browser(self.config, self.port)

    def close(self, *, preserve: bool | None = None) -> dict[str, Any]:
        if not self.opened:
            return self.cleanup
        if self._atexit_callback is not None:
            atexit.unregister(self._atexit_callback)
            self._atexit_callback = None
        preserve = self.keep_open_for_human if preserve is None else preserve
        errors: list[str] = []
        cleanup_frontmost_pid: int | None = None
        if not preserve:
            try:
                cleanup_frontmost_pid = frontmost_process_id()
            except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError) as error:
                errors.append(str(error))

        ancillary = self.owned_target_ids - {self.target_id}
        targets_to_close = ancillary if preserve else set(self.owned_target_ids)
        for target_id in targets_to_close:
            try:
                if cdp_target(self.port, target_id) is not None:
                    close_background_page(self.port, target_id)
            except ProfileConfigError as error:
                errors.append(str(error))

        if cleanup_frontmost_pid:
            try:
                restore_frontmost_process_if_browser_active(
                    cleanup_frontmost_pid,
                    self.port,
                )
            except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError) as error:
                errors.append(str(error))

        close_browser = False
        browser_cleanup: dict[str, Any] = {"status": "kept_open" if preserve else "not_owned"}
        with _file_lock(self.lock_path, self.lock_timeout):
            state = _read_state(self.state_path)
            state["active_runs"] = [
                item for item in state.get("active_runs") or [] if item.get("run_id") != self.run_id
            ]
            retained = [
                item
                for item in state.get("retained_targets") or []
                if str(item.get("target_id") or "") != self.target_id
            ]
            target_is_open = False
            if preserve and self.target_id:
                try:
                    target_is_open = cdp_target(self.port, self.target_id) is not None
                except ProfileConfigError as error:
                    errors.append(str(error))
            if target_is_open:
                retained.append(
                    {
                        "target_id": self.target_id,
                        "scope": self.scope,
                        "session": self.session,
                        "retained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )
            state["retained_targets"] = retained
            close_browser = bool(
                not preserve
                and not state["active_runs"]
                and not retained
                and state.get("launched_by_runtime")
            )
            if close_browser:
                try:
                    browser_cleanup = close_cdp_browser(self.config, self.port)
                    state["launched_by_runtime"] = False
                    state["browser_pid"] = None
                except ProfileConfigError as error:
                    errors.append(str(error))
                    browser_cleanup = {"status": "error", "error": str(error)}
            _write_state(self.state_path, state)

        self.opened = False
        self.cleanup = {
            "status": "kept_open_for_human" if preserve else ("closed" if not errors else "partial"),
            "target_id": self.target_id,
            "browser": browser_cleanup,
            "errors": errors,
        }
        return self.cleanup

    def __enter__(self) -> "BrowserSession":
        return self.open()

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()
