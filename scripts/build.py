#!/usr/bin/env python3
"""Synchronize channel manifests and runtime mirrors from package.json."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO / "package.json"
BROWSER_RUNTIME_SOURCE_DIR = REPO / "skills/donald-config-browser/scripts"
BROWSER_RUNTIME_FILES = ("profile_config.py", "browser_runtime.py")
BROWSER_RUNTIME_CONSUMERS = (
    "donald-chatgpt-imagegen",
    "donald-collect-wechat",
    "donald-collect-x",
)
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def require_fields(data: dict[str, Any], fields: tuple[str, ...], *, source: Path) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise SystemExit(f"{source.relative_to(REPO)} is missing: {', '.join(missing)}")


def single_plugin(data: dict[str, Any], *, source: Path) -> dict[str, Any]:
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        raise SystemExit(f"{source.relative_to(REPO)} must contain exactly one plugin entry")
    return plugins[0]


def common_fields(package: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: copy.deepcopy(package[key]) for key in keys}


def update_top_level(path: Path, package: dict[str, Any], keys: tuple[str, ...]) -> str:
    data = load_json(path)
    data.update(common_fields(package, keys))
    if "displayName" in data:
        data["displayName"] = package["displayName"]
    if "skills" in data:
        data["skills"] = "./skills/"

    interface = data.get("interface")
    if isinstance(interface, dict):
        if "displayName" in interface:
            interface["displayName"] = package["displayName"]
        if "developerName" in interface:
            interface["developerName"] = package["author"]["name"]
        if "websiteURL" in interface:
            interface["websiteURL"] = package["homepage"]
    return render_json(data)


def build_outputs(package: dict[str, Any]) -> dict[Path, str]:
    shared = (
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
    )
    outputs = {
        PACKAGE_PATH: render_json(package),
        REPO / ".claude-plugin/plugin.json": update_top_level(
            REPO / ".claude-plugin/plugin.json", package, shared
        ),
        REPO / ".codex-plugin/plugin.json": update_top_level(
            REPO / ".codex-plugin/plugin.json", package, shared
        ),
        REPO / ".cursor-plugin/plugin.json": update_top_level(
            REPO / ".cursor-plugin/plugin.json", package, shared
        ),
        REPO / ".kimi-plugin/plugin.json": update_top_level(
            REPO / ".kimi-plugin/plugin.json",
            package,
            tuple(key for key in shared if key != "repository"),
        ),
        REPO / "gemini-extension.json": update_top_level(
            REPO / "gemini-extension.json", package, ("name", "version", "description")
        ),
    }

    claude_marketplace_path = REPO / ".claude-plugin/marketplace.json"
    claude_marketplace = load_json(claude_marketplace_path)
    claude_marketplace["name"] = package["name"]
    claude_marketplace["owner"] = {
        "name": package["author"]["name"],
        "email": package["author"]["email"],
    }
    claude_marketplace.setdefault("metadata", {}).update(
        {"description": package["description"], "version": package["version"]}
    )
    claude_plugin = single_plugin(claude_marketplace, source=claude_marketplace_path)
    claude_plugin.update(
        {
            "name": package["name"],
            "description": package["description"],
            "version": package["version"],
            "author": {
                "name": package["author"]["name"],
                "email": package["author"]["email"],
            },
        }
    )
    outputs[claude_marketplace_path] = render_json(claude_marketplace)

    codex_marketplace_path = REPO / ".agents/plugins/marketplace.json"
    codex_marketplace = load_json(codex_marketplace_path)
    codex_marketplace["name"] = package["name"]
    codex_marketplace.setdefault("interface", {})["displayName"] = package["displayName"]
    single_plugin(codex_marketplace, source=codex_marketplace_path)["name"] = package["name"]
    outputs[codex_marketplace_path] = render_json(codex_marketplace)

    return outputs


def sync_outputs(outputs: dict[Path, str], *, check: bool) -> int:
    changed = [
        path for path, content in outputs.items() if path.read_text(encoding="utf-8") != content
    ]
    if check:
        if changed:
            print("Out of sync (run npm run build):")
            for path in changed:
                print(f"  {path.relative_to(REPO)}")
            return 1
        print(
            f"In sync: {len(outputs) - 1} channel manifest(s) match package.json.",
            flush=True,
        )
        return 0

    for path in changed:
        path.write_text(outputs[path], encoding="utf-8")
    print(
        f"Synchronized {len(outputs) - 1} channel manifest(s) from package.json"
        f" ({len(changed)} file(s) changed).",
        flush=True,
    )
    return 0


def sync_browser_runtime(*, check: bool) -> int:
    changed: list[Path] = []
    for skill_name in BROWSER_RUNTIME_CONSUMERS:
        target_dir = REPO / "skills" / skill_name / "scripts"
        for filename in BROWSER_RUNTIME_FILES:
            source = BROWSER_RUNTIME_SOURCE_DIR / filename
            target = target_dir / filename
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                changed.append(target)
                if not check:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
    if check:
        if changed:
            print("Out of sync browser runtime (run npm run build):")
            for path in changed:
                print(f"  {path.relative_to(REPO)}")
            return 1
        print(
            f"In sync: {len(BROWSER_RUNTIME_FILES)} browser runtime file(s) "
            f"vendored to {len(BROWSER_RUNTIME_CONSUMERS)} skill(s).",
            flush=True,
        )
        return 0
    print(
        f"Vendored browser runtime to {len(BROWSER_RUNTIME_CONSUMERS)} skill(s) "
        f"({len(changed)} file(s) changed).",
        flush=True,
    )
    return 0


def sync_runtime_mirrors(*, check: bool) -> int:
    command = [sys.executable, str(REPO / "skills/sync_runtime_skills.py")]
    if check:
        command.append("--check")
    return subprocess.run(command, cwd=REPO, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated files without writing")
    parser.add_argument("--version", help="set the canonical version before building")
    args = parser.parse_args()

    if args.check and args.version:
        parser.error("--check and --version cannot be used together")

    package = load_json(PACKAGE_PATH)
    require_fields(
        package,
        (
            "name",
            "displayName",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
        ),
        source=PACKAGE_PATH,
    )
    require_fields(package["author"], ("name", "email", "url"), source=PACKAGE_PATH)

    if args.version:
        if not SEMVER.fullmatch(args.version):
            parser.error("--version must be a valid semantic version, for example 1.2.3")
        package["version"] = args.version

    status = sync_outputs(build_outputs(package), check=args.check)
    if status:
        return status
    status = sync_browser_runtime(check=args.check)
    if status:
        return status
    return sync_runtime_mirrors(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
