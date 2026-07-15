#!/usr/bin/env python3
"""Resolve Donald Skills user-facing output directories."""

from __future__ import annotations

import ctypes
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Mapping


OUTPUT_ROOT_ENV = "DONALD_SKILLS_OUTPUT_ROOT"


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
                    configured = next((item for item in match.groups() if item is not None), "")
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


def resolve_tool_output_root(
    tool_directory: str,
    override: Path | None = None,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    environment = os.environ if env is None else env
    shared_override = environment.get(OUTPUT_ROOT_ENV)
    if shared_override:
        return (Path(shared_override).expanduser() / tool_directory).resolve()
    documents = user_documents_dir(platform_name=platform_name, home=home, env=environment)
    return (documents / "Donald Skills" / "Data" / tool_directory).resolve()


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
