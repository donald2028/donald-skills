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
from pathlib import Path
from typing import Any

import capture_thread
import download_media
import extract_thread
from browser_runtime import BrowserSession
from output_paths import resolve_tool_output_root
from profile_config import (
    ProfileConfigError,
    activate_browser,
    configured_browser,
    default_runtime_root,
)

MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DATA_DIR = os.environ.get("X_COLLECTOR_CHROME_DATA_DIR", "")

LEGACY_DEFAULT_CDP_PORT = 9222
HUMAN_ATTENTION_REASONS = {"login_wall", "captcha", "rate_limited", "error_page"}


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


def browser_session_options(port: int) -> tuple[dict[str, Any] | None, bool]:
    legacy_override = bool(CHROME_DATA_DIR or os.environ.get("X_COLLECTOR_CHROME_EXECUTABLE"))
    if not legacy_override:
        return None, True
    return (
        {
            "scope": "donald-collect-x",
            "profile": {"directory": "Default", "name": "Default", "email": ""},
            "chrome": {
                "cdp_user_data_dir": CHROME_DATA_DIR or str(default_runtime_root() / "X Collector"),
                "default_cdp_port": port,
                "executable": chrome_executable(),
            },
        },
        False,
    )


def activate_for_human_attention(result: dict[str, Any], port: int) -> dict[str, Any]:
    if result.get("status") != "needs_ops" or result.get("reason") not in HUMAN_ATTENTION_REASONS:
        return result
    result = dict(result)
    try:
        result["browser_activation"] = activate_browser(configured_browser(), port)
    except (OSError, ProfileConfigError, subprocess.SubprocessError, TimeoutError) as error:
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
    explicit_config, require_initialized_profile = browser_session_options(port)
    browser_session: BrowserSession | None = None
    browser: capture_thread.AgentBrowser | None = None
    result: dict[str, Any]
    try:
        browser_session = BrowserSession(
            scope="donald-collect-x",
            session=f"donald-x-post-{status_id}",
            url=url,
            port=port,
            config=explicit_config,
            require_initialized_profile=require_initialized_profile,
        ).open()
        browser = capture_thread.AgentBrowser(port, target_id=browser_session.target_id)

        cap = capture_thread.capture(
            url,
            post_dir,
            browser=browser,
            max_scrolls=max_scrolls,
        )
        if cap["status"] != "complete":
            result = dict(cap)
            if browser_session and cap.get("reason") in HUMAN_ATTENTION_REASONS:
                result["browser_activation"] = browser_session.preserve_for_human()
            else:
                result = activate_for_human_attention(result, port)
        else:
            thread = extract_thread.write_thread(url, data_root)
            thread = download_media.download_thread(post_dir)
            total = sum(len(p.get("media", [])) for p in thread.get("posts", []))
            done = sum(1 for p in thread.get("posts", []) for m in p.get("media", [])
                       if m.get("status") == "downloaded")
            result = {"status": "complete", "post_dir": str(post_dir),
                      "posts": thread["count"], "media_downloaded": done, "media_total": total}
    except (OSError, ProfileConfigError, subprocess.SubprocessError) as error:
        result = {"status": "needs_ops", "reason": "cdp_unavailable", "hint": str(error)}
    finally:
        if browser is not None:
            browser.close()
        if browser_session is not None:
            cleanup = browser_session.close()
            if "result" in locals():
                result["browser_cleanup"] = cleanup
    return result


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
