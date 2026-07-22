#!/usr/bin/env python3
"""One-command orchestrator: capture -> extract -> download for an X
account's own Posts + Articles timeline.

    python research_user.py --handle OpenAI --capture-max-posts 200
    python research_user.py --handle OpenAI --since 2026-05-01
    python research_user.py --handle OpenAI --incremental

Reuses research_post.py's Chrome/CDP bootstrap. Pulls in a human only for
the one-time login or when capture reports `needs_ops`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import capture_user_timeline
import download_media
import extract_user_timeline
from browser_runtime import BrowserSession
from profile_config import ProfileConfigError
from output_paths import resolve_tool_output_root
from research_post import (
    HUMAN_ATTENTION_REASONS,
    activate_for_human_attention,
    browser_session_options,
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
STATUS_SCHEMA_VERSION = 1
INTERRUPT_RECOVERY_HINT = (
    "Rerun the same command to resume from immutable runs; the account lock was released."
)


def account_output_paths(handle: str, data_root: Path) -> tuple[Path, Path]:
    output_root = data_root.expanduser().resolve()
    return output_root, output_root / handle / "_user"


def invalid_output_root_result(
    handle: str,
    output_root: Path,
    post_dir: Path,
) -> dict[str, Any] | None:
    root_name = output_root.name.lstrip("@").casefold()
    passed_handle_dir = root_name == handle.casefold()
    passed_user_dir = (
        output_root.name == "_user"
        and output_root.parent.name.lstrip("@").casefold() == handle.casefold()
    )
    if not passed_handle_dir and not passed_user_dir:
        return None

    suggested_root = output_root.parent if passed_handle_dir else output_root.parent.parent
    result = status_result(
        "needs_ops",
        handle,
        output_root,
        post_dir,
        reason="invalid_output_root",
        hint=(
            "--output-root must be the X collection root, not a handle or _user directory; "
            f"pass {suggested_root} so the canonical user directory is "
            f"{suggested_root / handle / '_user'}."
        ),
    )
    result["suggested_output_root"] = str(suggested_root)
    result["suggested_canonical_user_dir"] = str(suggested_root / handle / "_user")
    return result


def _archive_counts(
    post_dir: Path,
    timeline: dict[str, Any] | None = None,
) -> tuple[int, int, int]:
    source = timeline
    if source is None:
        meta_path = post_dir / "timeline.meta.json"
        if meta_path.exists():
            try:
                source = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                source = None
    source = source or {}
    sections = source.get("section_counts") or {}
    posts = int(sections.get("posts") or 0)
    articles = int(sections.get("articles") or 0)
    total = int(source.get("count") or posts + articles)
    return posts, articles, total


def status_result(
    status: str,
    handle: str,
    output_root: Path,
    post_dir: Path,
    *,
    reason: str | None = None,
    hint: str | None = None,
    stop_reason: str | None = None,
    timeline: dict[str, Any] | None = None,
    capture_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capture_result = capture_result or {}
    posts, articles, total = _archive_counts(post_dir, timeline)
    result = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "hint": hint,
        "handle": handle,
        "output_root": str(output_root),
        "canonical_user_dir": str(post_dir),
        "user_dir": str(post_dir),
        "stop_reason": stop_reason or reason,
        "posts": posts,
        "articles": articles,
        "total_items": total,
        "capture_posts_seen": int(capture_result.get("capture_posts_seen") or 0),
        "capture_new_posts": int(capture_result.get("capture_new_posts") or 0),
        "capture_known_overlap_posts": int(
            capture_result.get("capture_known_overlap_posts") or 0
        ),
        "known_before_posts": int(capture_result.get("known_before_posts") or 0),
        "articles_included": bool(capture_result.get("articles_included", False)),
    }
    for key in ("responses", "new_responses", "rejected_responses"):
        if key in capture_result:
            result[key] = capture_result[key]
    return result


def write_status_meta(post_dir: Path, result: dict[str, Any]) -> None:
    meta_path = post_dir / "timeline.meta.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta.update({
        "schema_version": STATUS_SCHEMA_VERSION,
        "handle": result["handle"],
        "captured_at": meta.get("captured_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": result["status"],
        "reason": result["reason"],
        "stop_reason": result["stop_reason"],
        "output_root": result["output_root"],
        "canonical_user_dir": result["canonical_user_dir"],
        "count": result["total_items"],
        "section_counts": {
            "posts": result["posts"],
            "articles": result["articles"],
        },
        "capture_posts_seen": result["capture_posts_seen"],
        "capture_new_posts": result["capture_new_posts"],
        "capture_known_overlap_posts": result["capture_known_overlap_posts"],
        "known_before_posts": result["known_before_posts"],
        "articles_included": result["articles_included"],
    })
    if result.get("hint"):
        meta["recovery_hint"] = result["hint"]
    extract_user_timeline.atomic_write_text(
        meta_path,
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
    )


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
        entry["capture_posts_seen"] = capture_result.get("capture_posts_seen")
        entry["capture_new_posts"] = capture_result.get("capture_new_posts")
        entry["capture_known_overlap_posts"] = capture_result.get(
            "capture_known_overlap_posts"
        )
        entry["known_before_posts"] = capture_result.get("known_before_posts")
        entry["articles_included"] = capture_result.get("articles_included")
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
    include_articles: bool | None = None,
    article_max_scrolls: int = capture_user_timeline.ARTICLE_MAX_SCROLLS,
    article_stable_rounds: int = capture_user_timeline.ARTICLE_STABLE_ROUNDS,
    article_timeout_seconds: int = capture_user_timeline.ARTICLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    handle = extract_user_timeline.normalize_handle(handle)
    output_root, post_dir = account_output_paths(handle, data_root)
    invalid_root = invalid_output_root_result(handle, output_root, post_dir)
    if invalid_root:
        return invalid_root
    if incremental:
        mode = "head"  # back-compat alias
    if include_articles is None:
        include_articles = mode != "head"

    try:
        port = resolve_cdp_port(port)
    except ProfileConfigError as error:
        return status_result(
            "needs_ops",
            handle,
            output_root,
            post_dir,
            reason="browser_profile_unconfigured",
            hint=str(error),
        )

    holder_pid = acquire_session_lock(post_dir)
    if holder_pid is not None:
        return status_result(
            "needs_ops",
            handle,
            output_root,
            post_dir,
            reason="already_running",
            hint=(
                f"Another collection for {handle} is already running (pid {holder_pid}); "
                "wait for it to finish instead of starting a second one against the same CDP tab."
            ),
        )

    try:
        browser_session: BrowserSession | None = None
        browser: capture_user_timeline.AgentBrowser | None = None
        result: dict[str, Any]
        try:
            explicit_config, require_initialized_profile = browser_session_options(port)
            browser_session = BrowserSession(
                scope="donald-collect-x",
                session=f"donald-x-user-{handle}",
                url=f"https://x.com/{handle}",
                port=port,
                config=explicit_config,
                require_initialized_profile=require_initialized_profile,
            ).open()
            browser = capture_user_timeline.AgentBrowser(
                port,
                target_id=browser_session.target_id,
            )

            overlap_ids, overlap_k = resolve_overlap(mode, post_dir)
            cap = capture_user_timeline.capture(
                handle, post_dir, browser=browser,
                max_scrolls=max_scrolls, stable_rounds=stable_rounds,
                max_posts=max_posts, since=since, overlap_ids=overlap_ids, overlap_k=overlap_k,
                include_articles=include_articles,
                article_max_scrolls=article_max_scrolls,
                article_stable_rounds=article_stable_rounds,
                article_timeout_seconds=article_timeout_seconds)
            if cap["status"] != "complete":
                reason = cap.get("reason") or "capture_failed"
                result = status_result(
                    "needs_ops",
                    handle,
                    output_root,
                    post_dir,
                    reason=reason,
                    hint=cap.get("hint"),
                    capture_result=cap,
                )
                if browser_session and cap.get("reason") in HUMAN_ATTENTION_REASONS:
                    result["browser_activation"] = browser_session.preserve_for_human()
                else:
                    result = activate_for_human_attention(result, port)
                write_status_meta(post_dir, result)
            else:
                # max_posts/since/overlap are browser capture boundaries only. Rebuild the
                # persisted timeline from every immutable run so a small head budget can never
                # truncate previously archived history.
                timeline = extract_user_timeline.write_timeline(handle, output_root)
                timeline = preserve_capture_stop_reason(post_dir, timeline, cap)
                if download_media_files:
                    timeline = download_media.download_timeline(post_dir)

                append_manifest(post_dir, timeline, capture_result=cap)
                result = status_result(
                    "complete",
                    handle,
                    output_root,
                    post_dir,
                    stop_reason=timeline["stop_reason"],
                    timeline=timeline,
                    capture_result=cap,
                )
                write_status_meta(post_dir, result)
        except KeyboardInterrupt:
            result = status_result(
                "interrupted",
                handle,
                output_root,
                post_dir,
                hint=INTERRUPT_RECOVERY_HINT,
                stop_reason="interrupted",
            )
            write_status_meta(post_dir, result)
        except (OSError, ProfileConfigError, subprocess.SubprocessError, TimeoutError) as error:
            result = status_result(
                "needs_ops",
                handle,
                output_root,
                post_dir,
                reason="cdp_unavailable",
                hint=str(error),
            )
            write_status_meta(post_dir, result)
        finally:
            if browser is not None:
                browser.close()
            if browser_session is not None:
                cleanup = browser_session.close()
                if "result" in locals():
                    result["browser_cleanup"] = cleanup
        return result
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
    parser.add_argument(
        "--capture-max-posts",
        "--max-posts",
        dest="max_posts",
        type=int,
        default=None,
        help="Posts budget for this browser pass only; never truncates the persisted timeline.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date capture boundary, e.g. 2026-05-01; does not trim archived output.",
    )
    parser.add_argument("--incremental", action="store_true",
                        help="Alias for --mode head: stop once reconnected with the previous run.")
    parser.add_argument("--mode", choices=VALID_MODES, default="full",
                        help="head: catch new posts and stop at reconnect. "
                             "backfill/full: keep scrolling to extend the older tail.")
    parser.add_argument(
        "--include-articles",
        action="store_true",
        default=None,
        help="Capture Articles too. Head mode skips Articles unless this flag is passed.",
    )
    parser.add_argument(
        "--article-max-scrolls",
        type=int,
        default=capture_user_timeline.ARTICLE_MAX_SCROLLS,
        help="Independent maximum scroll rounds for the Articles phase.",
    )
    parser.add_argument(
        "--article-stable-rounds",
        type=int,
        default=capture_user_timeline.ARTICLE_STABLE_ROUNDS,
        help="Independent stable-round limit for the Articles phase.",
    )
    parser.add_argument(
        "--article-timeout-seconds",
        type=int,
        default=capture_user_timeline.ARTICLE_TIMEOUT_SECONDS,
        help="Independent wall-clock budget for the Articles phase.",
    )
    parser.add_argument("--no-media", action="store_true",
                        help="Capture and parse timeline only; skip media downloads for faster sampling.")
    args = parser.parse_args()

    output_root = resolve_tool_output_root("x", args.output_root)
    result = run(args.handle, output_root, port=args.cdp, max_scrolls=args.max_scrolls,
                 stable_rounds=args.stable_rounds, max_posts=args.max_posts,
                 since=args.since, incremental=args.incremental,
                 download_media_files=not args.no_media, mode=args.mode,
                 include_articles=args.include_articles,
                 article_max_scrolls=args.article_max_scrolls,
                 article_stable_rounds=args.article_stable_rounds,
                 article_timeout_seconds=args.article_timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "complete":
        return 0
    if result["status"] == "interrupted":
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
