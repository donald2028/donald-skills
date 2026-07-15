#!/usr/bin/env python3
"""Parse WeChat backend dumps into a sharded account archive."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from archive_store import write_archive


WECHAT_TZ = timezone(timedelta(hours=8))


def _response_body(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("responseBody"),
        payload.get("body"),
        (payload.get("data") or {}).get("responseBody") if isinstance(payload.get("data"), dict) else None,
        (payload.get("response") or {}).get("body") if isinstance(payload.get("response"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _source_account_from_path(path: Path) -> str:
    prefix = "wechat-network-request-"
    stem = path.stem
    if not stem.startswith(prefix):
        return ""
    rest = stem[len(prefix) :]
    parts = rest.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else ""


def _wechat_timestamp_to_iso(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str) and value.strip().isdigit():
        timestamp = float(value.strip())
    else:
        return ""
    return datetime.fromtimestamp(timestamp, tz=WECHAT_TZ).isoformat(timespec="seconds")


def _parse_dump(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body_text = _response_body(payload)
    if not body_text:
        return {}, []

    body = json.loads(body_text)
    page = _jsonish(body.get("publish_page"))
    if not isinstance(page, dict):
        return {}, []

    source_account = _source_account_from_path(path)
    meta = {
        "source_account": source_account,
        "total_count": page.get("total_count"),
        "publish_count": page.get("publish_count"),
        "masssend_count": page.get("masssend_count"),
        "featured_count": page.get("featured_count"),
    }

    items: list[dict[str, Any]] = []
    for publish in page.get("publish_list") or []:
        if not isinstance(publish, dict):
            continue
        info = _jsonish(publish.get("publish_info"))
        if not isinstance(info, dict):
            continue
        for article in info.get("appmsgex") or []:
            if not isinstance(article, dict):
                continue
            title = str(article.get("title") or "").strip()
            if not title:
                continue
            publish_time = article.get("update_time") or article.get("create_time") or publish.get("publish_time")
            items.append(
                {
                    "source_account": source_account,
                    "title": title,
                    "url": str(article.get("link") or "").strip(),
                    "author": str(article.get("author_name") or "").strip(),
                    "publish_time": publish_time,
                    "published_at": _wechat_timestamp_to_iso(publish_time),
                    "digest": str(article.get("digest") or "").strip(),
                    "content": str(article.get("content") or "").strip(),
                    "item_show_type": article.get("item_show_type"),
                    "is_user_title": article.get("is_user_title"),
                    "media_duration": str(article.get("media_duration") or "").strip(),
                    "cover": str(article.get("cover") or "").strip(),
                    "source_network_file": str(path),
                }
            )
    return meta, items


def _merge_meta(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        if value is not None and merged.get(key) is None:
            merged[key] = value
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "input_dir",
        type=Path,
        help="One <account>/runs/<timestamp> capture or an <account> root whose runs should be merged.",
    )
    args = parser.parse_args()

    root = args.input_dir.expanduser().resolve()
    if root.parent.name == "runs":
        account_root = root.parent.parent
        evidence_root = root
    elif (root / "runs").is_dir():
        account_root = root
        evidence_root = root / "runs"
    else:
        raise SystemExit("input_dir must be <account>/runs/<timestamp> or an <account> root containing runs/")
    files = sorted(evidence_root.rglob("wechat-network-request-*.json"))
    if not files:
        raise SystemExit(f"No wechat-network-request-*.json files found under {root}")

    meta: dict[str, Any] = {}
    per_account: dict[str, dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for path in files:
        dump_meta, items = _parse_dump(path)
        meta = _merge_meta(meta, dump_meta)
        account = str(dump_meta.get("source_account") or "unknown")
        if account not in per_account:
            per_account[account] = dump_meta
        for item in items:
            key = item.get("url") or f"{item.get('title')}|{item.get('publish_time')}"
            key_text = str(key)
            if key_text not in by_key:
                by_key[key_text] = item
                continue
            existing = by_key[key_text]
            for field, value in item.items():
                if value and not existing.get(field):
                    existing[field] = value

    articles = sorted(
        by_key.values(),
        key=lambda item: int(item.get("publish_time") or 0),
        reverse=True,
    )
    coverage_status = "complete_or_unknown"
    warnings: list[str] = []
    for account, account_meta in per_account.items():
        publish_count = account_meta.get("publish_count")
        parsed_count = sum(1 for item in articles if (item.get("source_account") or "unknown") == account)
        account_meta["parsed_item_count"] = parsed_count
        if isinstance(publish_count, int) and parsed_count < publish_count:
            coverage_status = "partial_capture"
            warnings.append(
                f"{account}: parsed fewer article items than publish groups reported by WeChat; "
                "capture more backend pages or scroll/load more in the article picker, then rerun."
            )

    account = str(meta.get("source_account") or account_root.name)
    output = write_archive(
        account_root,
        articles,
        account=account,
        coverage_status=coverage_status,
        warnings=warnings,
        parsed_network_files=len(files),
        source_meta=meta,
    )
    print(f"Wrote {len(articles)} article records and sharded indexes -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
