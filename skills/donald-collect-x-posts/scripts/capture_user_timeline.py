#!/usr/bin/env python3
"""Drive a real Chrome over CDP to capture an X account's Posts + Articles
timelines. Human-paced scrolling, same anti-detection posture as
capture_thread.py. Only known bad states (login wall, rate limit, error
page) short-circuit to `needs_ops` for a human to resolve.
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from capture_thread import AgentBrowser, _jitter_sleep, detect_block
from extract_thread import _maybe_body, iter_tweets, load_tweets_from_runs
from extract_user_timeline import (
    build_timeline_posts,
    count_sections,
    extract_timeline_tweet,
    find_stop_cut,
    limit_output_posts,
    write_timeline_jsonl,
)

DEFAULT_STABLE_ROUNDS = 60
DEFAULT_BOTTOM_STABLE_ROUNDS = 5
PROFILE_SCROLL_RANGE = (3000, 5000)
ARTICLE_SCROLL_RANGE = (2400, 4000)
ARTICLE_STABLE_ROUNDS = 2
MAX_ERROR_PAGE_RETRIES = 3
BOTTOM_REMAINING_PX = 1200


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class Browser(Protocol):
    def open(self, url: str) -> None: ...
    def page_text(self) -> str: ...
    def list_request_ids(self, filter_name: str) -> list[str]: ...
    def save_response(self, request_id: str, path: Path) -> None: ...
    def scroll(self, pixels: int) -> None: ...
    def click_tab(self, label: str) -> None: ...
    def click_retry(self) -> bool: ...
    def enter_page(self) -> None: ...


def write_checkpoint(post_dir: Path, posts: list[dict[str, Any]]) -> None:
    ids: list[str] = []
    for p in posts:
        if p.get("is_thread"):
            ids.extend(t["status_id"] for t in p.get("thread", []))
        else:
            ids.append(p["status_id"])
    created_ats = [p.get("created_at") or "" for p in posts if p.get("created_at")]
    checkpoint = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "known_ids": ids,
        "post_count": len(posts),
        "oldest_created_at": min(created_ats, default=""),
        "newest_created_at": max(created_ats, default=""),
    }
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "checkpoint.json").write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def write_progress_timeline(
    handle: str,
    post_dir: Path,
    posts: list[dict[str, Any]],
    *,
    max_posts: int | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    visible_posts, _ = limit_output_posts(posts, max_posts=max_posts, since=since)
    timeline = {
        "handle": handle,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop_reason": "capturing",
        "count": len(visible_posts),
        "section_counts": count_sections(visible_posts),
        "posts": visible_posts,
    }
    write_timeline_jsonl(post_dir, timeline)
    return timeline


def append_capture_debug(post_dir: Path, event: dict[str, Any]) -> None:
    post_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **event,
    }
    with (post_dir / "capture_debug.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _capture_articles(
    browser: Browser,
    post_dir: Path,
    runs: Path,
    saved: set[str],
    sleep,
    max_scrolls: int,
    stable_rounds: int = ARTICLE_STABLE_ROUNDS,
) -> str | None:
    block = _recover_blocking_page(browser, sleep)
    if block:
        return block
    append_capture_debug(post_dir, {"phase": "articles", "event": "click_tab", "label": "Articles"})
    browser.click_tab("Articles")
    append_capture_debug(post_dir, {"phase": "articles", "event": "tab_clicked", "label": "Articles"})
    sleep()
    last_scroll_marker = _scroll_marker(browser)
    stable = 0
    for round_index in range(max_scrolls):
        block = _recover_blocking_page(browser, sleep)
        if block:
            return block
        request_ids = browser.list_request_ids("UserArticlesTweets")
        new_ids = [rid for rid in request_ids if rid not in saved]
        if not new_ids:
            append_capture_debug(post_dir, {
                "phase": "articles",
                "round": round_index,
                "request_ids": len(request_ids),
                "new_request_ids": 0,
                "valid_new_responses": 0,
                "stable": stable,
                "stop_reason": "no_new_article_requests",
            })
            return None
        round_had_valid_response = False
        round_valid = 0
        round_rejected = 0
        for rid in new_ids:
            path = runs / f"ua-{_safe(rid)}.json"
            existed = path.exists()
            saved_response = _save_response_once(browser, rid, path)
            if saved_response:
                round_had_valid_response = True
                round_valid += 1
            if saved_response or existed:
                saved.add(rid)
            elif not path.exists():
                round_rejected += 1
                saved.add(rid)
        browser.scroll(random.randint(*ARTICLE_SCROLL_RANGE))
        sleep()
        current_scroll_marker = _scroll_marker(browser)
        scroll_moved = (
            current_scroll_marker is not None
            and last_scroll_marker is not None
            and current_scroll_marker != last_scroll_marker
        )
        near_bottom = _near_bottom(current_scroll_marker)
        if round_had_valid_response and not (near_bottom and not scroll_moved):
            stable = 0
        elif scroll_moved and not near_bottom:
            stable = 0
        else:
            stable += 1
            if stable >= stable_rounds:
                append_capture_debug(post_dir, {
                    "phase": "articles",
                    "round": round_index,
                    "request_ids": len(request_ids),
                    "new_request_ids": len(new_ids),
                    "valid_new_responses": round_valid,
                    "rejected_responses": round_rejected,
                    "scroll_marker": current_scroll_marker,
                    "scroll_moved": scroll_moved,
                    "near_bottom": near_bottom,
                    "stable": stable,
                    "stop_reason": "no_new_articles",
                })
                return None
        last_scroll_marker = current_scroll_marker
        append_capture_debug(post_dir, {
            "phase": "articles",
            "round": round_index,
            "request_ids": len(request_ids),
            "new_request_ids": len(new_ids),
            "valid_new_responses": round_valid,
            "rejected_responses": round_rejected,
            "scroll_marker": current_scroll_marker,
            "scroll_moved": scroll_moved,
            "near_bottom": near_bottom,
            "stable": stable,
            "stop_reason": None,
        })
    return None


def _save_response_once(browser: Browser, request_id: str, path: Path) -> bool:
    """Save an in-browser response without ever clobbering an existing run.

    Long user captures are resumed often. Chrome can keep old request ids in
    its network buffer after their response bodies are no longer available, so
    overwriting a previous good capture with that stale metadata would corrupt
    the timeline. Existing run files are immutable evidence.
    """
    if path.exists():
        return False
    browser.save_response(request_id, path)
    if _saved_response_has_tweets(path):
        return True
    _reject_response(path)
    return False


def _reject_response(path: Path) -> None:
    if not path.exists():
        return
    rejected_dir = path.parent.parent / "rejected_responses"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    path.replace(rejected_dir / path.name)


def _saved_response_has_tweets(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool(iter_tweets(_maybe_body(payload)))


def _scroll_marker(browser: Browser) -> str | None:
    marker = getattr(browser, "scroll_marker", None)
    if not callable(marker):
        return None
    try:
        return str(marker())
    except Exception:
        return None


def _near_bottom(scroll_marker: str | None) -> bool:
    if not scroll_marker:
        return False
    try:
        marker = json.loads(scroll_marker)
        y = float(marker.get("y", 0))
        height = float(marker.get("h", 0))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return False
    return y > 0 and height > 0 and height - y <= BOTTOM_REMAINING_PX


def _click_retry(browser: Browser) -> bool:
    retry = getattr(browser, "click_retry", None)
    if callable(retry):
        return bool(retry())
    click_button = getattr(browser, "click_button_texts", None)
    if callable(click_button):
        return bool(click_button(["Try again", "Retry"]))
    return False


def _recover_blocking_page(browser: Browser, sleep) -> str | None:
    retries = 0
    while True:
        block = detect_block(browser.page_text())
        if block != "error_page":
            return block
        if retries >= MAX_ERROR_PAGE_RETRIES or not _click_retry(browser):
            return block
        retries += 1
        sleep()


def capture(
    handle: str,
    post_dir: Path,
    browser: Browser | None = None,
    sleep=_jitter_sleep,
    max_scrolls: int = 60,
    stable_rounds: int = DEFAULT_STABLE_ROUNDS,
    bottom_stable_rounds: int = DEFAULT_BOTTOM_STABLE_ROUNDS,
    max_posts: int | None = None,
    since: str | None = None,
    overlap_ids: set[str] | None = None,
    overlap_k: int = 1,
) -> dict[str, Any]:
    browser = browser or AgentBrowser()
    runs = post_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    browser.open(f"https://x.com/{handle}")
    block = _recover_blocking_page(browser, sleep)
    if block:
        return {"status": "needs_ops", "reason": block, "responses": 0}
    browser.enter_page()

    saved: set[str] = set()
    new_responses = 0
    rejected_responses = 0
    last_size = -1
    last_scroll_marker = _scroll_marker(browser)
    stable = 0
    stop_reason = "max_scrolls_reached"
    for round_index in range(max_scrolls):
        block = _recover_blocking_page(browser, sleep)
        if block:
            return {"status": "needs_ops", "reason": block, "responses": len(saved)}
        round_had_timeline_response = False
        request_ids = browser.list_request_ids("UserTweets")
        new_ids = [rid for rid in request_ids if rid not in saved]
        round_valid = 0
        round_rejected = 0
        for rid in new_ids:
            if rid not in saved:
                path = runs / f"ut-{_safe(rid)}.json"
                existed = path.exists()
                saved_response = _save_response_once(browser, rid, path)
                if saved_response:
                    new_responses += 1
                    round_valid += 1
                    round_had_timeline_response = True
                if saved_response or existed:
                    saved.add(rid)
                elif not path.exists():
                    round_rejected += 1
                    rejected_responses += 1
                    saved.add(rid)

        posts = build_timeline_posts(
            load_tweets_from_runs(runs, extract_fn=extract_timeline_tweet), handle)
        write_checkpoint(post_dir, posts)
        write_progress_timeline(
            handle, post_dir, posts, max_posts=max_posts, since=since)

        cut, reason = find_stop_cut(
            posts, max_posts=max_posts, since=since,
            overlap_ids=overlap_ids, overlap_k=overlap_k)
        if cut is not None:
            stop_reason = reason
            append_capture_debug(post_dir, {
                "phase": "posts",
                "round": round_index,
                "request_ids": len(request_ids),
                "new_request_ids": len(new_ids),
                "valid_new_responses": round_valid,
                "rejected_responses": round_rejected,
                "post_count": len(posts),
                "stop_reason": stop_reason,
            })
            break

        size = len(posts)
        current_scroll_marker = _scroll_marker(browser)
        scroll_moved = (
            current_scroll_marker is not None
            and last_scroll_marker is not None
            and current_scroll_marker != last_scroll_marker
        )
        near_bottom = _near_bottom(current_scroll_marker)
        no_timeline_growth = size == last_size
        bottomed_without_growth = no_timeline_growth and near_bottom
        idle_without_growth = (
            no_timeline_growth and not round_had_timeline_response and not scroll_moved
        )
        if bottomed_without_growth or idle_without_growth:
            stable += 1
            # A literally-maxed-out scroll position with zero growth is a far
            # stronger "we're done" signal than generic idle rounds — there is
            # nothing left to discover by scrolling again, so it needs far
            # fewer confirmations than the generic stable_rounds budget.
            effective_stable_rounds = (
                bottom_stable_rounds if bottomed_without_growth else stable_rounds
            )
            if stable >= effective_stable_rounds:
                stop_reason = "no_new_posts"
                append_capture_debug(post_dir, {
                    "phase": "posts",
                    "round": round_index,
                    "request_ids": len(request_ids),
                    "new_request_ids": len(new_ids),
                    "valid_new_responses": round_valid,
                    "rejected_responses": round_rejected,
                    "post_count": size,
                    "scroll_marker": current_scroll_marker,
                    "scroll_moved": scroll_moved,
                    "near_bottom": near_bottom,
                    "stable": stable,
                    "stop_reason": stop_reason,
                })
                break
        else:
            stable = 0
        last_size = size
        last_scroll_marker = current_scroll_marker
        append_capture_debug(post_dir, {
            "phase": "posts",
            "round": round_index,
            "request_ids": len(request_ids),
            "new_request_ids": len(new_ids),
            "valid_new_responses": round_valid,
            "rejected_responses": round_rejected,
            "post_count": size,
            "scroll_marker": current_scroll_marker,
            "scroll_moved": scroll_moved,
            "near_bottom": near_bottom,
            "stable": stable,
            "stop_reason": stop_reason if stable >= stable_rounds else None,
        })

        browser.scroll(random.randint(*PROFILE_SCROLL_RANGE))
        sleep()

    if stop_reason == "max_posts":
        return {
            "status": "complete",
            "responses": len(saved),
            "new_responses": new_responses,
            "rejected_responses": rejected_responses,
            "stop_reason": stop_reason,
        }

    block = _capture_articles(browser, post_dir, runs, saved, sleep, max_scrolls)
    if block:
        return {"status": "needs_ops", "reason": block, "responses": len(saved)}

    return {
        "status": "complete",
        "responses": len(saved),
        "new_responses": new_responses,
        "rejected_responses": rejected_responses,
        "stop_reason": stop_reason,
    }
