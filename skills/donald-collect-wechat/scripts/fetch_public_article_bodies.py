#!/usr/bin/env python3
"""Fetch public WeChat article bodies for account research evidence pools.

Default mode uses a real Chrome tab over CDP for WeChat article access. It
does not replay WeChat backend APIs or hidden endpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from archive_store import (
    article_record_path,
    build_article_record,
    load_article_record,
    load_index_entries,
    rich_text_to_markdown,
    write_article_record,
    write_markdown,
)
from profile_config import (
    ProfileConfigError,
    activate_browser,
    call_background_page,
    close_background_page,
    configured_browser,
    create_background_page,
    frontmost_process_id,
    hide_browser_without_focus,
    preflight_browser,
    restore_frontmost_process_if_browser_active,
    wait_for_background_page_url,
)


WECHAT_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _clean_text(value: str) -> str:
    text = unescape(value or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


class WeChatArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._captures: list[tuple[str, str]] = []
        self._buffers: dict[str, list[str]] = {
            "title": [],
            "account_name": [],
            "content_text": [],
        }
        self.audio_markers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = _attr(attrs, "id")
        class_name = _attr(attrs, "class")
        capture = ""
        if element_id == "activity-name":
            capture = "title"
        elif element_id == "js_name":
            capture = "account_name"
        elif element_id == "js_content" or "rich_media_content" in class_name.split():
            capture = "content_text"

        if tag in {"mp-common-mpaudio", "mpvoice", "qqmusic"}:
            marker = (
                _attr(attrs, "voice_encode_fileid")
                or _attr(attrs, "mid")
                or _attr(attrs, "musicid")
                or _attr(attrs, "name")
            )
            if marker and marker not in self.audio_markers:
                self.audio_markers.append(marker)

        self._stack.append(tag)
        if capture:
            self._captures.append((tag, capture))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "br", "li"}:
            for _, capture in self._captures:
                self._buffers[capture].append("\n")
        if self._captures and self._captures[-1][0] == tag:
            self._captures.pop()
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if not self._captures:
            return
        text = data.strip()
        if not text:
            return
        capture = self._captures[-1][1]
        self._buffers[capture].append(text)

    def as_article(self, url: str) -> dict[str, Any]:
        content_text = _clean_text("\n".join(self._buffers["content_text"]))
        title = _clean_text(" ".join(self._buffers["title"]))
        account_name = _clean_text(" ".join(self._buffers["account_name"]))
        status = "downloaded" if len(content_text) >= 20 else "blocked_or_unavailable"
        blocked_reason = "" if status == "downloaded" else "No #js_content text found"
        return {
            "url": url,
            "title": title,
            "account_name": account_name,
            "content_text": content_text,
            "content_chars": len(content_text),
            "audio_markers": self.audio_markers,
            "fetch_status": status,
            "blocked_reason": blocked_reason,
            "fetched_at": datetime.now(WECHAT_TZ).isoformat(timespec="seconds"),
        }


def extract_article(html: str, url: str) -> dict[str, Any]:
    parser = WeChatArticleParser()
    parser.feed(html)
    article = parser.as_article(url)
    if not article["title"]:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        if title_match:
            article["title"] = _clean_text(re.sub(r"<[^>]+>", "", title_match.group(1)))
    if article["fetch_status"] == "blocked_or_unavailable":
        lowered = html.lower()
        if "环境异常" in html or "weixin110" in lowered or "verify" in lowered:
            article["blocked_reason"] = "WeChat verification or abnormal-environment page"
    return article


def _article_from_cdp_payload(payload: dict[str, Any], requested_url: str) -> dict[str, Any]:
    content_text = _clean_text(str(payload.get("content_text") or ""))
    status = "downloaded" if len(content_text) >= 20 else "blocked_or_unavailable"
    title = _clean_text(str(payload.get("title") or ""))
    blocked_reason = ""
    if status != "downloaded":
        page_text = " ".join(
            str(payload.get(key) or "")
            for key in ("title", "account_name", "document_title", "body_head")
        )
        if "环境异常" in page_text or "验证" in page_text:
            blocked_reason = "WeChat verification or abnormal-environment page"
        elif any(marker in page_text.lower() for marker in ("log in", "sign in", "扫码登录", "微信登录")):
            blocked_reason = "WeChat login page"
        else:
            blocked_reason = "No #js_content text found in Chrome CDP page"
    return {
        "url": payload.get("url") or requested_url,
        "title": title,
        "account_name": _clean_text(str(payload.get("account_name") or "")),
        "content_text": content_text,
        "content_markdown": str(payload.get("content_markdown") or "").strip(),
        "content_chars": len(content_text),
        "images": payload.get("images") or [],
        "audio_markers": payload.get("audio_markers") or [],
        "interaction_metrics": payload.get("interaction_metrics")
        or {
            "status": "unavailable",
            "read_count": None,
            "like_count": None,
            "share_count": None,
            "note": "No public interaction counts were exposed by the article page.",
        },
        "fetch_status": status,
        "blocked_reason": blocked_reason,
        "fetch_method": "chrome_cdp",
        "fetched_at": datetime.now(WECHAT_TZ).isoformat(timespec="seconds"),
    }


def _evaluate_background_page(port: int, target_id: str, script: str, timeout: int) -> dict[str, Any]:
    result = call_background_page(
        port,
        target_id,
        "Runtime.evaluate",
        {
            "expression": script,
            "returnByValue": True,
            "awaitPromise": True,
        },
        timeout=timeout,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"WeChat article evaluation failed: {result['exceptionDetails']}")
    value = (result.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError(f"WeChat article evaluation returned {type(value).__name__}, expected object")
    return value


def fetch_article_with_cdp(url: str, cdp: str, session: str | None = None, timeout: int = 45) -> dict[str, Any]:
    del session  # Kept for CLI compatibility; focus-safe page control uses the configured CDP browser directly.
    port = int(cdp)
    script = r"""
(() => {
  const contentEl = document.querySelector("#js_content,.rich_media_content");
  const contentText = contentEl ? contentEl.innerText : "";
  const images = [];
  const imageToken = el => {
    const raw = el.getAttribute("data-src") || el.getAttribute("data-original") || el.currentSrc || el.src || "";
    if (!raw) return "";
    let url = raw;
    try { url = new URL(raw, location.href).href; } catch (_) {}
    if (!/^https?:/i.test(url)) return url;
    const existing = images.find(image => image.url === url);
    if (existing) return existing.token;
    const token = `__DONALD_IMAGE_${String(images.length + 1).padStart(3, "0")}__`;
    images.push({
      token,
      url,
      alt: (el.getAttribute("alt") || "").replace(/[\[\]\r\n]/g, " ").trim(),
      width: el.naturalWidth || Number(el.getAttribute("width")) || null,
      height: el.naturalHeight || Number(el.getAttribute("height")) || null
    });
    return token;
  };
  const children = node => [...node.childNodes].map(render).join("");
  const render = node => {
    if (node.nodeType === Node.TEXT_NODE) return (node.nodeValue || "").replace(/\s+/g, " ");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const tag = node.tagName.toLowerCase();
    if (["script", "style", "noscript"].includes(tag)) return "";
    if (tag === "br") return "\n";
    if (tag === "img") {
      const token = imageToken(node);
      return token ? `\n\n![${(node.getAttribute("alt") || "").replace(/[\[\]\r\n]/g, " ").trim()}](${token})\n\n` : "";
    }
    const inner = children(node);
    if (["p", "div", "section", "figure", "figcaption", "article"].includes(tag)) return `\n\n${inner}\n\n`;
    if (/^h[1-6]$/.test(tag)) return `\n\n${"#".repeat(Math.min(6, Number(tag[1]) + 1))} ${inner.trim()}\n\n`;
    if (["strong", "b"].includes(tag)) return inner.trim() ? `**${inner.trim()}**` : "";
    if (["em", "i"].includes(tag)) return inner.trim() ? `*${inner.trim()}*` : "";
    if (tag === "code") return inner.trim() ? `\`${inner.trim()}\`` : "";
    if (tag === "pre") return inner.trim() ? `\n\n\`\`\`\n${inner.trim()}\n\`\`\`\n\n` : "";
    if (tag === "blockquote") return `\n\n${inner.trim().split("\n").map(line => `> ${line}`).join("\n")}\n\n`;
    if (tag === "li") return `\n- ${inner.trim()}`;
    if (["ul", "ol"].includes(tag)) return `\n${inner.trim()}\n`;
    if (tag === "a") {
      const href = node.href || "";
      const label = inner.trim();
      return href && label ? `[${label.replace(/[\[\]]/g, "")}](${href})` : label;
    }
    return inner;
  };
  const contentMarkdown = contentEl
    ? render(contentEl)
        .replace(/[ \t]+\n/g, "\n")
        .replace(/\n[ \t]+/g, "\n")
        .replace(/\n{3,}/g, "\n\n")
        .trim()
    : "";
  const audioMarkers = [...document.querySelectorAll("mp-common-mpaudio,mpvoice,qqmusic")]
    .map(el =>
      el.getAttribute("voice_encode_fileid") ||
      el.getAttribute("mid") ||
      el.getAttribute("musicid") ||
      el.getAttribute("name") ||
      ""
    )
    .filter(Boolean);
  const appmsgstat = window.appmsgstat && typeof window.appmsgstat === "object"
    ? window.appmsgstat
    : {};
  const firstOwnNumber = keys => {
    for (const key of keys) {
      if (!Object.prototype.hasOwnProperty.call(appmsgstat, key)) continue;
      const value = Number(appmsgstat[key]);
      if (Number.isFinite(value)) return value;
    }
    return null;
  };
  const firstText = selectors => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      const text = el ? (el.innerText || el.textContent || "").trim() : "";
      if (text) return text;
    }
    return "";
  };
  const readText = firstText(["#js_read_num3", "#js_read_num", "#readNum3"]);
  const likeText = firstText(["#js_like_num", "#js_zan_num", "#likeNum"]);
  const shareText = firstText(["#js_share_num", ".sns_share_num"]);
  const readCount = firstOwnNumber(["read_num_new", "read_num"]);
  const likeCount = firstOwnNumber(["like_num", "old_like_num"]);
  const shareCount = firstOwnNumber(["share_num"]);
  const hasMetrics = [readCount, likeCount, shareCount].some(value => value !== null) ||
    [readText, likeText, shareText].some(Boolean);
  return {
    url: location.href,
    title: document.querySelector("#activity-name,h1")?.innerText?.trim() || "",
    document_title: document.title || "",
    account_name: document.querySelector("#js_name")?.innerText?.trim() || "",
    content_text: contentText,
    content_markdown: contentMarkdown,
    content_chars: contentText.length,
    images,
    audio_markers: audioMarkers,
    interaction_metrics: {
      status: hasMetrics ? "available" : "unavailable",
      read_count: readCount,
      like_count: likeCount,
      share_count: shareCount,
      read_count_text: readText,
      like_count_text: likeText,
      share_count_text: shareText,
      note: hasMetrics ? "" : "No public interaction counts were exposed by the article page."
    },
    body_head: document.body?.innerText?.slice(0, 300) || "",
    ready_state: document.readyState
  };
})()
""".strip()
    article: dict[str, Any] | None = None
    previous_frontmost_pid = frontmost_process_id()
    browser_config = configured_browser()
    target_id = create_background_page(port, url)
    try:
        hide_browser_without_focus(browser_config, port, previous_frontmost_pid)
        wait_for_background_page_url(port, target_id, url, timeout=min(timeout, 15))
        call_background_page(
            port,
            target_id,
            "Emulation.setFocusEmulationEnabled",
            {"enabled": True},
            timeout=timeout,
        )
        deadline = time.monotonic() + timeout
        ready_after = time.monotonic() + 3.0
        payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            payload = _evaluate_background_page(port, target_id, script, timeout)
            page_text = " ".join(
                str(payload.get(key) or "")
                for key in ("title", "account_name", "document_title", "body_head")
            )
            has_content = int(payload.get("content_chars") or 0) >= 20
            ready = payload.get("ready_state") == "complete"
            human_page = any(marker in page_text for marker in ("环境异常", "验证", "扫码登录", "微信登录"))
            if has_content or (ready and time.monotonic() >= ready_after) or (human_page and time.monotonic() >= ready_after):
                break
            time.sleep(0.25)
        article = _article_from_cdp_payload(payload, requested_url=url)
        article["cdp_target_id"] = target_id
    finally:
        keep_open_for_human = bool(article) and article.get("blocked_reason") in {
            "WeChat verification or abnormal-environment page",
            "WeChat login page",
        }
        if keep_open_for_human:
            article["browser_tab_cleanup"] = "kept_open_for_human"
        else:
            close_background_page(port, target_id)
            hide_browser_without_focus(browser_config, port, previous_frontmost_pid)
            if previous_frontmost_pid:
                restore_frontmost_process_if_browser_active(previous_frontmost_pid, port)
            if article is not None:
                article["browser_tab_cleanup"] = "closed"
    if article is None:
        raise RuntimeError(f"Could not inspect WeChat article target {target_id}")
    if article["blocked_reason"] in {
        "WeChat verification or abnormal-environment page",
        "WeChat login page",
    }:
        try:
            article["browser_activation"] = activate_browser(configured_browser(), int(cdp))
        except (OSError, ProfileConfigError, subprocess.SubprocessError) as error:
            article["browser_activation"] = {"status": "error", "error": str(error)}
    return article


def validate_article_identity(item: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    if article.get("fetch_status") != "downloaded":
        return article
    expected_title = _clean_text(str(item.get("title") or ""))
    observed_title = _clean_text(str(article.get("title") or ""))
    normalized_expected = re.sub(r"\W+", "", expected_title, flags=re.UNICODE).casefold()
    normalized_observed = re.sub(r"\W+", "", observed_title, flags=re.UNICODE).casefold()
    if not expected_title or not observed_title or normalized_expected == normalized_observed:
        return article
    result = dict(article)
    result.update(
        {
            "url": str(item.get("url") or article.get("url") or ""),
            "title": expected_title,
            "content_text": "",
            "content_chars": 0,
            "fetch_status": "blocked_or_unavailable",
            "blocked_reason": f"Article URL resolved to a different title: {observed_title}",
            "observed_title": observed_title,
            "observed_url": str(article.get("url") or ""),
        }
    )
    return result


def recover_metadata_text_post(item: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    if article.get("fetch_status") != "blocked_or_unavailable" or item.get("item_show_type") != 10:
        return article
    markdown = rich_text_to_markdown(item.get("content") or item.get("title"))
    text = _clean_text(markdown)
    if len(text) < 20:
        return article
    result = dict(article)
    result.update(
        {
            "title": str(item.get("title") or ""),
            "content_text": text,
            "content_markdown": markdown,
            "content_chars": len(text),
            "fetch_status": "metadata_only",
            "blocked_reason": "Public text post has no #js_content; content retained from WeChat backend metadata.",
            "fetch_method": "wechat_backend_metadata_text",
        }
    )
    return result


def _fetch_url_with_curl(url: str, timeout: int) -> str:
    completed = subprocess.run(
        [
            "curl",
            "-L",
            "--max-time",
            str(timeout),
            "-A",
            USER_AGENT,
            "-sS",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def fetch_url(url: str, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return _fetch_url_with_curl(url, timeout)


def _image_extension(content_type: str, url: str) -> str:
    by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
    }
    if content_type in by_type:
        return by_type[content_type]
    match = re.search(r"\.(jpe?g|png|gif|webp|avif|svg)(?:[?#]|$)", url, flags=re.I)
    return f".{match.group(1).lower().replace('jpeg', 'jpg')}" if match else ".img"


def localize_images(
    article_dir: Path,
    content_markdown: str,
    images: list[dict[str, Any]],
    *,
    referer: str,
    timeout: int,
) -> tuple[str, list[dict[str, Any]]]:
    localized = content_markdown
    records: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        token = str(image.get("token") or "")
        url = str(image.get("url") or "")
        record = {"remote_url": url, "alt": str(image.get("alt") or ""), "status": "failed", "local_path": ""}
        if not token or not url.startswith(("http://", "https://")):
            localized = localized.replace(token, url)
            records.append(record)
            continue
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                data = response.read(25 * 1024 * 1024 + 1)
            if not content_type.startswith("image/") or len(data) > 25 * 1024 * 1024:
                raise ValueError("response is not a supported image or exceeds 25 MB")
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
            filename = f"{index:03d}-{digest}{_image_extension(content_type, url)}"
            path = article_dir / "images" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            relative = path.relative_to(article_dir).as_posix()
            localized = localized.replace(token, relative)
            record.update({"status": "downloaded", "local_path": relative, "bytes": len(data)})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            localized = localized.replace(token, url)
            record["error"] = f"{error.__class__.__name__}: {error}"
        records.append(record)
    return localized, records


def prune_unreferenced_images(account_root: Path) -> int:
    referenced: set[Path] = set()
    for record_path in account_root.glob("articles/*/*/*/article.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for image in (record.get("assets") or {}).get("images") or []:
            local_path = str(image.get("local_path") or "") if isinstance(image, dict) else ""
            if local_path:
                referenced.add((record_path.parent / local_path).resolve())

    removed = 0
    managed_name = re.compile(r"^\d{3}-[0-9a-f]{10}\.(?:jpg|png|gif|webp|avif|svg|img)$")
    for path in account_root.glob("articles/*/*/*/images/*"):
        if path.is_file() and managed_name.match(path.name) and path.resolve() not in referenced:
            path.unlink()
            removed += 1
    return removed


def _ensure_record(account_root: Path, item: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    record = str(item.get("record") or "")
    if record:
        path = account_root / record
        return path, load_article_record(account_root, item)
    path = article_record_path(account_root, item)
    built = build_article_record(account_root, item)
    write_article_record(path, built)
    built["record"] = path.relative_to(account_root).as_posix()
    built["item_show_type"] = (built.get("type") or {}).get("item_show_type")
    built["source_account"] = built.get("account") or ""
    return path, built


def select_items(
    items: list[dict[str, Any]],
    title_terms: list[str],
    limit: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing: list[str] = []

    if title_terms:
        for term in title_terms:
            match = None
            for item in items:
                title = str(item.get("title") or item.get("display_title") or "")
                url = str(item.get("url") or "")
                if term in title and url and url not in seen:
                    match = item
                    break
            if match:
                selected.append(match)
                seen.add(str(match.get("url")))
            else:
                missing.append(term)
        return selected, missing

    for item in items:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        selected.append(item)
        seen.add(url)
        if limit and len(selected) >= limit:
            break
    return selected, missing


def _direct_url_item(value: str, account: str) -> dict[str, Any]:
    title = ""
    url = value
    if "|" in value:
        title, url = value.split("|", 1)
    return {
        "title": title.strip(),
        "url": url.strip(),
        "source_account": account,
        "published_at": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("archive", type=Path, help="Account directory or its index.json.")
    parser.add_argument("--title-contains", action="append", default=[], help="Select first article title containing text.")
    parser.add_argument("--url", action="append", default=[], help="Extra public URL, optionally '<title>|<url>'.")
    parser.add_argument("--account", default="", help="Account name for direct URLs.")
    parser.add_argument("--cdp", default="", help="Explicit CDP port override; otherwise use the configured Profile port.")
    parser.add_argument("--session", default="", help="Optional agent-browser session name.")
    parser.add_argument("--limit", type=int, default=12, help="Default recent article limit when no terms are supplied.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between public fetches.")
    parser.add_argument("--timeout", type=int, default=45, help="Browser/diagnostic timeout in seconds.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Accepted for compatibility; stable per-article files are always refreshed in place.",
    )
    parser.add_argument(
        "--http-diagnostic",
        action="store_true",
        help="Diagnostic only. Direct HTTP does not satisfy the browser-evidence workflow.",
    )
    args = parser.parse_args()
    if not args.http_diagnostic:
        try:
            browser_config = configured_browser()
            port = int(args.cdp or browser_config["chrome"]["default_cdp_port"])
            startup = preflight_browser(
                browser_config,
                port,
                args.session or "donald-wechat-bodies",
                "about:blank",
                60,
            )
            startup_target_id = str(startup.get("background_target_id") or "")
            if startup_target_id:
                close_background_page(port, startup_target_id)
            args.cdp = str(port)
        except (OSError, ProfileConfigError, subprocess.SubprocessError) as error:
            print(
                json.dumps(
                    {
                        "status": "needs_ops",
                        "reason": "browser_startup_failed",
                        "hint": str(error),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2

    account_root, items = load_index_entries(args.archive)
    output_dir = account_root / "articles"
    manifest_path = account_root / "body-fetch-manifest.json"
    selected, missing = select_items(items, args.title_contains, limit=args.limit)
    selected.extend(_direct_url_item(value, args.account) for value in args.url)

    manifest: dict[str, Any] = {
        "archive": str(account_root / "index.json"),
        "output_dir": str(output_dir),
        "selected_count": len(selected),
        "missing_title_terms": missing,
        "results": [],
    }

    for index, item in enumerate(selected, start=1):
        record_path, item = _ensure_record(account_root, item)
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        existing_body = item.get("body") or {}
        output = record_path.with_name("article.md")
        if item.get("item_show_type") == 10 and existing_body.get("status") == "metadata_only" and output.is_file():
            article = {
                "url": url,
                "title": item.get("title") or item.get("display_title") or "",
                "content_chars": existing_body.get("content_chars") or 0,
                "audio_markers": [],
                "images": [],
                "interaction_metrics": {
                    "status": "unavailable",
                    "read_count": None,
                    "like_count": None,
                    "share_count": None,
                    "note": "Public text post content came from WeChat backend metadata.",
                },
                "fetch_status": "metadata_only",
                "blocked_reason": "Public text post has no #js_content; content retained from WeChat backend metadata.",
                "fetch_method": "wechat_backend_metadata_text",
                "fetched_at": datetime.now(WECHAT_TZ).isoformat(timespec="seconds"),
                "browser_tab_cleanup": "not_opened",
            }
        else:
            try:
                if args.cdp:
                    article = fetch_article_with_cdp(
                        url,
                        cdp=str(args.cdp),
                        session=args.session or None,
                        timeout=args.timeout,
                    )
                    article = recover_metadata_text_post(item, article)
                    article = validate_article_identity(item, article)
                else:
                    html = fetch_url(url, timeout=args.timeout)
                    article = extract_article(html, url)
                    article["fetch_method"] = "http_diagnostic"
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
            ) as exc:
                article = {
                    "url": url,
                    "title": "",
                    "account_name": "",
                    "content_text": "",
                    "content_markdown": "",
                    "content_chars": 0,
                    "audio_markers": [],
                    "images": [],
                    "interaction_metrics": {
                        "status": "unavailable",
                        "read_count": None,
                        "like_count": None,
                        "share_count": None,
                        "note": "Article page inspection did not complete.",
                    },
                    "fetch_status": "blocked_or_unavailable",
                    "blocked_reason": f"{exc.__class__.__name__}: {exc}",
                    "fetch_method": "chrome_cdp" if args.cdp else "http_diagnostic",
                    "fetched_at": datetime.now(WECHAT_TZ).isoformat(timespec="seconds"),
                }

        image_records: list[dict[str, Any]] = []
        if article.get("fetch_status") == "downloaded":
            body_markdown = str(article.get("content_markdown") or article.get("content_text") or "")
            body_markdown, image_records = localize_images(
                record_path.parent,
                body_markdown,
                [image for image in article.get("images") or [] if isinstance(image, dict)],
                referer=url,
                timeout=args.timeout,
            )
            write_markdown(output, item, body_markdown)

        stored_record = dict(item)
        for transient in ("record", "item_show_type", "source_account"):
            stored_record.pop(transient, None)
        stored_record["body"] = {
            "status": article.get("fetch_status") or "blocked_or_unavailable",
            "content_chars": article.get("content_chars") or 0,
            "markdown": "article.md" if output.is_file() else "",
            "fetched_at": article.get("fetched_at") or "",
            "method": article.get("fetch_method") or "",
            "blocked_reason": article.get("blocked_reason") or "",
            "audio_markers": article.get("audio_markers") or [],
            "interaction_metrics": article.get("interaction_metrics") or {},
            "browser_tab_cleanup": article.get("browser_tab_cleanup") or "",
        }
        stored_record["assets"] = {"images": image_records}
        write_article_record(record_path, stored_record)
        manifest["results"].append(
            {
                "title": article.get("title") or item.get("title") or item.get("display_title") or "",
                "url": url,
                "output": str(output),
                "article_record": str(record_path),
                "fetch_status": article.get("fetch_status"),
                "content_chars": article.get("content_chars"),
                "audio_markers": article.get("audio_markers") or [],
                "interaction_metrics": article.get("interaction_metrics") or {},
                "blocked_reason": article.get("blocked_reason") or "",
                "fetch_method": article.get("fetch_method") or "",
                "browser_tab_cleanup": article.get("browser_tab_cleanup") or "",
                "browser_activation": article.get("browser_activation"),
                "observed_title": article.get("observed_title") or "",
                "observed_url": article.get("observed_url") or "",
                "images_found": len(article.get("images") or []),
                "images_downloaded": sum(image.get("status") == "downloaded" for image in image_records),
                "image_failures": sum(image.get("status") != "downloaded" for image in image_records),
            }
        )
        if article.get("browser_activation"):
            print("needs_ops: complete login or verification in the active Chrome window")
        print(f"{article.get('fetch_status')}: {output}")
        if index < len(selected) and args.delay > 0:
            time.sleep(args.delay)

    manifest["pruned_orphan_images"] = prune_unreferenced_images(account_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote manifest -> {manifest_path}")
    if missing:
        print(f"Missing title terms: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
