#!/usr/bin/env python3
"""One-command orchestrator: capture -> extract -> download for an X post.

Runs the whole pipeline with zero agent involvement on the happy path:

    python research_post.py --url https://x.com/<handle>/status/<id>

Pulls in a human only for the one-time login or when capture reports
`needs_ops` (login wall / rate limit / error page it cannot resolve).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import capture_thread
import download_media
import extract_thread
from output_paths import resolve_tool_output_root
from profile_config import (
    ProfileConfigError,
    activate_browser,
    configured_browser,
    create_background_page,
    default_runtime_root,
    ensure_agent_browser,
    preflight_browser,
)

MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DATA_DIR = os.environ.get("X_COLLECTOR_CHROME_DATA_DIR", "")

LEGACY_DEFAULT_CDP_PORT = 9222
HUMAN_ATTENTION_REASONS = {"login_wall", "rate_limited", "error_page"}


def resolve_cdp_port(port: int | None = None) -> int:
    if port is not None:
        return port
    if not CHROME_DATA_DIR and not os.environ.get("X_COLLECTOR_CHROME_EXECUTABLE"):
        return int(configured_browser()["chrome"]["default_cdp_port"])
    return LEGACY_DEFAULT_CDP_PORT


def chrome_executable() -> str:
    explicit = os.environ.get("X_COLLECTOR_CHROME_EXECUTABLE")
    if explicit:
        return explicit
    candidates = [
        MACOS_CHROME,
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError(
        "Google Chrome/Chromium not found; set X_COLLECTOR_CHROME_EXECUTABLE"
    )


def cdp_up(port: int = 9222) -> bool:
    """True if a real Chrome is reachable over CDP on the given port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "Chrome/" in info.get("Browser", "")


def agent_browser_attaches(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            targets = json.loads(response.read().decode("utf-8"))
        if not any(target.get("type") == "page" for target in targets):
            create_background_page(port, "about:blank")
        executable = ensure_agent_browser(auto_install=True)["executable"]
        result = subprocess.run(
            [
                executable,
                "--session",
                f"donald-x-cdp-{port}-{os.getpid()}",
                "--cdp",
                str(port),
                "get",
                "url",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            ensure_chrome_cdp.last_error = result.stdout.strip()
        return result.returncode == 0
    except (OSError, ProfileConfigError, subprocess.TimeoutExpired) as error:
        ensure_chrome_cdp.last_error = str(error)
        return False


def _launch_chrome(port: int) -> None:
    chrome = chrome_executable()
    user_data_dir = CHROME_DATA_DIR or str(
        default_runtime_root() / "X Collector"
    )
    launch_args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-startup-window",
    ]
    command = (
        ["open", "-g", "-j", "-n", "-a", "Google Chrome", "--args", *launch_args]
        if sys.platform == "darwin" and chrome == MACOS_CHROME
        else [chrome, *launch_args]
    )
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def ensure_chrome_cdp(
    port: int | None = None,
    check: Callable[[int], bool] = cdp_up,
    launch: Callable[[int], None] = _launch_chrome,
    wait: Callable[[], None] = lambda: time.sleep(2),
    retries: int = 4,
) -> bool:
    """Ensure a CDP-enabled real Chrome is up; launch one if needed.

    Does NOT kill an existing Chrome — a separate user-data-dir coexists with
    the user's normal browser.
    """
    ensure_chrome_cdp.last_error = ""
    try:
        port = resolve_cdp_port(port)
    except ProfileConfigError as error:
        ensure_chrome_cdp.last_error = str(error)
        return False
    use_shared_config = not CHROME_DATA_DIR and not os.environ.get(
        "X_COLLECTOR_CHROME_EXECUTABLE"
    )
    if use_shared_config:
        try:
            ensure_agent_browser(auto_install=True)
            config = configured_browser()
            preflight_browser(
                config,
                port,
                f"donald-x-cdp-{port}-{os.getpid()}",
                "about:blank",
                60,
            )
            return check(port)
        except (OSError, ProfileConfigError, subprocess.TimeoutExpired) as error:
            ensure_chrome_cdp.last_error = str(error)
            return False

    if check(port):
        return agent_browser_attaches(port)
    try:
        launch(port)
    except OSError as error:
        ensure_chrome_cdp.last_error = str(error)
        return False
    for _ in range(retries):
        wait()
        if check(port):
            return agent_browser_attaches(port)
    return False


ensure_chrome_cdp.last_error = ""


def activate_for_human_attention(result: dict[str, Any], port: int) -> dict[str, Any]:
    if result.get("status") != "needs_ops" or result.get("reason") not in HUMAN_ATTENTION_REASONS:
        return result
    result = dict(result)
    try:
        result["browser_activation"] = activate_browser(configured_browser(), port)
    except (OSError, ProfileConfigError, subprocess.SubprocessError) as error:
        result["browser_activation"] = {"status": "error", "error": str(error)}
    return result


def run(
    url: str,
    data_root: Path,
    port: int | None = None,
    max_scrolls: int = 15,
) -> dict[str, Any]:
    handle, status_id = extract_thread.parse_target_url(url)
    post_dir = data_root.expanduser().resolve() / handle / status_id

    try:
        port = resolve_cdp_port(port)
    except ProfileConfigError as error:
        return {"status": "needs_ops", "reason": "browser_profile_unconfigured", "hint": str(error)}
    if not ensure_chrome_cdp(port):
        return {"status": "needs_ops", "reason": "cdp_unavailable",
                "hint": getattr(ensure_chrome_cdp, "last_error", "") or
                        "Run the shared Chrome-over-CDP preflight, log in to X, then rerun."}

    cap = capture_thread.capture(url, post_dir,
                                 browser=capture_thread.AgentBrowser(port),
                                 max_scrolls=max_scrolls)
    if cap["status"] != "complete":
        return activate_for_human_attention(cap, port)

    thread = extract_thread.write_thread(url, data_root)
    thread = download_media.download_thread(post_dir)
    total = sum(len(p.get("media", [])) for p in thread.get("posts", []))
    done = sum(1 for p in thread.get("posts", []) for m in p.get("media", [])
               if m.get("status") == "downloaded")
    return {"status": "complete", "post_dir": str(post_dir),
            "posts": thread["count"], "media_downloaded": done, "media_total": total}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True, help="Target X post URL.")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Exact X collection root for this run. Defaults to the Donald Skills Documents data directory.",
    )
    parser.add_argument(
        "--cdp",
        type=int,
        default=None,
        help="Explicit legacy override; otherwise use this skill's configured Profile port.",
    )
    parser.add_argument("--max-scrolls", type=int, default=15)
    args = parser.parse_args()

    output_root = resolve_tool_output_root("x", args.output_root)
    result = run(args.url, output_root, port=args.cdp, max_scrolls=args.max_scrolls)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
