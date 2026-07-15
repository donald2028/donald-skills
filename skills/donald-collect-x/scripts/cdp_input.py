#!/usr/bin/env python3
"""Minimal stdlib-only CDP-over-WebSocket client for dispatching real input.

This talks directly to a Chrome page's CDP endpoint so concurrent browser
sessions cannot redirect input to an unrelated tab. It calls
`Input.dispatchMouseEvent`; events
dispatched this way go through Chrome's real input pipeline and arrive in the
page as `isTrusted: true`, unlike `Runtime.evaluate`-driven `element.click()`/
`window.scrollBy()`, which produce untrusted or incomplete event sequences.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import random
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Any

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_SQRT3 = math.sqrt(3)
_SQRT5 = math.sqrt(5)


def compute_accept_key(key: str) -> str:
    """RFC 6455 Sec-WebSocket-Accept value for a given Sec-WebSocket-Key."""
    digest = hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_handshake_request(host: str, port: int, path: str, key: str) -> bytes:
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def encode_text_frame(payload: bytes, mask_key: bytes) -> bytes:
    """Client->server text frame. RFC 6455 requires client frames be masked."""
    header = bytearray([0x81])  # FIN=1, opcode=0x1 (text)
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def decode_frame(buf: bytes) -> tuple[int, bytes, int] | None:
    """Parse one frame (masked or not) from the start of `buf`.

    Returns (opcode, payload, bytes_consumed), or None if `buf` doesn't yet
    contain a complete frame (caller should read more and retry).
    """
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < offset + 2:
            return None
        length = struct.unpack(">H", buf[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buf) < offset + 8:
            return None
        length = struct.unpack(">Q", buf[offset:offset + 8])[0]
        offset += 8
    mask_key = b""
    if masked:
        if len(buf) < offset + 4:
            return None
        mask_key = buf[offset:offset + 4]
        offset += 4
    if len(buf) < offset + length:
        return None
    payload = buf[offset:offset + length]
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload, offset + length


def _random_mask_key() -> bytes:
    return bytes(random.getrandbits(8) for _ in range(4))


class CDPConnection:
    """A JSON-RPC call() over an already-handshaken WebSocket-like socket.

    `sock` only needs `.sendall(bytes)` / `.recv(n) -> bytes` / `.close()` —
    real usage is a real `socket.socket`; tests inject a fake.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._buf = b""
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

    @classmethod
    def connect(cls, host: str, port: int, path: str, timeout: float = 10.0) -> "CDPConnection":
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(bytes(random.getrandbits(8) for _ in range(16))).decode("ascii")
        sock.sendall(build_handshake_request(host, port, path, key))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP handshake closed before completing")
            response += chunk
        head, _, rest = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise ConnectionError(f"CDP handshake failed: {status_line!r}")
        conn = cls(sock)
        conn._buf = rest
        return conn

    def _recv_frame(self) -> tuple[int, bytes]:
        while True:
            parsed = decode_frame(self._buf)
            if parsed is not None:
                opcode, payload, consumed = parsed
                self._buf = self._buf[consumed:]
                return opcode, payload
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP connection closed unexpectedly")
            self._buf += chunk

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        payload = json.dumps(
            {"id": msg_id, "method": method, "params": params or {}}
        ).encode("utf-8")
        self._sock.sendall(encode_text_frame(payload, _random_mask_key()))
        while True:
            opcode, payload = self._recv_frame()
            if opcode != 0x1:  # text frame; CDP doesn't send binary/control here
                continue
            data = json.loads(payload.decode("utf-8"))
            if data.get("method"):
                self.events.append(data)
            if data.get("id") == msg_id:
                return data.get("result", {})

    def poll_events(self, timeout: float = 0.05) -> list[dict[str, Any]]:
        """Drain pending CDP events without waiting for a specific command result."""
        collected: list[dict[str, Any]] = []
        gettimeout = getattr(self._sock, "gettimeout", None)
        settimeout = getattr(self._sock, "settimeout", None)
        old_timeout = gettimeout() if callable(gettimeout) else None
        if callable(settimeout):
            settimeout(timeout)
        try:
            while True:
                parsed = decode_frame(self._buf)
                if parsed is None:
                    try:
                        chunk = self._sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    self._buf += chunk
                    continue
                opcode, payload, consumed = parsed
                self._buf = self._buf[consumed:]
                if opcode != 0x1:
                    continue
                data = json.loads(payload.decode("utf-8"))
                if data.get("method"):
                    self.events.append(data)
                    collected.append(data)
        finally:
            if callable(settimeout):
                settimeout(old_timeout)
        return collected

    def close(self) -> None:
        self._sock.close()


def dispatch_wheel(conn: Any, x: float, y: float, delta_x: float, delta_y: float) -> None:
    """Dispatch a real, trusted mouse-wheel event via CDP's Input domain."""
    conn.call("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y,
    })


def dispatch_move(conn: Any, x: float, y: float) -> None:
    """Dispatch a single, trusted mouseMoved event (no press/release)."""
    conn.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})


def wind_mouse_path(
    start_x: float, start_y: float, dest_x: float, dest_y: float,
    *, gravity: float = 9.0, wind: float = 3.0, max_step: float = 15.0,
    target_area: float = 12.0,
) -> list[tuple[int, int]]:
    """WindMouse path from (start_x, start_y) to (dest_x, dest_y).

    Models the cursor as a particle under two forces: gravity (constant pull
    toward the destination) and wind (a randomly, smoothly varying force).
    The continuous physics produces a non-uniform, non-repeating step shape
    (slow near the endpoints, faster mid-path) that a fixed Bezier curve
    family doesn't. Always ends exactly at the
    destination, even though the simulation only gets within 1px of it.
    """
    current_x, current_y = float(start_x), float(start_y)
    velocity_x = velocity_y = wind_x = wind_y = 0.0
    max_velocity = max_step
    points: list[tuple[int, int]] = []
    while True:
        dist = math.hypot(dest_x - current_x, dest_y - current_y)
        if dist < 1:
            break
        wind_mag = min(wind, dist)
        if dist >= target_area:
            wind_x = wind_x / _SQRT3 + (random.random() * (2 * wind_mag + 1) - wind_mag) / _SQRT5
            wind_y = wind_y / _SQRT3 + (random.random() * (2 * wind_mag + 1) - wind_mag) / _SQRT5
        else:
            wind_x /= _SQRT3
            wind_y /= _SQRT3
            if max_velocity < 3:
                max_velocity = random.random() * 3 + 3
            else:
                max_velocity /= _SQRT5
        velocity_x += wind_x + gravity * (dest_x - current_x) / dist
        velocity_y += wind_y + gravity * (dest_y - current_y) / dist
        velocity_mag = math.hypot(velocity_x, velocity_y)
        if velocity_mag > max_velocity:
            clipped = max_velocity / 2 + random.random() * max_velocity / 2
            velocity_x = (velocity_x / velocity_mag) * clipped
            velocity_y = (velocity_y / velocity_mag) * clipped
        current_x += velocity_x
        current_y += velocity_y
        points.append((round(current_x), round(current_y)))
    points.append((round(dest_x), round(dest_y)))
    return points


def enable_focus_emulation(conn: Any) -> None:
    """Let a page target accept input without raising Chrome to the OS front."""
    conn.call("Emulation.setFocusEmulationEnabled", {"enabled": True})


def dispatch_click(conn: Any, x: float, y: float) -> None:
    """Dispatch a real, trusted move+press+release click sequence."""
    conn.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    conn.call("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    conn.call("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
    })


def get_bounding_rect(conn: Any, js_element_expr: str) -> dict[str, Any] | None:
    """Read-only: the viewport rect of the element `js_element_expr` evaluates
    to. This only queries DOM state via Runtime.evaluate — it dispatches no
    event, so it doesn't affect the page's isTrusted signal either way.
    """
    result = conn.call("Runtime.evaluate", {
        "expression": (
            f"(() => {{ const el = {js_element_expr}; "
            "return el ? JSON.stringify(el.getBoundingClientRect()) : null; })()"
        ),
        "returnByValue": True,
    })
    value = (result.get("result") or {}).get("value")
    return json.loads(value) if value else None


def list_page_targets(port: int) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_page_target(port: int, url_prefix: str) -> dict[str, Any] | None:
    """The first open page target whose URL starts with `url_prefix`.

    Filtering by URL prevents attaching to an unrelated browser tab.
    """
    for target in list_page_targets(port):
        if target.get("type") == "page" and target.get("url", "").startswith(url_prefix):
            return target
    return None


def create_page_target(port: int, url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
        version = json.loads(resp.read().decode("utf-8"))
    ws_url = str(version.get("webSocketDebuggerUrl") or "")
    parts = urllib.parse.urlsplit(ws_url)
    if not parts.hostname or not parts.port or not parts.path:
        raise RuntimeError(f"Browser CDP websocket unavailable on port {port}")
    conn = CDPConnection.connect(parts.hostname, parts.port, parts.path)
    try:
        result = conn.call("Target.createTarget", {"url": url, "background": True})
    finally:
        conn.close()
    target_id = str(result.get("targetId") or "")
    deadline = time.time() + 5
    while time.time() < deadline:
        for target in list_page_targets(port):
            if target.get("id") == target_id:
                return target
        time.sleep(0.1)
    raise RuntimeError(f"Background CDP target {target_id or '<missing>'} was not exposed")


def connect_to_target(target: dict[str, Any], host: str = "127.0.0.1") -> CDPConnection:
    ws_url = target["webSocketDebuggerUrl"]
    parts = urllib.parse.urlsplit(ws_url)
    return CDPConnection.connect(host, parts.port, parts.path)
