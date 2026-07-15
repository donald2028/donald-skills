#!/usr/bin/env python3
"""Download media listed in thread.json from public X CDN.

Images: curl. Video MP4 variants: curl. m3u8: ffmpeg -c copy. yt-dlp is used
only as a fallback fed an already-resolved CDN URL — never to hit the X API.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REFERER = "https://x.com/"


def ext_from_url(url: str) -> str:
    base = url.split("?", 1)[0]
    tail = base.rsplit("/", 1)[-1]
    if "." in tail:
        return tail.rsplit(".", 1)[-1]
    if "format=" in url:
        return url.split("format=", 1)[1].split("&", 1)[0]
    return "jpg"


def media_filename(status_id: str, media: dict[str, Any]) -> str:
    if media["type"] == "photo":
        return f"{status_id}-img-{media['index']:02d}.{ext_from_url(media.get('url', ''))}"
    return f"{status_id}-video-{media['index']:02d}.mp4"


def build_curl_cmd(url: str, out: Path) -> list[str]:
    return ["curl", "-sL", "--fail", "-A", USER_AGENT, "-e", REFERER, "-o", str(out), url]


def build_ffmpeg_cmd(m3u8: str, out: Path) -> list[str]:
    headers = f"Referer: {REFERER}\r\nUser-Agent: {USER_AGENT}\r\n"
    return ["ffmpeg", "-y", "-headers", headers, "-i", m3u8, "-c", "copy", str(out)]


def build_ytdlp_cmd(url: str, out: Path) -> list[str]:
    return ["yt-dlp", "--no-playlist", "--sleep-requests", "2",
            "--user-agent", USER_AGENT, "-o", str(out), url]


def _default_runner(cmd: list[str], out: Path) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and out.exists() and out.stat().st_size > 0


def _default_sleep() -> None:
    time.sleep(random.uniform(0.8, 2.5))


def _download_one(media: dict[str, Any], out: Path, runner) -> bool:
    if media["type"] == "photo":
        return runner(build_curl_cmd(media.get("url", ""), out), out)
    url = media.get("best_url", "")
    if not url:
        return False
    kind = media.get("kind")
    if kind == "mp4":
        if runner(build_curl_cmd(url, out), out):
            return True
        return runner(build_ytdlp_cmd(url, out), out)  # fallback, still a CDN URL
    if kind == "m3u8":
        if runner(build_ffmpeg_cmd(url, out), out):
            return True
        return runner(build_ytdlp_cmd(url, out), out)
    return False


def _download_media_list(
    media_dir: Path, status_id: str, media_list: list[dict[str, Any]], runner, sleep,
) -> None:
    for media in media_list:
        if media.get("status") == "downloaded":
            continue
        name = media_filename(status_id, media)
        out = media_dir / name
        if out.exists() and out.stat().st_size > 0:
            media["local"] = f"media/{name}"
            media["status"] = "downloaded"
            continue
        ok = _download_one(media, out, runner)
        if ok:
            media["local"] = f"media/{name}"
            media["status"] = "downloaded"
        else:
            media["local"] = None
            media["status"] = "blocked_or_unavailable"
        sleep()


def download_thread(post_dir: Path, runner=_default_runner, sleep=_default_sleep) -> dict[str, Any]:
    thread_path = post_dir / "thread.json"
    thread = json.loads(thread_path.read_text(encoding="utf-8"))
    media_dir = post_dir / "media"
    media_dir.mkdir(exist_ok=True)

    for post in thread.get("posts", []):
        _download_media_list(media_dir, post["status_id"], post.get("media", []), runner, sleep)

    thread_path.write_text(json.dumps(thread, ensure_ascii=False, indent=2), encoding="utf-8")
    _refresh_post_md(post_dir, thread)
    return thread


def _sync_article_blocks(post: dict[str, Any]) -> None:
    """Copy downloaded media status back into `blocks`.

    extract_user_timeline.extract_timeline_tweet builds an article's `media`
    list from the same dict objects as its `blocks` list, but that identity
    doesn't survive a JSON round-trip: the timeline is written once with each
    photo's content duplicated textually, so re-reading it via json.loads (as
    this function does) produces two distinct-but-equal dict objects. Sync them
    back explicitly by `index` instead of relying on identity.
    """
    blocks = post.get("blocks")
    if not blocks:
        return
    media_by_index = {m.get("index"): m for m in post.get("media", [])}
    for b in blocks:
        if b.get("type") == "photo" and b.get("index") in media_by_index:
            m = media_by_index[b["index"]]
            b["local"] = m.get("local")
            b["status"] = m.get("status")


def _load_timeline_for_media(post_dir: Path) -> dict[str, Any]:
    """Load the timeline as a dict, preferring JSONL + meta over legacy json."""
    jsonl = post_dir / "timeline.jsonl"
    if jsonl.exists():
        meta_path = post_dir / "timeline.meta.json"
        timeline = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        timeline["posts"] = [
            json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        return timeline
    return json.loads((post_dir / "timeline.json").read_text(encoding="utf-8"))


def download_timeline(post_dir: Path, runner=_default_runner, sleep=_default_sleep) -> dict[str, Any]:
    """Download media for every post in the timeline: plain tweets, every
    sub-tweet inside a thread, and every photo block inside an article.

    Reads JSONL (legacy json fallback) and writes the result back via
    extract_user_timeline.write_timeline_jsonl (atomic, streaming) after each
    post, not just once at the end. A backlog can be hundreds of paced
    downloads deep; without per-post flushing, a crash or interruption
    partway through silently discards every status already earned in this
    run, even though the bytes are already sitting on disk.
    """
    import extract_user_timeline

    timeline = _load_timeline_for_media(post_dir)
    media_dir = post_dir / "media"
    media_dir.mkdir(exist_ok=True)

    posts = timeline.get("posts", [])
    for i, post in enumerate(posts, start=1):
        if post.get("is_thread"):
            for tw in post.get("thread", []):
                _download_media_list(media_dir, tw["status_id"], tw.get("media", []), runner, sleep)
        else:
            _download_media_list(media_dir, post["status_id"], post.get("media", []), runner, sleep)
            _sync_article_blocks(post)
        extract_user_timeline.write_timeline_jsonl(post_dir, timeline)
        print(f"media progress: post {i}/{len(posts)} ({post['status_id']})", flush=True)

    return timeline


def _refresh_post_md(post_dir: Path, thread: dict[str, Any]) -> None:
    """Re-render post.md so media links point at the downloaded local files."""
    try:
        import extract_thread
    except ImportError:
        return
    target = thread.get("target") or {}
    md = extract_thread.render_post_md(target, thread.get("posts", []))
    (post_dir / "post.md").write_text(md, encoding="utf-8")


def _iter_media(posts: list[dict[str, Any]]):
    for post in posts:
        if post.get("is_thread"):
            for tw in post.get("thread", []):
                yield from tw.get("media", [])
        else:
            yield from post.get("media", [])


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--post-dir", type=Path,
                        help="data/<handle>/<status_id> directory containing thread.json.")
    group.add_argument("--timeline-dir", type=Path,
                        help="data/<handle>/_user directory containing timeline.jsonl. "
                             "Backfills media for the whole account timeline — no browser "
                             "involved, safe to run while a different account is being "
                             "captured over CDP.")
    args = parser.parse_args(argv)

    if args.timeline_dir is not None:
        post_dir = args.timeline_dir.expanduser().resolve()
        timeline = download_timeline(post_dir)
    else:
        post_dir = args.post_dir.expanduser().resolve()
        timeline = download_thread(post_dir)

    media_items = list(_iter_media(timeline.get("posts", [])))
    total = len(media_items)
    done = sum(1 for m in media_items if m.get("status") == "downloaded")
    print(f"Downloaded {done}/{total} media -> {post_dir}")
    return 0


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
