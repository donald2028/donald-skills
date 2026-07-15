#!/usr/bin/env python3
"""One-command orchestrator: capture -> extract -> download for an X
account's own Posts + Articles timeline.

    python research_user.py --handle OpenAI --max-posts 200
    python research_user.py --handle OpenAI --since 2026-05-01
    python research_user.py --handle OpenAI --incremental

Reuses research_post.py's Chrome/CDP bootstrap. Pulls in a human only for
the one-time login or when capture reports `needs_ops`.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import capture_user_timeline
import download_media
import extract_user_timeline
from profile_config import ProfileConfigError
from output_paths import resolve_tool_output_root
from research_post import (
    activate_for_human_attention,
    ensure_chrome_cdp,
    resolve_cdp_port,
)


def _lock_path(post_dir: Path) -> Path:
    return post_dir / ".collect.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_session_lock(post_dir: Path, pid: int | None = None) -> int | None:
    """Claim the per-account collection lock; one capture session at a time.

    Returns the live holder's pid if another process already owns the lock
    (the caller should refuse to start), else None after writing our own pid.
    A lock left behind by a pid that's no longer running is stale and gets
    reclaimed automatically rather than blocking forever.
    """
    pid = os.getpid() if pid is None else pid
    post_dir.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(post_dir)
    if lock_file.exists():
        try:
            holder_pid = int(lock_file.read_text(encoding="utf-8").strip())
        except ValueError:
            holder_pid = None
        if holder_pid and holder_pid != pid and _pid_alive(holder_pid):
            return holder_pid
    lock_file.write_text(str(pid), encoding="utf-8")
    return None


def release_session_lock(post_dir: Path) -> None:
    _lock_path(post_dir).unlink(missing_ok=True)


def manifest_path(post_dir: Path) -> Path:
    return post_dir / "manifest.json"


def load_manifest(post_dir: Path) -> dict[str, Any]:
    path = manifest_path(post_dir)
    if not path.exists():
        return {"runs": []}
    return json.loads(path.read_text(encoding="utf-8"))


VALID_MODES = ("head", "backfill", "full")
HEAD_OVERLAP_K = 2


def _post_status_ids(post: dict[str, Any]) -> list[str]:
    if post.get("is_thread"):
        return [t["status_id"] for t in post.get("thread", []) if t.get("status_id")]
    return [post["status_id"]] if post.get("status_id") else []


def load_known_status_ids(post_dir: Path) -> set[str]:
    """All status_ids (incl. thread sub-tweets) already in the saved timeline.

    Drives head-mode reconnection detection. Reads timeline.jsonl, falling back
    to a legacy timeline.json.
    """
    known: set[str] = set()
    jsonl = post_dir / "timeline.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                known.update(_post_status_ids(json.loads(line)))
        return known
    legacy = post_dir / "timeline.json"
    if legacy.exists():
        for post in json.loads(legacy.read_text(encoding="utf-8")).get("posts", []):
            known.update(_post_status_ids(post))
    return known


def resolve_overlap(mode: str, post_dir: Path, k: int = HEAD_OVERLAP_K) -> tuple[set[str], int]:
    """Map a collection mode to (overlap_ids, overlap_k) for the scroll stop.

    head: stop once K consecutive already-known posts reappear — catches every new
    post since last run and reconnects with prior coverage with no gap, because the
    anchor is content (status_id), not a scroll position.
    backfill / full: no overlap stop — keep scrolling past prior coverage to extend
    the older tail; the already-known top dedups for free against immutable runs.
    """
    if mode == "head":
        return load_known_status_ids(post_dir), k
    return set(), k


def append_manifest(
    post_dir: Path, timeline: dict[str, Any], capture_result: dict[str, Any] | None = None
) -> None:
    manifest = load_manifest(post_dir)
    posts = timeline.get("posts") or []
    newest_id = posts[0]["status_id"] if posts else None
    oldest_id = posts[-1]["status_id"] if posts else None
    created_ats = [p.get("created_at") or "" for p in posts if p.get("created_at")]
    entry = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": timeline.get("count", 0),
        "expanded_status_count": sum(len(_post_status_ids(p)) for p in posts),
        "section_counts": timeline.get("section_counts", {}),
        "stop_reason": timeline.get("stop_reason"),
        "newest_status_id": newest_id,
        "oldest_status_id": oldest_id,
        "newest_created_at": max(created_ats, default=""),
        "oldest_created_at": min(created_ats, default=""),
    }
    if capture_result:
        entry["capture_responses"] = capture_result.get("responses")
        entry["capture_new_responses"] = capture_result.get("new_responses")
        entry["capture_rejected_responses"] = capture_result.get("rejected_responses")
    manifest["runs"].append(entry)
    post_dir.mkdir(parents=True, exist_ok=True)
    manifest_path(post_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def preserve_capture_stop_reason(
    post_dir: Path, timeline: dict[str, Any], capture_result: dict[str, Any]
) -> dict[str, Any]:
    """Use capture's concrete stop reason when extraction did not cut posts.

    extract_user_timeline.write_timeline returns "exhausted" whenever no
    max/since/overlap cut is applied. For account capture, the browser phase
    already knows whether it stopped because the page stopped yielding posts,
    hit max scrolls, or reached max_posts. Preserve that signal for manifests
    and final JSON output.
    """
    capture_reason = capture_result.get("stop_reason")
    if timeline.get("stop_reason") != "exhausted" or not capture_reason:
        return timeline

    timeline = dict(timeline)
    timeline["stop_reason"] = capture_reason
    if (post_dir / "timeline.jsonl").exists():
        extract_user_timeline.write_timeline_jsonl(post_dir, timeline)
    return timeline


def run(
    handle: str,
    data_root: Path,
    port: int | None = None,
    max_scrolls: int = 60,
    stable_rounds: int = capture_user_timeline.DEFAULT_STABLE_ROUNDS,
    max_posts: int | None = None,
    since: str | None = None,
    incremental: bool = False,
    download_media_files: bool = True,
    mode: str = "full",
) -> dict[str, Any]:
    handle = extract_user_timeline.normalize_handle(handle)
    post_dir = data_root.expanduser().resolve() / handle / "_user"
    try:
        port = resolve_cdp_port(port)
    except ProfileConfigError as error:
        return {"status": "needs_ops", "reason": "browser_profile_unconfigured", "hint": str(error)}
    if incremental:
        mode = "head"  # back-compat alias

    holder_pid = acquire_session_lock(post_dir)
    if holder_pid is not None:
        return {"status": "needs_ops", "reason": "already_running",
                "hint": f"Another collection for {handle} is already running "
                        f"(pid {holder_pid}); wait for it to finish instead of "
                        "starting a second one against the same CDP tab."}

    try:
        if not ensure_chrome_cdp(port):
            return {"status": "needs_ops", "reason": "cdp_unavailable",
                    "hint": getattr(ensure_chrome_cdp, "last_error", "") or
                            "Run the shared Chrome-over-CDP preflight, log in to X, then rerun."}

        overlap_ids, overlap_k = resolve_overlap(mode, post_dir)
        cap = capture_user_timeline.capture(
            handle, post_dir, browser=capture_user_timeline.AgentBrowser(port),
            max_scrolls=max_scrolls, stable_rounds=stable_rounds,
            max_posts=max_posts, since=since, overlap_ids=overlap_ids, overlap_k=overlap_k)
        if cap["status"] != "complete":
            return activate_for_human_attention(cap, port)

        # The overlap stop only tells the scroll loop when to stop early — the saved
        # timeline.jsonl must keep the full accumulated history, not just this run's
        # new delta.
        timeline = extract_user_timeline.write_timeline(
            handle, data_root, max_posts=max_posts, since=since)
        timeline = preserve_capture_stop_reason(post_dir, timeline, cap)
        if download_media_files:
            timeline = download_media.download_timeline(post_dir)

        append_manifest(post_dir, timeline, capture_result=cap)
        return {
            "status": "complete", "handle": handle, "user_dir": str(post_dir),
            "posts": timeline["count"], "stop_reason": timeline["stop_reason"],
        }
    finally:
        release_session_lock(post_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--handle", required=True, help="X handle, e.g. OpenAI (or a profile URL).")
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
    parser.add_argument("--max-scrolls", type=int, default=60)
    parser.add_argument("--stable-rounds", type=int, default=capture_user_timeline.DEFAULT_STABLE_ROUNDS,
                        help="Consecutive scroll rounds without new posts before stopping.")
    parser.add_argument("--max-posts", type=int, default=None)
    parser.add_argument("--since", default=None, help="ISO date, e.g. 2026-05-01.")
    parser.add_argument("--incremental", action="store_true",
                        help="Alias for --mode head: stop once reconnected with the previous run.")
    parser.add_argument("--mode", choices=VALID_MODES, default="full",
                        help="head: catch new posts and stop at reconnect. "
                             "backfill/full: keep scrolling to extend the older tail.")
    parser.add_argument("--no-media", action="store_true",
                        help="Capture and parse timeline only; skip media downloads for faster sampling.")
    args = parser.parse_args()

    output_root = resolve_tool_output_root("x", args.output_root)
    result = run(args.handle, output_root, port=args.cdp, max_scrolls=args.max_scrolls,
                 stable_rounds=args.stable_rounds, max_posts=args.max_posts,
                 since=args.since, incremental=args.incremental,
                 download_media_files=not args.no_media, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
