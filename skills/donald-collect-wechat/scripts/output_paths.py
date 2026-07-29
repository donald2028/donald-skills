#!/usr/bin/env python3
"""Persist and resolve Donald Skills configuration, output, and state directories."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
CONFIG_ROOT_ENV = "DONALD_SKILLS_CONFIG_ROOT"
OUTPUT_ROOT_ENV = "DONALD_SKILLS_OUTPUT_ROOT"
CONFIG_FILENAME = "storage.json"
TOOL_OUTPUT_ROOT_ENVS = {
    "github-research": "DONALD_GITHUB_RESEARCH_ROOT",
}


class StorageConfigError(RuntimeError):
    """Raised when Donald storage configuration is invalid."""


def _expand_windows_environment(value: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        requested = match.group(1).casefold()
        for key, replacement in env.items():
            if key.casefold() == requested:
                return replacement
        return match.group(0)

    return re.sub(r"%([^%]+)%", replace, value)


def _native_windows_documents() -> Path | None:
    if sys.platform != "win32":
        return None

    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    value = uuid.UUID("fdd39ad0-238f-46af-adb4-6c85480369c7")
    guid = GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8)(*value.bytes[8:]),
    )
    path_pointer = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_pointer))
    if result != 0 or not path_pointer.value:
        return None
    try:
        return Path(path_pointer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(path_pointer, ctypes.c_void_p))


def _windows_documents(home: Path, env: Mapping[str, str]) -> Path:
    known_folder = _native_windows_documents()
    if known_folder:
        return known_folder
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore[import-not-found]

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, "Personal")
            if raw:
                return Path(_expand_windows_environment(str(raw), env))
        except (ImportError, OSError):
            pass
    onedrive = env.get("OneDrive") or env.get("OneDriveConsumer")
    if onedrive:
        return Path(onedrive) / "Documents"
    user_profile = env.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / "Documents"
    return home / "Documents"


def _linux_documents(home: Path, env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_DOCUMENTS_DIR")
    if not configured:
        config_home = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
        user_dirs = config_home / "user-dirs.dirs"
        if user_dirs.is_file():
            for line in user_dirs.read_text(encoding="utf-8").splitlines():
                match = re.match(r'^XDG_DOCUMENTS_DIR=(?:"([^"]*)"|\'([^\']*)\'|(.*))$', line.strip())
                if match:
                    configured = next(
                        (item for item in match.groups() if item is not None),
                        "",
                    )
                    break
    if not configured:
        return home / "Documents"
    expanded = configured.replace("${HOME}", str(home)).replace("$HOME", str(home))
    path = Path(expanded).expanduser()
    return path if path.is_absolute() else home / path


def user_documents_dir(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "win32":
        return _windows_documents(home_value, environment)
    if platform_value == "darwin":
        return home_value / "Documents"
    return _linux_documents(home_value, environment)


def default_config_root(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    override = environment.get(CONFIG_ROOT_ENV)
    if override and override.strip():
        return Path(override).expanduser().resolve()

    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        return home_value / "Library" / "Application Support" / "Donald Skills" / "config"
    if platform_value == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home_value / "AppData" / "Local"
        return base / "Donald Skills" / "config"
    xdg_config = environment.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else home_value / ".config"
    return base / "donald-skills"


def default_config_path(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return default_config_root(platform_name=platform_name, home=home, env=env) / CONFIG_FILENAME


def default_output_root(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return (
        user_documents_dir(platform_name=platform_name, home=home, env=env)
        / "Donald Skills"
        / "Data"
    ).resolve()


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def load_storage_config(config_path: Path | None = None) -> dict[str, Any] | None:
    path = default_config_path() if config_path is None else config_path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StorageConfigError(f"Cannot read Donald storage config {path}: {error}") from error
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise StorageConfigError(
            f"Unsupported Donald storage config schema in {path}: "
            f"{payload.get('schema_version')!r}"
        )
    output_root = payload.get("output_root")
    if not isinstance(output_root, str) or not output_root.strip():
        raise StorageConfigError(f"Donald storage config has no output_root: {path}")
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        raise StorageConfigError(f"Donald output root must be absolute in {path}")
    return {
        "schema_version": SCHEMA_VERSION,
        "output_root": str(root.resolve()),
    }


def save_output_root(
    output_root: str | Path,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    path = default_config_path() if config_path is None else config_path
    root = _absolute_path(output_root)
    if root.exists() and not root.is_dir():
        raise StorageConfigError(f"Donald output root is not a directory: {root}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "output_root": str(root),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def reset_output_root(*, config_path: Path | None = None) -> bool:
    path = default_config_path() if config_path is None else config_path
    removed = path.is_file()
    if removed:
        path.unlink()
    return removed


def describe_output_root(
    tool_directory: str | None = None,
    override: str | Path | None = None,
    *,
    config_path: Path | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ if env is None else env
    path = (
        default_config_path(platform_name=platform_name, home=home, env=environment)
        if config_path is None
        else config_path
    )
    if override is not None:
        root = _absolute_path(override)
        source = "explicit"
    else:
        legacy_env_name = TOOL_OUTPUT_ROOT_ENVS.get(tool_directory or "")
        legacy_value = environment.get(legacy_env_name, "") if legacy_env_name else ""
        shared_value = environment.get(OUTPUT_ROOT_ENV, "")
        if legacy_value.strip():
            root = _absolute_path(legacy_value)
            source = "environment"
        elif shared_value.strip():
            root = _absolute_path(shared_value)
            if tool_directory:
                root /= tool_directory
            source = "environment"
        else:
            saved = load_storage_config(path)
            if saved:
                root = Path(saved["output_root"])
                source = "config"
            else:
                root = default_output_root(
                    platform_name=platform_name,
                    home=home,
                    env=environment,
                )
                source = "default"
            if tool_directory:
                root /= tool_directory
    return {
        "output_root": str(root.resolve()),
        "source": source,
        "config_path": str(path),
    }


def resolve_tool_output_root(
    tool_directory: str,
    override: Path | None = None,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    resolved = describe_output_root(
        tool_directory,
        override,
        platform_name=platform_name,
        home=home,
        env=env,
    )
    return Path(resolved["output_root"])


def resolve_tool_state_root(
    tool_directory: str,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        base = home_value / "Library" / "Application Support" / "Donald Skills" / "state"
    elif platform_value == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        app_data = Path(local_app_data) if local_app_data else home_value / "AppData" / "Local"
        base = app_data / "Donald Skills" / "state"
    else:
        xdg_state = environment.get("XDG_STATE_HOME")
        state_home = Path(xdg_state) if xdg_state else home_value / ".local" / "state"
        base = state_home / "donald-skills"
    return base / tool_directory


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="show the effective shared output root")

    set_parser = subparsers.add_parser("set", help="persist a shared output root")
    set_parser.add_argument("output_root")

    subparsers.add_parser("reset", help="remove the persistent shared output root")

    resolve_parser = subparsers.add_parser("resolve", help="resolve a tool output root")
    resolve_parser.add_argument("tool_directory", nargs="?")
    resolve_parser.add_argument("--root", help="exact one-operation output root")

    args = parser.parse_args()
    config_path = default_config_path()
    try:
        if args.command == "show":
            saved = load_storage_config(config_path)
            result = describe_output_root(config_path=config_path)
            result.update(
                {
                    "status": "configured" if saved else result["source"],
                    "configured_output_root": saved["output_root"] if saved else None,
                }
            )
            _print_json(result)
        elif args.command == "set":
            saved = save_output_root(args.output_root, config_path=config_path)
            _print_json(
                {
                    "status": "configured",
                    "config_path": str(config_path),
                    "output_root": saved["output_root"],
                }
            )
        elif args.command == "reset":
            removed = reset_output_root(config_path=config_path)
            result = describe_output_root(config_path=config_path)
            result.update({"status": "reset", "removed": removed})
            _print_json(result)
        else:
            _print_json(
                describe_output_root(
                    args.tool_directory,
                    args.root,
                    config_path=config_path,
                )
            )
    except StorageConfigError as error:
        _print_json({"status": "error", "error": str(error)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
