#!/usr/bin/env python3
"""Parse captured X UserTweets/UserArticlesTweets GraphQL responses into
timeline.jsonl + timeline.meta.json + timeline.md for a whole account.

Reuses extract_thread.py's tweet-parsing primitives unchanged: real packet
captures confirmed UserTweets/UserArticlesTweets responses use the exact
same per-tweet object shape as TweetDetail (tweet_results.result with
rest_id + legacy), just wrapped in different timeline entry types.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from extract_thread import extract_article, extract_tweet, load_tweets_from_runs


def normalize_handle(value: str) -> str:
    """Accept a bare handle, an @handle, or a full profile URL."""
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        value = value.split("://", 1)[1]
        parts = value.split("/", 1)
        value = parts[1] if len(parts) > 1 else ""
    value = value.lstrip("@/")
    return value.split("/")[0].split("?")[0]


# extract_tweet (extract_thread.py) already tags type/source_section and
# parses Article content_state via extract_article — both single-post and
# account-level capture share that logic so neither path silently drops
# Article content. Kept as an alias: capture_user_timeline.py and existing
# tests refer to it by this name.
extract_timeline_tweet = extract_tweet


def group_threads(tweets: list[dict[str, Any]], handle: str) -> list[list[dict[str, Any]]]:
    """Chain same-author replies into ordered thread groups.

    A tweet by someone else (e.g. a reply from another account) never joins
    a chain — it simply isn't in `mine`, so chains stop at the boundary.
    """
    mine = {t["status_id"]: t for t in tweets if t.get("handle") == handle}
    reply_to = {sid: t.get("in_reply_to_status_id") for sid, t in mine.items()}
    children: dict[str, str] = {}
    for sid, parent in reply_to.items():
        if parent and parent in mine:
            children[parent] = sid

    roots = [sid for sid in mine if reply_to.get(sid) not in mine]
    chains: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for root in sorted(roots, key=lambda sid: mine[sid].get("created_at") or ""):
        if root in seen:
            continue
        chain_ids = [root]
        seen.add(root)
        cur = root
        while cur in children and children[cur] not in seen:
            cur = children[cur]
            chain_ids.append(cur)
            seen.add(cur)
        chains.append([mine[sid] for sid in chain_ids])
    return chains


def chain_seems_incomplete(chain: list[dict[str, Any]]) -> bool:
    """True if the chain's last tweet reports replies we have no local
    record of — a signal (not proof) that X may have truncated this
    self-thread in the timeline response instead of inlining it fully.
    """
    reply_count = (chain[-1].get("metrics") or {}).get("reply") or 0
    return reply_count > 0


def build_timeline_posts(tweets: list[dict[str, Any]], handle: str) -> list[dict[str, Any]]:
    """Flatten tweets+articles into newest-first timeline posts.

    A thread counts as a single post (its own `created_at` is the root
    tweet's), matching how --max-posts counts entries.
    """
    articles = [t for t in tweets if t.get("type") == "article" and t.get("handle") == handle]
    plain = [t for t in tweets if t.get("type") != "article"]

    posts: list[dict[str, Any]] = []
    for chain in group_threads(plain, handle):
        if len(chain) == 1:
            item = dict(chain[0])
            item["is_thread"] = False
            posts.append(item)
        else:
            posts.append({
                "status_id": chain[0]["status_id"], "type": "thread", "is_thread": True,
                "source_section": "posts",
                "created_at": chain[0].get("created_at"), "thread": chain,
                "possibly_incomplete": chain_seems_incomplete(chain),
            })
    for a in articles:
        item = dict(a)
        item["is_thread"] = False
        posts.append(item)

    posts.sort(key=status_sort_key, reverse=True)
    return posts


def status_sort_key(post: dict[str, Any]) -> int:
    """Newest-first ordering by snowflake status_id.

    X status_ids are monotonic with creation time, so they give a stable total
    order independent of created_at string formatting/timezone and of the order
    in which posts were collected (append order in timeline.jsonl is irrelevant).
    """
    try:
        return int(post.get("status_id") or 0)
    except (TypeError, ValueError):
        return 0


def count_sections(posts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"posts": 0, "articles": 0}
    for post in posts:
        section = post.get("source_section")
        if section in counts:
            counts[section] += 1
    return counts


def _post_is_known(post: dict[str, Any], known: set[str]) -> bool:
    if post.get("status_id") in known:
        return True
    if post.get("is_thread"):
        return any(t.get("status_id") in known for t in post.get("thread", []))
    return False


def _first_overlap_run(posts: list[dict[str, Any]], known: set[str], k: int) -> int | None:
    """Index of the first post in the earliest run of `k` consecutive already-known
    posts. Requiring K-in-a-row makes the head reconnect robust to a deleted anchor,
    a pinned tweet, or timeline reordering (a lone known post won't trigger a stop).
    """
    run = 0
    start: int | None = None
    for i, post in enumerate(posts):
        if _post_is_known(post, known):
            if run == 0:
                start = i
            run += 1
            if run >= k:
                return start
        else:
            run = 0
            start = None
    return None


def find_stop_cut(
    posts: list[dict[str, Any]], *,
    max_posts: int | None = None, since: str | None = None,
    overlap_ids: set[str] | None = None, overlap_k: int = 1,
) -> tuple[int | None, str | None]:
    """Return (cut_index, reason) for the first stop condition that applies.

    `posts` must already be sorted newest-first. When multiple conditions
    would trigger, the smallest cut index wins (most restrictive applies).
    `overlap_ids` + `overlap_k` stop the scroll once it reconnects with prior
    coverage (K consecutive already-known posts).
    """
    candidates: list[tuple[int, str]] = []
    if overlap_ids:
        idx = _first_overlap_run(posts, overlap_ids, overlap_k)
        if idx is not None:
            candidates.append((idx, "overlap_with_manifest"))
    if since is not None:
        idx = next((i for i, p in enumerate(posts) if (p.get("created_at") or "") < since), None)
        if idx is not None:
            candidates.append((idx, "since_date"))
    if max_posts is not None:
        post_count = 0
        for idx, post in enumerate(posts):
            if post.get("source_section") != "posts":
                continue
            post_count += 1
            if post_count >= max_posts:
                candidates.append((idx + 1, "max_posts"))
                break
    if not candidates:
        return None, None
    return min(candidates, key=lambda c: c[0])


def limit_output_posts(
    posts: list[dict[str, Any]], *,
    max_posts: int | None = None, since: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Apply user-facing output limits with Posts as the primary section.

    Browser capture may collect both Posts and Articles, but `--max-posts N`
    means "give me N normal profile posts first." Articles are supplementary
    and only fill remaining slots when the captured Posts section has fewer
    than N entries.
    """
    limited = posts
    stop_reason = "exhausted"

    if since is not None:
        idx = next((i for i, p in enumerate(limited) if (p.get("created_at") or "") < since), None)
        if idx is not None:
            limited = limited[:idx]
            stop_reason = "since_date"

    if max_posts is not None and len(limited) >= max_posts:
        primary = [p for p in limited if p.get("source_section") == "posts"]
        if len(primary) >= max_posts:
            return primary[:max_posts], "max_posts"

        supplements_needed = max_posts - len(primary)
        supplements = [
            p for p in limited if p.get("source_section") != "posts"
        ][:supplements_needed]
        selected = {id(p) for p in [*primary, *supplements]}
        return [p for p in limited if id(p) in selected], "max_posts"

    return limited, stop_reason


def render_timeline_md(handle: str, posts: list[dict[str, Any]]) -> str:
    lines = [f"# @{handle} — timeline", ""]
    for p in posts:
        if p.get("type") == "article":
            lines.append(f"## [article] {p.get('status_id', '')} · {p.get('created_at', '')}")
            lines.append("")
            lines.append(f"**{p.get('title', '')}**")
            lines.append("")
            for b in p.get("blocks", []):
                if b.get("type") == "text":
                    lines.append(b.get("text", ""))
                else:
                    ref = b.get("local") or b.get("url") or ""
                    lines.append(f"- [photo #{b.get('index', '')}] {ref}")
            lines.append("")
        elif p.get("is_thread"):
            chain_ids = " -> ".join(t["status_id"] for t in p.get("thread", []))
            flag = " ⚠ possibly incomplete" if p.get("possibly_incomplete") else ""
            lines.append(f"## [thread] {chain_ids} · {p.get('created_at', '')}{flag}")
            lines.append("")
            for tw in p.get("thread", []):
                lines.append(tw.get("full_text", ""))
                for m in tw.get("media", []):
                    ref = m.get("local") or m.get("url") or m.get("best_url") or ""
                    lines.append(f"- [{m.get('type', '')} #{m.get('index', '')}] {ref}")
                lines.append("")
        else:
            lines.append(f"## {p.get('status_id', '')} · {p.get('created_at', '')}")
            lines.append("")
            lines.append(p.get("full_text", ""))
            for m in p.get("media", []):
                ref = m.get("local") or m.get("url") or m.get("best_url") or ""
                lines.append(f"- [{m.get('type', '')} #{m.get('index', '')}] {ref}")
            lines.append("")
    return "\n".join(lines)


def write_timeline(
    handle: str, data_root: Path, *,
    max_posts: int | None = None, since: str | None = None,
) -> dict[str, Any]:
    """Parse runs under data_root/<handle>/_user into timeline.jsonl + meta + md.

    No overlap cut here on purpose: that's only meaningful for deciding when
    capture_user_timeline.capture() can stop scrolling early. The saved timeline
    must always be the full accumulated history, bounded only by explicit
    `max_posts`/`since`.

    Raises FileNotFoundError if the runs directory doesn't exist yet.
    """
    handle = normalize_handle(handle)
    post_dir = data_root.expanduser().resolve() / handle / "_user"
    run_dir = post_dir / "runs"
    if not run_dir.exists():
        raise FileNotFoundError(f"No runs directory: {run_dir}")

    tweets = load_tweets_from_runs(run_dir, extract_fn=extract_timeline_tweet)
    posts = build_timeline_posts(tweets, handle)
    posts, stop_reason = limit_output_posts(posts, max_posts=max_posts, since=since)

    timeline = {
        "handle": handle,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "count": len(posts),
        "section_counts": count_sections(posts),
        "posts": posts,
    }
    write_timeline_jsonl(post_dir, timeline)
    return timeline


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a crash never leaves a half-written
    file. The timeline can reach tens of MB; a torn write would corrupt the whole
    account history, so every produced file goes through here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
    os.replace(tmp, path)


def write_timeline_jsonl(post_dir: Path, timeline: dict[str, Any]) -> dict[str, Any]:
    """Persist a timeline as streaming JSONL + a small meta header + the md view.

    `timeline.jsonl` holds one post per line (a thread is one line with its
    `thread` array); `timeline.meta.json` carries the header and the coverage
    endpoints. Replaces the legacy monolithic `timeline.json`.
    """
    post_dir = Path(post_dir)
    posts = timeline.get("posts", [])
    handle = timeline.get("handle", "")
    _atomic_write_lines(
        post_dir / "timeline.jsonl",
        (json.dumps(post, ensure_ascii=False) for post in posts),
    )
    ids = [status_sort_key(post) for post in posts if post.get("status_id")]
    meta = {
        "schema_version": 1,
        "handle": handle,
        "captured_at": timeline.get("captured_at", ""),
        "stop_reason": timeline.get("stop_reason"),
        "count": timeline.get("count", len(posts)),
        "section_counts": timeline.get("section_counts", {}),
        "newest_status_id": str(max(ids)) if ids else None,
        "oldest_status_id": str(min(ids)) if ids else None,
    }
    atomic_write_text(
        post_dir / "timeline.meta.json",
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(post_dir / "timeline.md", render_timeline_md(handle, posts))
    return timeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("handle", help="X account handle.")
    parser.add_argument("output_root", type=Path, help="X output root containing <handle>/_user/runs.")
    parser.add_argument("--max-posts", type=int, default=None)
    parser.add_argument("--since", default=None)
    args = parser.parse_args()
    timeline = write_timeline(args.handle, args.output_root, max_posts=args.max_posts, since=args.since)
    print(f"Wrote {timeline['count']} posts -> {args.output_root.expanduser().resolve()}/{normalize_handle(args.handle)}/_user/timeline.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
