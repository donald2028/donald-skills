#!/usr/bin/env python3
"""Resolve the self-contained output root for visual skill matrix runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
CONFIG_FILENAME = "storage.json"
CONFIG_ROOT_ENV = "DONALD_SKILLS_CONFIG_ROOT"
OUTPUT_ROOT_ENV = "DONALD_SKILLS_OUTPUT_ROOT"
TOOL_DIRECTORY = "visual-skill-matrix"


class StorageConfigError(RuntimeError):
    """Raised when the shared Donald Skills storage configuration is invalid."""


def default_config_root(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    override = environment.get(CONFIG_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()

    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "darwin":
        return home_value / "Library" / "Application Support" / "Donald Skills" / "config"
    if platform_value == "win32":
        local = environment.get("LOCALAPPDATA")
        base = Path(local) if local else home_value / "AppData" / "Local"
        return base / "Donald Skills" / "config"
    xdg = environment.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home_value / ".config") / "donald-skills"


def default_documents_dir(
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if env is None else env
    platform_value = sys.platform if platform_name is None else platform_name
    home_value = Path.home() if home is None else home
    if platform_value == "win32":
        onedrive = environment.get("OneDrive") or environment.get("OneDriveConsumer")
        base = Path(onedrive) if onedrive else Path(environment.get("USERPROFILE", home_value))
        return base / "Documents"
    if platform_value == "linux":
        configured = environment.get("XDG_DOCUMENTS_DIR", "").strip()
        if not configured:
            config_home = Path(environment.get("XDG_CONFIG_HOME", home_value / ".config"))
            user_dirs = config_home / "user-dirs.dirs"
            if user_dirs.is_file():
                for line in user_dirs.read_text(encoding="utf-8").splitlines():
                    match = re.match(r'^XDG_DOCUMENTS_DIR="?([^"\n]+)"?$', line.strip())
                    if match:
                        configured = match.group(1)
                        break
        if configured:
            expanded = configured.replace("${HOME}", str(home_value)).replace("$HOME", str(home_value))
            path = Path(expanded).expanduser()
            return path if path.is_absolute() else home_value / path
    return home_value / "Documents"


def load_shared_output_root(config_path: Path) -> Path | None:
    if not config_path.is_file():
        return None
    try:
        payload: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StorageConfigError(f"Cannot read Donald storage config {config_path}: {error}") from error
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise StorageConfigError(f"Unsupported Donald storage schema in {config_path}")
    value = payload.get("output_root")
    if not isinstance(value, str) or not value.strip():
        raise StorageConfigError(f"Donald storage config has no output_root: {config_path}")
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise StorageConfigError(f"Donald output root must be absolute: {config_path}")
    return root.resolve()


def resolve_output_root(
    override: str | Path | None = None,
    *,
    config_path: Path | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    environment = os.environ if env is None else env
    if override is not None:
        return Path(override).expanduser().resolve(), "explicit"

    shared_env = environment.get(OUTPUT_ROOT_ENV, "").strip()
    if shared_env:
        return (Path(shared_env).expanduser().resolve() / TOOL_DIRECTORY), "environment"

    path = (
        default_config_root(platform_name=platform_name, home=home, env=environment) / CONFIG_FILENAME
        if config_path is None
        else config_path
    )
    configured = load_shared_output_root(path)
    if configured:
        return configured / TOOL_DIRECTORY, "config"
    default = default_documents_dir(
        platform_name=platform_name,
        home=home,
        env=environment,
    )
    return (default / "Donald Skills" / "Data" / TOOL_DIRECTORY).resolve(), "default"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="exact one-operation output root")
    args = parser.parse_args()
    try:
        root, source = resolve_output_root(args.root)
    except StorageConfigError as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"output_root": str(root), "source": source}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
