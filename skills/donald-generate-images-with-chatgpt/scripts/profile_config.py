#!/usr/bin/env python3
"""Prepare and verify per-skill Chrome-over-CDP environments for Donald skills."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 3
CONFIG_ROOT_ENV = "DONALD_AGENT_BROWSER_CONFIG_DIR"
CONFIG_DIRECTORY = "Donald Skills"
CONFIG_SUBDIRECTORY = "agent-browser"
SCOPE_CONFIGS = {
    "donald-collect-wechat-accounts": {
        "label": "WeChat collection",
    },
    "donald-collect-x-posts": {
        "label": "X collection",
    },
    "donald-generate-images-with-chatgpt": {
        "label": "ChatGPT image generation",
    },
}
SKILL_DIRECTORY_NAME = Path(__file__).resolve().parents[1].name
DEFAULT_SCOPE = SKILL_DIRECTORY_NAME if SKILL_DIRECTORY_NAME in SCOPE_CONFIGS else ""
AGENT_BROWSER_INSTALL_URL = "https://agent-browser.dev/installation"
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROFILE_COPY_EXCLUDE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "blob_storage",
    "File System",
    "GCM Store",
    "optimization_guide",
    "ShaderCache",
    "component_crx_cache",
}
PROFILE_COPY_EXCLUDE_FILES = {
    "RunningChromeVersion",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
}


class ProfileConfigError(RuntimeError):
    """Raised when a browser environment cannot be prepared or used."""


def resolve_scope(value: str | None = None) -> str:
    scope = str(value or DEFAULT_SCOPE).strip()
    if scope in SCOPE_CONFIGS:
        return scope
    choices = ", ".join(SCOPE_CONFIGS)
    raise ProfileConfigError(
        f"Choose which skill to configure with --scope. Available values: {choices}"
    )


def default_config_path(
    scope: str | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    resolved_scope = resolve_scope(scope)
    environment = os.environ if env is None else env
    override = environment.get(CONFIG_ROOT_ENV)
    if override:
        return Path(override).expanduser() / f"{resolved_scope}.json"

    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        root = (
            home_value
            / "Library"
            / "Application Support"
            / CONFIG_DIRECTORY
            / "config"
            / CONFIG_SUBDIRECTORY
        )
    elif platform_value == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home_value / "AppData" / "Local"
        root = base / CONFIG_DIRECTORY / "config" / CONFIG_SUBDIRECTORY
    else:
        xdg_config = environment.get("XDG_CONFIG_HOME")
        base = Path(xdg_config) if xdg_config else home_value / ".config"
        root = base / "donald-skills" / CONFIG_SUBDIRECTORY
    return root / f"{resolved_scope}.json"


def default_runtime_root(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        return home_value / "Library" / "Application Support" / CONFIG_DIRECTORY / "Chrome CDP"
    if platform_value == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home_value / "AppData" / "Local"
        return base / CONFIG_DIRECTORY / "Chrome CDP"
    xdg_data = environment.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else home_value / ".local" / "share"
    return base / "donald-skills" / "chrome-cdp"


def chrome_environment(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ if env is None else env
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    executable_override = environment.get("DONALD_CHROME_EXECUTABLE")
    source_override = environment.get("DONALD_CHROME_SOURCE_USER_DATA_DIR")

    if platform_value == "darwin":
        executable_candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            home_value / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        source = home_value / "Library" / "Application Support" / "Google" / "Chrome"
    elif platform_value == "win32":
        executable_candidates = []
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if environment.get(key):
                executable_candidates.append(
                    Path(environment[key]) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
        source = Path(environment.get("LOCALAPPDATA", str(home_value / "AppData" / "Local")))
        source = source / "Google" / "Chrome" / "User Data"
    else:
        executable_candidates = [
            Path(candidate)
            for candidate in (
                shutil.which("google-chrome") or "",
                shutil.which("google-chrome-stable") or "",
                shutil.which("chromium") or "",
                shutil.which("chromium-browser") or "",
            )
            if candidate
        ]
        source = home_value / ".config" / "google-chrome"

    executable = Path(executable_override).expanduser() if executable_override else next(
        (candidate for candidate in executable_candidates if candidate.is_file()),
        Path(executable_candidates[0]) if executable_candidates else Path("google-chrome"),
    )
    if source_override:
        source = Path(source_override).expanduser()
    return {"executable": str(executable), "source_user_data_dir": str(source)}


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _agent_browser_version(executable: str) -> str:
    result = _run([executable, "--version"], timeout=30)
    if result.returncode != 0:
        raise ProfileConfigError(result.stdout.strip() or "agent-browser --version failed")
    return result.stdout.strip()


def ensure_agent_browser(auto_install: bool = True) -> dict[str, Any]:
    executable = shutil.which("agent-browser")
    installed_now = False
    install_command: list[str] | None = None
    if not executable:
        if not auto_install:
            raise ProfileConfigError(
                f"agent-browser is missing. Installation instructions: {AGENT_BROWSER_INSTALL_URL}"
            )
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        brew = shutil.which("brew") if sys.platform == "darwin" else None
        if npm:
            install_command = [npm, "install", "-g", "agent-browser"]
        elif brew:
            install_command = [brew, "install", "agent-browser"]
        else:
            raise ProfileConfigError(
                "agent-browser is missing and no supported installer was found. Install npm "
                f"or Homebrew, then follow {AGENT_BROWSER_INSTALL_URL}."
            )
        result = _run(install_command, timeout=900)
        if result.returncode != 0:
            raise ProfileConfigError(
                f"Automatic agent-browser install failed: {result.stdout.strip()}"
            )
        executable = shutil.which("agent-browser")
        if not executable:
            raise ProfileConfigError(
                "agent-browser installed but is not on PATH; restart the shell and run preflight again."
            )
        browser_install = _run([executable, "install"], timeout=1200)
        if browser_install.returncode != 0:
            raise ProfileConfigError(
                f"agent-browser installed, but browser setup failed: {browser_install.stdout.strip()}"
            )
        installed_now = True

    return {
        "executable": executable,
        "version": _agent_browser_version(executable),
        "installed_now": installed_now,
        "install_command": install_command,
    }


def _profile_email(
    source_user_data_dir: Path,
    directory: str,
    local_state_info: dict[str, Any],
) -> str:
    value = local_state_info.get("user_name") or local_state_info.get("email")
    if isinstance(value, str) and "@" in value:
        return value

    preferences_path = source_user_data_dir / directory / "Preferences"
    if not preferences_path.is_file():
        return ""
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    account_info = preferences.get("account_info")
    if not isinstance(account_info, list):
        return ""
    for account in account_info:
        if not isinstance(account, dict):
            continue
        value = account.get("email")
        if isinstance(value, str) and "@" in value:
            return value
    return ""


def list_profiles(auto_install: bool = True) -> tuple[dict[str, Any], list[dict[str, str]]]:
    agent_browser = ensure_agent_browser(auto_install=auto_install)
    chrome = chrome_environment()
    executable = Path(chrome["executable"])
    source_user_data_dir = Path(chrome["source_user_data_dir"])
    if not executable.is_file():
        raise ProfileConfigError(
            f"Google Chrome was not found at {executable}. Install Chrome or set "
            "DONALD_CHROME_EXECUTABLE."
        )
    if not source_user_data_dir.is_dir():
        raise ProfileConfigError(
            f"Chrome User Data directory was not found at {source_user_data_dir}. "
            "Launch Chrome once or set DONALD_CHROME_SOURCE_USER_DATA_DIR."
        )

    result = _run([agent_browser["executable"], "profiles", "--json"], timeout=60)
    if result.returncode != 0:
        raise ProfileConfigError(result.stdout.strip() or "agent-browser profiles failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProfileConfigError(f"agent-browser returned invalid profile JSON: {error}") from error
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ProfileConfigError("agent-browser profile output does not contain a list")

    try:
        local_state = json.loads(
            (source_user_data_dir / "Local State").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        local_state = {}
    info_cache = local_state.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        info_cache = {}

    profiles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        directory = str(row.get("directory") or "").strip()
        name = str(row.get("name") or "").strip()
        if directory and (source_user_data_dir / directory).is_dir():
            local_info = info_cache.get(directory, {})
            profiles.append(
                {
                    "directory": directory,
                    "name": name or directory,
                    "email": _profile_email(
                        source_user_data_dir,
                        directory,
                        local_info if isinstance(local_info, dict) else {},
                    ),
                }
            )
    if not profiles:
        raise ProfileConfigError("No usable Chrome profiles were found")
    environment = {"agent_browser": agent_browser, "chrome": chrome}
    return environment, profiles


def _match_profile(value: str, profiles: list[dict[str, str]]) -> dict[str, str]:
    directory_matches = [item for item in profiles if item["directory"] == value]
    if directory_matches:
        return directory_matches[0]
    name_matches = [item for item in profiles if item["name"] == value]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise ProfileConfigError(
            f"Profile display name {value!r} is ambiguous; choose its directory instead."
        )
    raise ProfileConfigError(
        f"Chrome Profile {value!r} was not found. Run profiles and choose an exact value."
    )


def _runtime_directory_for_profile(profile_directory: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_directory).strip("-.") or "profile"
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, profile_directory).hex[:8]
    return default_runtime_root() / f"{slug}-{suffix}"


def default_cdp_port_for_profile(profile_directory: str) -> int:
    if profile_directory == "Default":
        return 9222
    match = re.fullmatch(r"Profile (\d+)", profile_directory)
    if match:
        candidate = 9222 + int(match.group(1))
        if candidate <= 65535:
            return candidate
    offset = int(uuid.uuid5(uuid.NAMESPACE_URL, profile_directory).hex[:8], 16) % 500
    return 9300 + offset


def _copy_profile_tree(source: Path, destination: Path, warnings: list[str]) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in source.iterdir():
        if entry.name in PROFILE_COPY_EXCLUDE_FILES:
            continue
        if entry.is_dir() and entry.name in PROFILE_COPY_EXCLUDE_DIRS:
            continue
        target = destination / entry.name
        try:
            if entry.is_dir():
                copied += _copy_profile_tree(entry, target, warnings)
            else:
                shutil.copy2(entry, target)
                copied += 1
        except OSError as error:
            if len(warnings) < 20:
                warnings.append(f"{entry}: {error}")
    return copied


def _windows_chrome_is_running() -> bool:
    if sys.platform != "win32":
        return False
    tasklist = shutil.which("tasklist") or shutil.which("tasklist.exe")
    if not tasklist:
        return False
    result = _run([tasklist, "/FI", "IMAGENAME eq chrome.exe"], timeout=20)
    return "chrome.exe" in result.stdout.casefold()


def prepare_cdp_user_data_dir(
    source_user_data_dir: Path,
    profile: dict[str, str],
    destination: Path,
    cdp_port: int,
) -> dict[str, Any]:
    source_user_data_dir = source_user_data_dir.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination == source_user_data_dir:
        raise ProfileConfigError(
            "CDP user-data-dir must not be Chrome's normal User Data directory."
        )
    source_profile = source_user_data_dir / profile["directory"]
    if not source_profile.is_dir():
        raise ProfileConfigError(f"Source Chrome Profile is missing: {source_profile}")

    destination_profile = destination / profile["directory"]
    if (destination / "Local State").is_file() and destination_profile.is_dir():
        shared_port = cdp_port
        warnings = []
        metadata_path = destination / ".donald-cdp-profile.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_port = metadata.get("cdp_port")
            if isinstance(metadata_port, int) and 1 <= metadata_port <= 65535:
                shared_port = metadata_port
        except (OSError, json.JSONDecodeError):
            pass
        if shared_port != cdp_port:
            warnings.append(
                f"Reused shared Profile CDP port {shared_port}; requested port {cdp_port} was ignored."
            )
        return {
            "status": "reused",
            "user_data_dir": str(destination),
            "profile_directory": profile["directory"],
            "cdp_port": shared_port,
            "warnings": warnings,
        }
    if destination.exists() and any(destination.iterdir()):
        raise ProfileConfigError(
            f"CDP user-data-dir exists but is incomplete: {destination}. Choose another path or "
            "move the incomplete directory before reinitializing."
        )
    if destination.exists():
        destination.rmdir()

    if _windows_chrome_is_running():
        raise ProfileConfigError(
            "Close all Google Chrome windows before the first Profile initialization on Windows, "
            "then run set again. Chrome locks files needed for the login-state snapshot."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    warnings: list[str] = []
    try:
        temporary.mkdir(parents=True)
        local_state = source_user_data_dir / "Local State"
        if local_state.is_file():
            try:
                shutil.copy2(local_state, temporary / "Local State")
            except OSError as error:
                warnings.append(f"{local_state}: {error}")
        copied_files = _copy_profile_tree(
            source_profile,
            temporary / profile["directory"],
            warnings,
        )
        if copied_files == 0:
            raise ProfileConfigError(f"No Profile files could be copied from {source_profile}")
        metadata = {
            "schema_version": 2,
            "source_user_data_dir": str(source_user_data_dir),
            "profile": profile,
            "cdp_port": cdp_port,
            "initialized_at": datetime.now(timezone.utc).isoformat(),
        }
        (temporary / ".donald-cdp-profile.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return {
        "status": "initialized",
        "user_data_dir": str(destination),
        "profile_directory": profile["directory"],
        "cdp_port": cdp_port,
        "copied_files": copied_files,
        "warnings": warnings,
    }


def read_config(path: Path, scope: str | None = None) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileConfigError(f"Cannot read browser config {path}: {error}") from error
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ProfileConfigError(
            f"Browser config {path} uses an old schema. Run reset, then initialize again."
        )
    if scope is not None and payload.get("scope") != scope:
        raise ProfileConfigError(
            f"Browser config {path} belongs to {payload.get('scope')!r}, not {scope!r}."
        )
    profile = payload.get("profile")
    chrome = payload.get("chrome")
    if not isinstance(profile, dict) or not str(profile.get("directory") or "").strip():
        raise ProfileConfigError(f"Browser config {path} has no valid Profile")
    if not isinstance(chrome, dict) or not str(chrome.get("cdp_user_data_dir") or "").strip():
        raise ProfileConfigError(f"Browser config {path} has no valid CDP user-data-dir")
    if not str(chrome.get("source_user_data_dir") or "").strip():
        raise ProfileConfigError(f"Browser config {path} has no source User Data directory")
    configured_port = chrome.get("default_cdp_port")
    if not isinstance(configured_port, int) or not 1 <= configured_port <= 65535:
        raise ProfileConfigError(f"Browser config {path} has no valid CDP port")
    return payload


def configured_browser(path: Path | None = None, scope: str | None = None) -> dict[str, Any]:
    resolved_scope = resolve_scope(scope)
    config_path = path or default_config_path(resolved_scope)
    config = read_config(config_path, resolved_scope)
    if config is None:
        raise ProfileConfigError(
            f"Chrome-over-CDP is not initialized for {resolved_scope}. Run the "
            f"donald-configure-agent-browser-profile skill first. Expected config: {config_path}"
        )
    return config


def _existing_profile_binding(
    profile_directory: str,
    source_user_data_dir: Path,
) -> tuple[str, dict[str, Any]] | None:
    expected_source = source_user_data_dir.expanduser().resolve()
    for candidate_scope in SCOPE_CONFIGS:
        candidate_path = default_config_path(candidate_scope)
        try:
            candidate = read_config(candidate_path, candidate_scope)
        except ProfileConfigError:
            continue
        if not candidate or candidate["profile"]["directory"] != profile_directory:
            continue
        configured_source = Path(candidate["chrome"]["source_user_data_dir"]).expanduser().resolve()
        if configured_source == expected_source:
            return candidate_scope, candidate
    return None


def _write_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize_config(
    path: Path,
    scope: str,
    profile_value: str,
    cdp_port: int | None,
    user_data_dir: Path | None,
    auto_install: bool,
) -> dict[str, Any]:
    environment, profiles = list_profiles(auto_install=auto_install)
    profile = _match_profile(profile_value, profiles)
    requested_port = cdp_port or default_cdp_port_for_profile(profile["directory"])
    if not 1 <= requested_port <= 65535:
        raise ProfileConfigError("CDP port must be between 1 and 65535")
    source = Path(environment["chrome"]["source_user_data_dir"])
    existing_binding = _existing_profile_binding(profile["directory"], source)
    if existing_binding:
        existing_scope, existing_config = existing_binding
        shared_runtime = Path(existing_config["chrome"]["cdp_user_data_dir"]).expanduser()
        shared_port = int(existing_config["chrome"]["default_cdp_port"])
        if user_data_dir and user_data_dir.expanduser().resolve() != shared_runtime.resolve():
            raise ProfileConfigError(
                f"{profile['directory']} already uses {shared_runtime} through {existing_scope}; "
                "the same Chrome Profile must share one CDP user-data-dir."
            )
        if cdp_port is not None and cdp_port != shared_port:
            raise ProfileConfigError(
                f"{profile['directory']} already uses CDP port {shared_port} through "
                f"{existing_scope}; the same Chrome Profile must share one port."
            )
        runtime = shared_runtime
        requested_port = shared_port
    else:
        existing_scope = ""
        runtime = (user_data_dir or _runtime_directory_for_profile(profile["directory"])).expanduser()
    preparation = prepare_cdp_user_data_dir(source, profile, runtime, requested_port)
    if existing_scope:
        preparation["shared_from_scope"] = existing_scope
    shared_port = int(preparation["cdp_port"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "profile": profile,
        "chrome": {
            "executable": environment["chrome"]["executable"],
            "source_user_data_dir": str(source.resolve()),
            "cdp_user_data_dir": str(runtime.resolve()),
            "default_cdp_port": shared_port,
        },
        "agent_browser": {
            "executable": environment["agent_browser"]["executable"],
            "version": environment["agent_browser"]["version"],
        },
        "platform": sys.platform,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_config(path, payload)
    return {"status": "saved", "config_path": str(path), "preparation": preparation, **payload}


def _cdp_version(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if "Chrome/" in str(payload.get("Browser") or "") else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


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
    return bytes(header) + bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )


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
        payload = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
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
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()
        ).decode("ascii")
        if expected.encode("ascii") not in head:
            raise ConnectionError("CDP websocket accept key mismatch")
        connection = cls(sock)
        connection._buffer = rest
        return connection

    def __enter__(self) -> "_CDPConnection":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._sock.close()

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
        message_id = self._next_id
        payload = json.dumps(
            {"id": message_id, "method": method, "params": params or {}}
        ).encode("utf-8")
        self._sock.sendall(_encode_ws_text_frame(payload))
        while True:
            opcode, response = self._recv_frame()
            if opcode != 0x1:
                continue
            message = json.loads(response.decode("utf-8"))
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise ProfileConfigError(f"CDP {method} failed: {message['error']}")
            return message.get("result") or {}


def create_background_page(port: int, url: str = "about:blank") -> str:
    version = _cdp_version(port)
    websocket_url = str((version or {}).get("webSocketDebuggerUrl") or "")
    if not websocket_url:
        raise ProfileConfigError(f"Chrome CDP browser websocket is unavailable on port {port}")
    try:
        with _CDPConnection.connect(websocket_url, timeout=10) as connection:
            result = connection.call(
                "Target.createTarget", {"url": url, "background": True}
            )
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError) as error:
        raise ProfileConfigError(f"Could not create a background CDP page: {error}") from error
    target_id = str(result.get("targetId") or "")
    if not target_id:
        raise ProfileConfigError("Chrome did not return a target id for the background page")
    return target_id


def wait_for_background_page_url(
    port: int,
    target_id: str,
    requested_url: str,
    timeout: int = 15,
) -> str:
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2
            ) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        target = next(
            (item for item in targets if str(item.get("id") or "") == target_id),
            None,
        )
        if target:
            last_url = str(target.get("url") or "")
            if requested_url == "about:blank" or (last_url and last_url != "about:blank"):
                return last_url
        time.sleep(0.1)
    raise ProfileConfigError(
        f"Background CDP page {target_id} did not navigate to {requested_url!r}; "
        f"last URL was {last_url!r}"
    )


def chrome_launch_command(config: dict[str, Any], port: int, url: str = "about:blank") -> list[str]:
    chrome = config["chrome"]
    profile = config["profile"]
    launch_args = [
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={chrome['cdp_user_data_dir']}",
        f"--profile-directory={profile['directory']}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-startup-window",
    ]
    executable = str(chrome["executable"])
    if sys.platform == "darwin" and executable == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome":
        return ["open", "-g", "-j", "-n", "-a", "Google Chrome", "--args", *launch_args]
    return [executable, *launch_args]


def _launch_chrome(config: dict[str, Any], port: int, url: str) -> list[str]:
    command = chrome_launch_command(config, port, url)
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(command, **kwargs)
    return command


def _listening_process_id(port: int) -> str:
    if sys.platform == "win32":
        netstat = _run(["netstat", "-ano"], timeout=20)
        for line in netstat.stdout.splitlines():
            if f"127.0.0.1:{port}" in line and "LISTENING" in line.upper():
                return line.split()[-1]
        return ""

    lsof = shutil.which("lsof")
    if not lsof:
        return ""
    listener = _run([lsof, f"-tiTCP:{port}", "-sTCP:LISTEN"], timeout=20)
    return next((line.strip() for line in listener.stdout.splitlines() if line.strip()), "")


def _listening_process_command(port: int) -> str:
    pid = _listening_process_id(port)
    if not pid:
        return ""
    if sys.platform == "win32":
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if not powershell:
            return ""
        result = _run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            timeout=20,
        )
        return result.stdout.strip()

    ps = shutil.which("ps")
    if not ps:
        return ""
    return _run([ps, "-p", pid, "-o", "command="], timeout=20).stdout.strip()


def _verify_existing_cdp_owner(config: dict[str, Any], port: int) -> None:
    command = _listening_process_command(port)
    expected_user_data = f"--user-data-dir={config['chrome']['cdp_user_data_dir']}"
    expected_profile = f"--profile-directory={config['profile']['directory']}"
    if not command or expected_user_data not in command or expected_profile not in command:
        raise ProfileConfigError(
            f"CDP port {port} is already in use by a different or unverifiable Chrome process. "
            "Choose another port or close that process; refusing to attach to the wrong account."
        )


def activate_browser(config: dict[str, Any], port: int) -> dict[str, Any]:
    if not _cdp_version(port):
        raise ProfileConfigError(f"Chrome CDP is unavailable on port {port}")
    _verify_existing_cdp_owner(config, port)
    pid = _listening_process_id(port)
    if not pid:
        raise ProfileConfigError(f"Could not identify the Chrome process on CDP port {port}")

    if sys.platform == "darwin":
        script = (
            'tell application "System Events"\n'
            f"tell first application process whose unix id is {pid}\n"
            "set visible to true\n"
            "set frontmost to true\n"
            "end tell\n"
            "end tell"
        )
        result = _run(["osascript", "-e", script], timeout=20)
    elif sys.platform == "win32":
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if not powershell:
            raise ProfileConfigError("PowerShell is required to activate Chrome on Windows")
        result = _run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                f"$shell = New-Object -ComObject WScript.Shell; "
                f"if (-not $shell.AppActivate({pid})) {{ exit 1 }}",
            ],
            timeout=20,
        )
    else:
        raise ProfileConfigError(
            "Automatic Chrome activation is currently supported on macOS and Windows only"
        )
    if result.returncode != 0:
        raise ProfileConfigError(
            f"Could not activate Chrome for human interaction: {result.stdout.strip()}"
        )
    return {"status": "active", "cdp_port": port, "pid": int(pid)}


def show_browser_without_focus(config: dict[str, Any], port: int) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "platform_default", "cdp_port": port}
    if not _cdp_version(port):
        raise ProfileConfigError(f"Chrome CDP is unavailable on port {port}")
    _verify_existing_cdp_owner(config, port)
    pid = _listening_process_id(port)
    if not pid:
        raise ProfileConfigError(f"Could not identify the Chrome process on CDP port {port}")
    script = (
        'tell application "System Events"\n'
        "set previousPid to unix id of first application process whose frontmost is true\n"
        f"tell first application process whose unix id is {pid}\n"
        "set visible to true\n"
        f"if previousPid is not {pid} then set frontmost to false\n"
        "end tell\n"
        f"if previousPid is not {pid} then set frontmost of first application process whose unix id is previousPid to true\n"
        "return previousPid\n"
        "end tell"
    )
    result = _run(["osascript", "-e", script], timeout=20)
    if result.returncode != 0:
        raise ProfileConfigError(
            f"Could not reveal Chrome behind the active app: {result.stdout.strip()}"
        )
    return {
        "status": "visible_in_background",
        "cdp_port": port,
        "pid": int(pid),
        "frontmost": False,
        "restored_frontmost_pid": int(result.stdout.strip()),
    }


def frontmost_process_id() -> int | None:
    if sys.platform != "darwin":
        return None
    result = _run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get unix id of first application process whose frontmost is true',
        ],
        timeout=5,
    )
    if result.returncode != 0:
        raise ProfileConfigError(f"Could not read the current foreground app: {result.stdout.strip()}")
    return int(result.stdout.strip())


def hide_browser_without_focus(config: dict[str, Any], port: int, restore_pid: int | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "platform_default", "cdp_port": port}
    if not _cdp_version(port):
        raise ProfileConfigError(f"Chrome CDP is unavailable on port {port}")
    _verify_existing_cdp_owner(config, port)
    pid = int(_listening_process_id(port) or 0)
    if not pid:
        raise ProfileConfigError(f"Could not identify the Chrome process on CDP port {port}")
    previous_pid = restore_pid or frontmost_process_id()
    restore_line = ""
    if previous_pid and previous_pid != pid:
        restore_line = f"set frontmost of first application process whose unix id is {previous_pid} to true\n"
    script = (
        'tell application "System Events"\n'
        f"set visible of first application process whose unix id is {pid} to false\n"
        + restore_line
        + "end tell"
    )
    result = _run(["osascript", "-e", script], timeout=20)
    if result.returncode != 0:
        raise ProfileConfigError(f"Could not keep Chrome hidden in headed mode: {result.stdout.strip()}")
    return {
        "status": "hidden_headed",
        "cdp_port": port,
        "pid": pid,
        "frontmost": False,
        "restored_frontmost_pid": previous_pid,
    }


def restore_frontmost_process(pid: int) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "platform_default", "pid": pid}
    script = (
        'tell application "System Events"\n'
        f"set frontmost of first application process whose unix id is {pid} to true\n"
        "end tell"
    )
    result = _run(["osascript", "-e", script], timeout=20)
    if result.returncode != 0:
        raise ProfileConfigError(
            f"Could not restore the previous foreground app (pid {pid}): "
            f"{result.stdout.strip()}"
        )
    return {"status": "restored", "pid": pid}


def preflight_browser(
    config: dict[str, Any],
    port: int,
    session: str,
    url: str,
    timeout: int,
) -> dict[str, Any]:
    if not 1 <= port <= 65535:
        raise ProfileConfigError("CDP port must be between 1 and 65535")
    previous_frontmost_pid = frontmost_process_id()
    ensure_agent_browser(auto_install=True)
    user_data_dir = Path(config["chrome"]["cdp_user_data_dir"])
    profile_dir = user_data_dir / config["profile"]["directory"]
    if not (user_data_dir / "Local State").is_file() or not profile_dir.is_dir():
        raise ProfileConfigError(
            f"Configured CDP user-data-dir is incomplete: {user_data_dir}. Reinitialize it."
        )

    version = _cdp_version(port)
    launch_command: list[str] | None = None
    if version:
        _verify_existing_cdp_owner(config, port)
    else:
        launch_command = _launch_chrome(config, port, "about:blank")
        deadline = time.time() + timeout
        while time.time() < deadline:
            version = _cdp_version(port)
            if version:
                break
            time.sleep(0.5)
        if not version:
            raise ProfileConfigError(
                f"Chrome did not expose CDP port {port} within {timeout}s. Command: {launch_command}"
            )

    background_target_id = create_background_page(port, url)
    background_url = wait_for_background_page_url(
        port, background_target_id, url, timeout=min(timeout, 15)
    )
    background_visibility = hide_browser_without_focus(config, port, previous_frontmost_pid)
    session_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", session).strip("-") or "donald-cdp"
    transport_session = f"{session_prefix[:30]}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    agent_browser = ensure_agent_browser(auto_install=True)["executable"]
    attach_command = [
        agent_browser,
        "--session",
        transport_session,
        "--cdp",
        str(port),
        "get",
        "url",
    ]
    attach = _run(attach_command, timeout=60)
    if attach.returncode != 0:
        raise ProfileConfigError(
            "Chrome CDP is reachable, but agent-browser could not attach: "
            f"{attach.stdout.strip()}"
        )
    agent_browser_url = attach.stdout.strip()
    if background_url != "about:blank" and agent_browser_url == "about:blank":
        deadline = time.time() + 5
        while time.time() < deadline and agent_browser_url == "about:blank":
            time.sleep(0.2)
            attach = _run(attach_command, timeout=30)
            if attach.returncode != 0:
                raise ProfileConfigError(
                    "agent-browser attached but could not reread the background page URL: "
                    f"{attach.stdout.strip()}"
                )
            agent_browser_url = attach.stdout.strip()
    background_visibility = hide_browser_without_focus(config, port, previous_frontmost_pid)
    return {
        "status": "ready",
        "profile": config["profile"],
        "user_data_dir": str(user_data_dir),
        "cdp_port": port,
        "browser": version.get("Browser"),
        "browser_websocket": bool(version.get("webSocketDebuggerUrl")),
        "agent_browser_session": transport_session,
        "requested_session": session,
        "url": background_url,
        "agent_browser_url": agent_browser_url,
        "background_target_id": background_target_id,
        "background_visibility": background_visibility,
        "launch_command": launch_command,
        "attach_command": attach_command,
    }


def check_config(path: Path, scope: str, auto_install: bool) -> tuple[int, dict[str, Any]]:
    environment, profiles = list_profiles(auto_install=auto_install)
    config = read_config(path, scope)
    if config is None:
        return 2, {
            "status": "needs_initialization",
            "scope": scope,
            "config_path": str(path),
            "environment": environment,
            "profiles": profiles,
            "next": "Ask the user to choose a Profile, then run set.",
        }
    selected = config["profile"]
    if not any(item["directory"] == selected["directory"] for item in profiles):
        return 2, {
            "status": "stale_profile",
            "config_path": str(path),
            "profile": selected,
            "next": "Ask the user to choose again; do not switch accounts silently.",
        }
    user_data_dir = Path(config["chrome"]["cdp_user_data_dir"])
    runtime_ready = (
        (user_data_dir / "Local State").is_file()
        and (user_data_dir / selected["directory"]).is_dir()
    )
    if not runtime_ready:
        return 2, {
            "status": "incomplete_user_data_dir",
            "config_path": str(path),
            "profile": selected,
            "cdp_user_data_dir": str(user_data_dir),
            "next": "Reinitialize the selected Profile.",
        }
    return 0, {"status": "ready", "config_path": str(path), **config}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def target_statuses() -> list[dict[str, Any]]:
    targets = []
    for scope, metadata in SCOPE_CONFIGS.items():
        path = default_config_path(scope)
        target: dict[str, Any] = {
            "scope": scope,
            **metadata,
            "config_path": str(path),
            "initialized": path.is_file(),
        }
        if path.is_file():
            try:
                config = read_config(path, scope)
                if config:
                    target["profile"] = config["profile"]
                    target["cdp_user_data_dir"] = config["chrome"]["cdp_user_data_dir"]
                    target["cdp_port"] = config["chrome"]["default_cdp_port"]
            except ProfileConfigError as error:
                target["config_error"] = str(error)
        targets.append(target)
    return targets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=list(SCOPE_CONFIGS),
        default=DEFAULT_SCOPE or None,
        help="Skill-specific browser configuration to read or write.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Explicit config file path; normally omit to use the selected skill's global file.",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not automatically install agent-browser when it is missing.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("environment", help="Check or install agent-browser and locate Chrome.")
    subparsers.add_parser("targets", help="List independently configurable Donald skills.")
    subparsers.add_parser("profiles", help="List Chrome Profiles without changing config.")
    subparsers.add_parser("check", help="Check installation, Profile, and CDP user-data-dir.")
    subparsers.add_parser("show", help="Show the saved configuration.")
    subparsers.add_parser("reset", help="Delete config; keep persistent browser data for safety.")

    set_parser = subparsers.add_parser("set", help="Initialize an explicitly confirmed Profile.")
    set_parser.add_argument("--profile", required=True, help="Exact directory or unique display name.")
    set_parser.add_argument("--user-data-dir", type=Path)
    set_parser.add_argument(
        "--cdp-port",
        type=int,
        default=None,
        help="Defaults to the selected Chrome Profile's shared port.",
    )

    preflight = subparsers.add_parser("preflight", help="Launch Chrome over CDP and verify attach.")
    preflight.add_argument("--session", default="")
    preflight.add_argument("--url", default="about:blank")
    preflight.add_argument("--cdp-port", type=int, default=None)
    preflight.add_argument("--timeout", type=int, default=60)
    activate = subparsers.add_parser(
        "activate",
        help="Bring the configured Chrome to the foreground for login or verification.",
    )
    activate.add_argument("--cdp-port", type=int, default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    auto_install = not args.no_install
    path: Path | None = None
    scope: str | None = None
    try:
        if args.command == "environment":
            environment = ensure_agent_browser(auto_install=auto_install)
            chrome = chrome_environment()
            if not Path(chrome["executable"]).is_file():
                raise ProfileConfigError(f"Google Chrome is missing: {chrome['executable']}")
            _emit({"status": "ready", "agent_browser": environment, "chrome": chrome})
            return 0
        if args.command == "targets":
            _emit({"status": "ok", "targets": target_statuses()})
            return 0
        if args.command == "profiles":
            environment, profiles = list_profiles(auto_install=auto_install)
            _emit(
                {
                    "status": "ok",
                    "environment": environment,
                    "count": len(profiles),
                    "profiles": profiles,
                }
            )
            return 0
        scope = resolve_scope(args.scope)
        path = (
            args.config.expanduser().resolve()
            if args.config is not None
            else default_config_path(scope)
        )
        if args.command == "set":
            _emit(
                initialize_config(
                    path,
                    scope,
                    args.profile,
                    args.cdp_port,
                    args.user_data_dir,
                    auto_install,
                )
            )
            return 0
        if args.command == "reset":
            existed = path.exists()
            if existed:
                path.unlink()
            _emit(
                {
                    "status": "reset",
                    "scope": scope,
                    "config_path": str(path),
                    "existed": existed,
                    "runtime_data_removed": False,
                }
            )
            return 0
        if args.command == "show":
            config = read_config(path, scope)
            if config is None:
                _emit(
                    {
                        "status": "needs_initialization",
                        "scope": scope,
                        "config_path": str(path),
                    }
                )
                return 2
            _emit({"status": "ready", "config_path": str(path), **config})
            return 0
        if args.command == "check":
            return_code, payload = check_config(path, scope, auto_install)
            _emit(payload)
            return return_code
        if args.command == "preflight":
            config = configured_browser(path, scope)
            port = args.cdp_port or int(config["chrome"]["default_cdp_port"])
            session = args.session or f"donald-cdp-{port}-{os.getpid()}"
            _emit(preflight_browser(config, port, session, args.url, args.timeout))
            return 0
        if args.command == "activate":
            config = configured_browser(path, scope)
            port = args.cdp_port or int(config["chrome"]["default_cdp_port"])
            _emit(activate_browser(config, port))
            return 0
    except (ProfileConfigError, subprocess.TimeoutExpired) as error:
        payload = {"status": "error", "error": str(error)}
        if scope:
            payload["scope"] = scope
        if path:
            payload["config_path"] = str(path)
        _emit(payload)
        return 2
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
