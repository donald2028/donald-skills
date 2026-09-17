#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import shlex
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError as error:  # pragma: no cover - depends on the local Python environment
    Image = ImageChops = ImageStat = None  # type: ignore[assignment]
    PIL_IMPORT_ERROR = error
else:
    PIL_IMPORT_ERROR = None

from browser_runtime import BrowserSession, process_is_alive
from output_paths import resolve_tool_state_root
from profile_config import (
    ProfileConfigError,
    activate_browser,
    chrome_environment,
    configured_browser,
    ensure_agent_browser,
    restore_frontmost_process,
    run_agent_browser,
    show_browser_without_focus,
)
from reference_contract import (
    reference_audit_fields,
    validate_model_message,
    validate_reference_contract,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None  # type: ignore[assignment]


def _default_chrome_executable() -> str:
    explicit = os.environ.get("CHATGPT_WEB_CHROME_EXECUTABLE")
    if explicit:
        return explicit
    return chrome_environment()["executable"]


DEFAULT_CHROME = _default_chrome_executable()
DEFAULT_CDP_USER_DATA_DIR = os.environ.get(
    "CHATGPT_WEB_USER_DATA_DIR",
    "",
)
TARGET_AGENT_BROWSER_PROFILE = os.environ.get("CHATGPT_WEB_PROFILE", "")
TARGET_CHATGPT_ACCOUNT_SIGNAL = os.environ.get(
    "CHATGPT_WEB_ACCOUNT_LABEL",
    "the ChatGPT account logged into the configured Chrome profile",
)


def _try_file_lock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:  # pragma: no cover - Windows fallback
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise RuntimeError("No supported file-lock implementation is available")


def _unlock_file(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows fallback
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


BUSY_MARKERS = (
    "Stop answering",
    "Stop generating",
    "Stop streaming",
    "Generating a more detailed image",
    "Generating image",
    "Creating image",
    "Aligning prompt with image references",
    "Thinking",
)
GENERATION_FAILED_MARKERS = (
    "Image generation failed",
    "unable to generate the image because the image generation tool encountered an error",
    "image generation tool encountered an error",
    "Something went wrong",
    "Something is wrong",
    "There was an error generating a response",
    "Error generating response",
    "Network error",
    "Request timed out",
    "生成工具这次连续报错",
    "没有成功产出图片",
    "出了点问题",
    "出现错误",
    "发生错误",
    "网络错误",
    "请求超时",
)
REFERENCE_UPLOAD_FAILED_MARKERS = (
    "Unable to upload",
    "Failed to upload",
    "Couldn't upload",
    "Could not upload",
    "无法上传",
    "上传失败",
)
POLICY_REFUSAL_MARKERS = (
    "image we created may violate our guardrails",
    "may violate our content policies",
    "may violate our content policy",
    "violates our content policies",
    "violates our content policy",
    "can't help create that image",
    "cannot help create that image",
    "unable to generate that image",
)
MAX_TIMING_SAMPLES = 50
MAX_AGENT_BROWSER_TRANSPORT_SESSION_LENGTH = 40
DEFAULT_STALE_GENERATION_REFRESH_SECONDS = 0
DEFAULT_PAGE_RECOVERY_ATTEMPTS = 3
PAGE_STATE_FAILURES_BEFORE_RECOVERY = 3
REFERENCE_UPLOAD_NO_EVIDENCE_RECOVERY_SECONDS = 15
MISSING_SUBMITTED_TURN_HEARTBEATS = 2
DEFAULT_SUBMIT_THROTTLE_MIN_INTERVAL_SECONDS = 90
DEFAULT_SUBMIT_THROTTLE_MAX_SUBMITS_PER_HOUR = 40
DEFAULT_SUBMIT_THROTTLE_MAX_EXPECTED_IMAGES_PER_HOUR = 0
SUBMIT_THROTTLE_WINDOW_SECONDS = 3600
SUBMIT_THROTTLE_LOCK_STALE_SECONDS = 300
ROUTINE_TRACE_SCREENSHOT_NAMES = frozenset(
    {
        "01_open.png",
        "01_resumed.png",
        "01_conversation_followup.png",
        "01_current_page.png",
        "02_after_upload.png",
        "03_after_prompt.png",
        "04_after_submit.png",
        "05_after_generated.png",
    }
)
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
REFERENCE_LINE_RE = re.compile(r"^\s*(\d+)\.\s+`([^`]+)`\s*$", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class ReferenceUploadError(RuntimeError):
    def __init__(self, failure: dict[str, Any]):
        super().__init__(json.dumps(failure, ensure_ascii=False))
        self.failure = failure


class ImageModeSelectionError(RuntimeError):
    pass


class ChatSurfaceSelectionError(RuntimeError):
    pass


class HumanAttentionRequired(RuntimeError):
    def __init__(self, reason: str, activation: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.activation = activation


class RecoverablePageError(RuntimeError):
    pass


class PageRecoveryExhaustedError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__(str(report.get("reason") or "ChatGPT page recovery was exhausted"))
        self.report = report


class SubmissionStateUnknownError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__(str(report.get("reason") or "ChatGPT submission state is unknown"))
        self.report = report


def _resolve_browser_profile(args: argparse.Namespace) -> None:
    ensure_agent_browser(auto_install=True)
    if args.user_data_dir:
        args.profile = args.profile or "Default"
        args.cdp_port = args.cdp_port or "9333"
        args._uses_shared_browser_profile = False
        args._shared_profile_name = args.profile
        return

    config = configured_browser()
    configured_profile = str(config["profile"]["directory"])
    configured_port = str(config["chrome"]["default_cdp_port"])
    if args.profile and args.profile != configured_profile:
        raise ProfileConfigError(
            "--profile cannot select a different account without its matching --user-data-dir. "
            "Reinitialize this skill's Chrome-over-CDP binding or pass both overrides."
        )
    args.profile = configured_profile
    if args.cdp_port and str(args.cdp_port) != configured_port:
        raise ProfileConfigError(
            f"--cdp-port {args.cdp_port} does not match the shared Profile port "
            f"{configured_port}. Omit --cdp-port to reuse the configured browser."
        )
    args.cdp_port = configured_port
    args._shared_profile_name = config["profile"].get("name") or configured_profile
    args.user_data_dir = str(config["chrome"]["cdp_user_data_dir"])
    if not os.environ.get("CHATGPT_WEB_CHROME_EXECUTABLE"):
        args.executable_path = str(config["chrome"]["executable"])
    args._uses_shared_browser_profile = True


def _profile_label(args: argparse.Namespace) -> str:
    name = str(getattr(args, "_shared_profile_name", "") or args.profile)
    return f"{args.profile} ({name})"


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    if command and Path(command[0]).name.lower().startswith("agent-browser"):
        return run_agent_browser(command, cwd=cwd, timeout=timeout)
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _sections(markdown: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(markdown))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        result[match.group(1).strip()] = markdown[start:end].strip()
    return result


def _job_reference_source(ref: dict[str, Any], reference_base_dir: Path | None) -> str:
    source_path = str(ref.get("source_path") or "").strip()
    if source_path:
        return source_path
    path = str(ref.get("path") or "").strip()
    if not path:
        return ""
    if reference_base_dir:
        try:
            return Path(path).resolve().relative_to(reference_base_dir.resolve()).as_posix()
        except ValueError:
            pass
    return path


def _validate_manifest_reference_mapping(job: dict[str, Any]) -> None:
    prompt_card = Path(str(job.get("prompt_card") or ""))
    if prompt_card.is_file():
        reference_base_dir = (
            Path(str(job["reference_base_dir"]))
            if job.get("reference_base_dir")
            else prompt_card.parent
        )
        reference_section = _sections(prompt_card.read_text(encoding="utf-8")).get(
            "Required Reference Images",
            "",
        )
        if reference_section.strip():
            prompt_refs = [
                (int(index), source_path)
                for index, source_path in REFERENCE_LINE_RE.findall(reference_section)
            ]
            job_refs = [
                (int(ref.get("index")), _job_reference_source(ref, reference_base_dir))
                for ref in job.get("reference_images", [])
            ]
            if prompt_refs != job_refs:
                raise ValueError(
                    "ChatGPT job manifest reference_images are stale or out of sync with "
                    f"{prompt_card}. Re-run prepare_job.py before uploading references.\n"
                    f"prompt_card_refs={prompt_refs}\n"
                    f"manifest_refs={job_refs}"
                )

    reference_images = [
        dict(entry) for entry in job.get("reference_images", []) if isinstance(entry, dict)
    ]
    model_reference_map = [
        dict(entry) for entry in job.get("model_reference_map", []) if isinstance(entry, dict)
    ]
    ordered_upload_paths = [str(path) for path in job.get("ordered_upload_paths", [])]
    compiled_model_reference_map = str(job.get("compiled_model_reference_map") or "")
    validate_reference_contract(
        reference_images=reference_images,
        model_reference_map=model_reference_map,
        ordered_upload_paths=ordered_upload_paths,
        compiled_model_reference_map=compiled_model_reference_map,
    )
    messages = [
        str(job.get("chatgpt_batch_message") or ""),
        str(job.get("chatgpt_message") or ""),
        *[
            str(entry.get("message") or "")
            for entry in job.get("chatgpt_messages", [])
            if isinstance(entry, dict)
        ],
    ]
    populated_messages = {message for message in messages if message}
    if reference_images and not populated_messages:
        raise ValueError("reference job has no model-facing message containing the compiled map")
    for message in populated_messages:
        validate_model_message(
            message=message,
            reference_images=reference_images,
            compiled_model_reference_map=compiled_model_reference_map,
        )


def _validate_session_reference_mapping(job: dict[str, Any], session: dict[str, Any]) -> None:
    expected = reference_audit_fields(job)
    if not expected["reference_upload_order"]:
        return
    for field, expected_value in expected.items():
        actual = session.get(field)
        if actual != expected_value:
            raise ValueError(
                "Existing ChatGPT session reference contract is missing or stale. "
                "Start a fresh conversation with --no-resume so references are uploaded again.\n"
                f"field={field}\nexpected={expected_value}\nactual={actual}"
            )


def _encode_ws_text_frame(payload: bytes) -> bytes:
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    return bytes(header) + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def _decode_ws_frame(buffer: bytes) -> tuple[int, bytes, int] | None:
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = struct.unpack(">H", buffer[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = struct.unpack(">Q", buffer[offset : offset + 8])[0]
        offset += 8
    mask = b""
    if masked:
        if len(buffer) < offset + 4:
            return None
        mask = buffer[offset : offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        return None
    payload = buffer[offset : offset + length]
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload, offset + length


class _CDPConnection:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""
        self._next_id = 0

    @classmethod
    def connect(cls, ws_url: str, timeout: float = 30.0) -> "_CDPConnection":
        parts = urllib.parse.urlsplit(ws_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = "\r\n".join(
            [
                f"GET {path} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                "",
                "",
            ]
        ).encode("ascii")
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP websocket closed during handshake")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("CDP websocket handshake failed")
        expected = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        if expected.encode("ascii") not in head:
            raise ConnectionError("CDP websocket accept key mismatch")
        conn = cls(sock)
        conn._buffer = rest
        return conn

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "_CDPConnection":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _recv_frame(self) -> tuple[int, bytes]:
        while True:
            parsed = _decode_ws_frame(self._buffer)
            if parsed:
                opcode, payload, consumed = parsed
                self._buffer = self._buffer[consumed:]
                return opcode, payload
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP websocket closed")
            self._buffer += chunk

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8")
        self._sock.sendall(_encode_ws_text_frame(payload))
        while True:
            opcode, payload = self._recv_frame()
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") != msg_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result") or {}


def _safe_agent_browser_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return slug or "chatgpt"


def _agent_browser_transport_session(args: argparse.Namespace) -> str:
    existing = getattr(args, "_agent_browser_transport_session", "")
    if existing:
        return str(existing)

    original = str(getattr(args, "session", "chatgpt-web-agent-browser"))
    safe = _safe_agent_browser_slug(original)
    if len(safe) > MAX_AGENT_BROWSER_TRANSPORT_SESSION_LENGTH:
        safe = f"cgpt-{hashlib.sha1(original.encode('utf-8')).hexdigest()[:16]}"

    args._agent_browser_original_session = original
    args._agent_browser_transport_session = safe
    return safe


def _agent_browser_session_record(args: argparse.Namespace) -> dict[str, str]:
    original = str(getattr(args, "session", "chatgpt-web-agent-browser"))
    return {
        "original": str(getattr(args, "_agent_browser_original_session", original)),
        "transport": _agent_browser_transport_session(args),
    }


def _agent_browser_base(args: argparse.Namespace) -> list[str]:
    executable = ensure_agent_browser(auto_install=True)["executable"]
    command = [executable, "--session", _agent_browser_transport_session(args)]
    if args.download_path:
        command.extend(["--download-path", str(args.download_path)])
    if args.cdp:
        command.extend(["--cdp", str(args.cdp)])
    elif args.auto_connect:
        command.append("--auto-connect")
    return command


def _cdp_page_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    port = _cdp_launch_port(args)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.loads(response.read().decode("utf-8"))
    if not isinstance(targets, list):
        return []
    return [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and str(target.get("webSocketDebuggerUrl") or "").startswith(("ws://", "wss://"))
    ]


def _browser_cdp_websocket_url(args: argparse.Namespace) -> str:
    port = _cdp_launch_port(args)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    cdp_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not cdp_url.startswith(("ws://", "wss://")):
        raise RuntimeError("Chrome browser CDP websocket URL is unavailable")
    return cdp_url


def _remember_owned_cdp_target(args: argparse.Namespace, target: dict[str, Any]) -> bool:
    cdp_url = str(target.get("webSocketDebuggerUrl") or "")
    if not cdp_url.startswith(("ws://", "wss://")):
        return False
    args._owned_tab_cdp_url = cdp_url
    args._owned_tab_cdp_target_id = str(target.get("id") or "")
    args._owned_tab_cdp_error = ""
    browser_session = getattr(args, "_browser_session", None)
    if browser_session is not None and args._owned_tab_cdp_target_id:
        browser_session.owned_target_ids.add(args._owned_tab_cdp_target_id)
        browser_session.target_id = args._owned_tab_cdp_target_id
        browser_session.target_url = cdp_url
    return True


def _forget_owned_cdp_target(args: argparse.Namespace) -> None:
    args._owned_tab_cdp_target_id = ""
    args._owned_tab_cdp_url = ""
    args._opened_tab = False
    args._tab_label = ""


def _capture_owned_tab_cdp_url(
    args: argparse.Namespace,
    cwd: Path,
    before_target_ids: set[str] | None = None,
) -> None:
    deadline = time.time() + 5
    last_error = ""
    while time.time() < deadline:
        try:
            targets = _cdp_page_targets(args)
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(0.2)
            continue
        if before_target_ids is not None:
            for target in targets:
                if str(target.get("id") or "") not in before_target_ids:
                    if _remember_owned_cdp_target(args, target):
                        return
        elif targets and _remember_owned_cdp_target(args, targets[0]):
            return
        time.sleep(0.2)

    try:
        targets = _cdp_page_targets(args)
    except Exception as error:
        args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
        return
    if targets and _remember_owned_cdp_target(args, targets[0]):
        return
    args._owned_tab_cdp_error = last_error or "page_target_not_found"


def _open_background_cdp_tab(args: argparse.Namespace, url: str) -> bool:
    try:
        targets = _cdp_page_targets(args)
        blank_target = next(
            (
                target
                for target in targets
                if target.get("url") == "about:blank"
                and str(target.get("webSocketDebuggerUrl") or "").startswith(("ws://", "wss://"))
            ),
            None,
        )
        if blank_target and _remember_owned_cdp_target(args, blank_target):
            args._opened_tab = True
            args._tab_label = ""
            if _navigate_owned_cdp_tab(args, url):
                return True
            _forget_owned_cdp_target(args)

        before_target_ids = {str(target.get("id") or "") for target in targets}
        with _CDPConnection.connect(_browser_cdp_websocket_url(args), timeout=10) as conn:
            result = conn.call("Target.createTarget", {"url": url, "background": True})
        target_id = str(result.get("targetId") or "")
        deadline = time.time() + 10
        while time.time() < deadline:
            for target in _cdp_page_targets(args):
                current_id = str(target.get("id") or "")
                if current_id == target_id or current_id not in before_target_ids:
                    if _remember_owned_cdp_target(args, target):
                        args._owned_tab_cdp_target_id = current_id
                        args._opened_tab = True
                        args._tab_label = ""
                        return True
            time.sleep(0.2)
    except Exception as error:
        args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
    return False


def _navigate_owned_cdp_tab(args: argparse.Namespace, url: str) -> bool:
    if not _owned_tab_cdp_url(args):
        return False
    try:
        with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=30) as conn:
            conn.call("Page.enable")
            conn.call("Page.navigate", {"url": url})
        return True
    except Exception as error:
        args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
        return False


def _owned_tab_cdp_url(args: argparse.Namespace) -> str:
    return str(getattr(args, "_owned_tab_cdp_url", "") or "")


def _try_cdp_eval_js(args: argparse.Namespace, script: str, timeout: int = 120) -> tuple[bool, str]:
    cdp_url = _owned_tab_cdp_url(args)
    if not cdp_url:
        return False, ""
    try:
        with _CDPConnection.connect(cdp_url, timeout=max(5, timeout)) as conn:
            conn.call("Runtime.enable")
            conn.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            result = conn.call(
                "Runtime.evaluate",
                {
                    "expression": script,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if result.get("exceptionDetails"):
                raise RuntimeError(f"Runtime.evaluate exception: {result['exceptionDetails']}")
    except Exception as error:
        args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
        return False, ""
    args._owned_tab_cdp_error = ""
    remote = result.get("result") or {}
    if remote.get("type") == "undefined":
        return True, ""
    if "value" in remote:
        value = remote.get("value")
        if isinstance(value, str):
            return True, value
        return True, json.dumps(value, ensure_ascii=False)
    return True, str(remote.get("description") or "")


def _command_lock_path_for_cdp(args: argparse.Namespace) -> Path:
    return _lock_path_for_cdp(args).with_suffix(".command.lock")


@contextlib.contextmanager
def _cdp_command_lock(args: argparse.Namespace, timeout_s: int = 180):
    lock_path = _command_lock_path_for_cdp(args)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                _try_file_lock(handle)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for ChatGPT Web CDP command lock: {lock_path}")
                time.sleep(0.2)
        try:
            yield
        finally:
            _unlock_file(handle)


def _should_select_owned_tab(args: argparse.Namespace, subcommand: list[str]) -> bool:
    if not getattr(args, "_opened_tab", False) or not getattr(args, "_tab_label", ""):
        return False
    if not subcommand:
        return False
    if subcommand[0] in {"tab", "close"}:
        return False
    return True


def _call_agent(args: argparse.Namespace, cwd: Path, subcommand: list[str], timeout: int = 120) -> str:
    command = _agent_browser_base(args) + subcommand
    with _cdp_command_lock(args):
        if _should_select_owned_tab(args, subcommand):
            _run(_agent_browser_base(args) + ["tab", getattr(args, "_tab_label")], cwd=cwd, timeout=30)
        result = _run(command, cwd=cwd, timeout=timeout)
    rendered = " ".join(shlex.quote(part) for part in command)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {rendered}\n{result.stdout}")
    return result.stdout


def _safe_agent(args: argparse.Namespace, cwd: Path, subcommand: list[str], timeout: int = 120) -> str:
    command = _agent_browser_base(args) + subcommand
    with _cdp_command_lock(args):
        if _should_select_owned_tab(args, subcommand):
            _run(_agent_browser_base(args) + ["tab", getattr(args, "_tab_label")], cwd=cwd, timeout=30)
        result = _run(command, cwd=cwd, timeout=timeout)
    return result.stdout


def _close_owned_tab(args: argparse.Namespace, cwd: Path) -> None:
    try:
        target_id = str(getattr(args, "_owned_tab_cdp_target_id", "") or "")
        if target_id:
            with _CDPConnection.connect(_browser_cdp_websocket_url(args), timeout=10) as conn:
                conn.call("Target.closeTarget", {"targetId": target_id})
            args._owned_tab_cdp_target_id = ""
            args._owned_tab_cdp_url = ""
            args._opened_tab = False
            args._tab_label = ""
            return
        label = getattr(args, "_tab_label", "")
        if label and getattr(args, "_opened_tab", False):
            with _cdp_command_lock(args):
                _run(_agent_browser_base(args) + ["tab", "close", label], cwd=cwd, timeout=60)
            args._opened_tab = False
            args._tab_label = ""
    except Exception:
        pass


def _about_blank_tab_ids(tab_list_output: str) -> list[str]:
    return _tab_summary(tab_list_output)["about_blank_tab_ids"]


def _tab_summary(tab_list_output: str) -> dict[str, Any]:
    try:
        payload = json.loads(tab_list_output)
    except json.JSONDecodeError:
        about_blank_tab_ids = []
        non_blank_count = 0
        for line in tab_list_output.splitlines():
            if match := re.search(r"\[(t\d+)\].*\babout:blank\s+-\s+about:blank\s*$", line):
                about_blank_tab_ids.append(match.group(1))
            elif re.search(r"\[(t\d+)\]", line):
                non_blank_count += 1
        return {
            "listed": bool(about_blank_tab_ids or non_blank_count),
            "about_blank_tab_ids": about_blank_tab_ids,
            "non_blank_count": non_blank_count,
        }
    tabs = ((payload.get("data") or {}).get("tabs") or []) if isinstance(payload, dict) else []
    about_blank_tab_ids = []
    non_blank_count = 0
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        if tab.get("url") == "about:blank" and tab.get("title") == "about:blank" and tab.get("tabId"):
            about_blank_tab_ids.append(str(tab["tabId"]))
        else:
            non_blank_count += 1
    return {
        "listed": isinstance(tabs, list),
        "about_blank_tab_ids": about_blank_tab_ids,
        "non_blank_count": non_blank_count,
    }


def _close_about_blank_tabs(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    summary = {"listed": False, "about_blank_tab_ids": [], "non_blank_count": 0}
    if args.cdp:
        try:
            targets = _cdp_page_targets(args)
            blank_targets = [
                target
                for target in targets
                if target.get("url") == "about:blank" and target.get("id")
            ]
            summary = {
                "listed": True,
                "about_blank_tab_ids": [str(target["id"]) for target in blank_targets],
                "non_blank_count": len(targets) - len(blank_targets),
            }
            if blank_targets:
                with _CDPConnection.connect(_browser_cdp_websocket_url(args), timeout=10) as conn:
                    for target in blank_targets:
                        conn.call("Target.closeTarget", {"targetId": str(target["id"])})
            return summary
        except Exception:
            return summary
    try:
        with _cdp_command_lock(args):
            result = _run(_agent_browser_base(args) + ["--json", "tab", "list"], cwd=cwd, timeout=60)
            if result.returncode != 0:
                return summary
            summary = _tab_summary(result.stdout)
            for tab_id in summary["about_blank_tab_ids"]:
                _run(_agent_browser_base(args) + ["tab", "close", tab_id], cwd=cwd, timeout=60)
    except Exception:
        return summary
    return summary


def _close_cdp_browser(args: argparse.Namespace, cwd: Path) -> None:
    try:
        if args.cdp:
            with _CDPConnection.connect(_browser_cdp_websocket_url(args), timeout=10) as conn:
                conn.call("Browser.close")
        _terminate_owned_cdp_chrome(args, cwd)
    except Exception:
        _terminate_owned_cdp_chrome(args, cwd)


def _listening_process_ids(port: str, cwd: Path) -> list[int]:
    if sys.platform == "win32":
        result = _run(["netstat", "-ano", "-p", "tcp"], cwd=cwd, timeout=10)
        process_ids: list[int] = []
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) >= 5 and f"127.0.0.1:{port}" in columns[1] and columns[3].upper() == "LISTENING":
                try:
                    process_ids.append(int(columns[-1]))
                except ValueError:
                    continue
        return process_ids

    lsof = shutil.which("lsof")
    if not lsof:
        return []
    result = _run([lsof, f"-tiTCP:{port}", "-sTCP:LISTEN"], cwd=cwd, timeout=10)
    if result.returncode != 0:
        return []
    process_ids = []
    for pid_text in result.stdout.splitlines():
        try:
            process_ids.append(int(pid_text.strip()))
        except ValueError:
            continue
    return process_ids


def _process_command(pid: int, cwd: Path) -> str:
    if sys.platform == "win32":
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if not powershell:
            return ""
        return _run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            cwd=cwd,
            timeout=10,
        ).stdout
    return _run(["ps", "-p", str(pid), "-o", "command="], cwd=cwd, timeout=10).stdout


def _terminate_process(pid: int, cwd: Path) -> None:
    if sys.platform == "win32":
        _run(["taskkill", "/PID", str(pid), "/T", "/F"], cwd=cwd, timeout=10)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if _run(["ps", "-p", str(pid)], cwd=cwd, timeout=5).returncode != 0:
            break
        time.sleep(0.25)


def _terminate_owned_cdp_chrome(args: argparse.Namespace, cwd: Path) -> None:
    port = _cdp_launch_port(args)
    user_data_dir = str(Path(args.user_data_dir).expanduser())
    for pid in _listening_process_ids(port, cwd):
        command = _process_command(pid, cwd)
        if f"--remote-debugging-port={port}" not in command or f"--user-data-dir={user_data_dir}" not in command:
            continue
        _terminate_process(pid, cwd)


def _cleanup_agent_browser(args: argparse.Namespace, cwd: Path) -> None:
    browser_session = getattr(args, "_browser_session", None)
    if browser_session is not None:
        args._browser_cleanup = browser_session.close(preserve=bool(args.keep_browser_open))
        return

    close_browser = False
    blank_tab_summary: dict[str, Any] = {"listed": False, "about_blank_tab_ids": [], "non_blank_count": 0}
    if not args.keep_browser_open:
        _close_owned_tab(args, cwd)
        blank_tab_summary = _close_about_blank_tabs(args, cwd)
    with _cdp_state_lock(args, 30):
        state = _read_cdp_state_locked(args)
        active = [
            item
            for item in state.get("active_runs", [])
            if item.get("run_id") != getattr(args, "_active_run_id", "")
        ]
        state["active_runs"] = active
        no_non_blank_tabs_remain = (
            bool(blank_tab_summary.get("listed"))
            and int(blank_tab_summary.get("non_blank_count") or 0) == 0
        )
        if not args.keep_browser_open and not active and (state.get("launched_by_runner") or no_non_blank_tabs_remain):
            close_browser = True
            state["launched_by_runner"] = False
        _write_cdp_state_locked(args, state)
    if close_browser:
        _close_cdp_browser(args, cwd)
        previous_pid = getattr(args, "_frontmost_pid_before_launch", None)
        if previous_pid:
            try:
                args._frontmost_restore = restore_frontmost_process(int(previous_pid))
            except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError) as error:
                args._frontmost_restore = {"status": "error", "error": str(error)}


def _split_browser_args(value: str | None) -> list[str]:
    if not value:
        return []
    parts: list[str] = []
    for chunk in re.split(r"[,\n]", value):
        parts.extend(shlex.split(chunk))
    return parts


def _agent_connection_check(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    result = _run(_agent_browser_base(args) + ["get", "url"], cwd=cwd, timeout=20)
    return {
        "ok": result.returncode == 0,
        "command": _agent_browser_base(args) + ["get", "url"],
        "returncode": result.returncode,
        "output": result.stdout.strip()[-2000:],
    }


def _agent_connection_works(args: argparse.Namespace, cwd: Path) -> bool:
    return bool(_agent_connection_check(args, cwd)["ok"])


def _cdp_launch_port(args: argparse.Namespace) -> str:
    value = str(args.cdp or args.cdp_port)
    if value.isdigit():
        return value
    match = re.match(r"https?://(?:localhost|127\.0\.0\.1):(\d+)(?:/.*)?$", value)
    if match:
        return match.group(1)
    if args.cdp:
        raise RuntimeError(
            f"CDP endpoint {args.cdp!r} is not reachable and cannot be used as a local launch port."
        )
    return str(args.cdp_port)


def _cdp_json_version_status(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    try:
        port = _cdp_launch_port(args)
    except RuntimeError as error:
        return {"ok": False, "error": str(error)}

    status: dict[str, Any] = {
        "ok": False,
        "port": port,
        "url": f"http://127.0.0.1:{port}/json/version",
    }
    try:
        with urllib.request.urlopen(status["url"], timeout=2) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
        payload = json.loads(raw)
        status.update(
            {
                "ok": True,
                "browser": payload.get("Browser"),
                "protocol_version": payload.get("Protocol-Version"),
                "websocket_debugger_url": payload.get("webSocketDebuggerUrl"),
            }
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        status["error"] = f"{type(error).__name__}: {error}"

    if shutil.which("lsof"):
        lsof = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], cwd=cwd, timeout=10)
        status["port_listen_returncode"] = lsof.returncode
        status["port_listen_output"] = lsof.stdout.strip()[-2000:]
    else:
        status["port_listen_returncode"] = None
        status["port_listen_output"] = "lsof unavailable"
    return status


def _connection_diagnostic(args: argparse.Namespace, cwd: Path, agent_check: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "agent_browser": agent_check or _agent_connection_check(args, cwd),
        "cdp_json_version": _cdp_json_version_status(args, cwd),
        "agent_browser_original_session": str(getattr(args, "_agent_browser_original_session", args.session)),
        "agent_browser_transport_session": _agent_browser_transport_session(args),
        "cdp": str(args.cdp or ""),
        "cdp_port": str(args.cdp_port),
        "user_data_dir": str(Path(args.user_data_dir).expanduser()),
        "profile": str(args.profile),
    }


def _format_connection_diagnostic(diagnostic: dict[str, Any]) -> str:
    return json.dumps(diagnostic, ensure_ascii=False, indent=2)


def _lock_path_for_cdp(args: argparse.Namespace) -> Path:
    port = _cdp_launch_port(args)
    user_data_key = hashlib.sha1(str(Path(args.user_data_dir).expanduser()).encode("utf-8")).hexdigest()[:12]
    return resolve_tool_state_root("chatgpt-web") / f"cdp_{port}_{user_data_key}.lock"


@contextlib.contextmanager
def _cdp_state_lock(args: argparse.Namespace, timeout_s: int):
    lock_path = _lock_path_for_cdp(args)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                _try_file_lock(handle)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for ChatGPT Web CDP lane lock: {lock_path}")
                print(
                    json.dumps(
                        {
                            "event": "chatgpt_web_progress",
                            "phase": "cdp_state_lock",
                            "status": "waiting_for_cdp_lane",
                            "lock_path": str(lock_path),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(2)
        try:
            yield
        finally:
            _unlock_file(handle)


def _cdp_state_path(args: argparse.Namespace) -> Path:
    lock_path = _lock_path_for_cdp(args)
    return lock_path.with_suffix(".state.json")


def _pid_alive(pid: int) -> bool:
    return process_is_alive(pid)


def _read_cdp_state_locked(args: argparse.Namespace) -> dict[str, Any]:
    path = _cdp_state_path(args)
    state = _read_json_if_exists(path) or {"schema_version": 1, "active_runs": [], "launched_by_runner": False}
    active = []
    for item in state.get("active_runs", []):
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        if _pid_alive(pid):
            active.append(item)
    state["active_runs"] = active
    return state


def _write_cdp_state_locked(args: argparse.Namespace, state: dict[str, Any]) -> None:
    path = _cdp_state_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _prepare_browser_lane(args: argparse.Namespace, cwd: Path) -> None:
    del cwd
    original_transport_session = _agent_browser_transport_session(args)
    if getattr(args, "_uses_shared_browser_profile", False):
        config = configured_browser()
    else:
        config = {
            "scope": "donald-chatgpt-imagegen",
            "profile": {"directory": args.profile, "name": args.profile, "email": ""},
            "chrome": {
                "cdp_user_data_dir": str(Path(args.user_data_dir).expanduser()),
                "default_cdp_port": int(_cdp_launch_port(args)),
                "executable": args.executable_path,
            },
        }
    browser_session = BrowserSession(
        scope="donald-chatgpt-imagegen",
        session=original_transport_session,
        url="about:blank",
        port=int(_cdp_launch_port(args)),
        config=config,
        timeout=60,
        lock_timeout=args.lock_timeout,
        allow_launch=not args.no_launch_browser,
    ).open()
    args._browser_session = browser_session
    args._agent_browser_transport_session = browser_session.transport_session
    args._active_run_id = browser_session.run_id
    args._owned_tab_cdp_target_id = browser_session.target_id
    args._owned_tab_cdp_url = browser_session.target_url
    args._owned_tab_cdp_error = ""
    args._opened_tab = True
    args._tab_label = ""
    args._launched_chrome = browser_session.launched
    args._frontmost_pid_before_launch = browser_session.previous_frontmost_pid
    args.cdp = str(browser_session.port)


def _launch_chrome_for_cdp(args: argparse.Namespace, cwd: Path) -> None:
    port = _cdp_launch_port(args)
    user_data_dir = Path(args.user_data_dir).expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    launch_args = [
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={args.profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-startup-window",
        *_split_browser_args(args.browser_args),
    ]

    if args.launch_background and args.executable_path == DEFAULT_CHROME:
        command = [
            "open",
            "-g",
            "-j",
            "-n",
            "-a",
            "Google Chrome",
            "--args",
            *launch_args,
        ]
    else:
        command = [args.executable_path, *launch_args]

    subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args.cdp = port
    args.auto_connect = False
    args._launched_chrome = True


def _ensure_browser_connection(args: argparse.Namespace, cwd: Path) -> None:
    args._opened_tab = False
    args._launched_chrome = False
    transport_session = _agent_browser_transport_session(args)
    tab_key = hashlib.sha1(f"{args.session}:{os.getpid()}:{time.time()}".encode("utf-8")).hexdigest()[:8]
    args._tab_label = f"chatgpt-{transport_session}-{tab_key}"[:48]

    initial_check = _agent_connection_check(args, cwd)
    if initial_check["ok"]:
        return
    initial_status = _cdp_json_version_status(args, cwd)

    if args.no_launch_browser:
        diagnostic = _connection_diagnostic(args, cwd, initial_check)
        diagnostic["initial_cdp_json_version"] = initial_status
        raise RuntimeError(
            "agent-browser could not attach to an existing Chrome/CDP session. "
            "Start Chrome with remote debugging or omit --no-launch-browser to let the runner open "
            f"{_profile_label(args)} automatically.\n"
            f"Connection diagnostic:\n{_format_connection_diagnostic(diagnostic)}"
        )

    if initial_status.get("ok") or initial_status.get("port_listen_returncode") == 0:
        deadline = time.time() + 15
        last_check = initial_check
        last_status = initial_status
        while time.time() < deadline:
            last_status = _cdp_json_version_status(args, cwd)
            if last_status.get("ok") and not getattr(args, "_opened_tab", False):
                if _open_background_cdp_tab(args, "about:blank") and getattr(
                    args, "_uses_shared_browser_profile", False
                ):
                    visibility = show_browser_without_focus(
                        configured_browser(), int(_cdp_launch_port(args))
                    )
                    args._frontmost_pid_before_launch = visibility.get(
                        "restored_frontmost_pid"
                    )
            last_check = _agent_connection_check(args, cwd)
            if last_check["ok"]:
                return
            time.sleep(1)
        diagnostic = _connection_diagnostic(args, cwd, last_check)
        diagnostic["initial_cdp_json_version"] = initial_status
        diagnostic["last_cdp_json_version"] = last_status
        raise RuntimeError(
            "A Chrome/CDP endpoint is already present, but agent-browser could not attach to it.\n"
            f"Connection diagnostic:\n{_format_connection_diagnostic(diagnostic)}"
        )

    _launch_chrome_for_cdp(args, cwd)
    deadline = time.time() + 45
    last_check = initial_check
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        last_status = _cdp_json_version_status(args, cwd)
        if last_status.get("ok") and not getattr(args, "_opened_tab", False):
            if _open_background_cdp_tab(args, "about:blank") and getattr(
                args, "_uses_shared_browser_profile", False
            ):
                visibility = show_browser_without_focus(
                    configured_browser(), int(_cdp_launch_port(args))
                )
                args._frontmost_pid_before_launch = visibility.get(
                    "restored_frontmost_pid"
                )
        last_check = _agent_connection_check(args, cwd)
        if last_check["ok"]:
            return
        time.sleep(1)
    diagnostic = _connection_diagnostic(args, cwd, last_check)
    diagnostic["last_cdp_json_version"] = last_status
    raise RuntimeError(
        "Chrome launched for CDP, but agent-browser could not connect to it.\n"
        f"Connection diagnostic:\n{_format_connection_diagnostic(diagnostic)}"
    )


def _screenshot(args: argparse.Namespace, cwd: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    if _owned_tab_cdp_url(args):
        try:
            with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=120) as conn:
                conn.call("Page.enable")
                result = conn.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
            output.write_bytes(base64.b64decode(str(result.get("data") or "")))
            return str(output)
        except Exception as error:
            args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
            raise RuntimeError(f"Owned CDP screenshot failed without using focus-stealing fallback: {error}") from error
    return _call_agent(args, cwd, ["screenshot", str(output)], timeout=120)


def _snapshot(args: argparse.Namespace, cwd: Path) -> str:
    return _call_agent(args, cwd, ["snapshot", "-i", "-c"], timeout=120)


def _eval_js(args: argparse.Namespace, cwd: Path, script: str, timeout: int = 120) -> str:
    ok, output = _try_cdp_eval_js(args, script, timeout=timeout)
    if ok:
        return output
    if _owned_tab_cdp_url(args):
        raise RuntimeError(
            "Owned CDP evaluation failed; refusing agent-browser fallback because it can steal focus. "
            f"last_error={getattr(args, '_owned_tab_cdp_error', '')}"
        )
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return _call_agent(args, cwd, ["eval", "-b", encoded], timeout=timeout)


def _eval_json(args: argparse.Namespace, cwd: Path, script: str, timeout: int = 120) -> dict[str, Any]:
    output = _eval_js(args, cwd, script, timeout=timeout).strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("{")
        end = output.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(output[start : end + 1])
        else:
            raise
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    return parsed if isinstance(parsed, dict) else {}


def _page_text(args: argparse.Namespace, cwd: Path) -> str:
    return _eval_js(args, cwd, "document.body.innerText", timeout=60)


def _is_busy(text: str) -> bool:
    return any(marker in text for marker in BUSY_MARKERS)


def _generation_error_from_text(text: str) -> dict[str, str] | None:
    normalized = " ".join((text or "").split())
    lowered = normalized.lower()
    matched = next(
        (marker for marker in GENERATION_FAILED_MARKERS if marker.lower() in lowered),
        "",
    )
    if not matched:
        return None
    return {
        "error_type": "chatgpt_generation_error",
        "matched_marker": matched,
        "message": normalized[-800:],
    }


def _has_generation_failed(text: str) -> bool:
    return _generation_error_from_text(text) is not None


def _reference_upload_failure(text: str, references: list[str]) -> dict[str, Any] | None:
    normalized = " ".join((text or "").split())
    lowered = normalized.lower()
    matched = next(
        (
            marker
            for marker in REFERENCE_UPLOAD_FAILED_MARKERS
            if marker.lower() in lowered
        ),
        "",
    )
    if not matched:
        return None
    failed_names = [
        Path(reference).name
        for reference in references
        if Path(reference).name and Path(reference).name in normalized
    ]
    return {
        "status": "reference_upload_failed",
        "error_type": "reference_upload_failed",
        "retryable": True,
        "terminal": True,
        "upload_failure_marker": matched,
        "failed_reference_names": failed_names,
        "reference_count": len(references),
        "upload_failure_message": normalized[:600],
        "recommended_next_action": "fresh_conversation_with_reupload",
    }


def _content_policy_refusal(text: str) -> dict[str, Any] | None:
    normalized = " ".join((text or "").split())
    lowered = normalized.lower()
    matched = next((marker for marker in POLICY_REFUSAL_MARKERS if marker in lowered), "")
    if not matched:
        return None
    return {
        "status": "policy_refused",
        "error_type": "content_policy_refusal",
        "retryable": False,
        "terminal": True,
        "policy_refusal_marker": matched,
        "policy_refusal_message": normalized[:600],
        "recommended_next_action": "revise_prompt_safety_boundary",
    }


GENERATION_BUTTON_STATE_JS = r"""
(() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const labelFor = (button) => [
    button.getAttribute("aria-label"),
    button.getAttribute("data-testid"),
    button.getAttribute("title"),
    button.innerText,
    button.textContent,
    button.type,
  ].filter(Boolean).join(" ");
  const buttons = Array.from(document.querySelectorAll("button,[role='button']")).filter(visible);
  const stopButton = buttons.find((button) => {
    const label = labelFor(button);
    return /stop|停止|中止/i.test(label) && /generat|stream|answer|生成|回答|输出/i.test(label);
  }) || null;
  const downloadButtons = buttons.filter((button) => {
    const label = labelFor(button);
    if (!/download|下载/i.test(label)) {
      return false;
    }
    return !button.closest('form') && !button.closest('[contenteditable="true"]');
  });
  const sendButton = buttons.find((button) => /send|submit|发送|提交/i.test(labelFor(button)))
    || buttons.find((button) => /send-button|composer-submit/i.test(labelFor(button)))
    || buttons.find((button) => button.type === "submit")
    || null;
  const editor = document.querySelector("[contenteditable='true']");
  const sendDisabled = sendButton
    ? Boolean(sendButton.disabled || sendButton.getAttribute("aria-disabled") === "true" || sendButton.closest("[aria-disabled='true']"))
    : true;
  return JSON.stringify({
    checked: true,
    composerVisible: Boolean(editor && visible(editor)),
    stopButtonVisible: Boolean(stopButton),
    stopButtonLabel: stopButton ? labelFor(stopButton) : "",
    sendButtonVisible: Boolean(sendButton),
    sendButtonDisabled: sendDisabled,
    sendButtonLabel: sendButton ? labelFor(sendButton) : "",
    downloadButtonVisible: downloadButtons.length > 0,
    downloadButtonCount: downloadButtons.length,
    downloadButtonLabels: downloadButtons.slice(0, 5).map(labelFor),
    generationActive: Boolean(stopButton),
    readyForNextPrompt: Boolean(editor && visible(editor) && !stopButton)
  });
})()
"""


def _generation_button_state(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    try:
        state = _eval_json(args, cwd, GENERATION_BUTTON_STATE_JS, timeout=60)
        if state.get("checked"):
            return state
    except Exception as error:
        return {
            "checked": False,
            "generationActive": True,
            "readyForNextPrompt": False,
            "error": f"{type(error).__name__}: {error}",
        }
    return {"checked": False, "generationActive": True, "readyForNextPrompt": False}


def _checked_generation_button_state(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    state = _generation_button_state(args, cwd)
    if not state.get("checked") and state.get("error"):
        raise RecoverablePageError(
            "ChatGPT generation heartbeat could not read the composer controls: "
            f"{state['error']}"
        )
    return state


def _generation_active_from_button_state(state: dict[str, Any]) -> bool:
    if not state.get("checked"):
        return True
    if state.get("generationActive") or state.get("stopButtonVisible"):
        return True
    return not bool(state.get("readyForNextPrompt"))


def _validate_profile(args: argparse.Namespace) -> None:
    return


def _account_guard(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    return {
        "ok": True,
        "required": TARGET_CHATGPT_ACCOUNT_SIGNAL,
        "observed": f"profile={args.profile!r}, user_data_dir={str(Path(args.user_data_dir).expanduser())!r}",
        "verification": "caller_configured_profile",
    }


def _validate_account_lane(
    args: argparse.Namespace,
    cwd: Path,
    job: dict[str, Any],
    label: int | str = "batch",
) -> dict[str, Any]:
    guard = _account_guard(args, cwd)
    if guard["ok"]:
        return guard
    _write_session_patch(
        job,
        {
            "status": "blocked_wrong_chatgpt_account",
            "label": label,
            "agent_browser_profile": _profile_label(args),
            "account_guard": guard,
            "attempt": {
                "action": "account_guard",
                "label": label,
                "mode": "agent_browser",
                "status": "blocked_wrong_chatgpt_account",
                "required": guard["required"],
                "observed": guard["observed"],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "conversation_url": _current_url(args, cwd),
            },
        },
    )
    raise RuntimeError(
        "Wrong ChatGPT account lane for this image job: "
        f"required {guard['required']}, observed {guard['observed']}. "
        f"agent-browser profile must be {_profile_label(args)}."
    )


def _variant_prefix(label: int | str) -> str:
    if isinstance(label, int):
        return f"variant_{label:02d}"
    if isinstance(label, str) and label.isdigit():
        return f"variant_{int(label):02d}"
    return str(label)


def _session_path(job: dict[str, Any], label: int | str = "batch") -> Path:
    if label == "batch" or label is None:
        return Path(job.get("chatgpt_session_path") or Path(job["download_dir"]) / "chatgpt_session.json")
    pattern = job.get("chatgpt_variant_session_path_pattern")
    if pattern:
        return Path(pattern.replace("{NN}", f"{int(label):02d}"))
    return Path(job["download_dir"]) / f"{_variant_prefix(label)}_chatgpt_session.json"


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write_session_patch(job: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    label = patch.get("label", "batch")
    path = _session_path(job, label)
    previous = _read_json_if_exists(path) or {}
    attempts = previous.get("attempts") or []
    if patch.get("attempt"):
        attempts = [*attempts, patch["attempt"]]
    session = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "job_name": job["job_name"],
        "prompt_card": job["prompt_card"],
        "download_dir": job["download_dir"],
        "suggested_conversation_title": job.get("suggested_conversation_title"),
        **previous,
        **patch,
        "label": label,
        "attempts": attempts,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    session.pop("attempt", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return session


def _conversation_source_session(session_path: Path) -> dict[str, Any]:
    session = _read_json_if_exists(session_path)
    conversation_url = str((session or {}).get("conversation_url") or "")
    if not _conversation_id_from_url(conversation_url):
        raise ValueError(
            "Conversation follow-up requires a source session with a valid "
            f"ChatGPT conversation_url: {session_path}"
        )
    return {
        "session_path": str(session_path),
        "conversation_id": _conversation_id_from_url(conversation_url),
        "conversation_url": conversation_url,
        "job_name": session.get("job_name"),
        "reference_upload_order": session.get("reference_upload_order") or [],
        "ordered_upload_paths": session.get("ordered_upload_paths") or [],
        "model_reference_map": session.get("model_reference_map") or [],
        "compiled_model_reference_map": session.get("compiled_model_reference_map") or "",
        "chat_surface": session.get("chat_surface"),
    }


def _submit_throttle_state_path() -> Path:
    return resolve_tool_state_root("chatgpt-web") / "submit_throttle.json"


def _submit_throttle_lock_path() -> Path:
    return _submit_throttle_state_path().with_suffix(".lockdir")


@contextlib.contextmanager
def _submit_throttle_lock(timeout_s: int = 120):
    lock_path = _submit_throttle_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > SUBMIT_THROTTLE_LOCK_STALE_SECONDS:
                shutil.rmtree(lock_path, ignore_errors=True)
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for ChatGPT submit throttle lock: {lock_path}")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock_path, ignore_errors=True)


def _submit_throttle_limits(args: argparse.Namespace) -> dict[str, int]:
    return {
        "min_interval_seconds": max(0, int(getattr(args, "submit_throttle_min_interval", 0))),
        "max_submits_per_hour": max(0, int(getattr(args, "submit_throttle_max_submits_per_hour", 0))),
        "max_expected_images_per_hour": max(0, int(getattr(args, "submit_throttle_max_expected_images_per_hour", 0))),
    }


def _submit_throttle_wait_seconds(
    events: list[dict[str, Any]],
    *,
    now: float,
    expected_count: int,
    min_interval_seconds: int,
    max_submits_per_hour: int,
    max_expected_images_per_hour: int,
) -> tuple[float, dict[str, Any]]:
    window_start = now - SUBMIT_THROTTLE_WINDOW_SECONDS
    recent = [
        event
        for event in events
        if float(event.get("at_epoch") or 0) > window_start
    ]
    wait_until = now
    reasons: list[str] = []
    if recent and min_interval_seconds > 0:
        last_at = max(float(event.get("at_epoch") or 0) for event in recent)
        if now < last_at + min_interval_seconds:
            wait_until = max(wait_until, last_at + min_interval_seconds)
            reasons.append("min_submit_interval")
    if max_submits_per_hour > 0 and len(recent) >= max_submits_per_hour:
        ordered = sorted(float(event.get("at_epoch") or 0) for event in recent)
        wait_until = max(wait_until, ordered[-max_submits_per_hour] + SUBMIT_THROTTLE_WINDOW_SECONDS)
        reasons.append("hourly_submit_cap")
    expected_count = max(1, int(expected_count or 1))
    if max_expected_images_per_hour > 0:
        total = sum(max(1, int(event.get("expected_image_count") or 1)) for event in recent)
        if total + expected_count > max_expected_images_per_hour:
            running = total
            for event in sorted(recent, key=lambda item: float(item.get("at_epoch") or 0)):
                running -= max(1, int(event.get("expected_image_count") or 1))
                if running == 0 or running + expected_count <= max_expected_images_per_hour:
                    wait_until = max(wait_until, float(event.get("at_epoch") or 0) + SUBMIT_THROTTLE_WINDOW_SECONDS)
                    reasons.append("hourly_expected_image_cap")
                    break
    wait_seconds = max(0.0, wait_until - now)
    return wait_seconds, {
        "window_seconds": SUBMIT_THROTTLE_WINDOW_SECONDS,
        "recent_submit_count": len(recent),
        "recent_expected_image_count": sum(max(1, int(event.get("expected_image_count") or 1)) for event in recent),
        "requested_expected_image_count": expected_count,
        "wait_reasons": sorted(set(reasons)),
        "next_slot_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wait_until)) if wait_seconds > 0 else None,
    }


def _emit_submit_throttle_progress(
    job: dict[str, Any],
    *,
    label: int | str,
    expected_count: int,
    wait_seconds: float,
    throttle: dict[str, Any],
) -> None:
    progress = {
        "event": "chatgpt_web_progress",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "job_name": job.get("job_name"),
        "label": label,
        "phase": "submit_throttle",
        "status": "waiting_for_submit_slot",
        "expected_image_count": expected_count,
        "throttle_wait_seconds": round(wait_seconds, 1),
        "throttle": throttle,
    }
    progress_path = Path(job["download_dir"]) / "chatgpt_progress.jsonl"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(progress, ensure_ascii=False) + "\n")
    print(json.dumps(progress, ensure_ascii=False), file=sys.stderr, flush=True)
    _write_session_patch(job, {"label": label, "status": "waiting_for_submit_throttle", "progress": progress})


def _wait_for_submit_throttle_slot(
    args: argparse.Namespace,
    job: dict[str, Any],
    *,
    label: int | str,
    expected_count: int,
) -> dict[str, Any]:
    if getattr(args, "no_submit_throttle", False):
        return {"enabled": False, "reason": "disabled"}
    limits = _submit_throttle_limits(args)
    if not any(limits.values()):
        return {"enabled": False, "reason": "no_limits"}
    state_path = _submit_throttle_state_path()
    while True:
        with _submit_throttle_lock():
            state = _read_json_if_exists(state_path) or {"schema_version": 1, "events": []}
            events = [event for event in state.get("events", []) if isinstance(event, dict)]
            now = time.time()
            wait_seconds, throttle = _submit_throttle_wait_seconds(
                events,
                now=now,
                expected_count=expected_count,
                **limits,
            )
            if wait_seconds <= 0:
                event = {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "at_epoch": now,
                    "pid": os.getpid(),
                    "route": "agent_browser",
                    "job_name": job.get("job_name"),
                    "label": label,
                    "download_dir": job.get("download_dir"),
                    "expected_image_count": max(1, int(expected_count or 1)),
                }
                window_start = now - SUBMIT_THROTTLE_WINDOW_SECONDS
                state.update(
                    {
                        "schema_version": 1,
                        "updated_at": event["at"],
                        "limits": limits,
                        "events": [
                            event,
                            *[
                                existing
                                for existing in events
                                if float(existing.get("at_epoch") or 0) > window_start
                            ],
                        ][:500],
                    }
                )
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return {"enabled": True, "reserved": True, "event": event, "limits": limits, **throttle}
        _emit_submit_throttle_progress(
            job,
            label=label,
            expected_count=max(1, int(expected_count or 1)),
            wait_seconds=wait_seconds,
            throttle={**throttle, "limits": limits},
        )
        time.sleep(min(20, max(1, wait_seconds)))


def _timing_metrics_path() -> Path:
    return resolve_tool_state_root("chatgpt-web") / "timing.json"


def _read_timing_samples() -> list[dict[str, Any]]:
    data = _read_json_if_exists(_timing_metrics_path())
    samples = data.get("samples") if isinstance(data, dict) else None
    return samples if isinstance(samples, list) else []


def _write_timing_sample(expected_count: int, elapsed_seconds: float, image_count: int) -> None:
    if elapsed_seconds <= 0 or image_count <= 0:
        return
    path = _timing_metrics_path()
    samples = _read_timing_samples()
    samples.append(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expected_image_count": expected_count,
            "image_count": image_count,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "seconds_per_image": round(elapsed_seconds / max(1, image_count), 1),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "samples": samples[-MAX_TIMING_SAMPLES:]}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _estimated_total_seconds(expected_count: int) -> float | None:
    samples = _read_timing_samples()
    seconds_per_image = [
        float(sample.get("seconds_per_image") or 0)
        for sample in samples
        if isinstance(sample, dict) and float(sample.get("seconds_per_image") or 0) > 0
    ]
    if not seconds_per_image:
        return None
    return (sum(seconds_per_image) / len(seconds_per_image)) * max(1, expected_count)


def _emit_progress(
    job: dict[str, Any],
    *,
    label: int | str,
    phase: str,
    status: str,
    started_at: float,
    expected_count: int,
    recognized_count: int = 0,
    downloaded_count: int = 0,
    retry_index: int = 0,
    page_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elapsed = max(0.0, time.time() - started_at)
    estimated_total = _estimated_total_seconds(expected_count)
    progress = {
        "event": "chatgpt_web_progress",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "job_name": job.get("job_name"),
        "label": label,
        "phase": phase,
        "status": status,
        "elapsed_seconds": round(elapsed, 1),
        "expected_image_count": expected_count,
        "recognized_candidate_count": recognized_count,
        "downloaded_count": downloaded_count,
        "retry_index": retry_index,
        "estimated_total_seconds": round(estimated_total, 1) if estimated_total else None,
        "estimated_remaining_seconds": round(max(0.0, estimated_total - elapsed), 1) if estimated_total else None,
        **({"page_health": page_health} if page_health is not None else {}),
    }
    progress_path = Path(job["download_dir"]) / "chatgpt_progress.jsonl"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(progress, ensure_ascii=False) + "\n")
    print(json.dumps(progress, ensure_ascii=False), file=sys.stderr, flush=True)
    _write_session_patch(job, {"label": label, "status": status, "progress": progress})
    return progress


def _generation_page_health(
    args: argparse.Namespace,
    cwd: Path,
    expected_conversation_url: str | None,
    page_text: str,
) -> dict[str, Any]:
    observation = _eval_json(
        args,
        cwd,
        r"""
JSON.stringify((() => {
  const assistants = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  const latest = assistants.length ? assistants[assistants.length - 1] : null;
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));
  const latestTurn = turns.length ? turns[turns.length - 1] : (latest ? latest.closest("article,section") : null);
  const belongsToCurrentTurn = (el) => {
    const owner = el.closest('section[data-testid^="conversation-turn-"]');
    return !owner || owner === latestTurn;
  };
  const labelParts = (el) => [
    el.getAttribute("aria-label"),
    el.getAttribute("title"),
    el.innerText,
    el.textContent,
  ].filter(Boolean).map((value) => value.trim()).filter(Boolean);
  const retryPattern = /^(try again|retry|再试一次|重试)$/i;
  const retryControls = Array.from(document.querySelectorAll("button,[role='button'],a"))
    .filter(visible)
    .filter(belongsToCurrentTurn)
    .filter((control) => labelParts(control).some((label) => retryPattern.test(label)));
  const errorSurfaceTexts = Array.from(document.querySelectorAll(
    '[role="alert"],[aria-live="assertive"],[data-testid*="error" i],[class*="toast" i]'
  ))
    .filter(visible)
    .filter(belongsToCurrentTurn)
    .map((el) => (el.innerText || el.textContent || "").trim())
    .filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index)
    .slice(0, 8);
  const hasComposer = Array.from(document.querySelectorAll('[contenteditable="true"]')).some(visible);
  const challengeFrame = Array.from(document.querySelectorAll('iframe'))
    .some((frame) => /captcha|challenge|turnstile|verify|cloudflare/i.test(`${frame.src} ${frame.title}`));
  const pageText = (document.body?.innerText || "").slice(0, 6000).toLowerCase();
  const challengeText = [
    "verify you are human", "verifying you are human", "confirm you are human",
    "checking your browser before accessing", "performing security verification",
    "complete the security check", "complete the challenge", "cloudflare ray id",
    "验证码", "安全验证", "人机验证", "验证您是真人"
  ].some((marker) => pageText.includes(marker));
  return {
    href: window.location.href,
    userMessageCount: document.querySelectorAll('[data-message-author-role="user"]').length,
    assistantMessageCount: assistants.length,
    latestAssistantMessage: latest ? (latest.innerText || latest.textContent || "") : "",
    latestAssistantIsCurrent: Boolean(latest && (!latestTurn || latestTurn.contains(latest))),
    latestTurnText: latestTurn ? (latestTurn.innerText || latestTurn.textContent || "") : "",
    retryControlLabels: retryControls.map((control) => labelParts(control)[0] || "").filter(Boolean),
    errorSurfaceTexts,
    hasComposer,
    challengeFrame,
    humanReason: challengeFrame || challengeText ? "anti_automation_verification" : "",
  };
})())
""".strip(),
        timeout=30,
    )
    current_url = str(observation.get("href") or "")
    current_id = _conversation_id_from_url(current_url)
    expected_id = _conversation_id_from_url(expected_conversation_url or "")
    latest_assistant = (
        str(observation.get("latestAssistantMessage") or "")
        if observation.get("latestAssistantIsCurrent", True)
        else ""
    )
    latest_turn = str(observation.get("latestTurnText") or "")
    error_surface_texts = [
        str(value)
        for value in observation.get("errorSurfaceTexts") or []
        if str(value).strip()
    ]
    retry_control_labels = [
        str(value)
        for value in observation.get("retryControlLabels") or []
        if str(value).strip()
    ]
    scoped_error_text = "\n".join([latest_assistant, latest_turn, *error_surface_texts]).strip()
    generation_error = _generation_error_from_text(scoped_error_text)
    if not generation_error and retry_control_labels:
        generation_error = {
            "error_type": "chatgpt_generation_error",
            "matched_marker": retry_control_labels[0],
            "message": (scoped_error_text or retry_control_labels[0])[-800:],
        }
    if not generation_error and not scoped_error_text:
        generation_error = _generation_error_from_text(page_text)
    conversation_ok = bool(
        current_id
        and (
            not expected_id
            or current_id == expected_id
            or (_temporary_conversation_id(expected_id) and not _temporary_conversation_id(current_id))
        )
    )
    status = "generation_error" if generation_error else ("ok" if conversation_ok else "unexpected_page")
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "conversation_ok": conversation_ok,
        "has_composer": bool(observation.get("hasComposer")),
        "challenge_frame": bool(observation.get("challengeFrame")),
        "human_reason": str(observation.get("humanReason") or ""),
        "user_message_count": max(0, int(observation.get("userMessageCount") or 0)),
        "assistant_message_count": max(0, int(observation.get("assistantMessageCount") or 0)),
        "generation_active": _is_busy(page_text),
        "latest_turn_excerpt": latest_turn[-500:],
        "retry_control_labels": retry_control_labels,
        "error_surface_texts": error_surface_texts,
        **({"generation_error": generation_error} if generation_error else {}),
    }


def _missing_submitted_turn_error(
    page_health: dict[str, Any],
    *,
    baseline_user_message_count: int | None,
    candidate_count: int,
    consecutive_heartbeats: int,
) -> dict[str, str] | None:
    expected_minimum = max(1, int(baseline_user_message_count or 0) + 1)
    actual_count = max(0, int(page_health.get("user_message_count") or 0))
    submitted_turn_present = actual_count >= expected_minimum
    missing = bool(
        page_health.get("conversation_ok")
        and page_health.get("has_composer")
        and not page_health.get("challenge_frame")
        and not page_health.get("generation_active")
        and candidate_count == 0
        and not submitted_turn_present
    )
    page_health.update(
        {
            "expected_minimum_user_message_count": expected_minimum,
            "submitted_turn_present": submitted_turn_present,
            "submitted_turn_missing": missing,
            "missing_submitted_turn_heartbeats": consecutive_heartbeats if missing else 0,
        }
    )
    if not missing or consecutive_heartbeats < MISSING_SUBMITTED_TURN_HEARTBEATS:
        return None
    return {
        "error_type": "chatgpt_submitted_turn_missing",
        "matched_marker": "submitted_turn_missing",
        "message": (
            "The submitted ChatGPT turn disappeared from the expected conversation: "
            f"expected at least {expected_minimum} user message(s), found {actual_count} "
            f"for {consecutive_heartbeats} consecutive page-health heartbeats."
        ),
    }


def _conversation_id_from_url(url: str) -> str | None:
    match = re.search(r"https://chatgpt\.com/c/([^/?#]+)|/c/([^/?#]+)", url or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def _conversation_href_from_url(url: str) -> str | None:
    conversation_id = _conversation_id_from_url(url)
    return f"/c/{conversation_id}" if conversation_id else None


def _temporary_conversation_id(conversation_id: str | None) -> bool:
    return bool(conversation_id and conversation_id.startswith("WEB:"))


def _current_url(args: argparse.Namespace, cwd: Path) -> str:
    ok, current = _try_cdp_eval_js(args, "window.location.href", timeout=30)
    if ok and current:
        return current.strip()
    if _owned_tab_cdp_url(args):
        raise RuntimeError(
            "Owned CDP current URL read failed; refusing agent-browser fallback because it can steal focus. "
            f"last_error={getattr(args, '_owned_tab_cdp_error', '')}"
        )
    return _call_agent(args, cwd, ["get", "url"], timeout=60).strip()


def _wait_for_conversation_url(args: argparse.Namespace, cwd: Path, timeout_s: int = 60) -> str:
    deadline = time.time() + timeout_s
    current = _current_url(args, cwd)
    while time.time() < deadline:
        current = _current_url(args, cwd)
        if _conversation_id_from_url(current):
            return current
        time.sleep(1)
    return current


def _open_url(args: argparse.Namespace, cwd: Path, url: str) -> None:
    if getattr(args, "_opened_tab", False):
        if _navigate_owned_cdp_tab(args, url):
            return
        if _owned_tab_cdp_url(args):
            raise RuntimeError(
                "Could not navigate owned background CDP tab; refusing agent-browser open fallback "
                f"because it can steal focus. last_error={getattr(args, '_owned_tab_cdp_error', '')}"
            )
        _call_agent(args, cwd, ["open", url], timeout=180)
        _capture_owned_tab_cdp_url(args, cwd)
        return

    if _open_background_cdp_tab(args, url):
        return
    if args.cdp:
        raise RuntimeError(
            "Could not open a background CDP tab; refusing agent-browser tab fallback because it can steal focus. "
            f"last_error={getattr(args, '_owned_tab_cdp_error', '')}"
        )

    label = getattr(args, "_tab_label", "")
    if label:
        with _cdp_command_lock(args):
            _run(_agent_browser_base(args) + ["tab", "close", label], cwd=cwd, timeout=30)
            try:
                before_target_ids = {str(target.get("id") or "") for target in _cdp_page_targets(args)}
            except Exception:
                before_target_ids = set()
            result = _run(_agent_browser_base(args) + ["tab", "new", "--label", label, url], cwd=cwd, timeout=180)
            if result.returncode == 0:
                args._opened_tab = True
                _capture_owned_tab_cdp_url(args, cwd, before_target_ids)
                return
            _run(_agent_browser_base(args) + ["tab", label], cwd=cwd, timeout=30)
            args._opened_tab = True
            _capture_owned_tab_cdp_url(args, cwd)

    _call_agent(args, cwd, ["open", url], timeout=180)
    _capture_owned_tab_cdp_url(args, cwd)


def _open_conversation(args: argparse.Namespace, cwd: Path, url: str) -> str:
    _open_url(args, cwd, url)
    _wait_ms(args, cwd, 2500)
    return _wait_for_conversation_url(args, cwd)


def _ensure_expected_conversation(
    args: argparse.Namespace,
    cwd: Path,
    expected_conversation_url: str | None,
    baseline_user_message_count: int | None = None,
) -> dict[str, Any]:
    expected_id = _conversation_id_from_url(expected_conversation_url or "")
    if not expected_id:
        return {
            "checked": False,
            "restored": False,
            "reason": "no_expected_conversation_url",
        }

    event: dict[str, Any] = {
        "checked": True,
        "restored": False,
        "expected_conversation_id": expected_id,
        "expected_conversation_url": expected_conversation_url,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        current_url = _current_url(args, cwd)
    except RuntimeError as error:
        event["previous_error"] = f"{type(error).__name__}: {error}"
        _forget_owned_cdp_target(args)
        restored_url = _open_conversation(args, cwd, expected_conversation_url)
        restored_id = _conversation_id_from_url(restored_url)
        event.update(
            {
                "restored": True,
                "restore_reason": "owned_target_unreadable",
                "current_url": restored_url,
                "current_conversation_id": restored_id,
            }
        )
        if restored_id != expected_id:
            raise RuntimeError(
                "ChatGPT conversation was lost and could not be restored to the expected session: "
                f"expected={expected_conversation_url} restored={restored_url}"
            )
        return event

    current_id = _conversation_id_from_url(current_url)
    event.update({"current_url": current_url, "current_conversation_id": current_id})
    if current_id == expected_id:
        return event

    if _temporary_conversation_id(expected_id) and current_id and not _temporary_conversation_id(current_id):
        message_counts = _conversation_message_counts(args, cwd)
        if (
            baseline_user_message_count is not None
            and message_counts["user_message_count"] > baseline_user_message_count
        ):
            event.update(
                {
                    "canonicalized": True,
                    "restore_reason": "temporary_conversation_url_canonicalized",
                    "previous_conversation_id": expected_id,
                    "message_counts": message_counts,
                }
            )
            return event

    restored_url = _open_conversation(args, cwd, expected_conversation_url)
    restored_id = _conversation_id_from_url(restored_url)
    event.update(
        {
            "restored": True,
            "restore_reason": "conversation_changed",
            "previous_url": current_url,
            "previous_conversation_id": current_id,
            "current_url": restored_url,
            "current_conversation_id": restored_id,
        }
    )
    if restored_id != expected_id:
        raise RuntimeError(
            "ChatGPT conversation changed during generation and could not be restored: "
            f"expected={expected_conversation_url} current={current_url} restored={restored_url}"
        )
    return event


def _persist_canonical_conversation(
    job: dict[str, Any],
    label: int | str,
    event: dict[str, Any],
    fallback_url: str | None,
) -> str | None:
    if not event.get("canonicalized"):
        return fallback_url
    current_url = str(event.get("current_url") or "")
    current_id = _conversation_id_from_url(current_url)
    if not current_id:
        return fallback_url
    _write_session_patch(
        job,
        {
            "label": label,
            "conversation_id": current_id,
            "conversation_url": current_url,
            "conversation_guard": event,
        },
    )
    return current_url


def _stale_generation_refresh_due(
    *,
    generation_active: bool,
    expected_conversation_url: str | None,
    started_at: float,
    last_candidate_growth_at: float | None,
    last_generation_refresh_at: float | None,
    now: float,
    refresh_interval_s: int,
) -> bool:
    if not generation_active or refresh_interval_s <= 0:
        return False
    if not _conversation_id_from_url(expected_conversation_url or ""):
        return False
    last_activity_at = last_candidate_growth_at or started_at
    if now - last_activity_at < refresh_interval_s:
        return False
    if last_generation_refresh_at is not None and now - last_generation_refresh_at < refresh_interval_s:
        return False
    return True


def _refresh_expected_conversation(
    args: argparse.Namespace,
    cwd: Path,
    expected_conversation_url: str,
    *,
    reason: str,
    candidate_count: int,
    expected_count: int,
) -> dict[str, Any]:
    expected_id = _conversation_id_from_url(expected_conversation_url)
    restored_url = _open_conversation(args, cwd, expected_conversation_url)
    restored_id = _conversation_id_from_url(restored_url)
    event = {
        "checked": True,
        "restored": True,
        "restore_reason": reason,
        "expected_conversation_id": expected_id,
        "expected_conversation_url": expected_conversation_url,
        "current_url": restored_url,
        "current_conversation_id": restored_id,
        "recognized_candidate_count": candidate_count,
        "expected_image_count": expected_count,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if restored_id != expected_id:
        raise RuntimeError(
            "ChatGPT generation session refresh did not return to the expected conversation: "
            f"expected={expected_conversation_url} restored={restored_url}"
        )
    return event


def _click_try_again(args: argparse.Namespace, cwd: Path) -> bool:
    if _owned_tab_cdp_url(args):
        script = r"""
(() => {
  const labels = ["Try again", "Retry", "重新生成", "再试一次", "重试"];
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const controls = Array.from(document.querySelectorAll("button,[role='button'],a")).filter(visible);
  const control = controls.find((candidate) => {
    const text = [
      candidate.getAttribute("aria-label"),
      candidate.getAttribute("title"),
      candidate.innerText,
      candidate.textContent,
    ].filter(Boolean).join(" ");
    return labels.some((label) => text.includes(label));
  });
  if (!control) return "";
  control.click();
  return "clicked";
})()
"""
        ok, result = _try_cdp_eval_js(args, script, timeout=60)
        if ok and result.strip() == "clicked":
            _wait_ms(args, cwd, 3000)
            return True
        return False
    for label in ("Try again", "Retry", "重新生成", "再试一次", "重试"):
        try:
            _call_agent(args, cwd, ["find", "text", label, "click"], timeout=120)
            _wait_ms(args, cwd, 3000)
            return True
        except RuntimeError:
            continue
    return False


def _element_ref(line: str) -> str | None:
    match = re.search(r"(@e\d+)", line)
    if match:
        return match.group(1)
    match = re.search(r"\bref=(e\d+)\b", line)
    return f"@{match.group(1)}" if match else None


def _snapshot_with_urls(args: argparse.Namespace, cwd: Path) -> str:
    return _call_agent(args, cwd, ["snapshot", "-i", "-c", "-u"], timeout=120)


def _line_has_exact_conversation_href(line: str, href: str) -> bool:
    escaped = re.escape(href)
    return bool(
        re.search(rf"\burl=https://chatgpt\.com{escaped}(?:[\]\s,]|$)", line)
        or re.search(rf"\burl={escaped}(?:[\]\s,]|$)", line)
        or re.search(rf"\bhref=['\"]{escaped}['\"]", line)
        or re.search(rf"\bhref=['\"]https://chatgpt\.com{escaped}['\"]", line)
    )


def _line_has_any_conversation_href(line: str) -> bool:
    return bool(
        re.search(r"\burl=https://chatgpt\.com/c/[^#\]\s,]+(?:[\]\s,]|$)", line)
        or re.search(r"\burl=/c/[^#\]\s,]+(?:[\]\s,]|$)", line)
        or re.search(r"\bhref=['\"](?:https://chatgpt\.com)?/c/[^#'\"\s]+['\"]", line)
    )


def _find_conversation_options(snapshot: str, conversation_url: str) -> tuple[str | None, bool]:
    href = _conversation_href_from_url(conversation_url)
    if not href:
        return None, False
    lines = snapshot.splitlines()
    link_index = -1
    for index, line in enumerate(lines):
        if _line_has_exact_conversation_href(line, href):
            link_index = index
            break
    if link_index < 0:
        return None, False
    for line in lines[link_index + 1 :]:
        if _line_has_any_conversation_href(line):
            return None, False
        if "Open conversation options" in line:
            return _element_ref(line), "expanded=true" in line
    return None, False


def _open_conversation_options_by_href(
    args: argparse.Namespace,
    cwd: Path,
    conversation_url: str,
) -> dict[str, Any]:
    href = _conversation_href_from_url(conversation_url)
    if not href:
        return {"ok": False, "reason": "conversation_href_not_ready"}
    script = f"""
(() => {{
  const href = {json.dumps(href)};
  const links = Array.from(document.querySelectorAll('a[href]'));
  const link = links.find((candidate) => {{
    const attr = candidate.getAttribute('href') || '';
    if (attr === href) return true;
    try {{
      const url = new URL(candidate.href);
      return url.origin === 'https://chatgpt.com' && url.pathname === href && !url.hash;
    }} catch {{
      return false;
    }}
  }});
  if (!link) {{
    return JSON.stringify({{ ok: false, reason: 'conversation_link_not_found' }});
  }}
  let node = link;
  for (let depth = 0; node && depth < 8; depth += 1) {{
    const buttons = Array.from(node.querySelectorAll ? node.querySelectorAll('button') : []);
    const options = buttons.find((button) => {{
      const label = button.getAttribute('aria-label') || '';
      return label.startsWith('Open conversation options');
    }});
    if (options) {{
      options.click();
      return JSON.stringify({{
        ok: true,
        link_text: link.innerText || '',
        button_label: options.getAttribute('aria-label') || '',
        expanded: options.getAttribute('aria-expanded') || ''
      }});
    }}
    node = node.parentElement;
  }}
  return JSON.stringify({{ ok: false, reason: 'conversation_options_button_not_found', link_text: link.innerText || '' }});
}})()
"""
    return _eval_json(args, cwd, script, timeout=60)


def _conversation_title_by_href(
    args: argparse.Namespace,
    cwd: Path,
    conversation_url: str,
) -> str:
    href = _conversation_href_from_url(conversation_url)
    if not href:
        return ""
    script = f"""
(() => {{
  const href = {json.dumps(href)};
  const link = Array.from(document.querySelectorAll('a[href]')).find((candidate) => {{
    const attr = candidate.getAttribute('href') || '';
    if (attr === href) return true;
    try {{
      const url = new URL(candidate.href);
      return url.origin === 'https://chatgpt.com' && url.pathname === href && !url.hash;
    }} catch {{
      return false;
    }}
  }});
  return JSON.stringify({{ title: link ? (link.innerText || '').trim() : '' }});
}})()
"""
    return str(_eval_json(args, cwd, script, timeout=60).get("title") or "")


def _set_chat_title_editor_value(args: argparse.Namespace, cwd: Path, title: str) -> dict[str, Any]:
    script = f"""
(() => {{
  const title = {json.dumps(title)};
  const selectors = [
    'input[aria-label="Chat title"]',
    'textarea[aria-label="Chat title"]',
    'input[name="title-editor"]',
    'textarea[name="title-editor"]',
    '[name="title-editor"]',
    '[contenteditable="true"][aria-label="Chat title"]'
  ];
  const editor = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
  if (!editor) {{
    return JSON.stringify({{ ok: false, reason: 'title_editor_not_found' }});
  }}
  editor.focus();
  if ('value' in editor) {{
    const prototype = Object.getPrototypeOf(editor);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value')
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
      || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (descriptor && descriptor.set) {{
      descriptor.set.call(editor, title);
    }} else {{
      editor.value = title;
    }}
  }} else {{
    editor.textContent = title;
  }}
  editor.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: title }}));
  editor.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return JSON.stringify({{
    ok: true,
    tag: editor.tagName,
    role: editor.getAttribute('role') || '',
    aria: editor.getAttribute('aria-label') || '',
    value: 'value' in editor ? editor.value : editor.textContent
  }});
}})()
"""
    return _eval_json(args, cwd, script, timeout=60)


def _click_rename_menu_item(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    script = r"""
(() => {
  const visible = (element) => {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const items = Array.from(document.querySelectorAll('[role="menuitem"]')).filter(visible);
  const item = items.find((candidate) => {
    const text = (candidate.innerText || candidate.textContent || '').trim();
    return text === 'Rename' || text === '重命名';
  });
  if (!item) {
    return JSON.stringify({ ok: false, reason: 'rename_menu_item_not_found' });
  }
  item.click();
  return JSON.stringify({ ok: true });
})()
"""
    return _eval_json(args, cwd, script, timeout=60)


def _press_enter_owned_cdp(args: argparse.Namespace) -> None:
    cdp_url = _owned_tab_cdp_url(args)
    if not cdp_url:
        raise RuntimeError("owned CDP target is unavailable")
    try:
        with _CDPConnection.connect(cdp_url, timeout=30) as conn:
            conn.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            )
            conn.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            )
    except Exception as error:
        args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
        raise RuntimeError(f"Could not submit the ChatGPT title editor through owned CDP: {error}") from error


def _rename_current_conversation_owned_cdp(
    args: argparse.Namespace,
    cwd: Path,
    conversation_url: str,
    conversation_id: str,
    title: str,
) -> dict[str, Any]:
    for _ in range(8):
        opened = _open_conversation_options_by_href(args, cwd, conversation_url)
        if not opened.get("ok"):
            _wait_ms(args, cwd, 1000)
            continue

        menu_result: dict[str, Any] = {}
        for _ in range(5):
            menu_result = _click_rename_menu_item(args, cwd)
            if menu_result.get("ok"):
                break
            _wait_ms(args, cwd, 200)
        if not menu_result.get("ok"):
            return {
                "attempted": True,
                "renamed": False,
                "reason": "rename_menu_item_not_found",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
            }

        set_result: dict[str, Any] = {}
        for _ in range(5):
            set_result = _set_chat_title_editor_value(args, cwd, title)
            if set_result.get("ok"):
                break
            _wait_ms(args, cwd, 200)
        if not set_result.get("ok"):
            return {
                "attempted": True,
                "renamed": False,
                "reason": "title_input_not_found",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
            }

        _press_enter_owned_cdp(args)
        actual_title = ""
        for _ in range(5):
            _wait_ms(args, cwd, 300)
            actual_title = _conversation_title_by_href(args, cwd, conversation_url)
            if actual_title == title:
                break
        return {
            "attempted": True,
            "renamed": actual_title == title,
            "reason": "ok" if actual_title == title else "title_not_exact_after_save",
            "conversation_id": conversation_id,
            "conversation_url": conversation_url,
            "actual_title": actual_title,
            "set_result": set_result,
        }

    return {
        "attempted": True,
        "renamed": False,
        "reason": "exact_conversation_item_not_found",
        "conversation_id": conversation_id,
        "conversation_url": conversation_url,
    }


def _rename_current_conversation(
    args: argparse.Namespace,
    job: dict[str, Any],
    cwd: Path,
    conversation_url: str,
) -> dict[str, Any]:
    title = job.get("suggested_conversation_title")
    conversation_id = _conversation_id_from_url(conversation_url)
    if not title or not conversation_id:
        return {
            "attempted": False,
            "renamed": False,
            "reason": "conversation_url_or_title_not_ready",
            "conversation_url": conversation_url,
        }
    if _owned_tab_cdp_url(args):
        try:
            return _rename_current_conversation_owned_cdp(
                args,
                cwd,
                conversation_url,
                conversation_id,
                str(title),
            )
        except RuntimeError as error:
            return {
                "attempted": True,
                "renamed": False,
                "reason": f"rename_failed: {error}",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
            }
    for _ in range(8):
        snapshot = _snapshot_with_urls(args, cwd)
        options_ref, options_expanded = _find_conversation_options(snapshot, conversation_url)
        if not options_ref:
            time.sleep(1)
            continue
        try:
            if options_expanded:
                menu_snapshot = snapshot
            else:
                opened = _open_conversation_options_by_href(args, cwd, conversation_url)
                if not opened.get("ok"):
                    _call_agent(args, cwd, ["click", options_ref], timeout=120)
                _call_agent(args, cwd, ["wait", "1000"], timeout=60)
                menu_snapshot = _snapshot(args, cwd)
            rename_ref = next(
                (_element_ref(line) for line in menu_snapshot.splitlines() if "Rename" in line),
                None,
            )
            if not rename_ref:
                try:
                    _call_agent(args, cwd, ["find", "text", "Rename", "click", "--exact"], timeout=120)
                except RuntimeError:
                    trace_dir = Path(job["download_dir"]) / "agent_browser_trace_submit"
                    trace_dir.mkdir(parents=True, exist_ok=True)
                    (trace_dir / "rename_menu_not_found.snapshot.txt").write_text(
                        menu_snapshot,
                        encoding="utf-8",
                    )
                    return {
                        "attempted": True,
                        "renamed": False,
                        "reason": "rename_menu_item_not_found",
                        "conversation_id": conversation_id,
                        "conversation_url": conversation_url,
                    }
            else:
                _call_agent(args, cwd, ["click", rename_ref], timeout=120)
            _call_agent(args, cwd, ["wait", "1000"], timeout=60)
            input_snapshot = _snapshot(args, cwd)
            input_ref = next(
                (
                    _element_ref(line)
                    for line in input_snapshot.splitlines()
                    if "Chat title" in line or "title-editor" in line
                ),
                None,
            )
            if not input_ref:
                return {
                    "attempted": True,
                    "renamed": False,
                    "reason": "title_input_not_found",
                    "conversation_id": conversation_id,
                    "conversation_url": conversation_url,
                }
            set_result = _set_chat_title_editor_value(args, cwd, title)
            if not set_result.get("ok"):
                _call_agent(args, cwd, ["click", input_ref], timeout=120)
                _safe_agent(args, cwd, ["press", "Meta+a"], timeout=60)
                _safe_agent(args, cwd, ["press", "Control+a"], timeout=60)
                _safe_agent(args, cwd, ["press", "Backspace"], timeout=60)
                _call_agent(args, cwd, ["keyboard", "inserttext", title], timeout=120)
            _call_agent(args, cwd, ["press", "Enter"], timeout=60)
            _call_agent(args, cwd, ["wait", "1500"], timeout=60)
            actual_title = _conversation_title_by_href(args, cwd, conversation_url)
            return {
                "attempted": True,
                "renamed": actual_title == title,
                "reason": "ok" if actual_title == title else "title_not_exact_after_save",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
                "actual_title": actual_title,
                "set_result": set_result,
            }
        except RuntimeError as error:
            return {
                "attempted": True,
                "renamed": False,
                "reason": f"rename_failed: {error}",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
            }
    return {
        "attempted": True,
        "renamed": False,
        "reason": "exact_conversation_item_not_found",
        "conversation_id": conversation_id,
        "conversation_url": conversation_url,
    }


def _retry_conversation_rename(
    args: argparse.Namespace,
    job: dict[str, Any],
    cwd: Path,
    conversation_url: str,
    previous: dict[str, Any],
) -> dict[str, Any]:
    if previous.get("renamed"):
        return previous
    retried = _rename_current_conversation(args, job, cwd, conversation_url)
    return {
        **retried,
        "retried_after_generation": True,
        "previous_reason": previous.get("reason"),
    }


def _open_new_chat(args: argparse.Namespace, cwd: Path) -> None:
    _open_url(args, cwd, "https://chatgpt.com/")
    _wait_ms(args, cwd, 2000)
    if _owned_tab_cdp_url(args):
        script = r"""
(() => {
  const editor = document.querySelector("[contenteditable='true']");
  if (editor) return "composer_ready";
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const controls = Array.from(document.querySelectorAll("a,button,[role='button']")).filter(visible);
  const control = controls.find((candidate) => {
    const text = [
      candidate.getAttribute("aria-label"),
      candidate.getAttribute("title"),
      candidate.innerText,
      candidate.textContent,
    ].filter(Boolean).join(" ");
    return /\bNew chat\b|新聊天|新建聊天/i.test(text);
  });
  if (!control) return "";
  control.click();
  return "clicked";
})()
"""
        ok, _ = _try_cdp_eval_js(args, script, timeout=60)
        _wait_ms(args, cwd, 2000)
        return
    _safe_agent(args, cwd, ["find", "text", "New chat", "click", "--exact"], timeout=120)
    _wait_ms(args, cwd, 2000)


def _dispatch_owned_tab_click(
    args: argparse.Namespace,
    cwd: Path,
    control: dict[str, Any],
) -> bool:
    if not control.get("found"):
        return False
    x = float(control.get("x") or 0)
    y = float(control.get("y") or 0)
    if x <= 0 or y <= 0:
        return False
    if _owned_tab_cdp_url(args):
        with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=30) as conn:
            conn.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            conn.call(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
            conn.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
        return True
    clicked = _eval_json(
        args,
        cwd,
        f"""
JSON.stringify((() => {{
  const control = document.elementFromPoint({x}, {y});
  if (!control) return {{clicked: false}};
  control.click();
  return {{clicked: true}};
}})())
""".strip(),
        timeout=60,
    )
    return bool(clicked.get("clicked"))


CHAT_SURFACE_STATE_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const textFor = (el) => String(el?.innerText || el?.textContent || "").replace(/\s+/g, " ").trim();
  const groups = Array.from(document.querySelectorAll("[role='radiogroup']")).filter(visible);
  const group = groups.find((el) => /^Select chat surface$/i.test(el.getAttribute("aria-label") || "")) || null;
  if (!group) {
    return {control_available: false, selected_surface: "unknown"};
  }
  const radios = Array.from(group.querySelectorAll("button[role='radio'],[role='radio']")).filter(visible);
  const chat = group.querySelector("[data-tpp-toggle-value='chatgpt']")
    || radios.find((el) => /^Chat$/i.test(textFor(el)))
    || null;
  const work = group.querySelector("[data-tpp-toggle-value='work']")
    || radios.find((el) => /^Work$/i.test(textFor(el)))
    || null;
  const selected = (el) => Boolean(el)
    && (el.getAttribute("aria-checked") === "true" || el.getAttribute("data-state") === "on");
  const control = (el) => {
    if (!el || !visible(el)) return {found: false};
    const rect = el.getBoundingClientRect();
    return {
      found: true,
      label: textFor(el),
      aria_checked: el.getAttribute("aria-checked"),
      data_state: el.getAttribute("data-state"),
      value: el.getAttribute("data-tpp-toggle-value"),
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    };
  };
  const chatSelected = selected(chat);
  const workSelected = selected(work);
  return {
    control_available: true,
    group_label: group.getAttribute("aria-label"),
    chat_selected: chatSelected,
    work_selected: workSelected,
    selected_surface: chatSelected && !workSelected ? "chat" : (workSelected && !chatSelected ? "work" : "unknown"),
    chat_control: control(chat),
    work_control: control(work),
  };
})())
"""


def _recorded_chat_surface_is_verified(record: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(record, dict)
        and record.get("verified")
        and record.get("selected_surface") == "chat"
    )


def _chat_surface_state(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    return _eval_json(args, cwd, CHAT_SURFACE_STATE_JS, timeout=60)


def _ensure_chat_surface(
    args: argparse.Namespace,
    cwd: Path,
    *,
    recorded: dict[str, Any] | None = None,
    timeout_s: float = 10,
) -> dict[str, Any]:
    deadline = time.time() + max(0, timeout_s)
    initial: dict[str, Any] = {}
    while True:
        initial = _chat_surface_state(args, cwd)
        if initial.get("control_available"):
            break
        if _recorded_chat_surface_is_verified(recorded):
            return {
                "requested_surface": "chat",
                "status": "verified_from_session",
                "verified": True,
                "control_available": False,
                "clicked_chat": False,
                "selected_surface": "chat",
                "evidence_source": "recorded_session",
                "origin_evidence_source": recorded.get("origin_evidence_source")
                or recorded.get("evidence_source"),
                "origin_status": recorded.get("origin_status") or recorded.get("status"),
            }
        if time.time() >= deadline:
            raise ChatSurfaceSelectionError(
                "ChatGPT's Chat/Work selector is not visible and this conversation has no "
                "previously verified Chat-surface record. Refusing to submit an image request."
            )
        _wait_ms(args, cwd, 250)

    if initial.get("selected_surface") == "chat":
        return {
            "requested_surface": "chat",
            "status": "already_selected",
            "verified": True,
            "control_available": True,
            "clicked_chat": False,
            "selected_surface": "chat",
            "evidence_source": "live_selector",
            "initial": initial,
            "verification": initial,
        }

    clicked = _dispatch_owned_tab_click(args, cwd, initial.get("chat_control") or {})
    if clicked:
        _wait_ms(args, cwd, 750)
    verification = _chat_surface_state(args, cwd)
    verified = bool(
        verification.get("control_available")
        and verification.get("selected_surface") == "chat"
        and verification.get("chat_selected")
        and not verification.get("work_selected")
    )
    if not verified:
        raise ChatSurfaceSelectionError(
            "ChatGPT Chat surface could not be selected and verified. "
            f"clicked_chat={clicked} initial={json.dumps(initial, ensure_ascii=False)} "
            f"verification={json.dumps(verification, ensure_ascii=False)}"
        )
    return {
        "requested_surface": "chat",
        "status": "selected",
        "verified": True,
        "control_available": True,
        "clicked_chat": bool(clicked),
        "selected_surface": "chat",
        "evidence_source": "live_selector",
        "initial": initial,
        "verification": verification,
    }


IMAGE_MODE_STATE_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const textFor = (el) => [
    el.getAttribute("aria-label"), el.getAttribute("title"),
    el.getAttribute("data-placeholder"), el.getAttribute("aria-placeholder"),
    el.getAttribute("placeholder"), el.innerText, el.textContent
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const imageModeText = /\b(?:Create|Generate) (?:an )?image\b|创建图片|生成图片/i;
  const exactImageModeText = /^(?:Create|Generate) (?:an )?image$|^创建图片$|^生成图片$/i;
  const editors = Array.from(document.querySelectorAll("[contenteditable='true']")).filter(visible);
  const editorText = editors.map(textFor).join(" ").trim();
  const placeholderText = editors.flatMap((editor) => [
    editor,
    ...Array.from(editor.querySelectorAll("[data-placeholder],[aria-placeholder],[placeholder]"))
  ]).map(textFor).filter(Boolean).join(" ").trim();
  const composer = editors[0]?.closest("form") || null;
  const selectedControl = composer
    ? Array.from(composer.querySelectorAll(
        "button,[role='button'],[aria-pressed],[aria-selected],[data-state]"
      )).filter(visible).find((el) => {
        if (!imageModeText.test(textFor(el))) return false;
        return el.getAttribute("aria-pressed") === "true"
          || el.getAttribute("aria-selected") === "true"
          || /^(?:on|checked|active|selected)$/i.test(el.getAttribute("data-state") || "");
      })
    : null;
  const hasPromptCategories = Array.from(document.querySelectorAll("[role='tablist']"))
    .filter(visible)
    .some((el) => /Image prompt categories/i.test(el.getAttribute("aria-label") || ""));
  return {
    selected: editors.some((editor) => exactImageModeText.test(textFor(editor)))
      || imageModeText.test(placeholderText)
      || Boolean(selectedControl)
      || hasPromptCategories,
    editorText,
    placeholderText,
    selectedControlLabel: selectedControl ? textFor(selectedControl) : "",
    hasPromptCategories,
  };
})())
""".strip()

CREATE_IMAGE_CONTROL_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const textFor = (el) => [
    el.getAttribute("aria-label"), el.getAttribute("title"),
    el.getAttribute("data-testid"), el.innerText, el.textContent
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const imageAction = /\b(?:Create|Generate) (?:an )?image\b|创建图片|生成图片/i;
  const editor = Array.from(document.querySelectorAll("[contenteditable='true']")).find(visible);
  const composer = editor?.closest("form") || null;
  const raw = Array.from(document.querySelectorAll(
    "[role='menuitem'],[role='option'],button,[role='button'],a,[data-testid],[tabindex]"
  )).filter(visible).filter((el) => !el.matches("[contenteditable='true']"));
  const seen = new Set();
  const controls = raw.map((el) => {
    const interactive = el.matches("button,a,[role='button'],[role='menuitem'],[role='option']")
      ? el
      : el.closest("button,a,[role='button'],[role='menuitem'],[role='option']") || el;
    return interactive;
  }).filter((el) => {
    if (seen.has(el)) return false;
    seen.add(el);
    const label = textFor(el);
    const role = el.getAttribute("role") || "";
    const testId = el.getAttribute("data-testid") || "";
    const inMenu = Boolean(el.closest(
      "[role='menu'],[role='group'],[data-radix-menu-content]," +
      "[data-radix-popper-content-wrapper],.popover"
    ));
    const inComposer = Boolean(composer && composer.contains(el));
    const explicitTestId = /create.*image|image.*create|generate.*image/i.test(testId);
    return label.length <= 240
      && imageAction.test(label)
      && (/menuitem|option/i.test(role) || inMenu || inComposer || explicitTestId);
  });
  const scored = controls.map((el) => {
    const label = textFor(el);
    const role = el.getAttribute("role") || "";
    const testId = el.getAttribute("data-testid") || "";
    let score = 0;
    if (/menuitem|option/i.test(role)) score += 80;
    if (el.closest(
      "[role='menu'],[role='group'],[data-radix-menu-content]," +
      "[data-radix-popper-content-wrapper],.popover"
    )) score += 60;
    if (/create.*image|image.*create|generate.*image/i.test(testId)) score += 40;
    if (/^(?:Create|Generate) (?:an )?image\b/i.test(label)) score += 30;
    score -= Math.min(label.length, 240) / 1000;
    return {el, label, role, testId, score};
  }).sort((a, b) => b.score - a.score);
  const choice = scored[0];
  const control = choice?.el;
  if (!control) return {found: false};
  const rect = control.getBoundingClientRect();
  return {
    found: true,
    label: choice.label,
    role: choice.role,
    testId: choice.testId,
    evidenceSource: choice.role || choice.testId || "visible_text",
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  };
})())
""".strip()

IMAGE_MODE_ADD_MENU_CONTROL_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const textFor = (el) => [
    el.getAttribute("aria-label"), el.getAttribute("title"),
    el.getAttribute("data-testid"), el.innerText, el.textContent
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const editor = Array.from(document.querySelectorAll("[contenteditable='true']")).find(visible);
  const composer = editor?.closest("form") || null;
  if (!editor || !composer) return {found: false, reason: "composer_not_found"};
  const controls = Array.from(composer.querySelectorAll("button,[role='button']")).filter(visible);
  const semantic = controls.find((el) => {
    const label = textFor(el);
    const testId = el.getAttribute("data-testid") || "";
    return /Add files and more|Add (?:photos? (?:and|&) )?files|Attach files|Upload files|Open (?:the )?(?:attachment|tools?) menu|添加文件|上传文件|附件/i.test(label)
      || /attach|upload|file|composer.*plus|plus.*composer|composer.*tool/i.test(testId);
  });
  const editorRect = editor.getBoundingClientRect();
  const fallback = controls.map((el) => ({el, rect: el.getBoundingClientRect(), label: textFor(el)}))
    .filter(({rect, label}) => {
      const centerY = rect.top + rect.height / 2;
      const sameRow = centerY >= editorRect.top - 24 && centerY <= editorRect.bottom + 24;
      const compact = rect.width > 0 && rect.height > 0 && rect.width <= 72 && rect.height <= 72;
      const atLeadingEdge = rect.left <= editorRect.left + 32
        && rect.right >= editorRect.left - 96;
      const excluded = /send|submit|voice|microphone|dictat|model|reasoning/i.test(label);
      return sameRow && compact && atLeadingEdge && !excluded;
    })
    .sort((a, b) => a.rect.left - b.rect.left)[0]?.el || null;
  const control = semantic || fallback;
  if (!control) return {found: false, reason: "add_menu_control_not_found"};
  const rect = control.getBoundingClientRect();
  return {
    found: true,
    label: textFor(control),
    testId: control.getAttribute("data-testid") || "",
    expanded: control.getAttribute("aria-expanded") === "true",
    evidenceSource: semantic ? "semantic_label_or_testid" : "composer_left_edge_button",
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  };
})())
""".strip()


def _image_mode_state(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    return _eval_json(args, cwd, IMAGE_MODE_STATE_JS, timeout=60)


def _find_create_image_control(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    return _eval_json(args, cwd, CREATE_IMAGE_CONTROL_JS, timeout=60)


def _find_image_mode_add_menu_control(
    args: argparse.Namespace,
    cwd: Path,
) -> dict[str, Any]:
    return _eval_json(args, cwd, IMAGE_MODE_ADD_MENU_CONTROL_JS, timeout=60)


def _poll_for_create_image_control(
    args: argparse.Namespace,
    cwd: Path,
    *,
    attempts: int = 12,
) -> dict[str, Any]:
    last = {"found": False}
    for attempt in range(max(1, attempts)):
        last = _find_create_image_control(args, cwd)
        if last.get("found"):
            return last
        if attempt + 1 < attempts:
            _wait_ms(args, cwd, 250)
    return last


def _poll_for_image_mode_selection(
    args: argparse.Namespace,
    cwd: Path,
    *,
    attempts: int = 20,
) -> dict[str, Any]:
    last: dict[str, Any] = {"selected": False}
    for attempt in range(max(1, attempts)):
        last = _image_mode_state(args, cwd)
        if last.get("selected"):
            return last
        if attempt + 1 < attempts:
            _wait_ms(args, cwd, 250)
    return last


def _enable_image_mode_if_available(
    args: argparse.Namespace,
    cwd: Path,
    snapshot: str,
) -> dict[str, Any]:
    del snapshot

    initial = _image_mode_state(args, cwd)
    if initial.get("selected"):
        return {
            "requested": True,
            "status": "already_selected",
            "verified": True,
            "opened_add_menu": False,
            "clicked_create_image": False,
            "verification": initial,
        }

    direct = _find_create_image_control(args, cwd)
    opened_add_menu = False
    clicked = bool(direct.get("found")) and _dispatch_owned_tab_click(args, cwd, direct)
    selected_label = str(direct.get("label") or "")
    menu: dict[str, Any] = {"found": False}
    menu_choice: dict[str, Any] = {"found": False}
    if not clicked:
        menu = _find_image_mode_add_menu_control(args, cwd)
        opened_add_menu = bool(menu.get("expanded")) or (
            bool(menu.get("found")) and _dispatch_owned_tab_click(args, cwd, menu)
        )
        if opened_add_menu:
            menu_choice = _poll_for_create_image_control(args, cwd)
            clicked = bool(menu_choice.get("found")) and _dispatch_owned_tab_click(
                args,
                cwd,
                menu_choice,
            )
            selected_label = str(menu_choice.get("label") or "")

    if clicked:
        _wait_ms(args, cwd, 250)
    verification = _poll_for_image_mode_selection(args, cwd)
    if not verification.get("selected") and not clicked:
        raise RecoverablePageError(
            "ChatGPT Create image control was unavailable before submission; "
            "the page may be in a transient error state. "
            f"opened_add_menu={opened_add_menu} "
            f"direct={json.dumps(direct, ensure_ascii=False)} "
            f"menu={json.dumps(menu, ensure_ascii=False)} "
            f"menu_choice={json.dumps(menu_choice, ensure_ascii=False)} "
            f"last_state={json.dumps(verification, ensure_ascii=False)}"
        )
    if not verification.get("selected"):
        raise ImageModeSelectionError(
            "ChatGPT Create image mode could not be selected and verified. "
            f"opened_add_menu={opened_add_menu} clicked_create_image={clicked} "
            f"direct={json.dumps(direct, ensure_ascii=False)} "
            f"menu={json.dumps(menu, ensure_ascii=False)} "
            f"menu_choice={json.dumps(menu_choice, ensure_ascii=False)} "
            f"last_state={json.dumps(verification, ensure_ascii=False)}"
        )
    return {
        "requested": True,
        "status": "selected",
        "verified": True,
        "opened_add_menu": opened_add_menu,
        "clicked_create_image": clicked,
        "selected_label": selected_label,
        "menu_control": menu,
        "menu_choice": menu_choice,
        "verification": verification,
    }


def _configure_aspect_ratio(
    args: argparse.Namespace,
    cwd: Path,
    requested: str,
) -> dict[str, Any]:
    requested = requested.strip()

    def find_control() -> dict[str, Any]:
        return _eval_json(
            args,
            cwd,
            f"""
JSON.stringify((() => {{
  const requested = {json.dumps(requested)};
  const visible = (el) => {{
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  }};
  const textFor = (el) => [
    el.getAttribute("aria-label"), el.getAttribute("title"), el.innerText, el.textContent
  ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim();
  const controls = Array.from(document.querySelectorAll(
    "button,[role='button'],[role='radio'],[role='option'],[role='menuitem'],[tabindex]"
  )).filter(visible);
  const exact = controls.find((el) => textFor(el) === requested);
  if (!exact) return {{found: false}};
  const rect = exact.getBoundingClientRect();
  const selected = exact.getAttribute("aria-checked") === "true"
    || exact.getAttribute("aria-selected") === "true"
    || exact.getAttribute("data-state") === "checked";
  return {{
    found: true,
    selected,
    label: textFor(exact),
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }};
}})())
""".strip(),
            timeout=60,
        )

    state = find_control()
    if state.get("found"):
        clicked = True if state.get("selected") else _dispatch_owned_tab_click(args, cwd, state)
        _wait_ms(args, cwd, 500)
        verification = find_control()
        verified = bool(state.get("selected") or verification.get("selected"))
        return {
            "requested": requested,
            "delivery": "ui_control_and_prompt_text",
            "ui_control_available": True,
            "clicked": bool(clicked),
            "verified": verified,
            "label": state.get("label") or requested,
        }
    return {
        "requested": requested,
        "delivery": "prompt_text",
        "ui_control_available": False,
        "clicked": False,
        "verified": False,
        "reason": "current_chatgpt_create_image_ui_has_no_visible_exact_ratio_control",
    }


def _wait_until_idle(args: argparse.Namespace, cwd: Path, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page_text = _page_text(args, cwd)
        if not _is_busy(page_text):
            return
        time.sleep(5)
    raise TimeoutError("ChatGPT stayed busy before the next agent-browser action")


def _wait_for_followup_composer(args: argparse.Namespace, cwd: Path, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    last_state: dict[str, Any] = {}
    while time.time() < deadline:
        last_state = _generation_button_state(args, cwd)
        if last_state.get("checked") and last_state.get("readyForNextPrompt"):
            _wait_for_prompt_box(args, cwd)
            return
        time.sleep(2)
    raise TimeoutError(
        "ChatGPT conversation did not expose an idle follow-up composer. "
        f"Last composer state: {json.dumps(last_state, ensure_ascii=False)}"
    )


def _wait_for_conversation_history(args: argparse.Namespace, cwd: Path, timeout_s: int = 60) -> dict[str, int]:
    deadline = time.time() + timeout_s
    last_counts = {"user_message_count": 0, "assistant_message_count": 0}
    while time.time() < deadline:
        last_counts = _conversation_message_counts(args, cwd)
        if last_counts["user_message_count"] > 0:
            return last_counts
        time.sleep(1)
    raise TimeoutError(
        "ChatGPT conversation URL opened, but its message history did not load before follow-up."
    )


PAGE_ACCESS_STATE_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const hasPrompt = Array.from(document.querySelectorAll("[contenteditable='true']")).some(visible);
  const rawText = (document.body?.innerText || "").slice(0, 6000);
  const text = rawText.toLowerCase();
  const loginPath = /\/(auth\/)?(login|signin)(\/|$)/i.test(location.pathname);
  const loginControl = Array.from(document.querySelectorAll("a,button"))
    .filter(visible)
    .some((el) => /^(log in|sign in|continue with|登录|登入)/i.test((el.innerText || "").trim()));
  const challengeFrame = Array.from(document.querySelectorAll("iframe"))
    .some((frame) => /captcha|challenge|turnstile|verify|cloudflare/i.test(`${frame.src} ${frame.title}`));
  const challengeText = [
    "verify you are human", "verifying you are human", "confirm you are human",
    "checking your browser before accessing", "performing security verification",
    "complete the security check", "complete the challenge", "cloudflare ray id",
    "验证码", "安全验证", "人机验证", "验证您是真人"
  ].some((marker) => text.includes(marker));
  let humanReason = "";
  if (challengeFrame || challengeText) humanReason = "anti_automation_verification";
  else if (loginPath || loginControl) humanReason = "login_required";
  const transientPageErrorPattern = /failed to load subscription|something went wrong|network error|failed to load|unable to load|try again later/i;
  const transientPageErrorTexts = Array.from(document.querySelectorAll("body *"))
    .filter(visible)
    .filter((el) => !el.closest(
      "[inert],#history,[aria-label='Chat history'],[data-sidebar-item]," +
      "[data-message-author-role],#prompt-textarea"
    ))
    .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
    .filter((value) => value.length >= 8 && value.length <= 800 && transientPageErrorPattern.test(value))
    .sort((a, b) => a.length - b.length);
  const transientPageErrorText = transientPageErrorTexts[0] || "";
  const crashMarkers = [
    "aw, snap!", "this page isn't working", "this page isn’t working",
    "this site can't be reached", "this site can’t be reached", "page crashed",
    "out of memory", "err_connection_", "err_internet_", "err_timed_out",
    "err_failed", "err_http2_", "err_network_"
  ];
  const matchedCrashMarker = crashMarkers.find((marker) => text.includes(marker)) || "";
  let crashReason = "";
  if (location.href.startsWith("chrome-error://")) crashReason = "chrome_error_page";
  else if (matchedCrashMarker) crashReason = matchedCrashMarker;
  else if (transientPageErrorText) crashReason = "chatgpt_transient_page_error";
  else if (document.readyState === "complete" && location.href === "about:blank") crashReason = "unexpected_blank_page";
  return {
    hasPrompt,
    humanReason,
    crashReason,
    transientPageErrorText,
    href: location.href,
    title: document.title,
    readyState: document.readyState,
  };
})())
""".strip()


def _page_access_state(args: argparse.Namespace, cwd: Path) -> dict[str, Any]:
    if _owned_tab_cdp_url(args):
        return _eval_json(args, cwd, PAGE_ACCESS_STATE_JS, timeout=30)
    snapshot = _snapshot(args, cwd)
    lowered = snapshot.lower()
    human_reason = ""
    if any(
        marker in lowered
        for marker in (
            "verify you are human",
            "verifying you are human",
            "captcha",
            "turnstile",
            "security check",
            "人机验证",
            "安全验证",
        )
    ):
        human_reason = "anti_automation_verification"
    elif any(marker in lowered for marker in ("log in", "sign in", "登录", "登入")):
        human_reason = "login_required"
    crash_reason = next(
        (
            marker
            for marker in (
                "aw, snap!",
                "this page isn't working",
                "this page isn’t working",
                "this site can't be reached",
                "this site can’t be reached",
                "page crashed",
                "err_connection_",
                "err_timed_out",
            )
            if marker in lowered
        ),
        "",
    )
    has_prompt = any(
        marker in snapshot
        for marker in (
            'role="textbox"',
            'aria-label="Chat with ChatGPT"',
            "Chat with ChatGPT",
            "Message ChatGPT",
        )
    )
    return {
        "hasPrompt": has_prompt,
        "humanReason": human_reason,
        "crashReason": crash_reason,
        "href": "",
        "readyState": "unknown",
    }


def _wait_for_prompt_box(args: argparse.Namespace, cwd: Path, timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    last_state: dict[str, Any] = {}
    last_error = ""
    consecutive_state_failures = 0
    while time.time() < deadline:
        try:
            state = _page_access_state(args, cwd)
            last_state = state
            last_error = ""
            consecutive_state_failures = 0
        except HumanAttentionRequired:
            raise
        except Exception as error:
            consecutive_state_failures += 1
            last_error = f"{type(error).__name__}: {error}"
            if consecutive_state_failures >= PAGE_STATE_FAILURES_BEFORE_RECOVERY:
                raise RecoverablePageError(
                    "ChatGPT page health could not be read for "
                    f"{consecutive_state_failures} consecutive checks: {last_error}"
                ) from error
            time.sleep(1)
            continue
        if state.get("humanReason"):
            _activate_for_human_attention(args, str(state["humanReason"]))
        if state.get("crashReason"):
            raise RecoverablePageError(
                "ChatGPT page is not usable: "
                f"{state['crashReason']} at {state.get('href') or 'unknown URL'}"
            )
        if state.get("hasPrompt"):
            return
        time.sleep(1)
    detail = {
        "last_state": last_state,
        **({"last_error": last_error} if last_error else {}),
    }
    raise RecoverablePageError(
        "ChatGPT prompt textbox did not become visible before the bounded wait expired: "
        f"{json.dumps(detail, ensure_ascii=False)}"
    )


def _activate_for_human_attention(args: argparse.Namespace, reason: str) -> None:
    args.keep_browser_open = True
    args._preserve_owned_tab = True
    try:
        activation = activate_browser(configured_browser(), int(_cdp_launch_port(args)))
    except (OSError, ProfileConfigError, subprocess.SubprocessError, ValueError) as error:
        activation = {"status": "error", "error": str(error)}
    raise HumanAttentionRequired(reason, activation)


def _page_recovery_attempt_limit(args: argparse.Namespace) -> int:
    return max(
        0,
        int(getattr(args, "page_recovery_attempts", DEFAULT_PAGE_RECOVERY_ATTEMPTS)),
    )


def _recover_chatgpt_page(
    args: argparse.Namespace,
    cwd: Path,
    target_url: str,
    *,
    phase: str,
    attempt: int,
    reason: str,
) -> dict[str, Any]:
    target_url = target_url or "https://chatgpt.com/"
    try:
        state = _page_access_state(args, cwd)
    except HumanAttentionRequired:
        raise
    except Exception:
        state = {}
    if state.get("humanReason"):
        _activate_for_human_attention(args, str(state["humanReason"]))

    previous_target_id = str(getattr(args, "_owned_tab_cdp_target_id", "") or "")
    recovery_method = "navigate_owned_tab"
    if _owned_tab_cdp_url(args):
        navigated = _navigate_owned_cdp_tab(args, target_url)
        if not navigated:
            recovery_method = "replace_owned_tab"
            _close_owned_tab(args, cwd)
            _forget_owned_cdp_target(args)
            if not _open_background_cdp_tab(args, target_url):
                raise RecoverablePageError(
                    "ChatGPT page target was unreadable and a replacement CDP tab could not be created: "
                    f"{getattr(args, '_owned_tab_cdp_error', '')}"
                )
    else:
        recovery_method = "reopen_url"
        _open_url(args, cwd, target_url)

    _wait_ms(args, cwd, 1000)
    _wait_for_prompt_box(args, cwd, timeout_s=30)
    return {
        "phase": phase,
        "attempt": attempt,
        "status": "page_recovered",
        "reason": reason,
        "target_url": target_url,
        "method": recovery_method,
        "previous_target_id": previous_target_id,
        "current_target_id": str(getattr(args, "_owned_tab_cdp_target_id", "") or ""),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _record_page_recovery(
    event: dict[str, Any],
    *,
    job: dict[str, Any] | None,
    label: int | str,
) -> None:
    progress = {"event": "chatgpt_page_recovery", **event}
    print(json.dumps(progress, ensure_ascii=False), file=sys.stderr, flush=True)
    if job is not None:
        _write_session_patch(
            job,
            {
                "label": label,
                "status": event["status"],
                "page_recovery": event,
                "attempt": {
                    "action": "page_recovery",
                    "label": label,
                    **event,
                },
            },
        )


def _run_with_page_recovery(
    args: argparse.Namespace,
    cwd: Path,
    operation: Callable[[], Any],
    *,
    phase: str,
    target_url: str,
    job: dict[str, Any] | None = None,
    label: int | str = "batch",
    failure_confirmations: int = 1,
) -> tuple[Any, list[dict[str, Any]]]:
    nonrecoverable_errors = (
        ChatSurfaceSelectionError,
        ImageModeSelectionError,
        ReferenceUploadError,
    )
    recoverable_errors = (
        RecoverablePageError,
        TimeoutError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        urllib.error.URLError,
        json.JSONDecodeError,
    )

    def stop_for_human_gate() -> None:
        try:
            state = _page_access_state(args, cwd)
        except Exception:
            return
        if state.get("humanReason"):
            _activate_for_human_attention(args, str(state["humanReason"]))

    try:
        return operation(), []
    except HumanAttentionRequired:
        raise
    except nonrecoverable_errors:
        stop_for_human_gate()
        raise
    except recoverable_errors as error:
        last_error: BaseException = error

    for _ in range(1, max(1, failure_confirmations)):
        time.sleep(1)
        try:
            return operation(), []
        except HumanAttentionRequired:
            raise
        except nonrecoverable_errors:
            stop_for_human_gate()
            raise
        except recoverable_errors as error:
            last_error = error

    events: list[dict[str, Any]] = []
    limit = _page_recovery_attempt_limit(args)
    for attempt in range(1, limit + 1):
        reason = f"{type(last_error).__name__}: {last_error}"
        started = {
            "phase": phase,
            "attempt": attempt,
            "status": "page_recovery_started",
            "reason": reason,
            "target_url": target_url or "https://chatgpt.com/",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _record_page_recovery(started, job=job, label=label)
        try:
            event = _recover_chatgpt_page(
                args,
                cwd,
                target_url,
                phase=phase,
                attempt=attempt,
                reason=reason,
            )
        except HumanAttentionRequired:
            raise
        except recoverable_errors as recovery_error:
            last_error = recovery_error
            event = {
                **started,
                "status": "page_recovery_failed",
                "recovery_error": f"{type(recovery_error).__name__}: {recovery_error}",
            }
            events.append(event)
            _record_page_recovery(event, job=job, label=label)
            continue

        try:
            result = operation()
        except HumanAttentionRequired:
            raise
        except nonrecoverable_errors:
            stop_for_human_gate()
            raise
        except recoverable_errors as operation_error:
            last_error = operation_error
            event.update(
                {
                    "status": "page_recovery_retry_failed",
                    "operation_error": f"{type(operation_error).__name__}: {operation_error}",
                }
            )
            events.append(event)
            _record_page_recovery(event, job=job, label=label)
            continue

        events.append(event)
        _record_page_recovery(event, job=job, label=label)
        return result, events

    post_submit = phase.startswith("post_submit")
    report = {
        "status": "generation_failed",
        "error_type": "chatgpt_page_recovery_exhausted",
        "retryable": True,
        "terminal": True,
        "recommended_next_action": (
            "collect_current_or_inspect_conversation" if post_submit else "rerun_same_job"
        ),
        "submission_committed": True if post_submit else False,
        "should_collect_current_first": post_submit,
        "phase": phase,
        "target_url": target_url or "https://chatgpt.com/",
        "recovery_attempt_count": len(events),
        "recovery_attempts": events,
        "reason": f"{type(last_error).__name__}: {last_error}",
    }
    if job is not None:
        _write_session_patch(job, {"label": label, **report})
    raise PageRecoveryExhaustedError(report) from last_error


def _wait_ms(args: argparse.Namespace, cwd: Path, milliseconds: int) -> None:
    if _owned_tab_cdp_url(args):
        time.sleep(max(0, milliseconds) / 1000)
        return
    _call_agent(args, cwd, ["wait", str(milliseconds)], timeout=60)


def _upload_files(args: argparse.Namespace, cwd: Path, references: list[str]) -> None:
    if not references:
        return
    if len(references) != 1:
        raise ValueError(
            "Reference files must be uploaded one at a time so attachment order can be audited"
        )
    if _owned_tab_cdp_url(args):
        try:
            with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=180) as conn:
                conn.call("DOM.enable")
                root = conn.call("DOM.getDocument", {"depth": -1, "pierce": True})
                root_id = (root.get("root") or {}).get("nodeId")
                node = conn.call("DOM.querySelector", {"nodeId": root_id, "selector": "input[type=file]"})
                node_id = node.get("nodeId")
                if not node_id:
                    raise RuntimeError("file input not found")
                conn.call("DOM.setFileInputFiles", {"nodeId": node_id, "files": references})
            return
        except Exception as error:
            args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
            raise RuntimeError(f"Owned CDP file upload failed without using focus-stealing fallback: {error}") from error
    _call_agent(args, cwd, ["upload", "input[type=file]", *references], timeout=180)


def _reference_upload_observation(
    args: argparse.Namespace,
    cwd: Path,
    references: list[str],
) -> dict[str, Any]:
    page_text = _page_text(args, cwd) if _owned_tab_cdp_url(args) else _snapshot(args, cwd)
    failure = _reference_upload_failure(page_text, references)
    try:
        rows = _image_inventory(args, cwd)
    except Exception:
        rows = []
    image_urls = sorted(
        {
            str(row.get("src") or "")
            for row in rows
            if str(row.get("src") or "").startswith("blob:https://chatgpt.com/")
        }
    )
    filenames = [Path(path).name for path in references]
    attachment_state: dict[str, Any] = {}
    if _owned_tab_cdp_url(args):
        try:
            attachment_state = _eval_json(
                args,
                cwd,
                r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const buttons = Array.from(document.querySelectorAll("button,[role='button']")).filter(visible);
  const removeLabels = buttons
    .map((el) => el.getAttribute("aria-label") || "")
    .filter((label) => /^Remove file\s+\d+:/i.test(label));
  const uploadedImageLabels = buttons
    .map((el) => el.getAttribute("aria-label") || "")
    .filter((label) => /^Open image:\s*User uploaded image/i.test(label));
  return {
    removeFileCount: removeLabels.length,
    removeFileLabels: removeLabels,
    uploadedImagePreviewCount: uploadedImageLabels.length,
    uploadedImagePreviewLabels: uploadedImageLabels,
  };
})())
""".strip(),
                timeout=60,
            )
        except Exception:
            attachment_state = {}
    attachment_labels = [str(label) for label in attachment_state.get("removeFileLabels") or []]
    attachment_count = max(
        int(attachment_state.get("removeFileCount") or 0),
        int(attachment_state.get("uploadedImagePreviewCount") or 0),
    )
    filename_mentions = {
        filename: (
            filename in page_text
            or Path(filename).stem in page_text
            or any(filename in label or Path(filename).stem in label for label in attachment_labels)
        )
        for filename in filenames
    }
    return {
        "failure": failure,
        "page_text": page_text,
        "reference_count": len(references),
        "blob_image_count": len(image_urls),
        "blob_image_urls": image_urls,
        "attachment_count": attachment_count,
        "attachment_labels": attachment_labels,
        "uploaded_image_preview_count": int(attachment_state.get("uploadedImagePreviewCount") or 0),
        "filename_mentions": filename_mentions,
        "ready": attachment_count >= len(references) or len(image_urls) >= len(references),
    }


def _wait_for_reference_uploads_ready(
    args: argparse.Namespace,
    cwd: Path,
    references: list[str],
    *,
    timeout_s: int = 90,
    min_wait_s: int = 1,
) -> dict[str, Any]:
    if not references:
        return {"reference_count": 0, "blob_image_count": 0, "filename_mentions": {}}
    started_at = time.time()
    observation_window_s = min(max(0, min_wait_s), max(0, timeout_s))
    while True:
        access_state = _page_access_state(args, cwd)
        if access_state.get("humanReason"):
            _activate_for_human_attention(args, str(access_state["humanReason"]))
        if access_state.get("crashReason"):
            raise RecoverablePageError(
                "ChatGPT page became unusable while waiting for reference uploads: "
                f"{access_state['crashReason']}"
            )
        observation = _reference_upload_observation(args, cwd, references)
        if observation.get("failure"):
            raise ReferenceUploadError(observation["failure"])
        elapsed = time.time() - started_at
        if observation.get("ready") and elapsed >= observation_window_s:
            return observation
        if (
            elapsed >= min(timeout_s, REFERENCE_UPLOAD_NO_EVIDENCE_RECOVERY_SECONDS)
            and int(observation.get("attachment_count") or 0) == 0
            and int(observation.get("blob_image_count") or 0) == 0
        ):
            raise RecoverablePageError(
                "Reference upload showed no attachment evidence after the bounded startup wait; "
                "the pre-submit page state may have been refreshed or lost."
            )
        if elapsed >= timeout_s:
            raise RecoverablePageError(
                "Reference attachments disappeared or never became verifiable before submission: "
                + json.dumps(
                    {
                        key: value
                        for key, value in observation.items()
                        if key != "page_text"
                    },
                    ensure_ascii=False,
                )
            )
        time.sleep(1)


def _upload_references_in_order(
    args: argparse.Namespace,
    cwd: Path,
    references: list[str],
) -> dict[str, Any]:
    """Upload each reference separately and prove a monotonic attachment sequence."""
    if not references:
        return {
            "reference_count": 0,
            "blob_image_count": 0,
            "filename_mentions": {},
            "upload_sequence": [],
            "ordered_upload_paths": [],
            "order_verified": True,
            "order_verification_method": "no_references",
        }

    steps: list[dict[str, Any]] = []
    final_observation: dict[str, Any] = {}
    for index, path in enumerate(references, start=1):
        _upload_files(args, cwd, [path])
        final_observation = _wait_for_reference_uploads_ready(
            args,
            cwd,
            references[:index],
        )
        observed_count = max(
            int(final_observation.get("attachment_count") or 0),
            int(final_observation.get("blob_image_count") or 0),
        )
        if observed_count < index:
            raise RecoverablePageError(
                "Sequential reference upload did not produce a monotonic attachment count: "
                f"Reference Image {index}, observed_count={observed_count}"
            )
        steps.append(
            {
                "index": index,
                "path": path,
                "observed_attachment_count": int(
                    final_observation.get("attachment_count") or 0
                ),
                "observed_blob_image_count": int(
                    final_observation.get("blob_image_count") or 0
                ),
            }
        )

    attachment_label_order_verified: bool | None = None
    attachment_labels = [str(label) for label in final_observation.get("attachment_labels") or []]
    indexed_labels: list[tuple[int, str]] = []
    for label in attachment_labels:
        match = re.match(r"^Remove file\s+(\d+):\s*(.+)$", label, re.IGNORECASE)
        if match:
            indexed_labels.append((int(match.group(1)), match.group(2)))
    if len(indexed_labels) >= len(references):
        indexed_labels.sort(key=lambda item: item[0])
        expected_indices = list(range(1, len(references) + 1))
        attachment_label_order_verified = (
            [index for index, _ in indexed_labels[: len(references)]] == expected_indices
            and all(
                Path(path).name.casefold() in label.casefold()
                or Path(path).stem.casefold() in label.casefold()
                for path, (_, label) in zip(
                    references,
                    indexed_labels[: len(references)],
                    strict=True,
                )
            )
        )
        if not attachment_label_order_verified:
            raise ReferenceUploadError(
                {
                    "status": "reference_upload_failed",
                    "error_type": "reference_upload_order_mismatch",
                    "retryable": True,
                    "terminal": True,
                    "recommended_next_action": "rerun_same_job_after_inspecting_upload_trace",
                    "reason": "Visible ChatGPT attachment labels do not match manifest array order",
                    "expected_upload_order": list(enumerate(references, start=1)),
                    "observed_attachment_labels": attachment_labels,
                }
            )

    return {
        **final_observation,
        "upload_sequence": steps,
        "ordered_upload_paths": list(references),
        "order_verified": True,
        "attachment_label_order_verified": attachment_label_order_verified,
        "order_verification_method": (
            "visible_attachment_labels_and_sequential_file_input"
            if attachment_label_order_verified
            else "sequential_file_input_with_monotonic_attachment_count"
        ),
    }


def _require_verified_reference_upload_order(
    references: list[str],
    observation: dict[str, Any],
) -> None:
    if not references:
        return
    steps = observation.get("upload_sequence") or []
    actual = [
        (int(step.get("index") or 0), str(step.get("path") or ""))
        for step in steps
        if isinstance(step, dict)
    ]
    expected = list(enumerate(references, start=1))
    if not observation.get("order_verified") or actual != expected:
        raise ReferenceUploadError(
            {
                "status": "reference_upload_failed",
                "error_type": "reference_upload_order_unverified",
                "retryable": True,
                "terminal": True,
                "recommended_next_action": "rerun_same_job_after_inspecting_upload_trace",
                "reason": (
                    "The runner could not prove that UI attachment order matches Reference "
                    "Image 1..N numbering"
                ),
                "expected_upload_order": expected,
                "observed_upload_order": actual,
            }
        )


def _upload_failure_report(
    *,
    job: dict[str, Any],
    args: argparse.Namespace,
    label: int | str,
    mode: str,
    trace_dir: Path,
    references: list[str],
    account_guard: dict[str, Any],
    failure: dict[str, Any],
    conversation_url: str = "",
    continued_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    patch = {
        **failure,
        "label": label,
        "resumed": False,
        "conversation_id": _conversation_id_from_url(conversation_url),
        "conversation_url": conversation_url,
        **reference_audit_fields(job),
        "agent_browser_profile": _profile_label(args),
        "account_lane": TARGET_CHATGPT_ACCOUNT_SIGNAL,
        "account_guard": account_guard,
        **({"continued_from": continued_from} if continued_from else {}),
        "attempt": {
            "action": "reference_upload_failed",
            "label": label,
            "mode": mode,
            "conversation_url": conversation_url,
            "at": now,
        },
    }
    _write_session_patch(job, patch)
    report = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "mode": mode,
        "label": label,
        "submitted": False,
        "prompt_card": job["prompt_card"],
        "agent_browser_session": _agent_browser_session_record(args),
        "conversation_url": conversation_url,
        "reference_count": len(references),
        **reference_audit_fields(job),
        "trace_dir": str(trace_dir),
        "account_guard": account_guard,
        **({"continued_from": continued_from} if continued_from else {}),
        "image_count": 0,
        "expected_image_count": _expected_image_count(job, label),
        "partial": False,
        "images": [],
        **failure,
    }
    report_path = _report_path_for_label(trace_dir, label)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report_path": str(report_path), "resumed": False}


def run_dry_upload(args: argparse.Namespace, job: dict[str, Any], cwd: Path) -> dict[str, Any]:
    trace_dir = Path(job["download_dir"]) / "agent_browser_trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    references = [ref["path"] for ref in job.get("reference_images", [])]

    def prepare_surface() -> dict[str, Any]:
        _open_new_chat(args, cwd)
        _wait_for_prompt_box(args, cwd)
        chat_surface = _ensure_chat_surface(args, cwd)
        account_guard = _validate_account_lane(args, cwd, job)
        _screenshot(args, cwd, trace_dir / "01_open.png")
        image_mode = _enable_image_mode_if_available(
            args,
            cwd,
            "" if _owned_tab_cdp_url(args) else _snapshot(args, cwd),
        )
        aspect_ratio_delivery = _configure_aspect_ratio(
            args,
            cwd,
            str(job.get("output_aspect_ratio") or "1:1"),
        )
        upload_observation = _upload_references_in_order(args, cwd, references)
        _require_verified_reference_upload_order(references, upload_observation)
        chat_surface = _ensure_chat_surface(args, cwd, recorded=chat_surface)
        _screenshot(args, cwd, trace_dir / "02_after_upload.png")
        after_upload = _page_text(args, cwd) if _owned_tab_cdp_url(args) else _snapshot(args, cwd)
        return {
            "chat_surface": chat_surface,
            "account_guard": account_guard,
            "image_mode": image_mode,
            "aspect_ratio_delivery": aspect_ratio_delivery,
            "upload_observation": upload_observation,
            "after_upload": after_upload,
        }

    prepared, page_recoveries = _run_with_page_recovery(
        args,
        cwd,
        prepare_surface,
        phase="dry_upload_prepare",
        target_url="https://chatgpt.com/",
        job=job,
        label="batch",
    )
    chat_surface = prepared["chat_surface"]
    account_guard = prepared["account_guard"]
    image_mode = prepared["image_mode"]
    aspect_ratio_delivery = prepared["aspect_ratio_delivery"]
    upload_observation = prepared["upload_observation"]
    after_upload = prepared["after_upload"]

    report = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "mode": "dry_upload",
        "prompt_card": job["prompt_card"],
        "agent_browser_session": _agent_browser_session_record(args),
        "reference_count": len(references),
        **reference_audit_fields(job),
        "trace_dir": str(trace_dir),
        "account_guard": account_guard,
        "chat_surface": chat_surface,
        "image_mode": image_mode,
        "aspect_ratio_delivery": aspect_ratio_delivery,
        "after_upload_mentions": {
            Path(path).name: Path(path).name in after_upload for path in references
        },
        "upload_observation": {
            key: value
            for key, value in upload_observation.items()
            if key != "page_text"
        },
        "page_recoveries": page_recoveries,
    }
    report_path = trace_dir / "dry_upload_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _paste_prompt(args: argparse.Namespace, cwd: Path, message: str) -> None:
    if _owned_tab_cdp_url(args):
        try:
            with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=180) as conn:
                conn.call("Runtime.enable")
                conn.call("Page.enable")
                conn.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                result = conn.call(
                    "Runtime.evaluate",
                    {
                        "expression": r"""
(() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const editors = Array.from(document.querySelectorAll(
    "#prompt-textarea[contenteditable='true'], [contenteditable='true'][role='textbox']"
  )).filter(visible);
  const editor = editors.find((candidate) => candidate.closest("form")) || editors.at(-1);
  if (!editor) throw new Error("composer editor not found");
  editor.focus();
  const block = editor.lastElementChild || editor;
  const selection = window.getSelection();
  const range = document.createRange();
  const pills = Array.from(block.querySelectorAll("[data-inline-selection-pill]"));
  if (pills.length > 0) {
    range.setStartAfter(pills.at(-1));
    range.setEnd(block, block.childNodes.length);
  } else {
    range.selectNodeContents(block);
  }
  selection.removeAllRanges();
  selection.addRange(range);
  if (!range.collapsed) document.execCommand("delete");
  const insertionRange = document.createRange();
  insertionRange.selectNodeContents(block);
  insertionRange.collapse(false);
  selection.removeAllRanges();
  selection.addRange(insertionRange);
  return "focused";
})()
""",
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                if result.get("exceptionDetails"):
                    raise RuntimeError(f"Runtime.evaluate exception: {result['exceptionDetails']}")
                conn.call("Input.insertText", {"text": message})
            _wait_ms(args, cwd, 1000)
            state = _composer_submit_state(args, cwd, message)
            if not state.get("editorHasPromptTail"):
                raise RuntimeError(
                    "Prompt text did not appear in the ChatGPT composer after CDP insertion. "
                    f"Composer state: {json.dumps(state, ensure_ascii=False)}"
                )
            return
        except Exception as error:
            args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
            raise RuntimeError(f"Owned CDP prompt paste failed without using focus-stealing fallback: {error}") from error
    _call_agent(args, cwd, ["click", "[contenteditable=true]"], timeout=120)
    _safe_agent(args, cwd, ["press", "Control+a"], timeout=60)
    _call_agent(args, cwd, ["keyboard", "inserttext", message], timeout=180)
    _wait_ms(args, cwd, 1000)


def _press_composer_enter(args: argparse.Namespace, cwd: Path) -> None:
    if _owned_tab_cdp_url(args):
        try:
            with _CDPConnection.connect(_owned_tab_cdp_url(args), timeout=60) as conn:
                conn.call("Runtime.enable")
                conn.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                result = conn.call(
                    "Runtime.evaluate",
                    {
                        "expression": "(() => { const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; }; const editors = Array.from(document.querySelectorAll(\"[contenteditable='true']\")).filter(visible); (editors.find((el) => el.closest('form')) || editors.at(-1))?.focus(); return 'focused'; })()",
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                if result.get("exceptionDetails"):
                    raise RuntimeError(f"Runtime.evaluate exception: {result['exceptionDetails']}")
                event = {
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    "code": "Enter",
                    "key": "Enter",
                }
                conn.call("Input.dispatchKeyEvent", {"type": "keyDown", **event})
                conn.call("Input.dispatchKeyEvent", {"type": "keyUp", **event})
            return
        except Exception as error:
            args._owned_tab_cdp_error = f"{type(error).__name__}: {error}"
            raise RuntimeError(f"Owned CDP Enter submit failed without using focus-stealing fallback: {error}") from error
    _safe_agent(args, cwd, ["focus", "[contenteditable=true]"], timeout=60)
    _safe_agent(args, cwd, ["press", "Enter"], timeout=60)


COMPOSER_SUBMIT_STATE_JS = r"""
(() => {
  const promptTail = __PROMPT_TAIL__;
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const labelFor = (button) => [
    button.getAttribute("aria-label"),
    button.getAttribute("data-testid"),
    button.getAttribute("title"),
    button.innerText,
    button.textContent,
    button.type,
  ].filter(Boolean).join(" ");
  const editors = Array.from(document.querySelectorAll(
    "#prompt-textarea[contenteditable='true'], [contenteditable='true'][role='textbox']"
  )).filter(visible);
  const editor = editors.find((candidate) => candidate.closest("form")) || editors.at(-1) || null;
  const root = editor ? (editor.closest("form") || editor.parentElement || editor) : null;
  const editorText = normalize(editor ? editor.innerText || editor.textContent : "");
  const hasPromptTail = Boolean(promptTail) && editorText.includes(normalize(promptTail));
  const hasComposerText = editorText.length > 0;
  const conversationStarted = /\/c\/[^/?#]+/.test(window.location.pathname);
  const userMessageCount = document.querySelectorAll('[data-message-author-role="user"]').length;
  const assistantMessageCount = document.querySelectorAll('[data-message-author-role="assistant"]').length;
  const buttons = root ? Array.from(root.querySelectorAll("button")).filter(visible) : [];
  const sendButton = buttons.find((button) => button.id === "composer-submit-button")
    || buttons.find((button) => button.getAttribute("data-testid") === "send-button")
    || buttons.find((button) => /^send prompt$/i.test(normalize(button.getAttribute("aria-label"))))
    || buttons.find((button) => button.type === "submit")
    || null;
  const disabled = sendButton
    ? Boolean(sendButton.disabled || sendButton.getAttribute("aria-disabled") === "true" || sendButton.closest("[aria-disabled='true']"))
    : true;
  return JSON.stringify({
    submitted: !hasPromptTail && editorText.length < 500,
    readyToSubmit: hasPromptTail && Boolean(sendButton) && !disabled,
    buttonFound: Boolean(sendButton),
    buttonDisabled: disabled,
    buttonLabel: sendButton ? labelFor(sendButton) : "",
    buttonCount: buttons.length,
    editorTextLength: editorText.length,
    editorHasPromptTail: hasPromptTail,
    editorHasText: hasComposerText,
    conversationStarted,
    userMessageCount,
    assistantMessageCount,
  });
})()
"""


COMPOSER_CLICK_SEND_JS = r"""
(() => {
  const promptTail = __PROMPT_TAIL__;
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const labelFor = (button) => [
    button.getAttribute("aria-label"),
    button.getAttribute("data-testid"),
    button.getAttribute("title"),
    button.innerText,
    button.textContent,
    button.type,
  ].filter(Boolean).join(" ");
  const editors = Array.from(document.querySelectorAll(
    "#prompt-textarea[contenteditable='true'], [contenteditable='true'][role='textbox']"
  )).filter(visible);
  const editor = editors.find((candidate) => candidate.closest("form")) || editors.at(-1) || null;
  const root = editor ? (editor.closest("form") || editor.parentElement || editor) : null;
  const editorText = normalize(editor ? editor.innerText || editor.textContent : "");
  const hasPromptTail = Boolean(promptTail) && editorText.includes(normalize(promptTail));
  const hasComposerText = editorText.length > 0;
  const buttons = root ? Array.from(root.querySelectorAll("button")).filter(visible) : [];
  const sendButton = buttons.find((button) => button.id === "composer-submit-button")
    || buttons.find((button) => button.getAttribute("data-testid") === "send-button")
    || buttons.find((button) => /^send prompt$/i.test(normalize(button.getAttribute("aria-label"))))
    || buttons.find((button) => button.type === "submit")
    || null;
  const disabled = sendButton
    ? Boolean(sendButton.disabled || sendButton.getAttribute("aria-disabled") === "true" || sendButton.closest("[aria-disabled='true']"))
    : true;
  const clicked = hasPromptTail && Boolean(sendButton) && !disabled;
  if (clicked) {
    sendButton.click();
  }
  return JSON.stringify({
    clicked,
    buttonFound: Boolean(sendButton),
    buttonDisabled: disabled,
    buttonLabel: sendButton ? labelFor(sendButton) : "",
    buttonCount: buttons.length,
    editorTextLength: editorText.length,
    editorHasPromptTail: hasPromptTail,
    editorHasText: hasComposerText,
  });
})()
"""


def _composer_submit_state(
    args: argparse.Namespace,
    cwd: Path,
    message: str,
) -> dict[str, Any]:
    normalized = " ".join(message.split())
    prompt_tail = normalized[-120:]
    script = (
        COMPOSER_SUBMIT_STATE_JS
        .replace("__PROMPT_TAIL__", json.dumps(prompt_tail))
    )
    return _eval_json(args, cwd, script, timeout=60)


def _click_composer_send_button(
    args: argparse.Namespace,
    cwd: Path,
    message: str,
) -> dict[str, Any]:
    normalized = " ".join(message.split())
    prompt_tail = normalized[-120:]
    script = (
        COMPOSER_CLICK_SEND_JS
        .replace("__PROMPT_TAIL__", json.dumps(prompt_tail))
    )
    return _eval_json(args, cwd, script, timeout=60)


def _composer_state_has_submission_evidence(
    state: dict[str, Any],
    *,
    saw_prompt: bool,
    enter_presses: int,
) -> bool:
    if not saw_prompt or not state.get("submitted") or state.get("editorHasPromptTail"):
        return False
    return True


def _submit_prompt(
    args: argparse.Namespace,
    cwd: Path,
    message: str,
    timeout_s: int = 240,
    trace_dir: Path | None = None,
) -> None:
    last_state: dict[str, Any] = {}
    states: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    iteration = 0
    enter_presses = 0
    saw_prompt = False
    max_enter_presses = 5

    while time.time() < deadline:
        iteration += 1
        last_state = _composer_submit_state(args, cwd, message)
        saw_prompt = saw_prompt or bool(last_state.get("editorHasPromptTail"))
        states.append({"iteration": iteration, "state": last_state, "time": time.time()})
        if trace_dir:
            (trace_dir / "send_button_states.json").write_text(
                json.dumps(states[-80:], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if _composer_state_has_submission_evidence(
            last_state,
            saw_prompt=saw_prompt,
            enter_presses=enter_presses,
        ):
            return
        if last_state.get("readyToSubmit"):
            if enter_presses >= max_enter_presses:
                break
            click_state = _click_composer_send_button(args, cwd, message)
            states.append(
                {
                    "iteration": iteration,
                    "action": "click_send",
                    "state": click_state,
                    "time": time.time(),
                }
            )
            if trace_dir:
                (trace_dir / "send_button_states.json").write_text(
                    json.dumps(states[-80:], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if click_state.get("clicked"):
                time.sleep(1)
                after_click = _composer_submit_state(args, cwd, message)
                saw_prompt = saw_prompt or bool(after_click.get("editorHasPromptTail"))
                states.append(
                    {
                        "iteration": iteration,
                        "action": "after_click_send",
                        "state": after_click,
                        "time": time.time(),
                    }
                )
                if trace_dir:
                    (trace_dir / "send_button_states.json").write_text(
                        json.dumps(states[-80:], ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                if _composer_state_has_submission_evidence(
                    after_click,
                    saw_prompt=saw_prompt,
                    enter_presses=enter_presses,
                ):
                    return
            enter_presses += 1
            _press_composer_enter(args, cwd)
            time.sleep(1)
            after_enter = _composer_submit_state(args, cwd, message)
            saw_prompt = saw_prompt or bool(after_enter.get("editorHasPromptTail"))
            states.append(
                {
                    "iteration": iteration,
                    "action": "press_enter",
                    "enter_press": enter_presses,
                    "state": after_enter,
                    "time": time.time(),
                }
            )
            if trace_dir:
                (trace_dir / "send_button_states.json").write_text(
                    json.dumps(states[-80:], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if _composer_state_has_submission_evidence(
                after_enter,
                saw_prompt=saw_prompt,
                enter_presses=enter_presses,
            ):
                return
        time.sleep(0.5)

    if _composer_state_has_submission_evidence(
        last_state,
        saw_prompt=saw_prompt,
        enter_presses=enter_presses,
    ):
        return

    if trace_dir:
        _screenshot(args, cwd, trace_dir / "submit_failed_still_in_composer.png")
        (trace_dir / "send_button_states.json").write_text(
            json.dumps(states, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    raise RuntimeError(
        "Prompt did not clear from the composer after Enter submit attempts.\n"
        f"Last send state:\n{json.dumps(last_state, ensure_ascii=False, indent=2)}\n"
    )


def _image_inventory(args: argparse.Namespace, cwd: Path) -> list[dict[str, Any]]:
    script = r"""
JSON.stringify(Array.from(document.images).map((img) => {
  const src = img.currentSrc || img.src;
  const alt = img.getAttribute('alt') || '';
  const labelFor = (node) => [
    node && node.getAttribute && node.getAttribute('aria-label'),
    node && node.getAttribute && node.getAttribute('title'),
    node && node.innerText,
    node && node.textContent,
    alt
  ].filter(Boolean).join(' ');
  const button = img.closest('button,[role="button"],a');
  const buttonLabel = labelFor(button);
  const inGeneratedImageControl = /Generated image|生成的图片|生成图/i.test(buttonLabel) || /Generated image/i.test(alt);
  const turnSection = img.closest('section[data-testid^="conversation-turn-"]');
  const article = img.closest('article');
  const roleNode = img.closest('[data-message-author-role]')
    || (turnSection ? turnSection.querySelector('[data-message-author-role]') : null)
    || (article ? article.querySelector('[data-message-author-role]') : null);
  const role = roleNode ? roleNode.getAttribute('data-message-author-role') : '';
  const turnSections = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));
  const turnIndex = turnSection ? turnSections.indexOf(turnSection) : -1;
  const messageUserTurn = turnIndex >= 0
    ? turnSections.slice(0, turnIndex + 1).filter((candidate) => candidate.querySelector('[data-message-author-role="user"]')).length
    : 0;
  const articleText = article ? (article.innerText || article.textContent || '').slice(0, 300) : '';
  const inAssistant = role === 'assistant' || inGeneratedImageControl || /\bChatGPT said\b/i.test(articleText);
  const inUser = role === 'user' || /\bYou said\b/i.test(articleText);
  const inComposer = Boolean(
    img.closest('form')
    || img.closest('[contenteditable="true"]')
    || img.closest('[data-testid*="composer" i]')
    || img.closest('[aria-label*="Message" i]')
  );
  return {
    src,
    width: img.naturalWidth || 0,
    height: img.naturalHeight || 0,
    alt,
    buttonLabel,
    inGeneratedImageControl,
    role,
    messageUserTurn,
    inAssistant,
    inUser,
    inComposer
  };
}))
"""
    output = _eval_js(args, cwd, script, timeout=120).strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("[")
        end = output.rfind("]")
        if start >= 0 and end > start:
            parsed = json.loads(output[start : end + 1])
        else:
            raise
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    if not isinstance(parsed, list):
        return []
    return parsed


def _conversation_message_counts(args: argparse.Namespace, cwd: Path) -> dict[str, int]:
    counts = _eval_json(
        args,
        cwd,
        "JSON.stringify({ userMessageCount: document.querySelectorAll('[data-message-author-role=\\\"user\\\"]').length, assistantMessageCount: document.querySelectorAll('[data-message-author-role=\\\"assistant\\\"]').length })",
        timeout=60,
    )
    return {
        "user_message_count": max(0, int(counts.get("userMessageCount") or 0)),
        "assistant_message_count": max(0, int(counts.get("assistantMessageCount") or 0)),
    }


def _is_likely_ui_asset(src: str) -> bool:
    return (
        "/cdn/assets/" in src
        or "favicon" in src
        or "apple-touch-icon" in src
        or "openai.com/favicon" in src
        or "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" in src
    )


def _generated_image_candidates(
    rows: list[dict[str, Any]],
    baseline: set[str],
    baseline_user_message_count: int | None = None,
) -> list[dict[str, Any]]:
    candidates = []
    seen: set[str] = set()
    scope_available = any(row.get("role") or row.get("inAssistant") or row.get("inUser") for row in rows)
    for row in rows:
        src = row.get("src") or ""
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
        if not src or src in baseline or src in seen:
            continue
        if (
            baseline_user_message_count is not None
            and int(row.get("messageUserTurn") or 0) <= baseline_user_message_count
        ):
            continue
        if row.get("inComposer") or row.get("inUser"):
            continue
        if scope_available and not row.get("inAssistant"):
            continue
        if _is_likely_ui_asset(src):
            continue
        if not row.get("inGeneratedImageControl") and (width < 256 or height < 256):
            continue
        seen.add(src)
        candidates.append(
            {
                **row,
                "download_source": "generated_image_control_src"
                if row.get("inGeneratedImageControl")
                else ("assistant_image_src" if row.get("inAssistant") else "page_image_src"),
                "recognition": "generated_image_control"
                if row.get("inGeneratedImageControl")
                else ("assistant_generated_image" if row.get("inAssistant") else "new_non_ui_image"),
            }
        )
    return candidates


def _vertical_images(
    rows: list[dict[str, Any]],
    baseline: set[str],
    baseline_user_message_count: int | None = None,
) -> list[dict[str, Any]]:
    return _generated_image_candidates(rows, baseline, baseline_user_message_count)


def _scroll_down(args: argparse.Namespace, cwd: Path, pixels: int = 700) -> None:
    ok, _ = _try_cdp_eval_js(args, f"window.scrollBy(0, {int(pixels)}); 'ok'", timeout=30)
    if ok:
        return
    if _owned_tab_cdp_url(args):
        raise RuntimeError(
            "Owned CDP scroll failed; refusing agent-browser fallback because it can steal focus. "
            f"last_error={getattr(args, '_owned_tab_cdp_error', '')}"
        )
    _safe_agent(args, cwd, ["scroll", "down", str(pixels)], timeout=60)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_mae(left_path: Path, right_path: Path) -> float:
    left = Image.open(left_path).convert("RGB")
    right = Image.open(right_path).convert("RGB")
    left_ratio = left.width / left.height
    right_ratio = right.width / right.height
    if abs(left_ratio - right_ratio) > 0.02:
        return 255.0
    size = (128, 128)
    left = left.resize(size)
    right = right.resize(size)
    diff = ImageChops.difference(left, right)
    return sum(ImageStat.Stat(diff).mean) / 3


def _is_uploaded_reference_download(output_path: Path, reference_paths: list[str]) -> bool:
    try:
        output_hash = _sha256(output_path)
        for reference in reference_paths:
            reference_path = Path(reference)
            if output_hash == _sha256(reference_path):
                return True
            if _image_mae(output_path, reference_path) <= 1.0:
                return True
    except Exception:
        return False
    return False


def _filter_reference_downloads(
    downloaded: list[dict[str, str]],
    reference_paths: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    for item in downloaded:
        path = Path(item["path"])
        if _is_uploaded_reference_download(path, reference_paths):
            filtered.append({"path": str(path), "reason": "matches_uploaded_reference"})
            path.unlink(missing_ok=True)
        else:
            kept.append(item)
    return kept, filtered


def _candidate_download_limit(expected_count: int, reference_paths: list[str]) -> int:
    return expected_count


def _should_download_candidates(
    *,
    candidate_count: int,
    expected_count: int,
    busy: bool,
    download_ready: bool = True,
    first_seen_at: float | None,
    last_growth_at: float | None,
    now: float,
    grace_seconds: int | None = None,
) -> tuple[bool, str]:
    if candidate_count <= 0 or busy:
        return False, "waiting"
    if candidate_count >= expected_count:
        return True, "complete"
    return True, "partial_terminal"


def _generation_completion_metadata(
    *,
    status: str,
    image_count: int,
    expected_count: int,
    download_reason: str,
) -> dict[str, Any]:
    missing_count = max(0, expected_count - image_count)
    missing_batch_message = ""
    if missing_count == 0:
        generation_state = "complete"
        safe_to_fallback = False
        should_collect_current_first = False
        recommended_next_action = "none"
        safe_to_request_missing_batch = False
    elif download_reason == "partial_terminal":
        generation_state = "partial_terminal"
        safe_to_fallback = False
        should_collect_current_first = False
        recommended_next_action = "request_missing_batch_in_same_conversation"
        safe_to_request_missing_batch = True
        image_word = "image" if missing_count == 1 else "images"
        result_word = "result" if missing_count == 1 else "results"
        missing_batch_message = (
            f"You generated {image_count} of the requested {expected_count} images. "
            f"Generate {missing_count} additional separate generated {image_word} now in this same conversation, "
            "using the same prompt and uploaded references. Do not regenerate or revise the images already produced. "
            f"Return exactly {missing_count} new separate image {result_word}. Do not create a grid, collage, contact sheet, "
            "split-screen, labels, captions, or multiple versions inside one canvas."
        )
    else:
        generation_state = "stale_or_unknown"
        safe_to_fallback = False
        should_collect_current_first = True
        recommended_next_action = "collect_current_first"
        safe_to_request_missing_batch = False
    return {
        "generation_state": generation_state,
        "safe_to_fallback": safe_to_fallback,
        "should_collect_current_first": should_collect_current_first,
        "missing_image_count": missing_count,
        "recommended_next_action": recommended_next_action,
        "safe_to_request_missing_batch": safe_to_request_missing_batch,
        "missing_batch_request_count": missing_count if safe_to_request_missing_batch else 0,
        "missing_image_followup_message": missing_batch_message,
    }


def _keep_expected_downloads(
    downloaded: list[dict[str, str]],
    expected_count: int,
) -> list[dict[str, str]]:
    kept = downloaded[:expected_count]
    for item in downloaded[expected_count:]:
        Path(item["path"]).unlink(missing_ok=True)
    return kept


def _validate_downloaded_image(output_path: Path) -> dict[str, Any]:
    if not output_path.exists():
        return {"ok": False, "path": str(output_path), "reason": "missing_file"}
    size = output_path.stat().st_size
    if size <= 0:
        return {"ok": False, "path": str(output_path), "reason": "empty_file", "bytes": size}
    try:
        with Image.open(output_path) as image:
            image.verify()
        with Image.open(output_path) as image:
            width, height = image.size
            fmt = image.format or ""
    except Exception as error:
        return {
            "ok": False,
            "path": str(output_path),
            "reason": f"invalid_image: {error}",
            "bytes": size,
        }
    if width < 256 or height < 256:
        return {
            "ok": False,
            "path": str(output_path),
            "reason": "image_too_small",
            "bytes": size,
            "width": width,
            "height": height,
            "format": fmt,
        }
    return {
        "ok": True,
        "path": str(output_path),
        "bytes": size,
        "width": width,
        "height": height,
        "format": fmt,
    }


def _annotate_aspect_ratio_validation(
    job: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    requested = str(job.get("output_aspect_ratio") or "1:1").strip()
    try:
        expected_width, expected_height = (float(part) for part in requested.split(":", 1))
        expected_ratio = expected_width / expected_height
    except (TypeError, ValueError, ZeroDivisionError):
        return {
            "requested": requested,
            "valid_request": False,
            "all_match": False,
            "images": [],
        }

    checks: list[dict[str, Any]] = []
    for image in payload.get("images") or []:
        validation = image.get("image_validation") or {}
        width = int(validation.get("width") or 0)
        height = int(validation.get("height") or 0)
        if width <= 0 or height <= 0:
            check = {
                "requested": requested,
                "matches": False,
                "reason": "missing_dimensions",
            }
        else:
            actual_ratio = width / height
            relative_error = abs(actual_ratio - expected_ratio) / expected_ratio
            check = {
                "requested": requested,
                "expected_ratio": round(expected_ratio, 6),
                "actual_dimensions": f"{width}x{height}",
                "actual_ratio": round(actual_ratio, 6),
                "relative_error": round(relative_error, 6),
                "tolerance": 0.02,
                "matches": relative_error <= 0.02,
            }
        image["aspect_ratio_validation"] = check
        checks.append(check)
    return {
        "requested": requested,
        "valid_request": True,
        "all_match": all(bool(check.get("matches")) for check in checks) if checks else None,
        "images": checks,
    }


def _normalize_image_to_png(source_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as image:
        mode = "RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"
        normalized = image.convert(mode)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        normalized.save(tmp_path, format="PNG")
    tmp_path.replace(output_path)
    if source_path != output_path:
        source_path.unlink(missing_ok=True)
    validation = _validate_downloaded_image(output_path)
    if not validation.get("ok"):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded asset is not a usable PNG: {validation}")
    return validation


def _candidate_debug(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "src": candidate.get("src") or "",
        "width": int(candidate.get("width") or 0),
        "height": int(candidate.get("height") or 0),
        "role": candidate.get("role") or "",
        "messageUserTurn": int(candidate.get("messageUserTurn") or 0),
        "inAssistant": bool(candidate.get("inAssistant")),
        "inGeneratedImageControl": bool(candidate.get("inGeneratedImageControl")),
        "recognition": candidate.get("recognition") or "",
        "download_source": candidate.get("download_source") or "",
    }


def _download_url_with_browser(
    args: argparse.Namespace,
    cwd: Path,
    url: str,
    output_path: Path,
) -> None:
    output_path.unlink(missing_ok=True)
    script = f"""
(async () => {{
  const response = await fetch({json.dumps(url)}, {{ credentials: "include" }});
  if (!response.ok) {{
    throw new Error(`download fetch failed: ${{response.status}} ${{response.statusText}}`);
  }}
  const blob = await response.blob();
  const dataUrl = await new Promise((resolve, reject) => {{
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  }});
  return JSON.stringify({{ ok: true, size: blob.size, type: blob.type, data_url: dataUrl }});
}})()
"""
    errors: list[str] = []
    for attempt, delay in enumerate((0, 2, 5, 10), start=1):
        if delay:
            time.sleep(delay)
        try:
            output = _eval_js(args, cwd, script, timeout=240).strip()
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                start = output.find("{")
                end = output.rfind("}")
                if start < 0 or end <= start:
                    raise
                payload = json.loads(output[start : end + 1])
            if isinstance(payload, str):
                payload = json.loads(payload)
            data_url = payload.get("data_url", "")
            if "," not in data_url:
                raise RuntimeError("browser fetch did not return a data URL")
            output_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            return
        except Exception as error:
            errors.append(f"attempt {attempt}: {type(error).__name__}: {error}")
    raise RuntimeError("browser-authenticated image download failed after 4 attempts: " + " | ".join(errors))


def _url_requires_browser_credentials(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def _download_urls(
    args: argparse.Namespace,
    cwd: Path,
    urls: list[str],
    output_dir: Path,
    prefix: str,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for index, url in enumerate(urls, start=1):
        output_path = output_dir / f"{prefix}_{index:02d}.png"
        method = "browser_fetch"
        try:
            _download_url_with_browser(args, cwd, url, output_path)
        except Exception:
            if _url_requires_browser_credentials(url):
                raise
            method = "urllib_fallback"
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                with output_path.open("wb") as file:
                    shutil.copyfileobj(response, file)
        validation = _normalize_image_to_png(output_path, output_path)
        downloaded.append(
            {
                "path": str(output_path),
                "source_url": url,
                "download_method": method,
                "image_validation": validation,
            }
        )
    return downloaded


def _download_candidates(
    args: argparse.Namespace,
    cwd: Path,
    candidates: list[dict[str, Any]],
    output_dir: Path,
    prefix: str,
    failures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        output_path = output_dir / f"{prefix}_{index:02d}.png"
        url = candidate.get("src") or ""
        try:
            item = _download_urls(args, cwd, [url], output_dir, f"{prefix}_{index:02d}_fetch")[0]
        except Exception as error:
            if failures is not None:
                failures.append(
                    {
                        "candidate_index": index,
                        "source_url": url,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "recognized_candidate": _candidate_debug(candidate),
                    }
                )
            continue
        item_path = Path(item["path"])
        if item_path != output_path:
            item_path.replace(output_path)
            item["path"] = str(output_path)
            item["image_validation"] = _validate_downloaded_image(output_path)
        item.update(
            {
                "download_method": f"{item.get('download_method', 'url_fetch')}_generated_url",
                "recognized_candidate": _candidate_debug(candidate),
            }
        )
        downloaded.append(item)
    return downloaded


def _message_for_variant(job: dict[str, Any], variant_index: int) -> str:
    for item in job.get("chatgpt_messages") or []:
        if int(item.get("variant_index", 0)) == int(variant_index):
            return item.get("message") or job["chatgpt_message"]
    return job["chatgpt_message"]


def _message_for_label(job: dict[str, Any], label: int | str) -> str:
    if label == "batch":
        return job.get("chatgpt_batch_message") or job["chatgpt_message"]
    return _message_for_variant(job, int(label))


def _expected_image_count(job: dict[str, Any], label: int | str = "batch") -> int:
    if label != "batch":
        return 1
    try:
        return max(1, int(job.get("variant_count") or 1))
    except (TypeError, ValueError):
        return 1


def _collect_generated_images_with_retries(
    args: argparse.Namespace,
    job: dict[str, Any],
    cwd: Path,
    trace_dir: Path,
    baseline: set[str],
    references: list[str],
    *,
    label: int | str,
    mode: str,
    resumed: bool,
    max_failure_retries: int = 2,
    expected_conversation_url: str | None = None,
    baseline_user_message_count: int | None = None,
) -> dict[str, Any]:
    downloaded_all: list[dict[str, Any]] = []
    downloaded: list[dict[str, Any]] = []
    filtered_reference_downloads: list[dict[str, str]] = []
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    download_failures: list[dict[str, Any]] = []
    conversation_restores: list[dict[str, Any]] = []
    conversation_guard: dict[str, Any] | None = None
    active_expected_conversation_url = expected_conversation_url
    expected_count = _expected_image_count(job, label)
    download_limit = _candidate_download_limit(expected_count, references)
    file_prefix = f"{job['job_name']}_agent_browser_{_variant_prefix(label)}_generated"
    started_at = time.time()
    progress_interval = max(5, int(getattr(args, "progress_interval", 20)))
    refresh_interval_s = max(
        0,
        int(
            getattr(
                args,
                "stale_generation_refresh_interval",
                DEFAULT_STALE_GENERATION_REFRESH_SECONDS,
            )
        ),
    )
    page_recoveries: list[dict[str, Any]] = []

    def monitored_page_call(operation: Callable[[], Any], operation_name: str) -> Any:
        result, recovery_events = _run_with_page_recovery(
            args,
            cwd,
            operation,
            phase=f"post_submit_monitor:{operation_name}",
            target_url=active_expected_conversation_url or "https://chatgpt.com/",
            job=job,
            label=label,
            failure_confirmations=3,
        )
        page_recoveries.extend(recovery_events)
        return result

    for retry_index in range(max_failure_retries + 1):
        deadline = time.time() + args.timeout
        generation_wait_started_at = time.time()
        next_progress_at = 0.0
        best_candidates: list[dict[str, Any]] = []
        first_candidate_seen_at: float | None = None
        last_candidate_growth_at: float | None = None
        last_generation_refresh_at: float | None = None
        best_candidate_count = 0
        missing_submitted_turn_heartbeats = 0
        detected_generation_error: dict[str, str] | None = None
        while time.time() < deadline:
            conversation_guard = monitored_page_call(
                lambda: _ensure_expected_conversation(
                    args,
                    cwd,
                    active_expected_conversation_url,
                    baseline_user_message_count,
                ),
                "conversation_guard",
            )
            active_expected_conversation_url = _persist_canonical_conversation(
                job,
                label,
                conversation_guard,
                active_expected_conversation_url,
            )
            if conversation_guard.get("restored"):
                conversation_restores.append(conversation_guard)
            page_text = monitored_page_call(
                lambda: _page_text(args, cwd),
                "page_text",
            )
            policy_refusal = _content_policy_refusal(page_text)
            if policy_refusal:
                _emit_progress(
                    job,
                    label=label,
                    phase="blocked",
                    status="policy_refused",
                    started_at=started_at,
                    expected_count=expected_count,
                    recognized_count=0,
                    retry_index=retry_index,
                )
                result = {
                    **policy_refusal,
                    "label": label,
                    "mode": mode,
                    "resumed": resumed,
                    "image_count": 0,
                    "raw_image_count": 0,
                    "recognized_candidate_count": 0,
                    "expected_image_count": expected_count,
                    "partial": False,
                    "images": [],
                    "failure_retries": failures,
                    "conversation_guard": conversation_guard,
                    "conversation_restores": conversation_restores,
                    "page_recoveries": page_recoveries,
                }
                _write_session_patch(
                    job,
                    {
                        **result,
                        "attempt": {
                            "action": "policy_refusal",
                            "label": label,
                            "mode": mode,
                            "status": "policy_refused",
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "conversation_url": (conversation_guard or {}).get("current_url")
                            or _current_url(args, cwd),
                        },
                    },
                )
                _screenshot(args, cwd, trace_dir / "05_policy_refused.png")
                return result
            rows = monitored_page_call(
                lambda: _image_inventory(args, cwd),
                "image_inventory",
            )
            candidates = _vertical_images(rows, baseline, baseline_user_message_count)
            if len(candidates) > len(best_candidates):
                best_candidates = candidates
            if len(candidates) > 0 and first_candidate_seen_at is None:
                first_candidate_seen_at = time.time()
                last_candidate_growth_at = first_candidate_seen_at
            if len(candidates) > best_candidate_count:
                best_candidate_count = len(candidates)
                last_candidate_growth_at = time.time()
            if time.time() >= next_progress_at:
                page_health = monitored_page_call(
                    lambda: _generation_page_health(
                        args,
                        cwd,
                        active_expected_conversation_url,
                        page_text,
                    ),
                    "heartbeat",
                )
                if page_health.get("human_reason") or page_health.get("challenge_frame"):
                    _activate_for_human_attention(
                        args,
                        str(page_health.get("human_reason") or "anti_automation_verification"),
                    )
                next_missing_heartbeat = missing_submitted_turn_heartbeats + 1
                missing_turn_error = _missing_submitted_turn_error(
                    page_health,
                    baseline_user_message_count=baseline_user_message_count,
                    candidate_count=len(candidates),
                    consecutive_heartbeats=next_missing_heartbeat,
                )
                missing_submitted_turn_heartbeats = (
                    next_missing_heartbeat if page_health["submitted_turn_missing"] else 0
                )
                detected_generation_error = (
                    page_health.get("generation_error") or missing_turn_error
                )
                if detected_generation_error:
                    page_health["status"] = "generation_error"
                    page_health["generation_error"] = detected_generation_error
                    _emit_progress(
                        job,
                        label=label,
                        phase="generation_error",
                        status="generation_failed",
                        started_at=started_at,
                        expected_count=expected_count,
                        recognized_count=len(best_candidates),
                        retry_index=retry_index,
                        page_health=page_health,
                    )
                    break
                _emit_progress(
                    job,
                    label=label,
                    phase="generation_wait",
                    status="waiting_for_generated_images",
                    started_at=started_at,
                    expected_count=expected_count,
                    recognized_count=len(candidates),
                    retry_index=retry_index,
                    page_health=page_health,
                )
                next_progress_at = time.time() + progress_interval
            button_state = monitored_page_call(
                lambda: _checked_generation_button_state(args, cwd),
                "composer_controls",
            )
            generation_active = _generation_active_from_button_state(button_state)
            now = time.time()
            if _stale_generation_refresh_due(
                generation_active=generation_active,
                expected_conversation_url=active_expected_conversation_url,
                started_at=generation_wait_started_at,
                last_candidate_growth_at=last_candidate_growth_at,
                last_generation_refresh_at=last_generation_refresh_at,
                now=now,
                refresh_interval_s=refresh_interval_s,
            ):
                refresh_event = _refresh_expected_conversation(
                    args,
                    cwd,
                    active_expected_conversation_url or "",
                    reason="stale_generation_active_refresh",
                    candidate_count=len(candidates),
                    expected_count=expected_count,
                )
                last_generation_refresh_at = now
                conversation_guard = refresh_event
                conversation_restores.append(refresh_event)
                _emit_progress(
                    job,
                    label=label,
                    phase="generation_wait",
                    status="refreshing_stale_generation_session",
                    started_at=started_at,
                    expected_count=expected_count,
                    recognized_count=len(candidates),
                    retry_index=retry_index,
                )
                _write_session_patch(
                    job,
                    {
                        "status": "generation_wait_refreshing_session",
                        "label": label,
                        "resumed": resumed,
                        "conversation_guard": refresh_event,
                        "conversation_restores": conversation_restores,
                        "page_recoveries": page_recoveries,
                        "recognized_candidate_count": len(candidates),
                        "expected_image_count": expected_count,
                    },
                )
                next_progress_at = 0.0
                continue
            should_download, download_reason = _should_download_candidates(
                candidate_count=len(candidates),
                expected_count=expected_count,
                busy=generation_active,
                download_ready=bool(button_state.get("downloadButtonVisible")),
                first_seen_at=first_candidate_seen_at,
                last_growth_at=last_candidate_growth_at,
                now=now,
            )
            if should_download:
                selected_candidates = candidates[:download_limit]
                recognized_candidates = [_candidate_debug(candidate) for candidate in selected_candidates]
                _emit_progress(
                    job,
                    label=label,
                    phase="download",
                    status="downloading_generated_images"
                    if download_reason == "complete"
                    else "downloading_partial_generated_images",
                    started_at=started_at,
                    expected_count=expected_count,
                    recognized_count=len(selected_candidates),
                    retry_index=retry_index,
                )
                downloaded_all = _download_candidates(
                    args,
                    cwd,
                    selected_candidates,
                    Path(job["download_dir"]),
                    file_prefix,
                    download_failures,
                )
                downloaded, filtered_reference_downloads = _filter_reference_downloads(
                    downloaded_all,
                    references,
                )
                downloaded = _keep_expected_downloads(downloaded, expected_count)
                if downloaded:
                    status = "downloaded" if len(downloaded) >= expected_count else "partial_downloaded"
                    completion_metadata = _generation_completion_metadata(
                        status=status,
                        image_count=len(downloaded),
                        expected_count=expected_count,
                        download_reason=download_reason,
                    )
                    elapsed_seconds = time.time() - started_at
                    _write_timing_sample(expected_count, elapsed_seconds, len(downloaded))
                    _write_session_patch(
                        job,
                        {
                            "status": status,
                            "label": label,
                            "resumed": resumed,
                            "generation_elapsed_seconds": round(elapsed_seconds, 1),
                            "raw_image_count": len(downloaded_all),
                            "recognized_candidate_count": len(selected_candidates),
                            "recognized_candidates": recognized_candidates,
                            "download_reason": download_reason,
                            "filtered_reference_downloads": filtered_reference_downloads,
                            "output_count": len(downloaded),
                            "expected_image_count": expected_count,
                            "shortfall_count": max(0, expected_count - len(downloaded)),
                            "conversation_guard": conversation_guard,
                            "conversation_restores": conversation_restores,
                            "page_recoveries": page_recoveries,
                            **completion_metadata,
                            "outputs": [item["path"] for item in downloaded],
                            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "attempt": {
                                "action": "collect_generated_images",
                                "label": label,
                                "mode": mode,
                                "status": status,
                                "output_count": len(downloaded),
                                "expected_count": expected_count,
                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "conversation_url": (conversation_guard or {}).get("current_url")
                                or _current_url(args, cwd),
                            },
                        },
                    )
                    _screenshot(args, cwd, trace_dir / "05_after_generated.png")
                    return {
                        "raw_image_count": len(downloaded_all),
                        "recognized_candidate_count": len(selected_candidates),
                        "recognized_candidates": recognized_candidates,
                        "download_reason": download_reason,
                        "filtered_reference_downloads": filtered_reference_downloads,
                        "image_count": len(downloaded),
                        "images": downloaded,
                        "failure_retries": failures,
                        "status": status,
                        "partial": len(downloaded) < expected_count,
                        "expected_image_count": expected_count,
                        "conversation_guard": conversation_guard,
                        "conversation_restores": conversation_restores,
                        "page_recoveries": page_recoveries,
                        **completion_metadata,
                    }
                if download_failures:
                    result = {
                        "status": "download_failed",
                        "error_type": "generated_image_download_failed",
                        "retryable": True,
                        "terminal": False,
                        "recommended_next_action": "collect_current_first",
                        "generation_state": "complete_download_pending",
                        "safe_to_fallback": False,
                        "should_collect_current_first": True,
                        "label": label,
                        "mode": mode,
                        "resumed": resumed,
                        "image_count": 0,
                        "raw_image_count": 0,
                        "recognized_candidate_count": len(selected_candidates),
                        "recognized_candidates": recognized_candidates,
                        "expected_image_count": expected_count,
                        "partial": False,
                        "images": [],
                        "download_failures": download_failures,
                        "failure_retries": failures,
                        "conversation_guard": conversation_guard,
                        "conversation_restores": conversation_restores,
                        "page_recoveries": page_recoveries,
                    }
                    _write_session_patch(job, {**result, "label": label})
                    _screenshot(args, cwd, trace_dir / "05_download_failed.png")
                    return result
            if len(candidates) < expected_count:
                _scroll_down(args, cwd, 700)
                time.sleep(2)
                continue
            time.sleep(5)

        conversation_guard = monitored_page_call(
            lambda: _ensure_expected_conversation(
                args,
                cwd,
                active_expected_conversation_url,
                baseline_user_message_count,
            ),
            "final_conversation_guard",
        )
        active_expected_conversation_url = _persist_canonical_conversation(
            job,
            label,
            conversation_guard,
            active_expected_conversation_url,
        )
        if conversation_guard.get("restored"):
            conversation_restores.append(conversation_guard)
        page_text = monitored_page_call(
            lambda: _page_text(args, cwd),
            "final_page_text",
        )
        if not detected_generation_error:
            page_health = monitored_page_call(
                lambda: _generation_page_health(
                    args,
                    cwd,
                    active_expected_conversation_url,
                    page_text,
                ),
                "final_heartbeat",
            )
            if page_health.get("human_reason") or page_health.get("challenge_frame"):
                _activate_for_human_attention(
                    args,
                    str(page_health.get("human_reason") or "anti_automation_verification"),
                )
            detected_generation_error = page_health.get("generation_error")
        policy_refusal = _content_policy_refusal(page_text)
        if policy_refusal:
            result = {
                **policy_refusal,
                "label": label,
                "mode": mode,
                "resumed": resumed,
                "image_count": 0,
                "raw_image_count": 0,
                "recognized_candidate_count": 0,
                "expected_image_count": expected_count,
                "partial": False,
                "images": [],
                "failure_retries": failures,
                "conversation_guard": conversation_guard,
                "conversation_restores": conversation_restores,
                "page_recoveries": page_recoveries,
            }
            _write_session_patch(
                job,
                {
                    **result,
                    "attempt": {
                        "action": "policy_refusal",
                        "label": label,
                        "mode": mode,
                        "status": "policy_refused",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "conversation_url": (conversation_guard or {}).get("current_url") or _current_url(args, cwd),
                    },
                },
            )
            _screenshot(args, cwd, trace_dir / "05_policy_refused.png")
            return result
        if best_candidates and not detected_generation_error:
            download_reason = (
                "complete"
                if len(best_candidates) >= expected_count
                else "timeout_partial"
            )
            selected_candidates = best_candidates[:download_limit]
            recognized_candidates = [_candidate_debug(candidate) for candidate in selected_candidates]
            _emit_progress(
                job,
                label=label,
                phase="download",
                status="downloading_partial_generated_images",
                started_at=started_at,
                expected_count=expected_count,
                recognized_count=len(selected_candidates),
                retry_index=retry_index,
            )
            downloaded_all = _download_candidates(
                args,
                cwd,
                selected_candidates,
                Path(job["download_dir"]),
                file_prefix,
                download_failures,
            )
            downloaded, filtered_reference_downloads = _filter_reference_downloads(
                downloaded_all,
                references,
            )
            downloaded = _keep_expected_downloads(downloaded, expected_count)
            completion_metadata = _generation_completion_metadata(
                status="partial_downloaded",
                image_count=len(downloaded),
                expected_count=expected_count,
                download_reason=download_reason,
            )
            elapsed_seconds = time.time() - started_at
            _write_timing_sample(expected_count, elapsed_seconds, len(downloaded))
            _write_session_patch(
                job,
                {
                    "status": "partial_downloaded",
                    "label": label,
                    "resumed": resumed,
                    "generation_elapsed_seconds": round(elapsed_seconds, 1),
                    "raw_image_count": len(downloaded_all),
                    "recognized_candidate_count": len(selected_candidates),
                    "recognized_candidates": recognized_candidates,
                    "download_reason": download_reason,
                    "filtered_reference_downloads": filtered_reference_downloads,
                    "download_failures": download_failures,
                    "output_count": len(downloaded),
                    "expected_image_count": expected_count,
                    "shortfall_count": max(0, expected_count - len(downloaded)),
                    "conversation_guard": conversation_guard,
                    "conversation_restores": conversation_restores,
                    "page_recoveries": page_recoveries,
                    **completion_metadata,
                    "outputs": [item["path"] for item in downloaded],
                    "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "attempt": {
                        "action": "collect_generated_images",
                        "label": label,
                        "mode": mode,
                        "status": "partial_downloaded",
                        "output_count": len(downloaded),
                        "expected_count": expected_count,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "conversation_url": (conversation_guard or {}).get("current_url") or _current_url(args, cwd),
                    },
                },
            )
            return {
                "raw_image_count": len(downloaded_all),
                "recognized_candidate_count": len(selected_candidates),
                "recognized_candidates": recognized_candidates,
                "download_reason": download_reason,
                "filtered_reference_downloads": filtered_reference_downloads,
                "download_failures": download_failures,
                "image_count": len(downloaded),
                "images": downloaded,
                "failure_retries": failures,
                "status": "partial_downloaded",
                "partial": len(downloaded) < expected_count,
                "expected_image_count": expected_count,
                "conversation_guard": conversation_guard,
                "conversation_restores": conversation_restores,
                "page_recoveries": page_recoveries,
                **completion_metadata,
            }
        if not detected_generation_error:
            _write_session_patch(
                job,
                {
                    "status": "timeout_no_images",
                    "label": label,
                    "resumed": resumed,
                    "conversation_guard": conversation_guard,
                    "conversation_restores": conversation_restores,
                    "page_recoveries": page_recoveries,
                },
            )
            raise TimeoutError("No generated image appeared before timeout")

        retry_available = retry_index < max_failure_retries
        failure = {
            "retry_index": retry_index,
            "retry_available": retry_available,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "conversation_url": (conversation_guard or {}).get("current_url") or _current_url(args, cwd),
            "generation_error": detected_generation_error,
        }
        failures.append(failure)
        _write_session_patch(
            job,
            {
                "status": "generation_failed_retrying" if retry_available else "generation_failed_no_retry_left",
                "label": label,
                "resumed": resumed,
                "conversation_guard": conversation_guard,
                "conversation_restores": conversation_restores,
                "page_recoveries": page_recoveries,
                "attempt": {"action": "failure", "label": label, "mode": mode, **failure},
            },
        )
        if not retry_available or not _click_try_again(args, cwd):
            result = {
                "status": "generation_failed",
                "error_type": detected_generation_error.get(
                    "error_type", "chatgpt_generation_error"
                ),
                "retryable": True,
                "terminal": True,
                "recommended_next_action": "submit_new_request",
                "generation_state": "failed",
                "safe_to_fallback": False,
                "should_collect_current_first": False,
                "label": label,
                "mode": mode,
                "resumed": resumed,
                "image_count": 0,
                "raw_image_count": 0,
                "recognized_candidate_count": 0,
                "expected_image_count": expected_count,
                "partial": False,
                "images": [],
                "generation_error": detected_generation_error,
                "failure_retries": failures,
                "conversation_guard": conversation_guard,
                "conversation_restores": conversation_restores,
                "page_recoveries": page_recoveries,
            }
            _write_session_patch(job, {**result, "label": label})
            _screenshot(args, cwd, trace_dir / "05_generation_failed.png")
            return result
        conversation_url = _wait_for_conversation_url(args, cwd)
        active_expected_conversation_url = conversation_url
        _write_session_patch(
            job,
            {
                "status": "retry_submitted",
                "label": label,
                "resumed": resumed,
                "conversation_id": _conversation_id_from_url(conversation_url),
                "conversation_url": conversation_url,
                "attempt": {
                    "action": "retry",
                    "label": label,
                    "mode": mode,
                    "retry_index": retry_index + 1,
                    "conversation_url": conversation_url,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            },
        )

    raise RuntimeError("ChatGPT image generation failed")


def _trace_dir_for_label(job: dict[str, Any], label: int | str) -> Path:
    if label == "batch":
        return Path(job["download_dir"]) / "agent_browser_trace_submit"
    return Path(job["download_dir"]) / f"agent_browser_trace_{_variant_prefix(label)}"


def _report_path_for_label(trace_dir: Path, label: int | str) -> Path:
    if label == "batch":
        return trace_dir / "submit_report.json"
    return trace_dir / f"{_variant_prefix(label)}_submit_report.json"


def _fully_downloaded(result: dict[str, Any]) -> bool:
    try:
        image_count = int(result.get("image_count") or 0)
        expected_count = int(result.get("expected_image_count") or 0)
    except (TypeError, ValueError):
        return False
    return (
        result.get("status") == "downloaded"
        and expected_count > 0
        and image_count >= expected_count
    )


def _prune_success_trace_screenshots(
    job: dict[str, Any],
    report: dict[str, Any],
    *,
    mode: str,
    start_variant: int,
    end_variant: int | None,
    keep_success_trace_screenshots: bool,
) -> dict[str, Any]:
    retention = {
        "policy": (
            "all"
            if keep_success_trace_screenshots or mode == "dry-upload"
            else "failures_only"
        ),
        "removed_count": 0,
        "reclaimed_bytes": 0,
        "preserved_failure_screenshots": True,
    }
    if keep_success_trace_screenshots or mode == "dry-upload":
        return retention

    output_dir = Path(job["download_dir"]).resolve()
    successful_trace_dirs: set[Path] = set()
    if mode == "collect-current":
        if _fully_downloaded(report) and report.get("trace_dir"):
            successful_trace_dirs.add(Path(str(report["trace_dir"])).resolve())
    else:
        request_mode = job.get("request_mode") or "independent_variants"
        if request_mode == "independent_variants" and mode == "single-batch-submit":
            last_variant = min(
                int(end_variant or job.get("variant_count") or 1),
                int(job.get("variant_count") or 1),
            )
            executed_labels: set[int | str] = set(
                range(max(1, int(start_variant)), last_variant + 1)
            )
        else:
            executed_labels = {"batch"}
        for variant in report.get("variants") or []:
            label = variant.get("variant_index")
            if label not in executed_labels:
                try:
                    label = int(label)
                except (TypeError, ValueError):
                    pass
            downloaded = variant.get("downloaded") or {}
            if label not in executed_labels or not _fully_downloaded(downloaded):
                continue
            report_path = str(variant.get("report_path") or "")
            trace_dir = (
                Path(report_path).resolve().parent
                if report_path
                else _trace_dir_for_label(job, label).resolve()
            )
            successful_trace_dirs.add(trace_dir)

    for trace_dir in successful_trace_dirs:
        try:
            trace_dir.relative_to(output_dir)
        except ValueError:
            continue
        if not trace_dir.name.startswith("agent_browser_trace"):
            continue
        for name in ROUTINE_TRACE_SCREENSHOT_NAMES:
            screenshot = trace_dir / name
            try:
                if not screenshot.is_file():
                    continue
                stat = screenshot.stat()
                screenshot.unlink()
            except OSError:
                continue
            retention["removed_count"] += 1
            retention["reclaimed_bytes"] += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
    return retention


def _persist_trace_retention(report: dict[str, Any], retention: dict[str, Any]) -> None:
    report["trace_retention"] = retention
    artifact_paths = {
        str(report.get("summary_path") or ""),
        str(report.get("report_path") or ""),
    }
    for artifact_path in artifact_paths - {""}:
        path = Path(artifact_path)
        artifact = _read_json_if_exists(path)
        if artifact is None:
            continue
        artifact["trace_retention"] = retention
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_summary(
    job: dict[str, Any],
    *,
    request_mode: str,
    variant_count: int,
    start_variant: int,
    end_variant: int | str,
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    summary_path = Path(job["download_dir"]) / "chatgpt_web_run_summary.json"
    summary = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "job_name": job["job_name"],
        "prompt_card": job["prompt_card"],
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "variant_count": variant_count,
        "request_mode": request_mode,
        "image_generation_mode": job.get("image_generation_mode") or "create_image",
        "output_aspect_ratio": job.get("output_aspect_ratio") or "1:1",
        **reference_audit_fields(job),
        "start_variant": start_variant,
        "end_variant": end_variant,
        "variants": variants,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**summary, "summary_path": str(summary_path)}


def _summary_path(job: dict[str, Any]) -> Path:
    return Path(job["download_dir"]) / "chatgpt_web_run_summary.json"


def _summary_conversation_url(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    variants = summary.get("variants") or []
    for variant in variants:
        if variant.get("variant_index") == "batch":
            url = str(variant.get("conversation_url") or "")
            if _conversation_id_from_url(url):
                return url
    for variant in variants:
        url = str(variant.get("conversation_url") or "")
        if _conversation_id_from_url(url):
            return url
    return str(summary.get("conversation_url") or "")


def _collect_current_target_url(
    *,
    current_url: str,
    existing_session: dict[str, Any],
    summary: dict[str, Any] | None,
) -> str:
    summary_url = _summary_conversation_url(summary)
    if _conversation_id_from_url(summary_url):
        return summary_url
    session_url = str(existing_session.get("conversation_url") or "")
    if _conversation_id_from_url(session_url):
        return session_url
    return current_url if _conversation_id_from_url(current_url) else ""


def _update_summary_for_collect_current(
    job: dict[str, Any],
    *,
    conversation_url: str,
    report_path: Path,
    downloaded: list[dict[str, Any]],
    status: str,
    expected_count: int,
) -> Path:
    summary_path = _summary_path(job)
    summary = _read_json_if_exists(summary_path) or {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "job_name": job["job_name"],
        "prompt_card": job["prompt_card"],
        "variant_count": max(1, int(job.get("variant_count") or 1)),
        "request_mode": job.get("request_mode") or "single_batch",
        "image_generation_mode": job.get("image_generation_mode") or "create_image",
            "output_aspect_ratio": job.get("output_aspect_ratio") or "1:1",
            **reference_audit_fields(job),
        "start_variant": 1,
        "end_variant": "batch",
        "variants": [],
    }
    summary.update(
        {
            "schema_version": 1,
            "adapter": "agent_browser_cdp",
            "job_name": job["job_name"],
            "prompt_card": job["prompt_card"],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **reference_audit_fields(job),
        }
    )
    variants = list(summary.get("variants") or [])
    aspect_ratio_validation = _annotate_aspect_ratio_validation(
        job,
        {"images": downloaded},
    )
    batch_variant = {
        "variant_index": "batch",
        "submitted": True,
        "resumed": True,
        "conversation_url": conversation_url,
        "report_path": str(report_path),
        "downloaded": {
            "status": status,
            "image_count": len(downloaded),
            "expected_image_count": expected_count,
            "partial": bool(len(downloaded) < expected_count),
            **_generation_completion_metadata(
                status=status,
                image_count=len(downloaded),
                expected_count=expected_count,
                download_reason="partial_terminal" if len(downloaded) < expected_count else status,
            ),
            "aspect_ratio_validation": aspect_ratio_validation,
            "images": downloaded,
        },
    }
    replaced = False
    for index, variant in enumerate(variants):
        if variant.get("variant_index") == "batch":
            variants[index] = batch_variant
            replaced = True
            break
    if not replaced:
        variants.append(batch_variant)
    summary["variants"] = variants
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary_path


def _variant_summary_from_report(
    *,
    variant_index: int,
    report: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    resumed = report.get("resumed")
    if resumed is None:
        resumed = "resume" in str(report.get("mode") or "")
    return {
        "variant_index": variant_index,
        "submitted": bool(report.get("submitted", True)),
        "resumed": resumed,
        "conversation_url": report.get("conversation_url"),
        "report_path": str(report_path),
        "chat_surface": report.get("chat_surface"),
        "image_mode": report.get("image_mode"),
        "aspect_ratio_delivery": report.get("aspect_ratio_delivery"),
        "reference_upload": report.get("upload_observation"),
        "downloaded": {
            "status": report.get("status"),
            "error_type": report.get("error_type"),
            "retryable": report.get("retryable"),
            "terminal": report.get("terminal"),
            "recommended_next_action": report.get("recommended_next_action"),
            "image_count": report.get("image_count", 0),
            "expected_image_count": report.get("expected_image_count"),
            "partial": bool(report.get("partial")),
            "generation_state": report.get("generation_state"),
            "safe_to_fallback": report.get("safe_to_fallback"),
            "should_collect_current_first": report.get("should_collect_current_first"),
            "missing_image_count": report.get("missing_image_count"),
            "aspect_ratio_validation": report.get("aspect_ratio_validation"),
            "images": report.get("images", []),
        },
    }


def _variant_summary_from_session(
    job: dict[str, Any],
    variant_index: int,
) -> dict[str, Any] | None:
    session = _read_json_if_exists(_session_path(job, variant_index)) or {}
    conversation_url = str(session.get("conversation_url") or "")
    outputs = [str(path) for path in session.get("outputs") or []]
    if not _conversation_id_from_url(conversation_url) and not outputs:
        return None
    output_count = int(session.get("output_count") or len(outputs))
    expected_count = int(session.get("expected_image_count") or 1)
    status = session.get("status") or ("downloaded" if output_count >= expected_count else "partial_downloaded")
    return {
        "variant_index": variant_index,
        "submitted": True,
        "resumed": session.get("resumed"),
        "conversation_url": conversation_url,
        "report_path": "",
        "chat_surface": session.get("chat_surface"),
        "downloaded": {
            "status": status,
            "image_count": output_count,
            "expected_image_count": expected_count,
            "partial": bool(output_count < expected_count),
            "generation_state": session.get("generation_state"),
            "safe_to_fallback": session.get("safe_to_fallback"),
            "should_collect_current_first": session.get("should_collect_current_first"),
            "missing_image_count": session.get("missing_image_count"),
            "images": [{"path": path} for path in outputs],
        },
    }


def _existing_independent_variant_summaries(
    job: dict[str, Any],
    variant_count: int,
) -> list[dict[str, Any]]:
    variants_by_index: dict[int, dict[str, Any]] = {}
    existing_summary = _read_json_if_exists(_summary_path(job)) or {}
    for variant in existing_summary.get("variants") or []:
        try:
            index = int(variant.get("variant_index"))
        except (TypeError, ValueError):
            continue
        if 1 <= index <= variant_count:
            variants_by_index[index] = variant

    for index in range(1, variant_count + 1):
        trace_dir = _trace_dir_for_label(job, index)
        report_path = _report_path_for_label(trace_dir, index)
        report = _read_json_if_exists(report_path)
        if report:
            variants_by_index[index] = _variant_summary_from_report(
                variant_index=index,
                report=report,
                report_path=report_path,
            )
            continue
        session_variant = _variant_summary_from_session(job, index)
        if session_variant:
            variants_by_index.setdefault(index, session_variant)

    return [variants_by_index[index] for index in sorted(variants_by_index)]


def _write_independent_variant_summary(
    job: dict[str, Any],
    *,
    variant_count: int,
    new_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    variants_by_index: dict[int, dict[str, Any]] = {}
    for variant in _existing_independent_variant_summaries(job, variant_count):
        variants_by_index[int(variant["variant_index"])] = variant
    for variant in new_variants:
        variants_by_index[int(variant["variant_index"])] = variant

    variants = [variants_by_index[index] for index in sorted(variants_by_index)]
    if variants:
        start_variant = min(int(variant["variant_index"]) for variant in variants)
        end_variant = max(int(variant["variant_index"]) for variant in variants)
    else:
        start_variant = 1
        end_variant = variant_count
    return _write_summary(
        job,
        request_mode="independent_variants",
        variant_count=variant_count,
        start_variant=start_variant,
        end_variant=end_variant,
        variants=variants,
    )


def _run_one_request(
    args: argparse.Namespace,
    job: dict[str, Any],
    cwd: Path,
    *,
    label: int | str,
    message: str,
    resume: bool,
    conversation_session_path: Path | None = None,
) -> dict[str, Any]:
    trace_dir = _trace_dir_for_label(job, label)
    trace_dir.mkdir(parents=True, exist_ok=True)
    is_followup = conversation_session_path is not None
    submit_mode = (
        "conversation_followup_submit"
        if is_followup
        else ("single_batch_submit" if label == "batch" else "independent_variant_submit")
    )
    resume_mode = (
        "conversation_followup_resume"
        if is_followup
        else ("single_batch_resume" if label == "batch" else "independent_variant_resume")
    )

    references = [ref["path"] for ref in job.get("reference_images", [])]
    existing_session = (_read_json_if_exists(_session_path(job, label)) or {}) if resume else {}
    if existing_session.get("error_type") == "reference_upload_failed" or existing_session.get("status") == "reference_upload_failed":
        existing_session = {}
    existing_conversation_url = existing_session.get("conversation_url")
    if existing_conversation_url:
        _validate_session_reference_mapping(job, existing_session)
        conversation_url = _open_conversation(args, cwd, existing_conversation_url)
        _wait_until_idle(args, cwd)
        chat_surface = _ensure_chat_surface(
            args,
            cwd,
            recorded=existing_session.get("chat_surface"),
            timeout_s=2,
        )
        account_guard = _validate_account_lane(args, cwd, job, label)
        _screenshot(args, cwd, trace_dir / "01_resumed.png")
        baseline = set(existing_session.get("baseline_asset_urls") or [])
        _write_session_patch(
            job,
            {
                "status": "resumed",
                "label": label,
                "resumed": True,
                "conversation_id": _conversation_id_from_url(conversation_url),
                "conversation_url": conversation_url,
                "agent_browser_profile": _profile_label(args),
                "account_lane": TARGET_CHATGPT_ACCOUNT_SIGNAL,
                "account_guard": account_guard,
                "chat_surface": existing_session.get("chat_surface") or chat_surface,
                "attempt": {
                    "action": "resume",
                    "label": label,
                    "mode": submit_mode,
                    "conversation_url": conversation_url,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            },
        )
        rename = existing_session.get("title_rename") or {
            "attempted": False,
            "renamed": False,
            "reason": "continued_conversation_keeps_existing_title",
        }
        if not existing_session.get("continued_from"):
            rename = _rename_current_conversation(args, job, cwd, conversation_url)
            _write_session_patch(job, {"label": label, "title_rename": rename})
        collected = _collect_generated_images_with_retries(
            args,
            job,
            cwd,
            trace_dir,
            baseline,
            references,
            label=label,
            mode=resume_mode,
            resumed=True,
            max_failure_retries=args.max_failure_retries,
            expected_conversation_url=conversation_url,
            baseline_user_message_count=existing_session.get("baseline_user_message_count"),
        )
        aspect_ratio_validation = _annotate_aspect_ratio_validation(job, collected)
        conversation_url = str(
            (collected.get("conversation_guard") or {}).get("current_url") or conversation_url
        )
        if not existing_session.get("continued_from"):
            rename = _retry_conversation_rename(args, job, cwd, conversation_url, rename)
            _write_session_patch(job, {"label": label, "title_rename": rename})
        report = {
            "schema_version": 1,
            "adapter": "agent_browser_cdp",
            "mode": resume_mode,
            "label": label,
            "prompt_card": job["prompt_card"],
            "agent_browser_session": _agent_browser_session_record(args),
            "conversation_url": conversation_url,
            "trace_dir": str(trace_dir),
            "reference_count": len(references),
            **reference_audit_fields(job),
            "account_guard": account_guard,
            "chat_surface": chat_surface,
            "image_mode": existing_session.get("image_mode"),
            "aspect_ratio_delivery": existing_session.get("aspect_ratio_delivery"),
            "aspect_ratio_validation": aspect_ratio_validation,
            "upload_observation": existing_session.get("reference_upload"),
            **({"continued_from": existing_session.get("continued_from")} if existing_session.get("continued_from") else {}),
            **collected,
        }
        report_path = _report_path_for_label(trace_dir, label)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {**report, "report_path": str(report_path), "resumed": True}

    expected_count = _expected_image_count(job, label)
    continued_from: dict[str, Any] | None = None
    if conversation_session_path is not None:
        source_path = conversation_session_path.resolve()
        destination_path = _session_path(job, label).resolve()
        if source_path == destination_path:
            raise ValueError(
                "Conversation follow-up must use a separate job/output session file so the "
                "original turn is not overwritten."
            )
        continued_from = _conversation_source_session(source_path)
        submit_throttle = {"enabled": False, "reason": "conversation_followup"}
        prepare_target_url = continued_from["conversation_url"]
    else:
        submit_throttle = _wait_for_submit_throttle_slot(
            args,
            job,
            label=label,
            expected_count=expected_count,
        )
        prepare_target_url = "https://chatgpt.com/"

    def prepare_surface() -> dict[str, Any]:
        if continued_from:
            _open_conversation(args, cwd, continued_from["conversation_url"])
            _wait_for_followup_composer(args, cwd)
            _wait_for_conversation_history(args, cwd)
            chat_surface = _ensure_chat_surface(
                args,
                cwd,
                recorded=continued_from.get("chat_surface"),
                timeout_s=2,
            )
            open_screenshot = trace_dir / "01_conversation_followup.png"
        else:
            _open_new_chat(args, cwd)
            _wait_for_prompt_box(args, cwd)
            chat_surface = _ensure_chat_surface(args, cwd)
            open_screenshot = trace_dir / "01_open.png"
        account_guard = _validate_account_lane(args, cwd, job, label)
        _screenshot(args, cwd, open_screenshot)
        image_mode = _enable_image_mode_if_available(
            args,
            cwd,
            "" if _owned_tab_cdp_url(args) else _snapshot(args, cwd),
        )
        aspect_ratio_delivery = _configure_aspect_ratio(
            args,
            cwd,
            str(job.get("output_aspect_ratio") or "1:1"),
        )
        upload_observation = _upload_references_in_order(args, cwd, references)
        _require_verified_reference_upload_order(references, upload_observation)
        _wait_for_prompt_box(args, cwd)
        chat_surface = _ensure_chat_surface(args, cwd, recorded=chat_surface, timeout_s=2)
        _screenshot(args, cwd, trace_dir / "02_after_upload.png")
        after_upload = _page_text(args, cwd) if _owned_tab_cdp_url(args) else _snapshot(args, cwd)
        _paste_prompt(args, cwd, message)
        _screenshot(args, cwd, trace_dir / "03_after_prompt.png")
        baseline_rows = _image_inventory(args, cwd)
        baseline = {row.get("src") for row in baseline_rows if row.get("src")}
        baseline_message_counts = _conversation_message_counts(args, cwd)
        return {
            "chat_surface": chat_surface,
            "account_guard": account_guard,
            "image_mode": image_mode,
            "aspect_ratio_delivery": aspect_ratio_delivery,
            "upload_observation": upload_observation,
            "after_upload": after_upload,
            "baseline": baseline,
            "baseline_message_counts": baseline_message_counts,
        }

    prepared, page_recoveries = _run_with_page_recovery(
        args,
        cwd,
        prepare_surface,
        phase="pre_submit_prepare",
        target_url=prepare_target_url,
        job=job,
        label=label,
    )
    chat_surface = prepared["chat_surface"]
    account_guard = prepared["account_guard"]
    image_mode = prepared["image_mode"]
    aspect_ratio_delivery = prepared["aspect_ratio_delivery"]
    upload_observation = prepared["upload_observation"]
    after_upload = prepared["after_upload"]
    baseline = prepared["baseline"]
    baseline_message_counts = prepared["baseline_message_counts"]
    upload_mentions = {Path(path).name: Path(path).name in after_upload for path in references}

    try:
        _submit_prompt(
            args,
            cwd,
            message,
            trace_dir=trace_dir,
        )
    except HumanAttentionRequired:
        raise
    except (TimeoutError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        submission_report = {
            "status": "submission_state_unknown",
            "error_type": "chatgpt_submission_state_unknown",
            "retryable": False,
            "terminal": True,
            "recommended_next_action": "inspect_or_collect_current_before_resubmitting",
            "submission_committed": "unknown",
            "label": label,
            "reason": f"{type(error).__name__}: {error}",
        }
        _write_session_patch(job, submission_report)
        raise SubmissionStateUnknownError(submission_report) from error
    _write_session_patch(
        job,
        {
            "status": "submission_committed",
            "label": label,
            "submission_committed": True,
            "page_recoveries": page_recoveries,
            "attempt": {
                "action": "submission_committed",
                "label": label,
                "mode": submit_mode,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        },
    )
    try:
        post_submit_target_url = _current_url(args, cwd)
    except RuntimeError:
        post_submit_target_url = prepare_target_url
    if not post_submit_target_url.startswith("https://chatgpt.com/"):
        post_submit_target_url = prepare_target_url

    def confirm_submitted_turn() -> dict[str, str]:
        _wait_ms(args, cwd, 3000)
        conversation_url = _wait_for_conversation_url(args, cwd)
        if not _conversation_id_from_url(conversation_url):
            raise RecoverablePageError(
                "The prompt was submitted, but its ChatGPT conversation URL did not become available."
            )
        _screenshot(args, cwd, trace_dir / "04_after_submit.png")
        after_submit = _page_text(args, cwd) if _owned_tab_cdp_url(args) else _snapshot(args, cwd)
        return {"conversation_url": conversation_url, "after_submit": after_submit}

    submitted_turn, post_submit_recoveries = _run_with_page_recovery(
        args,
        cwd,
        confirm_submitted_turn,
        phase="post_submit_confirm",
        target_url=post_submit_target_url,
        job=job,
        label=label,
        failure_confirmations=3,
    )
    page_recoveries.extend(post_submit_recoveries)
    conversation_url = submitted_turn["conversation_url"]
    if continued_from and _conversation_id_from_url(conversation_url) != continued_from["conversation_id"]:
        raise RuntimeError(
            "Conversation follow-up left the requested ChatGPT conversation: "
            f"expected={continued_from['conversation_url']} current={conversation_url}"
        )
    after_submit = submitted_turn["after_submit"]
    upload_failure = _reference_upload_failure(after_submit, references)
    if upload_failure:
        return _upload_failure_report(
            job=job,
            args=args,
            label=label,
            mode=submit_mode,
            trace_dir=trace_dir,
            references=references,
            account_guard=account_guard,
            failure=upload_failure,
            conversation_url=conversation_url,
            continued_from=continued_from,
        )
    _write_session_patch(
        job,
        {
            "status": "submitted",
            "label": label,
            "resumed": False,
            "submission_committed": True,
            "conversation_id": _conversation_id_from_url(conversation_url),
            "conversation_url": conversation_url,
            "baseline_asset_urls": list(baseline),
            "baseline_user_message_count": baseline_message_counts["user_message_count"],
            "baseline_assistant_message_count": baseline_message_counts["assistant_message_count"],
            **reference_audit_fields(job),
            "chat_surface": chat_surface,
            "image_mode": image_mode,
            "aspect_ratio_delivery": aspect_ratio_delivery,
            "reference_upload": {
                key: value
                for key, value in upload_observation.items()
                if key != "page_text"
            },
            "agent_browser_profile": _profile_label(args),
            "account_lane": TARGET_CHATGPT_ACCOUNT_SIGNAL,
            "account_guard": account_guard,
            "submit_throttle": submit_throttle,
            "page_recoveries": page_recoveries,
            **({"continued_from": continued_from} if continued_from else {}),
            "attempt": {
                "action": "conversation_followup_submit" if continued_from else "submit",
                "label": label,
                "mode": submit_mode,
                "conversation_url": conversation_url,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        },
    )
    rename = {
        "attempted": False,
        "renamed": False,
        "reason": "continued_conversation_keeps_existing_title",
    }
    if not continued_from:
        rename = _rename_current_conversation(args, job, cwd, conversation_url)
        _write_session_patch(job, {"label": label, "title_rename": rename})

    collected = _collect_generated_images_with_retries(
        args,
        job,
        cwd,
        trace_dir,
        baseline,
        references,
        label=label,
        mode=submit_mode,
        resumed=False,
        max_failure_retries=args.max_failure_retries,
        expected_conversation_url=conversation_url,
        baseline_user_message_count=baseline_message_counts["user_message_count"],
    )
    aspect_ratio_validation = _annotate_aspect_ratio_validation(job, collected)
    conversation_url = str(
        (collected.get("conversation_guard") or {}).get("current_url") or conversation_url
    )
    if not continued_from:
        rename = _retry_conversation_rename(args, job, cwd, conversation_url, rename)
        _write_session_patch(job, {"label": label, "title_rename": rename})

    report = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "mode": submit_mode,
        "label": label,
        "submission_committed": True,
        "prompt_card": job["prompt_card"],
        "agent_browser_session": _agent_browser_session_record(args),
        "conversation_url": conversation_url,
        "reference_count": len(references),
        **reference_audit_fields(job),
        "trace_dir": str(trace_dir),
        "account_guard": account_guard,
        "chat_surface": chat_surface,
        "image_mode": image_mode,
        "aspect_ratio_delivery": aspect_ratio_delivery,
        "aspect_ratio_validation": aspect_ratio_validation,
        "upload_mentions": upload_mentions,
        "upload_observation": {
            key: value
            for key, value in upload_observation.items()
            if key != "page_text"
        },
        "submit_throttle": submit_throttle,
        "page_recoveries": page_recoveries,
        **({"continued_from": continued_from} if continued_from else {}),
        **collected,
    }
    report_path = _report_path_for_label(trace_dir, label)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report_path": str(report_path), "resumed": False}


def run_submit(args: argparse.Namespace, job: dict[str, Any], cwd: Path) -> dict[str, Any]:
    request_mode = job.get("request_mode") or "independent_variants"
    variant_count = max(1, int(job.get("variant_count") or 1))
    variants: list[dict[str, Any]] = []

    if request_mode == "single_batch":
        report = _run_one_request(
            args,
            job,
            cwd,
            label="batch",
            message=_message_for_label(job, "batch"),
            resume=not args.no_resume,
        )
        variants.append(
            {
                "variant_index": "batch",
                "submitted": bool(report.get("submitted", True)),
                "resumed": report.get("resumed"),
                "conversation_url": report.get("conversation_url"),
                "report_path": report.get("report_path"),
                "chat_surface": report.get("chat_surface"),
                "image_mode": report.get("image_mode"),
                "aspect_ratio_delivery": report.get("aspect_ratio_delivery"),
                "reference_upload": report.get("upload_observation"),
                "downloaded": {
                    "status": report.get("status"),
                    "error_type": report.get("error_type"),
                    "retryable": report.get("retryable"),
                    "terminal": report.get("terminal"),
                    "recommended_next_action": report.get("recommended_next_action"),
                    "image_count": report.get("image_count", 0),
                    "expected_image_count": report.get("expected_image_count"),
                    "partial": bool(report.get("partial")),
                    "generation_state": report.get("generation_state"),
                    "safe_to_fallback": report.get("safe_to_fallback"),
                    "should_collect_current_first": report.get("should_collect_current_first"),
                    "missing_image_count": report.get("missing_image_count"),
                    "aspect_ratio_validation": report.get("aspect_ratio_validation"),
                    "images": report.get("images", []),
                },
            }
        )
        return _write_summary(
            job,
            request_mode=request_mode,
            variant_count=variant_count,
            start_variant=1,
            end_variant="batch",
            variants=variants,
        )

    start_variant = max(1, int(args.start_variant))
    end_variant = min(int(args.end_variant or variant_count), variant_count)
    if end_variant < start_variant:
        raise ValueError("end variant must be greater than or equal to start variant")

    for variant_index in range(start_variant, end_variant + 1):
        report = _run_one_request(
            args,
            job,
            cwd,
            label=variant_index,
            message=_message_for_label(job, variant_index),
            resume=not args.no_resume,
        )
        variants.append(
            _variant_summary_from_report(
                variant_index=variant_index,
                report=report,
                report_path=Path(str(report.get("report_path"))),
            )
        )

    return _write_independent_variant_summary(
        job,
        variant_count=variant_count,
        new_variants=variants,
    )


def run_conversation_followup(args: argparse.Namespace, job: dict[str, Any], cwd: Path) -> dict[str, Any]:
    if (job.get("request_mode") or "single_batch") != "single_batch":
        raise ValueError("Conversation follow-up currently requires request_mode=single_batch")
    if not args.conversation_session:
        raise ValueError("--conversation-session is required for conversation-followup")

    report = _run_one_request(
        args,
        job,
        cwd,
        label="batch",
        message=_message_for_label(job, "batch"),
        resume=not args.no_resume,
        conversation_session_path=Path(args.conversation_session),
    )
    variants = [
        {
            "variant_index": "batch",
            "submitted": bool(report.get("submitted", True)),
            "resumed": report.get("resumed"),
            "conversation_url": report.get("conversation_url"),
            "continued_from": report.get("continued_from"),
            "report_path": report.get("report_path"),
            "chat_surface": report.get("chat_surface"),
            "image_mode": report.get("image_mode"),
            "aspect_ratio_delivery": report.get("aspect_ratio_delivery"),
            "reference_upload": report.get("upload_observation"),
            "downloaded": {
                "status": report.get("status"),
                "error_type": report.get("error_type"),
                "retryable": report.get("retryable"),
                "terminal": report.get("terminal"),
                "recommended_next_action": report.get("recommended_next_action"),
                "image_count": report.get("image_count", 0),
                "expected_image_count": report.get("expected_image_count"),
                "partial": bool(report.get("partial")),
                "generation_state": report.get("generation_state"),
                "safe_to_fallback": report.get("safe_to_fallback"),
                "should_collect_current_first": report.get("should_collect_current_first"),
                "missing_image_count": report.get("missing_image_count"),
                "aspect_ratio_validation": report.get("aspect_ratio_validation"),
                "images": report.get("images", []),
            },
        }
    ]
    return _write_summary(
        job,
        request_mode="conversation_followup",
        variant_count=max(1, int(job.get("variant_count") or 1)),
        start_variant=1,
        end_variant="batch",
        variants=variants,
    )


def run_collect_current(args: argparse.Namespace, job: dict[str, Any], cwd: Path) -> dict[str, Any]:
    trace_dir = Path(job["download_dir"]) / "agent_browser_trace_collect"
    trace_dir.mkdir(parents=True, exist_ok=True)

    _validate_profile(args)
    account_guard = _validate_account_lane(args, cwd, job)
    existing_session = _read_json_if_exists(_session_path(job, "batch")) or {}
    existing_summary = _read_json_if_exists(_summary_path(job)) or {}
    current_url = _current_url(args, cwd)
    target_conversation_url = _collect_current_target_url(
        current_url=current_url,
        existing_session=existing_session,
        summary=existing_summary,
    )
    if not _conversation_id_from_url(target_conversation_url):
        raise RuntimeError(
            "collect-current is not on a ChatGPT conversation page and no resumable "
            "conversation_url exists in chatgpt_session.json or chatgpt_web_run_summary.json"
        )
    if _conversation_id_from_url(current_url) != _conversation_id_from_url(target_conversation_url):
        conversation_url = _open_conversation(args, cwd, target_conversation_url)
    else:
        conversation_url = current_url
    _screenshot(args, cwd, trace_dir / "01_current_page.png")
    references = [ref["path"] for ref in job.get("reference_images", [])]
    expected_count = _expected_image_count(job, "batch")
    deadline = time.time() + min(max(5, int(getattr(args, "timeout", 1200))), 120)
    started_at = time.time()
    progress_interval = max(5, int(getattr(args, "progress_interval", 20)))
    next_progress_at = 0.0
    candidates: list[dict[str, Any]] = []
    detected_generation_error: dict[str, str] | None = None
    conversation_guard: dict[str, Any] | None = None
    conversation_restores: list[dict[str, Any]] = []
    while time.time() < deadline:
        conversation_guard = _ensure_expected_conversation(args, cwd, conversation_url)
        if conversation_guard.get("restored"):
            conversation_restores.append(conversation_guard)
        page_text = _page_text(args, cwd)
        detected_generation_error = _generation_error_from_text(page_text)
        if detected_generation_error:
            page_health = _generation_page_health(args, cwd, conversation_url, page_text)
            detected_generation_error = page_health.get("generation_error") or detected_generation_error
            _emit_progress(
                job,
                label="batch",
                phase="generation_error",
                status="generation_failed",
                started_at=started_at,
                expected_count=expected_count,
                page_health=page_health,
            )
            break
        rows = _image_inventory(args, cwd)
        candidates = _generated_image_candidates(rows, set())
        if time.time() >= next_progress_at:
            page_health = _generation_page_health(args, cwd, conversation_url, page_text)
            detected_generation_error = page_health.get("generation_error")
            _emit_progress(
                job,
                label="batch",
                phase="collect_current",
                status="checking_current_conversation",
                started_at=started_at,
                expected_count=expected_count,
                recognized_count=len(candidates),
                page_health=page_health,
            )
            next_progress_at = time.time() + progress_interval
            if detected_generation_error:
                break
        if candidates:
            break
        _scroll_down(args, cwd, 700)
        time.sleep(2)
    selected_candidates = candidates[:_candidate_download_limit(expected_count, references)]
    recognized_candidates = [_candidate_debug(candidate) for candidate in selected_candidates]
    download_failures: list[dict[str, Any]] = []
    downloaded_all = _download_candidates(
        args,
        cwd,
        selected_candidates,
        Path(job["download_dir"]),
        f"{job['job_name']}_agent_browser_batch_generated",
        download_failures,
    )
    downloaded, filtered_reference_downloads = _filter_reference_downloads(downloaded_all, references)
    downloaded = _keep_expected_downloads(downloaded, expected_count)
    status = (
        "downloaded"
        if len(downloaded) >= expected_count
        else (
            "partial_downloaded"
            if downloaded
            else ("generation_failed" if detected_generation_error else "collect_current_no_new_images")
        )
    )
    if selected_candidates and not downloaded and download_failures:
        status = "download_failed"
    completion_metadata = (
        {
            "generation_state": "failed",
            "safe_to_fallback": False,
            "should_collect_current_first": False,
            "missing_image_count": expected_count,
            "recommended_next_action": "submit_new_request",
            "safe_to_request_missing_batch": False,
            "missing_batch_request_count": 0,
            "missing_image_followup_message": "",
        }
        if detected_generation_error
        else (
            {
                "generation_state": "complete_download_pending",
                "safe_to_fallback": False,
                "should_collect_current_first": True,
                "missing_image_count": expected_count,
                "recommended_next_action": "collect_current_first",
                "safe_to_request_missing_batch": False,
                "missing_batch_request_count": 0,
                "missing_image_followup_message": "",
            }
            if status == "download_failed"
            else _generation_completion_metadata(
                status=status,
                image_count=len(downloaded),
                expected_count=expected_count,
                download_reason="partial_terminal" if status == "partial_downloaded" else status,
            )
        )
    )
    aspect_ratio_validation = _annotate_aspect_ratio_validation(
        job,
        {"images": downloaded},
    )
    session_patch = {
        "status": status,
        "resumed": True,
        "conversation_id": _conversation_id_from_url(conversation_url),
        "conversation_url": conversation_url,
        **reference_audit_fields(job),
        "agent_browser_profile": _profile_label(args),
        "account_lane": TARGET_CHATGPT_ACCOUNT_SIGNAL,
        "account_guard": account_guard,
        "recognized_candidate_count": len(selected_candidates),
        "recognized_candidates": recognized_candidates,
        "conversation_guard": conversation_guard,
        "conversation_restores": conversation_restores,
        **({"generation_error": detected_generation_error} if detected_generation_error else {}),
        **({"download_failures": download_failures} if download_failures else {}),
        **completion_metadata,
        "aspect_ratio_validation": aspect_ratio_validation,
        "attempt": {
            "action": "collect_current",
            "mode": "collect_current",
            "status": status,
            "output_count": len(downloaded),
            "conversation_url": conversation_url,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    if downloaded:
        session_patch.update(
            {
                "filtered_reference_downloads": filtered_reference_downloads,
                "output_count": len(downloaded),
                "expected_image_count": expected_count,
                "shortfall_count": max(0, expected_count - len(downloaded)),
                "outputs": [item["path"] for item in downloaded],
            }
        )
    _write_session_patch(
        job,
        session_patch,
    )

    report = {
        "schema_version": 1,
        "adapter": "agent_browser_cdp",
        "mode": "collect_current",
        "prompt_card": job["prompt_card"],
        "agent_browser_session": _agent_browser_session_record(args),
        "conversation_url": conversation_url,
        **reference_audit_fields(job),
        "trace_dir": str(trace_dir),
        "account_guard": account_guard,
        "raw_image_count": len(downloaded_all),
        "recognized_candidate_count": len(selected_candidates),
        "recognized_candidates": recognized_candidates,
        "conversation_guard": conversation_guard,
        "conversation_restores": conversation_restores,
        **({"generation_error": detected_generation_error} if detected_generation_error else {}),
        **({"download_failures": download_failures} if download_failures else {}),
        "filtered_reference_downloads": filtered_reference_downloads,
        "status": status,
        "image_count": len(downloaded),
        "expected_image_count": expected_count,
        "partial": bool(downloaded and len(downloaded) < expected_count),
        **completion_metadata,
        "aspect_ratio_validation": aspect_ratio_validation,
        "preserved_existing_outputs": (not downloaded and bool(existing_session.get("outputs"))),
        "images": downloaded,
    }
    report_path = trace_dir / "collect_report.json"
    if downloaded:
        summary_path = _update_summary_for_collect_current(
            job,
            conversation_url=conversation_url,
            report_path=report_path,
            downloaded=downloaded,
            status=status,
            expected_count=expected_count,
        )
        report["summary_path"] = str(summary_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report_path": str(report_path)}


def _report_has_conversation_result(report: dict[str, Any] | None) -> bool:
    if not report:
        return False
    if _conversation_id_from_url(str(report.get("conversation_url") or "")):
        return True
    for variant in report.get("variants") or []:
        if _conversation_id_from_url(str(variant.get("conversation_url") or "")):
            return True
    return False


def _job_has_conversation_session(job: dict[str, Any]) -> bool:
    paths = [_session_path(job, "batch")]
    variant_count = max(1, int(job.get("variant_count") or 1))
    for index in range(1, variant_count + 1):
        paths.append(_session_path(job, index))
    for path in paths:
        session = _read_json_if_exists(path) or {}
        if _conversation_id_from_url(str(session.get("conversation_url") or "")):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent-browser CDP comparison runner for ChatGPT Web image jobs."
    )
    parser.add_argument("manifest", help="ChatGPT Web job manifest JSON")
    parser.add_argument("--session", default="chatgpt-web-agent-browser")
    parser.add_argument(
        "--profile",
        default=TARGET_AGENT_BROWSER_PROFILE or None,
        help="Explicit Chrome Profile directory; otherwise use this skill's saved binding.",
    )
    parser.add_argument("--executable-path", default=DEFAULT_CHROME)
    parser.add_argument(
        "--user-data-dir",
        default=DEFAULT_CDP_USER_DATA_DIR,
        help=(
            "Explicit persistent Chrome CDP data dir. When omitted, use this skill's bound "
            "per-Profile Donald Chrome-over-CDP environment."
        ),
    )
    parser.add_argument("--download-path")
    parser.add_argument(
        "--cdp",
        default=os.environ.get("AGENT_BROWSER_CDP"),
        help="Existing Chrome CDP port or URL. When omitted, the runner uses --cdp-port.",
    )
    parser.add_argument(
        "--cdp-port",
        default=None,
        help="Local remote-debugging port; normally reuse the configured Profile port.",
    )
    parser.add_argument(
        "--auto-connect",
        action="store_true",
        help="Use agent-browser --auto-connect instead of the dedicated --cdp-port route.",
    )
    parser.add_argument(
        "--no-launch-browser",
        action="store_true",
        help="Fail instead of naturally launching the correct Chrome profile when no CDP session is reachable.",
    )
    parser.add_argument(
        "--browser-args",
        default="",
        help=(
            "Extra Chrome launch args used only by the fallback CDP Chrome launch. "
            "Default keeps Chrome headed (not headless) without adding extra automation flags."
        ),
    )
    parser.add_argument(
        "--launch-background",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "On macOS, bootstrap headed Chrome hidden with `open -g -j`, then reveal it "
            "behind the active app without stealing OS focus."
        ),
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Leave the agent-browser session open after the run. Use only for live debugging.",
    )
    parser.add_argument(
        "--mode",
        choices=["dry-upload", "single-batch-submit", "conversation-followup", "collect-current"],
        default="dry-upload",
        help=(
            "Start with dry-upload to compare attachment behavior without submitting prompts. "
            "Use conversation-followup with --conversation-session to submit this job as a new "
            "turn in an existing conversation. Use collect-current to recover generated images."
        ),
    )
    parser.add_argument(
        "--conversation-session",
        help=(
            "Source chatgpt_session.json whose conversation_url receives a follow-up turn. "
            "The current manifest must use a separate download directory/session file."
        ),
    )
    parser.add_argument("--timeout", type=int, default=1200, help="Generation wait timeout in seconds")
    parser.add_argument("--progress-interval", type=int, default=20, help="Seconds between progress heartbeats")
    parser.add_argument(
        "--stale-generation-refresh-interval",
        type=int,
        default=DEFAULT_STALE_GENERATION_REFRESH_SECONDS,
        help=(
            "Seconds of no candidate growth while ChatGPT's Stop button stays active before "
            "reopening the same conversation URL. Disabled by default so a normally running "
            "generation is never refreshed merely for taking a long time; set a positive value "
            "only for explicit diagnostics."
        ),
    )
    parser.add_argument(
        "--page-recovery-attempts",
        type=int,
        default=DEFAULT_PAGE_RECOVERY_ATTEMPTS,
        help=(
            "Maximum automatic reload/reopen attempts after a confirmed page failure. "
            "Pre-submit recovery replays image mode, ratio, and reference upload; post-submit "
            "recovery only reopens the same conversation and never resubmits."
        ),
    )
    parser.add_argument(
        "--submit-throttle-min-interval",
        type=int,
        default=DEFAULT_SUBMIT_THROTTLE_MIN_INTERVAL_SECONDS,
        help="Minimum seconds between fresh ChatGPT image submit attempts across all runners.",
    )
    parser.add_argument(
        "--submit-throttle-max-submits-per-hour",
        type=int,
        default=DEFAULT_SUBMIT_THROTTLE_MAX_SUBMITS_PER_HOUR,
        help="Maximum fresh ChatGPT conversation submit attempts per rolling hour across all runners. Set 0 to disable.",
    )
    parser.add_argument(
        "--submit-throttle-max-expected-images-per-hour",
        type=int,
        default=DEFAULT_SUBMIT_THROTTLE_MAX_EXPECTED_IMAGES_PER_HOUR,
        help=(
            "Optional maximum requested image count per rolling hour across all runners. "
            "Default 0 disables this because the primary limit is fresh conversations."
        ),
    )
    parser.add_argument(
        "--no-submit-throttle",
        action="store_true",
        help="Disable the shared ChatGPT submit throttle. Use only for explicit debugging.",
    )
    parser.add_argument("--lock-timeout", type=int, default=900, help="Seconds to wait for the shared CDP lane lock")
    parser.add_argument("--start-variant", type=int, default=1)
    parser.add_argument("--end-variant", type=int)
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing session URLs and start fresh conversations.")
    parser.add_argument("--max-failure-retries", type=int, default=2)
    parser.add_argument(
        "--keep-success-trace-screenshots",
        action="store_true",
        help=(
            "Keep routine browser screenshots after a fully downloaded result. By default, "
            "successful runs retain reports and state but remove routine screenshots; failure "
            "screenshots are always preserved."
        ),
    )
    args = parser.parse_args()

    if PIL_IMPORT_ERROR is not None:
        print(
            json.dumps(
                {
                    "status": "needs_ops",
                    "reason": "pillow_unavailable",
                    "hint": (
                        "Install Pillow for the Python interpreter running this skill: "
                        f"{sys.executable} -m pip install Pillow"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    try:
        _resolve_browser_profile(args)
    except ProfileConfigError as error:
        print(
            json.dumps(
                {
                    "status": "needs_ops",
                    "reason": "browser_profile_unconfigured",
                    "hint": str(error),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    cwd = Path.cwd()
    manifest_path = Path(args.manifest).resolve()
    job = json.loads(manifest_path.read_text(encoding="utf-8"))
    if job.get("reuse_conversation_references") and args.mode not in {"conversation-followup", "collect-current"}:
        raise ValueError(
            "This job reuses existing conversation references and must run with "
            "--mode conversation-followup (or collect-current after submission)."
        )
    if args.mode != "collect-current":
        _validate_manifest_reference_mapping(job)
    if not args.download_path:
        args.download_path = Path(job["download_dir"]).resolve()

    if not args.cdp and not args.auto_connect:
        args.cdp = args.cdp_port
    report: dict[str, Any] | None = None
    try:
        _prepare_browser_lane(args, cwd)
        _validate_profile(args)
    except (OSError, ProfileConfigError, subprocess.SubprocessError, TimeoutError, RuntimeError) as error:
        if getattr(args, "_browser_session", None) is not None:
            _cleanup_agent_browser(args, cwd)
        print(
            json.dumps(
                {
                    "status": "needs_ops",
                    "reason": "browser_startup_failed",
                    "hint": str(error),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    exit_code = 0
    try:
        if args.mode == "single-batch-submit":
            report = run_submit(args, job, cwd)
        elif args.mode == "conversation-followup":
            report = run_conversation_followup(args, job, cwd)
        elif args.mode == "collect-current":
            report = run_collect_current(args, job, cwd)
        else:
            report = run_dry_upload(args, job, cwd)
    except PageRecoveryExhaustedError as error:
        report = error.report
        exit_code = 2
    except SubmissionStateUnknownError as error:
        report = error.report
        exit_code = 2
    except ReferenceUploadError as error:
        report = error.failure
        exit_code = 2
    except ChatSurfaceSelectionError as error:
        report = {
            "status": "generation_failed",
            "error_type": "chatgpt_chat_surface_unavailable",
            "retryable": False,
            "terminal": True,
            "recommended_next_action": "inspect_chatgpt_chat_work_selector",
            "reason": str(error),
        }
        exit_code = 2
    except ImageModeSelectionError as error:
        report = {
            "status": "generation_failed",
            "error_type": "chatgpt_create_image_mode_unavailable",
            "retryable": False,
            "terminal": True,
            "recommended_next_action": "inspect_chatgpt_create_image_ui",
            "reason": str(error),
        }
        exit_code = 2
    except HumanAttentionRequired as error:
        report = {
            "status": "needs_ops",
            "error_type": "human_attention_required",
            "reason": error.reason,
            "retryable": True,
            "terminal": False,
            "browser_activation": error.activation,
            "verification_must_be_completed_by_human": True,
            "next": "Complete the login or verification in the active Chrome window, then rerun the job.",
        }
        exit_code = 2
    except (TimeoutError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        report = {
            "status": "generation_failed",
            "error_type": "chatgpt_web_runner_failed",
            "retryable": True,
            "terminal": True,
            "recommended_next_action": "inspect_session_and_rerun_if_not_submitted",
            "reason": f"{type(error).__name__}: {error}",
        }
        exit_code = 2
    finally:
        _cleanup_agent_browser(args, cwd)
    if report is not None:
        retention = _prune_success_trace_screenshots(
            job,
            report,
            mode=args.mode,
            start_variant=args.start_variant,
            end_variant=args.end_variant,
            keep_success_trace_screenshots=args.keep_success_trace_screenshots,
        )
        _persist_trace_retention(report, retention)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
