#!/usr/bin/env python3
"""Parse captured X TweetDetail GraphQL responses into thread.json + post.md.

Only reads responses produced by genuine in-browser navigation (saved under
data/<handle>/<status_id>/runs/). Never replays the GraphQL API.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from output_paths import resolve_tool_output_root


def orig_image_url(url: str) -> str:
    """Return the original-quality image URL for a pbs.twimg.com media URL."""
    if not url:
        return ""
    base = url.split("?", 1)[0]
    tail = base.rsplit("/", 1)[-1]
    if "." in tail:
        root, ext = base.rsplit(".", 1)
    else:
        root, ext = base, "jpg"
    return f"{root}?format={ext}&name=orig"


def best_variant(variants: list[dict[str, Any]]) -> tuple[str, str]:
    """Pick the highest-bitrate MP4 variant, else an m3u8, else ('', 'unknown')."""
    mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
    if mp4s:
        best = max(mp4s, key=lambda v: v.get("bitrate") or 0)
        return best["url"], "mp4"
    for v in variants:
        if v.get("content_type") == "application/x-mpegURL" and v.get("url"):
            return v["url"], "m3u8"
    return "", "unknown"


def _unwrap(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        inner = result.get("tweet")
        return inner if isinstance(inner, dict) else None
    return result


def iter_tweets(node: Any) -> list[dict[str, Any]]:
    """Recursively collect every tweet result object (has rest_id + legacy)."""
    found: list[dict[str, Any]] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            tr = n.get("tweet_results")
            if isinstance(tr, dict):
                t = _unwrap(tr.get("result"))
                if isinstance(t, dict) and t.get("rest_id") and isinstance(t.get("legacy"), dict):
                    found.append(t)
            for value in n.values():
                walk(value)
        elif isinstance(n, list):
            for value in n:
                walk(value)

    walk(node)
    return found


def _user(t: dict[str, Any]) -> dict[str, str]:
    res = (((t.get("core") or {}).get("user_results") or {}).get("result")) or {}
    ulegacy = res.get("legacy") or {}
    ucore = res.get("core") or {}
    return {
        "screen_name": ulegacy.get("screen_name") or ucore.get("screen_name") or "",
        "name": ulegacy.get("name") or ucore.get("name") or "",
        "id": res.get("rest_id") or "",
    }


def _full_text(t: dict[str, Any], legacy: dict[str, Any]) -> str:
    note = (((t.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result")) or {}
    if note.get("text"):
        return note["text"]
    return legacy.get("full_text") or ""


def _views(t: dict[str, Any]) -> int | None:
    value = (t.get("views") or {}).get("count")
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _quoted_result(t: dict[str, Any]) -> dict[str, Any] | None:
    result = (t.get("quoted_status_result") or {}).get("result")
    return _unwrap(result)


def _quoted_url(legacy: dict[str, Any]) -> str:
    permalink = legacy.get("quoted_status_permalink") or {}
    return str(permalink.get("expanded") or permalink.get("url") or "")


def _iso(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return ""


def _media(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    ext = (legacy.get("extended_entities") or {}).get("media")
    base = ext if ext else ((legacy.get("entities") or {}).get("media") or [])
    out: list[dict[str, Any]] = []
    photo_i = vid_i = 0
    for m in base:
        mtype = m.get("type")
        if mtype == "photo":
            photo_i += 1
            out.append({
                "type": "photo", "index": photo_i,
                "url": orig_image_url(m.get("media_url_https", "")),
                "local": None, "status": "pending",
            })
        elif mtype in ("video", "animated_gif"):
            vid_i += 1
            variants = ((m.get("video_info") or {}).get("variants")) or []
            url, kind = best_variant(variants)
            out.append({
                "type": "video" if mtype == "video" else "gif",
                "index": vid_i, "variants": variants,
                "best_url": url, "kind": kind,
                "local": None, "status": "pending",
            })
    return out


def extract_article(t: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an X Article's content_state into ordered text/photo blocks.

    Returns None if `t` is not an Article tweet. Schema verified against a
    real captured UserArticlesTweets response (content_state.blocks and
    media_entities/entityMap are lists, not dicts).
    """
    art = (((t.get("article") or {}).get("article_results")) or {}).get("result")
    if not art:
        return None

    media_by_id = {m.get("media_id"): m for m in (art.get("media_entities") or [])}
    entity_by_key = {
        str(e.get("key")): (e.get("value") or {})
        for e in (art.get("content_state", {}).get("entityMap") or [])
    }

    blocks: list[dict[str, Any]] = []
    img_index = 0
    for b in art.get("content_state", {}).get("blocks") or []:
        if b.get("type") == "atomic":
            ranges = b.get("entityRanges") or []
            if not ranges:
                continue
            entity = entity_by_key.get(str(ranges[0].get("key")))
            if not entity or entity.get("type") != "MEDIA":
                continue
            media_items = (entity.get("data") or {}).get("mediaItems") or []
            if not media_items:
                continue
            media = media_by_id.get(media_items[0].get("mediaId"))
            url = ((media or {}).get("media_info") or {}).get("original_img_url", "")
            if not url:
                continue
            img_index += 1
            blocks.append({
                "type": "photo", "index": img_index,
                "url": orig_image_url(url), "local": None, "status": "pending",
            })
        else:
            text = b.get("text", "")
            if text.strip():
                blocks.append({"type": "text", "text": text})

    return {"title": art.get("title", ""), "blocks": blocks}


def extract_tweet(t: dict[str, Any]) -> dict[str, Any]:
    legacy = t.get("legacy") or {}
    user = _user(t)
    item = {
        "status_id": t.get("rest_id") or legacy.get("id_str") or "",
        "handle": user["screen_name"],
        "author_name": user["name"],
        "author_id": user["id"],
        "created_at": _iso(legacy.get("created_at")),
        "full_text": _full_text(t, legacy),
        "in_reply_to_status_id": legacy.get("in_reply_to_status_id_str"),
        "metrics": {
            "reply": legacy.get("reply_count"),
            "retweet": legacy.get("retweet_count"),
            "like": legacy.get("favorite_count"),
            "quote": legacy.get("quote_count"),
            "bookmark": legacy.get("bookmark_count"),
            "views": _views(t),
        },
        "media": _media(legacy),
    }
    article = extract_article(t)
    if article is not None:
        item["type"] = "article"
        item["source_section"] = "articles"
        item["title"] = article["title"]
        item["blocks"] = article["blocks"]
        item["media"] = [b for b in article["blocks"] if b["type"] == "photo"]
    else:
        item["type"] = "tweet"
        item["source_section"] = "posts"
    quoted = _quoted_result(t)
    quoted_id = legacy.get("quoted_status_id_str")
    if quoted_id or quoted:
        item["quoted_status_id"] = str(quoted_id or (quoted or {}).get("rest_id") or "")
        item["quoted_status_url"] = _quoted_url(legacy)
        if quoted and isinstance(quoted.get("legacy"), dict):
            quoted_item = extract_tweet(quoted)
            item["quoted_author_handle"] = quoted_item.get("handle", "")
            item["quoted_author_id"] = quoted_item.get("author_id", "")
            item["quoted_text"] = quoted_item.get("full_text", "")
            item["quoted_tweet"] = quoted_item
    return item


def build_self_thread(tweets: list[dict[str, Any]], target_id: str) -> list[dict[str, Any]]:
    """Return the author's connected self-thread containing target, in order."""
    by_id = {t["status_id"]: t for t in tweets}
    target = by_id.get(target_id)
    if not target:
        return []
    author = target.get("handle")
    same = {sid: t for sid, t in by_id.items() if t.get("handle") == author}

    chain = [target_id]
    cur = target
    while True:
        parent = cur.get("in_reply_to_status_id")
        if parent and parent in same and parent not in chain:
            chain.insert(0, parent)
            cur = same[parent]
        else:
            break

    changed = True
    while changed:
        changed = False
        last = chain[-1]
        for sid, t in same.items():
            if sid not in chain and t.get("in_reply_to_status_id") == last:
                chain.append(sid)
                changed = True
                break

    result = []
    for sid in chain:
        item = dict(same[sid])
        item["is_target"] = sid == target_id
        result.append(item)
    return result


def _maybe_body(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("responseBody", "body"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    pass
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("responseBody"), str):
            try:
                return json.loads(data["responseBody"])
            except json.JSONDecodeError:
                pass
    return payload


def _merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for field, value in other.items():
        if field == "media":
            if not into.get("media") and value:
                into["media"] = value
        elif value not in (None, "", []) and into.get(field) in (None, "", []):
            into[field] = value


def load_tweets_from_runs(
    run_dir: Path, extract_fn=extract_tweet
) -> list[dict[str, Any]]:
    tweets: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(run_dir).rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        root = _maybe_body(payload)
        for raw in iter_tweets(root):
            item = extract_fn(raw)
            sid = item["status_id"]
            if not sid:
                continue
            if sid not in tweets:
                tweets[sid] = item
            else:
                _merge(tweets[sid], item)
    return list(tweets.values())


def parse_target_url(url: str) -> tuple[str, str]:
    """Return (handle, status_id) from an x.com/twitter.com status URL."""
    parts = url.split("?", 1)[0].rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "status":
        return parts[-3], parts[-1]
    raise ValueError(f"Not a status URL: {url}")


def render_post_md(target: dict[str, str], posts: list[dict[str, Any]]) -> str:
    lines = [f"# @{target.get('handle', '')} — {target.get('status_id', '')}",
             "", f"{target.get('url', '')}", ""]
    for p in posts:
        marker = " (target)" if p.get("is_target") else ""
        lines.append(f"## {p.get('status_id', '')}{marker} · {p.get('created_at', '')}")
        lines.append("")
        if p.get("type") == "article":
            lines.append(f"**{p.get('title', '')}**")
            lines.append("")
            for b in p.get("blocks", []):
                if b.get("type") == "text":
                    lines.append(b.get("text", ""))
                else:
                    ref = b.get("local") or b.get("url") or ""
                    lines.append(f"- [photo #{b.get('index', '')}] {ref}")
            lines.append("")
        else:
            lines.append(p.get("full_text", ""))
            lines.append("")
            m = p.get("metrics") or {}
            lines.append(
                f"> ❤ {m.get('like')} · 🔁 {m.get('retweet')} · 💬 {m.get('reply')} · "
                f"🔖 {m.get('bookmark')} · 👁 {m.get('views')}"
            )
            for media in p.get("media", []):
                ref = media.get("local") or media.get("url") or media.get("best_url") or ""
                lines.append(f"- [{media.get('type', '')} #{media.get('index', '')}] {ref}")
            lines.append("")
    return "\n".join(lines)


def write_thread(url: str, data_root: Path) -> dict[str, Any]:
    """Parse runs under data_root/<handle>/<status_id> into thread.json + post.md.

    Returns the thread dict. Raises ValueError/FileNotFoundError on bad input,
    LookupError if the target tweet is not present in the captured runs.
    """
    handle, status_id = parse_target_url(url)
    post_dir = data_root.expanduser().resolve() / handle / status_id
    run_dir = post_dir / "runs"
    if not run_dir.exists():
        raise FileNotFoundError(f"No runs directory: {run_dir}")

    posts = build_self_thread(load_tweets_from_runs(run_dir), status_id)
    if not posts:
        raise LookupError(
            f"Target {status_id} not found in captured runs. "
            "Scroll the thread in the browser and re-save TweetDetail responses."
        )

    target = {"handle": handle, "status_id": status_id, "url": url}
    thread = {
        "target": target,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(posts),
        "posts": posts,
    }
    (post_dir / "thread.json").write_text(
        json.dumps(thread, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (post_dir / "post.md").write_text(render_post_md(target, posts), encoding="utf-8")
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True, help="Target X post URL.")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Exact X collection root. Defaults to the Donald Skills Documents data directory.",
    )
    args = parser.parse_args()
    output_root = resolve_tool_output_root("x", args.output_root)
    try:
        thread = write_thread(args.url, output_root)
    except (ValueError, FileNotFoundError, LookupError) as exc:
        raise SystemExit(str(exc))
    print(f"Wrote {thread['count']} posts -> "
          f"{output_root}/{thread['target']['handle']}/"
          f"{thread['target']['status_id']}/thread.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
