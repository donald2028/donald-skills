#!/usr/bin/env python3
"""Drive a real Chrome over CDP to capture an X post's self-thread responses.

Engineering goal: the happy path runs with zero agent involvement. The browser
is driven over a direct CDP WebSocket to the logged-in x.com page (see
`cdp_input`) — navigation, passive Network capture, and trusted input all go
through that one connection; no agent-browser daemon and no self-issued HTTP
requests. Only known bad states (login wall, rate limit, error page)
short-circuit to `needs_ops` for a human to resolve. Never replays the GraphQL
API: it only reads TweetDetail responses produced by genuine in-browser
navigation.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

import cdp_input
import wheel_motion
from extract_thread import build_self_thread, load_tweets_from_runs, parse_target_url
from output_paths import resolve_tool_output_root

# Known page states that a human (not the script) must resolve.
_BLOCK_MARKERS = {
    "something went wrong": "error_page",
    "try again": "error_page",
    "rate limit": "rate_limited",
    "verify you are human": "captcha",
    "prove you are human": "captcha",
    "prove you're human": "captcha",
    "captcha": "captcha",
}
_LOGIN_MARKERS = ("sign in to x", "log in", "create account", "sign up")
_SESSION_MARKERS = ("notifications", "profile", "bookmarks", "home")


def detect_block(page_text: str) -> str | None:
    """Return a block reason if the page is not a normal logged-in post view."""
    low = page_text.lower()
    for marker, reason in _BLOCK_MARKERS.items():
        if marker in low:
            return reason
    has_login = any(m in low for m in _LOGIN_MARKERS)
    has_session = any(m in low for m in _SESSION_MARKERS)
    if has_login and not has_session:
        return "login_wall"
    return None


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _frontmost_app_name() -> str | None:
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _activate_app(app_name: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _restore_frontmost_app(app_name: str | None) -> None:
    if not app_name:
        return
    current = _frontmost_app_name()
    if current and current != app_name:
        _activate_app(app_name)


class Browser(Protocol):
    def open(self, url: str) -> None: ...
    def page_text(self) -> str: ...
    def list_tweetdetail_ids(self) -> list[str]: ...
    def save_response(self, request_id: str, path: Path) -> None: ...
    def scroll(self, pixels: int) -> None: ...
    def enter_page(self) -> None: ...


class AgentBrowser:
    """Default Browser: drives the logged-in Chrome page over a direct CDP
    WebSocket (`cdp_input`) — no agent-browser daemon, no self-issued HTTP
    requests. Network evidence is read passively from the page's own
    `Network.*` CDP events; input is dispatched through the Input domain.
    """

    def __init__(self, port: int = 9222, target_id: str = "") -> None:
        self.port = str(port)
        self.target_id = target_id
        # Chrome exposes no "where is the mouse" query — the virtual cursor
        # position is whatever we last dispatched it to, tracked here so a
        # whole capture() session moves like one continuous hand instead of
        # teleporting to a fresh random spot on every action.
        self._cursor: tuple[float, float] | None = None
        # One input-device profile per session decides every scroll's tick
        # size, flick grouping, and momentum (see wheel_motion), so the scroll
        # signature differs run to run instead of one hardcoded distribution.
        self._wheel_profile = wheel_motion.new_wheel_profile()
        self._page_conn: cdp_input.CDPConnection | None = None
        self._network_responses: dict[str, dict[str, Any]] = {}
        self._network_finished: set[str] = set()

    def _x_target(self) -> dict[str, Any]:
        if self.target_id:
            target = next(
                (
                    item
                    for item in cdp_input.list_page_targets(int(self.port))
                    if str(item.get("id") or "") == self.target_id
                ),
                None,
            )
            if target is None:
                raise RuntimeError(
                    f"Owned X page target {self.target_id} is unavailable on CDP port {self.port}"
                )
            return target
        target = cdp_input.find_page_target(int(self.port), "https://x.com/")
        if target is None:
            target = cdp_input.find_page_target(int(self.port), "https://twitter.com/")
        if target is None:
            raise RuntimeError(f"No x.com page target found on CDP port {self.port}")
        return target

    def _connect_page(self) -> cdp_input.CDPConnection:
        if self._page_conn is None:
            self._page_conn = cdp_input.connect_to_target(self._x_target())
            self._page_conn.call("Page.enable")
            self._page_conn.call("Runtime.enable")
            self._page_conn.call("Network.enable")
        return self._page_conn

    def _reset_page_connection(self) -> None:
        if self._page_conn is not None:
            self._page_conn.close()
        self._page_conn = None
        self._network_responses = {}
        self._network_finished = set()

    def _runtime_value(self, expression: str) -> Any:
        result = self._connect_page().call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        return (result.get("result") or {}).get("value")

    def open(self, url: str) -> None:
        self._reset_page_connection()
        target = self._x_target() if self.target_id else (
            cdp_input.find_page_target(int(self.port), "https://x.com/")
            or cdp_input.find_page_target(int(self.port), "https://twitter.com/")
            or cdp_input.create_page_target(int(self.port), url)
        )
        self.target_id = str(target.get("id") or self.target_id)
        self._page_conn = cdp_input.connect_to_target(target)
        self._page_conn.call("Page.enable")
        self._page_conn.call("Runtime.enable")
        self._page_conn.call("Network.enable")
        self._page_conn.call("Page.navigate", {"url": url})
        time.sleep(4)
        self._drain_network_events(timeout=0.2)

    def close(self) -> None:
        self._reset_page_connection()

    def page_text(self) -> str:
        value = self._runtime_value("document.body.innerText.slice(0, 1200)")
        return str(value or "")

    def _drain_network_events(self, timeout: float = 0.05) -> None:
        conn = self._connect_page()
        events: list[dict[str, Any]] = []
        buffered = getattr(conn, "events", None)
        if isinstance(buffered, list):
            events.extend(buffered)
            buffered.clear()
        poll_events = getattr(conn, "poll_events", None)
        if callable(poll_events):
            events.extend(poll_events(timeout=timeout))
        for event in events:
            if event.get("method") == "Network.loadingFinished":
                request_id = str(((event.get("params") or {}).get("requestId")) or "")
                if request_id:
                    self._network_finished.add(request_id)
                continue
            if event.get("method") == "Network.loadingFailed":
                request_id = str(((event.get("params") or {}).get("requestId")) or "")
                if request_id:
                    self._network_finished.discard(request_id)
                continue
            if event.get("method") != "Network.responseReceived":
                continue
            params = event.get("params") or {}
            request_id = str(params.get("requestId") or "")
            if not request_id:
                continue
            response = params.get("response") or {}
            self._network_responses[request_id] = {
                "requestId": request_id,
                "url": response.get("url", ""),
                "status": response.get("status"),
                "resourceType": params.get("type"),
                "mimeType": response.get("mimeType"),
                "responseHeaders": response.get("headers") or {},
            }

    def list_request_ids(self, filter_name: str) -> list[str]:
        self._drain_network_events(timeout=0.05)
        return [
            rid for rid, meta in self._network_responses.items()
            if rid in self._network_finished and filter_name in (meta.get("url") or "")
        ]

    def list_tweetdetail_ids(self) -> list[str]:
        return self.list_request_ids("TweetDetail")

    def save_response(self, request_id: str, path: Path) -> None:
        meta = dict(self._network_responses.get(request_id) or {"requestId": request_id})
        try:
            body = self._connect_page().call("Network.getResponseBody", {"requestId": request_id})
            meta["responseBody"] = body.get("body", "")
            meta["base64Encoded"] = body.get("base64Encoded", False)
            payload = {"success": True, "data": meta, "error": None}
        except Exception as exc:
            payload = {"success": False, "data": meta, "error": str(exc)}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def scroll_marker(self) -> str:
        value = self._runtime_value(
            "JSON.stringify({y: Math.round(window.scrollY), h: document.documentElement.scrollHeight})",
        )
        return str(value or "").strip()

    def _connect_input(self) -> cdp_input.CDPConnection:
        """Connect directly to the x.com page's CDP endpoint for trusted
        Input-domain dispatch so concurrent sessions cannot redirect input
        to an unrelated browser tab.
        """
        return cdp_input.connect_to_target(self._x_target())

    def enter_page(self) -> None:
        """Move the virtual cursor onto the page once per session.

        Without this, the first dispatched event in a session would be a
        wheel event at a freshly-randomized point with no prior mouse
        presence, a sequence no real session has.
        """
        conn = self._connect_input()
        try:
            cdp_input.enable_focus_emulation(conn)
            start_x, start_y = random.randint(20, 80), random.randint(20, 80)
            dest_x = 640 + random.randint(-100, 100)
            dest_y = 360 + random.randint(-80, 80)
            path = cdp_input.wind_mouse_path(start_x, start_y, dest_x, dest_y)
            for x, y in path:
                cdp_input.dispatch_move(conn, x, y)
                time.sleep(random.uniform(0.003, 0.012))
            self._cursor = path[-1]
        finally:
            conn.close()

    def scroll(self, pixels: int) -> None:
        """Scroll via a burst of small, real mouseWheel ticks.

        Real wheel hardware fires several small ticks per scroll, not one big
        delta. The per-session
        `wheel_motion.WheelProfile` decides tick size, flick grouping, and
        momentum, so the scroll's statistical shape differs run to run instead
        of converging to one hardcoded distribution. The cursor stays put for
        the whole scroll — real users don't nudge the mouse on every tick.
        """
        conn = self._connect_input()
        try:
            cdp_input.enable_focus_emulation(conn)
            if self._cursor is None:
                self._cursor = (640 + random.randint(-100, 100), 400 + random.randint(-100, 100))
            x, y = self._cursor
            for delta, pause in wheel_motion.plan_wheel_motion(max(1, pixels), self._wheel_profile):
                cdp_input.dispatch_wheel(conn, x=x, y=y, delta_x=0, delta_y=delta)
                time.sleep(pause)
        finally:
            conn.close()

    def click_tab(self, label: str) -> None:
        """Move to and click a `role="tab"` link via a real, trusted sequence.

        The move from the session's last cursor position to the tab follows
        a WindMouse path instead of teleporting straight to the target.
        """
        conn = self._connect_input()
        try:
            cdp_input.enable_focus_emulation(conn)
            expr = (
                'Array.from(document.querySelectorAll(`a[role="tab"]`))'
                f'.find(a => a.textContent.includes("{label}"))'
            )
            rect = cdp_input.get_bounding_rect(conn, expr)
            if rect is None:
                return
            target_x = rect["x"] + rect["width"] / 2
            target_y = rect["y"] + rect["height"] / 2
            start_x, start_y = self._cursor or (target_x, target_y)
            path = cdp_input.wind_mouse_path(start_x, start_y, target_x, target_y)
            for x, y in path[:-1]:
                cdp_input.dispatch_move(conn, x, y)
                time.sleep(random.uniform(0.004, 0.015))
            cdp_input.dispatch_click(conn, target_x, target_y)
            self._cursor = (target_x, target_y)
        finally:
            conn.close()
        time.sleep(2)

    def click_button_texts(self, labels: list[str]) -> bool:
        """Click the first visible button-like element matching any label."""
        normalized = json.dumps([label.lower() for label in labels])
        conn = self._connect_input()
        front_app: str | None = None
        try:
            cdp_input.enable_focus_emulation(conn)
            expr = (
                "(() => {"
                f"const labels = {normalized};"
                "const candidates = Array.from(document.querySelectorAll("
                "'button,[role=\"button\"],a[role=\"button\"]'"
                "));"
                "return candidates.find(el => {"
                "const rect = el.getBoundingClientRect();"
                "const text = (el.innerText || el.textContent || '').trim().toLowerCase();"
                "return rect.width > 0 && rect.height > 0 && labels.some(label => text.includes(label));"
                "});"
                "})()"
            )
            rect = cdp_input.get_bounding_rect(conn, expr)
            if rect is None:
                return False
            front_app = _frontmost_app_name()
            cdp_input.dispatch_click(conn, rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
            return True
        finally:
            conn.close()
            if front_app is not None:
                _restore_frontmost_app(front_app)

    def click_retry(self) -> bool:
        return self.click_button_texts(["Try again", "Retry"])


def _jitter_sleep() -> None:
    time.sleep(random.uniform(0.35, 1.2))


def capture(
    url: str,
    post_dir: Path,
    browser: Browser | None = None,
    sleep=_jitter_sleep,
    max_scrolls: int = 15,
    stable_rounds: int = 3,
) -> dict[str, Any]:
    """Open the post, human-paced-scroll, and save TweetDetail responses.

    Stops when the author self-thread size is stable for `stable_rounds`
    consecutive scrolls. Returns a status dict.
    """
    browser = browser or AgentBrowser()
    _, target_id = parse_target_url(url)
    runs = post_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    browser.open(url)
    block = detect_block(browser.page_text())
    if block:
        return {"status": "needs_ops", "reason": block, "responses": 0}
    browser.enter_page()

    saved: set[str] = set()
    last_size = -1
    stable = 0
    for _ in range(max_scrolls):
        for rid in browser.list_tweetdetail_ids():
            if rid in saved:
                continue
            browser.save_response(rid, runs / f"td-{_safe(rid)}.json")
            saved.add(rid)

        chain = build_self_thread(load_tweets_from_runs(runs), target_id)
        size = len(chain)
        if chain and size == last_size:
            stable += 1
            if stable >= stable_rounds:
                return {"status": "complete", "posts": size, "responses": len(saved)}
        else:
            stable = 0
        last_size = size

        browser.scroll(random.randint(1400, 2600))
        sleep()

    final = build_self_thread(load_tweets_from_runs(runs), target_id)
    if final:
        return {"status": "complete", "posts": len(final), "responses": len(saved)}
    return {"status": "needs_ops", "reason": "target_not_found_after_scroll",
            "responses": len(saved)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True, help="Target X post URL.")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Exact X collection root for this run. Defaults to the Donald Skills Documents data directory.",
    )
    parser.add_argument("--cdp", type=int, default=9222)
    parser.add_argument("--max-scrolls", type=int, default=15)
    args = parser.parse_args()

    handle, status_id = parse_target_url(args.url)
    post_dir = resolve_tool_output_root("x", args.output_root) / handle / status_id
    result = capture(args.url, post_dir, browser=AgentBrowser(args.cdp),
                     max_scrolls=args.max_scrolls)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
