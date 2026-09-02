#!/usr/bin/env python3
"""Collect a WeChat account from backend home without taking macOS focus."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_runtime import BrowserSession
from output_paths import resolve_tool_output_root
from profile_config import (
    ProfileConfigError,
    close_background_page,
    connect_cdp_target,
    create_background_page,
    frontmost_process_id,
    list_cdp_targets,
    restore_frontmost_process_if_browser_active,
)


WECHAT_HOME = "https://mp.weixin.qq.com/"
ACCOUNT_SEARCH_PLACEHOLDER_MARKER = "输入文章来源的账号名称"
EDITOR_TIP_ACKNOWLEDGEMENTS = ("我知道了", "知道了")
ARTICLE_PICKER_LABELS = ("选择账号文章",)


class ExternalAccountPickerUnavailable(RuntimeError):
    """The authenticated account does not expose the external article picker."""


def _safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe.strip("-") or "account"


def _runtime_value(connection: Any, expression: str) -> Any:
    result = connection.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(f"CDP evaluation failed: {result['exceptionDetails']}")
    return (result.get("result") or {}).get("value")


def _wait_value(connection: Any, expression: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = _runtime_value(connection, expression)
        if last_value:
            return last_value
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for browser state; last value: {last_value!r}")


def _element_rect_expression(predicate: str, selectors: str) -> str:
    return f"""
(() => {{
  const visible = el => {{
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  }};
  const candidates = [...document.querySelectorAll({json.dumps(selectors)})]
    .filter(visible)
    .filter(el => {{ {predicate} }});
  candidates.sort((a, b) => {{
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return ar.width * ar.height - br.width * br.height;
  }});
  const raw = candidates[0];
  if (!raw) return null;
  const el = raw.closest("a,button,li,[role=button],[role=link],[role=radio]") || raw;
  el.scrollIntoView({{ block: "center", inline: "center" }});
  const r = el.getBoundingClientRect();
  return {{ x: r.x + r.width / 2, y: r.y + r.height / 2, text: (el.innerText || el.textContent || "").trim() }};
}})()
""".strip()


def _click_expression(connection: Any, expression: str) -> dict[str, Any]:
    rect = _runtime_value(connection, expression)
    if not isinstance(rect, dict):
        raise RuntimeError("Visible browser element was not found")
    x = float(rect["x"])
    y = float(rect["y"])
    connection.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    connection.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    connection.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    return rect


def _click_exact_text(connection: Any, text: str, selectors: str = "a,button,li,[role=button],div,span") -> dict[str, Any]:
    expected = json.dumps(text)
    predicate = f'const text = (el.innerText || el.textContent || "").trim(); return text === {expected};'
    return _click_expression(connection, _element_rect_expression(predicate, selectors))


def _click_exact_text_if_visible(
    connection: Any,
    text: str,
    selectors: str = "a,button,li,[role=button],div,span",
) -> bool:
    """Click an exact visible label only when it is rendered."""
    expected = json.dumps(text)
    predicate = f'const text = (el.innerText || el.textContent || "").trim(); return text === {expected};'
    rect = _runtime_value(connection, _element_rect_expression(predicate, selectors))
    if not isinstance(rect, dict):
        return False
    x = float(rect["x"])
    y = float(rect["y"])
    connection.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    connection.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    connection.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    return True


def _prepare_interactive_target(connection: Any) -> None:
    """Make this runner-owned tab interactive without activating Chrome's window.

    Recent WeChat editor builds ignore trusted CDP pointer events until their
    page target is the selected Chrome tab. ``Page.bringToFront`` switches
    only that owned tab; the caller still restores the foreground app, so it
    does not steal operating-system focus.
    """
    connection.call("Page.bringToFront", {})
    connection.call("Emulation.setFocusEmulationEnabled", {"enabled": True})


def _dismiss_editor_tips(connection: Any) -> None:
    for label in EDITOR_TIP_ACKNOWLEDGEMENTS:
        if _click_exact_text_if_visible(connection, label):
            time.sleep(0.3)
            return


def _current_account_name(connection: Any) -> str:
    """Return the nickname shown for the authenticated backend account."""
    value = _runtime_value(
        connection,
        """
(() => {
  const element = document.querySelector(".mp_account_box, .weui-desktop-account__info");
  return (element?.innerText || element?.textContent || "").replace(/\\s+/g, " ").trim();
})()
""".strip(),
    )
    return str(value or "").strip()


def _set_search_query(connection: Any, account: str) -> None:
    marker_json = json.dumps(ACCOUNT_SEARCH_PLACEHOLDER_MARKER)
    placeholder_predicate = (
        f'return (el.placeholder || "").includes({marker_json});'
    )
    _click_expression(
        connection,
        _element_rect_expression(placeholder_predicate, "input"),
    )
    account_json = json.dumps(account)
    changed = _runtime_value(
        connection,
        f"""
(() => {{
  const el = [...document.querySelectorAll("input")].find(input =>
    (input.placeholder || "").includes({marker_json})
  );
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(el, {account_json});
  el.dispatchEvent(new Event("input", {{ bubbles: true }}));
  el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  return true;
}})()
""".strip(),
    )
    if not changed:
        raise RuntimeError("WeChat account search input was not found")
    base_event = {
        "key": "Enter",
        "code": "Enter",
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 13,
    }
    connection.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base_event})
    connection.call(
        "Input.dispatchKeyEvent",
        {"type": "char", "text": "\r", "unmodifiedText": "\r", **base_event},
    )
    connection.call("Input.dispatchKeyEvent", {"type": "keyUp", **base_event})


def _account_result_expression(account: str, wechat_id: str) -> str:
    account_json = json.dumps(account)
    wechat_id_json = json.dumps(wechat_id)
    predicate = f"""
const label = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
const nickname = (el.querySelector(".inner_link_account_nickname")?.textContent || "").trim();
const account = {account_json};
const wechatId = {wechat_id_json};
if (wechatId) return nickname === account && label.includes(wechatId);
return nickname === account || label === account || label.startsWith(account + " 微信号：");
""".strip()
    return _element_rect_expression(predicate, "li,[role=radio],[role=listitem]")


def _wait_for_editor_target(port: int, before_ids: set[str], home_target_id: str, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        targets = list_cdp_targets(port)
        for target in targets:
            url = str(target.get("url") or "")
            target_id = str(target.get("id") or "")
            if "cgi-bin/appmsg" in url and "action=edit" in url and (target_id not in before_ids or target_id == home_target_id):
                return target_id
        time.sleep(0.2)
    return ""


def _response_begin(url: str) -> str:
    return parse_qs(urlsplit(url).query).get("begin", [""])[0]


def _wait_appmsgpublish(connection: Any, seen_begins: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection.poll_events(timeout=0.1)
        events = connection.events[:]
        connection.events.clear()
        for event in events:
            if event.get("method") != "Network.responseReceived":
                continue
            params = event.get("params") or {}
            response = params.get("response") or {}
            url = str(response.get("url") or "")
            begin = _response_begin(url)
            if "appmsgpublish" not in url or not begin or begin in seen_begins:
                continue
            request_id = str(params.get("requestId") or "")
            for _ in range(30):
                try:
                    body = connection.call("Network.getResponseBody", {"requestId": request_id}).get("body") or ""
                except ProfileConfigError:
                    connection.poll_events(timeout=0.05)
                    time.sleep(0.05)
                    continue
                return {
                    "requestId": request_id,
                    "url": url,
                    "status": response.get("status"),
                    "responseBody": body,
                    "begin": begin,
                }
        time.sleep(0.05)
    raise RuntimeError("No new appmsgpublish response was produced by the visible UI action")


def _save_response(run_dir: Path, account: str, page_number: int, response: dict[str, Any]) -> Path:
    request_id = str(response.get("requestId") or f"page-{page_number}")
    path = run_dir / f"wechat-network-request-{_safe_path_part(account)}-{_safe_path_part(request_id)}.json"
    path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = run_dir / f"wechat-network-requests-{_safe_path_part(account)}-page-{page_number:03d}.json"
    summary.write_text(json.dumps({"requests": [response]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _close_if_open(port: int, target_id: str) -> None:
    if not target_id:
        return
    if any(str(target.get("id") or "") == target_id for target in list_cdp_targets(port)):
        previous_frontmost_pid = frontmost_process_id()
        close_background_page(port, target_id)
        if previous_frontmost_pid:
            restore_frontmost_process_if_browser_active(previous_frontmost_pid, port)


def _open_editor_target(
    connection: Any,
    *,
    port: int,
    before_ids: set[str],
    home_target_id: str,
    home_url: str,
) -> str:
    previous_frontmost_pid = frontmost_process_id()
    try:
        _click_exact_text(connection, "文章")
    finally:
        if previous_frontmost_pid:
            restore_frontmost_process_if_browser_active(previous_frontmost_pid, port)
    target_id = _wait_for_editor_target(port, before_ids, home_target_id)
    if target_id:
        return target_id
    token = parse_qs(urlsplit(home_url).query).get("token", [""])[0]
    if not token:
        raise RuntimeError("Could not open the article editor or derive its token")
    return create_background_page(
        port,
        "https://mp.weixin.qq.com/cgi-bin/appmsg"
        f"?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&createType=0&token={token}&lang=zh_CN",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session", default="", help="Optional agent-browser session prefix for runner startup.")
    parser.add_argument("--cdp", type=int, help="Explicit CDP port override; otherwise use the configured Profile port.")
    parser.add_argument("--account", required=True, help="Exact WeChat Official Account nickname.")
    parser.add_argument("--wechat-id", default="", help="Optional exact WeChat ID for search-result disambiguation.")
    parser.add_argument("--pages", type=int, default=20, help="Article picker pages to capture, including page one.")
    parser.add_argument("--output-root", type=Path, help="Exact WeChat collection root override.")
    args = parser.parse_args()
    if args.pages < 1:
        raise SystemExit("--pages must be >= 1")

    browser_session: BrowserSession | None = None
    try:
        browser_session = BrowserSession(
            scope="donald-collect-wechat",
            session=args.session or "donald-wechat-collect",
            url=WECHAT_HOME,
            port=args.cdp,
        ).open()
        args.cdp = browser_session.port
    except (OSError, ProfileConfigError, subprocess.SubprocessError, TimeoutError) as error:
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

    output_root = resolve_tool_output_root("wechat", args.output_root)
    account_root = output_root / _safe_path_part(args.account)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = account_root / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    home_target_id = browser_session.target_id
    editor_target_id = ""
    home_connection: Any | None = None
    editor_connection: Any | None = None
    responses: list[dict[str, Any]] = []
    cleanup = "closed"
    business_error: Exception | None = None
    browser_cleanup: dict[str, Any] = {"status": "not_closed"}
    try:
        home_connection = connect_cdp_target(args.cdp, home_target_id)
        _prepare_interactive_target(home_connection)
        state = _wait_value(
            home_connection,
            '(() => document.readyState === "complete" && location.href !== "about:blank" '
            '? {url: location.href, text: (document.body?.innerText || "").slice(0, 500)} : null)()',
            timeout=12,
        )
        if "cgi-bin/home" not in str((state or {}).get("url") or ""):
            activation = browser_session.preserve_for_human()
            cleanup = "kept_open_for_human"
            print(json.dumps({"status": "needs_ops", "run_dir": str(run_dir), "browser_activation": activation}, ensure_ascii=False, indent=2))
            return 2

        before_ids = {str(target.get("id") or "") for target in list_cdp_targets(args.cdp)}
        editor_target_id = _open_editor_target(
            home_connection,
            port=args.cdp,
            before_ids=before_ids,
            home_target_id=home_target_id,
            home_url=str(state.get("url") or ""),
        )
        editor_connection = connect_cdp_target(args.cdp, editor_target_id)
        _prepare_interactive_target(editor_connection)
        editor_connection.call("Network.enable")
        _wait_value(editor_connection, 'location.href.includes("cgi-bin/appmsg") && document.body?.innerText.includes("超链接")', timeout=12)
        _dismiss_editor_tips(editor_connection)
        editor_connection.events.clear()
        _click_exact_text(editor_connection, "超链接")
        seen_begins: set[str] = set()
        _wait_value(
            editor_connection,
            'document.body?.innerText.includes("编辑超链接")',
            timeout=8,
        )
        opened_article_picker = False
        for label in ARTICLE_PICKER_LABELS:
            if _click_exact_text_if_visible(editor_connection, label):
                opened_article_picker = True
                break
        if opened_article_picker:
            marker_json = json.dumps(ACCOUNT_SEARCH_PLACEHOLDER_MARKER)
            _wait_value(
                editor_connection,
                'document.body?.innerText.includes("选择其他账号") || '
                f'[...document.querySelectorAll("input")].some(el => (el.placeholder || "").includes({marker_json}))',
                timeout=8,
            )
        has_external_picker = bool(
            _runtime_value(editor_connection, 'document.body?.innerText.includes("选择其他账号")')
        )
        if has_external_picker:
            _click_exact_text(editor_connection, "选择其他账号", "button,[role=button],div,span")
            marker_json = json.dumps(ACCOUNT_SEARCH_PLACEHOLDER_MARKER)
            _wait_value(
                editor_connection,
                f'[...document.querySelectorAll("input")].some(el => (el.placeholder || "").includes({marker_json}))',
                timeout=8,
            )
            _set_search_query(editor_connection, args.account)
            _wait_value(editor_connection, _account_result_expression(args.account, args.wechat_id), timeout=8)
            editor_connection.events.clear()
            _click_expression(editor_connection, _account_result_expression(args.account, args.wechat_id))
        else:
            current_account = _current_account_name(home_connection)
            if not current_account:
                raise ExternalAccountPickerUnavailable(
                    "The current account's article-link dialog does not expose an external-account picker"
                )
            if args.account != current_account:
                raise ExternalAccountPickerUnavailable(
                    "The current account's article-link dialog does not expose an external-account picker; "
                    f"it can only collect the authenticated account ({current_account!r}), not {args.account!r}"
                )

        first = _wait_appmsgpublish(editor_connection, seen_begins)
        seen_begins.add(first["begin"])
        responses.append(first)
        _save_response(run_dir, args.account, 1, first)

        next_expression = _element_rect_expression(
            'const text = (el.innerText || el.textContent || "").trim(); return text === "下一页" && !el.className.includes("disabled");',
            "a,button,[role=button]",
        )
        for page_number in range(2, args.pages + 1):
            if not _runtime_value(editor_connection, next_expression):
                break
            editor_connection.events.clear()
            _click_expression(editor_connection, next_expression)
            response = _wait_appmsgpublish(editor_connection, seen_begins)
            seen_begins.add(response["begin"])
            responses.append(response)
            _save_response(run_dir, args.account, page_number, response)

    except (OSError, ProfileConfigError, RuntimeError, TimeoutError) as error:
        business_error = error
    finally:
        if editor_connection is not None:
            editor_connection.close()
        if home_connection is not None:
            home_connection.close()
        if cleanup == "closed":
            _close_if_open(args.cdp, editor_target_id)
        browser_cleanup = browser_session.close()
        cleanup = browser_cleanup["status"]

    if business_error is not None:
        external_picker_unavailable = isinstance(business_error, ExternalAccountPickerUnavailable)
        print(
            json.dumps(
                {
                    "status": "needs_ops" if external_picker_unavailable else "error",
                    "reason": "external_account_picker_unavailable" if external_picker_unavailable else "wechat_ui_flow_failed",
                    "retryable": not external_picker_unavailable,
                    "error_type": type(business_error).__name__,
                    "hint": str(business_error),
                    "run_dir": str(run_dir),
                    "browser_cleanup": browser_cleanup,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    parse = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("parse_appmsgpublish.py")), str(account_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if parse.returncode != 0:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "wechat_parse_failed",
                    "retryable": True,
                    "hint": (parse.stderr or parse.stdout).strip() or "The WeChat archive parser exited without diagnostics",
                    "run_dir": str(run_dir),
                    "browser_cleanup": browser_cleanup,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "complete",
                "account": args.account,
                "run_dir": str(run_dir),
                "pages_saved": len(responses),
                "page_begins": [response["begin"] for response in responses],
                "request_count": len(responses),
                "browser_tab_cleanup": cleanup,
                "parse_output": parse.stdout.strip(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
