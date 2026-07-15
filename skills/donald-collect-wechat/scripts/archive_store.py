#!/usr/bin/env python3
"""Store a WeChat account as a small root index and per-article artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
WECHAT_TZ = timezone(timedelta(hours=8))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _clean_inline(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class _InlineMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag in {"br", "p", "div", "section", "li"}:
            self.parts.append("\n")
        if tag == "a":
            href = values.get("href") or values.get("data-link") or ""
            if href:
                self.parts.append("[")
            self.links.append(href)
        if tag == "img":
            url = values.get("data-src") or values.get("src") or ""
            if url:
                self.parts.append(f"\n\n![{values.get('alt', '')}]({url})\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.links:
            href = self.links.pop()
            if href:
                self.parts.append(f"]({href})")
        if tag in {"p", "div", "section", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def rich_text_to_markdown(value: Any) -> str:
    source = str(value or "")
    parser = _InlineMarkdownParser()
    parser.feed(source)
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def content_type(item: dict[str, Any]) -> str:
    return "image_text" if item.get("item_show_type") == 10 else "article"


def actual_title(item: dict[str, Any]) -> str:
    if content_type(item) == "image_text" and item.get("is_user_title") != 1:
        return ""
    return _clean_inline(item.get("title"))


def article_id(item: dict[str, Any]) -> str:
    identity = str(item.get("url") or f"{item.get('title')}|{item.get('publish_time')}")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def display_title(item: dict[str, Any]) -> str:
    title = actual_title(item)
    if title:
        return title
    preview = _clean_inline(rich_text_to_markdown(item.get("content") or item.get("title")))
    return f"{preview[:72].rstrip()}…" if len(preview) > 72 else preview or "未命名图文"


def article_relative_dir(item: dict[str, Any]) -> Path:
    published_at = str(item.get("published_at") or "")
    match = re.match(r"^(\d{4})-(\d{2})", published_at)
    year, month = match.groups() if match else ("unknown", "unknown")
    return Path("articles") / year / month / article_id(item)


def article_record_path(account_root: Path, item: dict[str, Any]) -> Path:
    return account_root / article_relative_dir(item) / "article.json"


def markdown_text(record: dict[str, Any], content_markdown: str) -> str:
    title = _clean_inline(record.get("title")) or "未命名文章"
    account = _clean_inline(record.get("account"))
    published_at = _clean_inline(record.get("published_at"))
    url = str(record.get("url") or "").strip()
    is_image_text = record.get("content_type") == "image_text"
    byline = " · ".join(value for value in (("图文" if is_image_text else ""), account, published_at) if value)
    if url:
        byline = f"{byline} · [微信原文]({url})" if byline else f"[微信原文]({url})"
    lines = [f"# {title}", ""] if not is_image_text or record.get("title") else []
    if byline:
        lines.extend([f"> {byline}", ""])
    lines.extend([content_markdown.strip(), ""])
    return "\n".join(lines)


def write_markdown(path: Path, record: dict[str, Any], content_markdown: str) -> None:
    _write_text(path, markdown_text(record, content_markdown))


def _relative_source(account_root: Path, source: Any) -> str:
    if not source:
        return ""
    path = Path(str(source))
    try:
        return path.resolve().relative_to(account_root.resolve()).as_posix()
    except ValueError:
        return path.name


def build_article_record(account_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    path = article_record_path(account_root, item)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": article_id(item),
        "account": _clean_inline(item.get("source_account")),
        "content_type": content_type(item),
        "title": actual_title(item),
        "display_title": display_title(item),
        "url": str(item.get("url") or "").strip(),
        "author": _clean_inline(item.get("author")),
        "published_at": str(item.get("published_at") or ""),
        "publish_time": item.get("publish_time"),
        "digest": _clean_inline(item.get("digest")),
        "type": {
            "item_show_type": item.get("item_show_type"),
            "is_user_title": item.get("is_user_title"),
            "media_duration": _clean_inline(item.get("media_duration")),
        },
        "cover": {
            "remote_url": str(item.get("cover") or "").strip(),
            "local_path": ((existing.get("cover") or {}).get("local_path") or ""),
        },
        "body": existing.get("body") or {"status": "pending"},
        "assets": existing.get("assets") or {"images": []},
        "provenance": {
            "network_file": _relative_source(account_root, item.get("source_network_file")),
        },
    }
    if item.get("item_show_type") == 10:
        content = rich_text_to_markdown(item.get("content") or item.get("title"))
        markdown_path = path.with_name("article.md")
        write_markdown(markdown_path, record, content)
        record["body"] = {
            "status": "metadata_only",
            "source": "wechat_backend_metadata_text",
            "content_chars": len(_clean_inline(content)),
            "markdown": "article.md",
        }
    return record


def write_article_record(path: Path, record: dict[str, Any]) -> None:
    _write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def index_entry(account_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    record_path = article_record_path(account_root, item)
    return {
        "id": article_id(item),
        "published_at": str(item.get("published_at") or ""),
        "content_type": content_type(item),
        "title": actual_title(item),
        "display_title": display_title(item),
        "digest": _clean_inline(item.get("digest")),
        "url": str(item.get("url") or "").strip(),
        "item_show_type": item.get("item_show_type"),
        "record": record_path.relative_to(account_root).as_posix(),
    }


def _month_key(item: dict[str, Any]) -> str:
    published_at = str(item.get("published_at") or "")
    return published_at[:7] if re.match(r"^\d{4}-\d{2}", published_at) else "unknown"


def write_archive(
    account_root: Path,
    articles: list[dict[str, Any]],
    *,
    account: str,
    coverage_status: str,
    warnings: list[str],
    parsed_network_files: int,
    source_meta: dict[str, Any],
) -> Path:
    account_root.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in articles:
        record_path = article_record_path(account_root, item)
        write_article_record(record_path, build_article_record(account_root, item))
        groups.setdefault(_month_key(item), []).append(index_entry(account_root, item))

    indexes_dir = account_root / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    expected_shards: set[Path] = set()
    shards: list[dict[str, Any]] = []
    for month in sorted(groups, reverse=True):
        entries = groups[month]
        shard_path = indexes_dir / f"{month}.jsonl"
        expected_shards.add(shard_path)
        _write_text(
            shard_path,
            "".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries),
        )
        dates = [entry["published_at"] for entry in entries if entry.get("published_at")]
        shards.append(
            {
                "month": month,
                "path": shard_path.relative_to(account_root).as_posix(),
                "count": len(entries),
                "newest_at": max(dates) if dates else "",
                "oldest_at": min(dates) if dates else "",
            }
        )
    for stale in indexes_dir.glob("*.jsonl"):
        if stale not in expected_shards:
            stale.unlink()

    index_path = account_root / "index.json"
    index = {
        "schema_version": SCHEMA_VERSION,
        "account": account,
        "updated_at": datetime.now(WECHAT_TZ).isoformat(timespec="seconds"),
        "count": len(articles),
        "coverage_status": coverage_status,
        "warnings": warnings,
        "source": {
            "provider": "wechat_official_accounts",
            "parsed_network_files": parsed_network_files,
            "reported_counts": {
                key: source_meta.get(key)
                for key in ("total_count", "publish_count", "masssend_count", "featured_count")
                if source_meta.get(key) is not None
            },
        },
        "shards": shards,
    }
    _write_text(index_path, json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    legacy = account_root / "all-articles.json"
    if legacy.is_file():
        legacy.unlink()
    return index_path


def resolve_index(value: Path) -> Path:
    path = value.expanduser().resolve()
    if path.is_dir():
        path = path / "index.json"
    return path


def load_index_entries(value: Path) -> tuple[Path, list[dict[str, Any]]]:
    index_path = resolve_index(value)
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(data.get("items") or data.get("articles"), list):
        items = data.get("items") or data.get("articles") or []
        return index_path.parent, [item for item in items if isinstance(item, dict)]
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"Unsupported WeChat archive schema in {index_path}")
    items: list[dict[str, Any]] = []
    for shard in data.get("shards") or []:
        shard_path = index_path.parent / str(shard.get("path") or "")
        for line in shard_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if isinstance(item, dict):
                    items.append(item)
    return index_path.parent, items


def load_article_record(account_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    record = str(item.get("record") or "")
    if not record:
        return item
    loaded = json.loads((account_root / record).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Article record is not an object: {record}")
    loaded["record"] = record
    loaded["item_show_type"] = (loaded.get("type") or {}).get("item_show_type")
    loaded["source_account"] = loaded.get("account") or ""
    return loaded


def iter_records(account_root: Path, items: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for item in items:
        yield load_article_record(account_root, item)
