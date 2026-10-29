#!/usr/bin/env python3
"""Keep a task-owned ChatGPT browser session open for JSON-lines commands."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from browser_runtime import BrowserSession, process_is_alive
from output_paths import resolve_tool_output_root
from run_health import RunHeartbeat
from task_supervisor import WorkerStopped, worker_signals, add_supervisor_arguments, supervise
from chatgpt_page_health import (
    PAGE_ACCESS_STATE_JS, CURRENT_RESPONSE_ERROR_STATE_JS, CLICK_PAGE_ERROR_RETRY_JS,
    DEFAULT_PAGE_RECOVERY_ATTEMPTS, page_recovery_action, original_turn_script, original_history_script,
)
from profile_config import (
    ProfileConfigError,
    _CDPConnection,
    _cdp_version,
    cdp_target,
    close_background_page,
    configured_browser,
    connect_cdp_target,
    create_background_page,
    frontmost_process_id,
    restore_frontmost_process_if_browser_active,
)

BINDING_SCOPE = "donald-chatgpt-imagegen"
OWNER_SCOPE = "donald-chatgpt"
READ_SCRIPT = (Path(__file__).with_name("read_messages.js").read_text(encoding="utf-8")
               .replace('__PAGE_ACCESS_STATE__', PAGE_ACCESS_STATE_JS.replace('__EXPECTED_PROMPT__', '""'))
               .replace('__CURRENT_RESPONSE_ERROR_STATE__', CURRENT_RESPONSE_ERROR_STATE_JS))
INSPECT_SCRIPT = Path(__file__).with_name("inspect_page.js").read_text(encoding="utf-8")


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def chatgpt_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or parsed.username or parsed.password:
        raise ValueError("Use an https://chatgpt.com/ conversation or project URL")
    return value


def durable_conversation(value: str) -> bool:
    match = re.search(r'/c/([^/?#]+)', urlparse(value).path)
    return bool(match and not unquote(match[1]).startswith(('WEB:', 'local-chatgpt:')))


class ChatBrowserSession(BrowserSession):
    def __init__(self, *, saved: dict[str, Any], recover_worker: bool = False, **kwargs: Any) -> None:
        self.saved = saved
        self.recover_worker = recover_worker
        super().__init__(**kwargs)

    def _claim_target(self, state: dict[str, Any]) -> str:
        original = str(self.saved.get("target_id") or "")
        if original and any(run.get("target_id") == original for run in state["active_runs"]):
            raise ProfileConfigError("The original conversation is still owned by an active run")
        if original and cdp_target(self.port, original):
            state["retained_targets"] = [item for item in state["retained_targets"]
                                         if item.get("target_id") != original]
            self.owned_target_ids.add(original)
            if self.recover_worker and durable_conversation(self.url):
                self._navigate(original, self.url)
            return original  # Ordinary resume preserves the live target; takeover may reopen its durable URL.
        if self.saved.get("pending") and not durable_conversation(self.url):
            raise ProfileConfigError("Unresolved temporary conversation lost its target; do not send again")
        # Never reclaim another text task or the image skill's retained target.
        target = create_background_page(self.port, self.url)
        self.owned_target_ids.add(target)
        return target


class Conversation:
    def __init__(self, browser: BrowserSession, path: Path, saved: dict[str, Any]) -> None:
        self.browser = browser
        self.path = path
        self.output = path.parent
        self.state = {**saved, "schema_version": 1, "status": "ready", "pid": os.getpid(),
                      "binding_scope": BINDING_SCOPE, "target_id": browser.target_id,
                      "session_path": str(path), "output_dir": str(self.output)}
        self.state.setdefault("turns", [])
        for transient in ("gate", "reason", "cleanup", "handoff_requested"):
            self.state.pop(transient, None)
        self.observed: dict[str, Any] = {}
        self.observed_at = 0.0
        self.human_wait_timeout = 600
        self.reconcile_only = False
        self.persist()

    def persist(self) -> None:
        try:
            target = cdp_target(self.browser.port, self.browser.target_id)
        except ProfileConfigError as error:
            target = None
            self.state["last_browser_error"] = str(error)
        if target and str(target.get("url", "")).startswith("https://chatgpt.com/"):
            current = str(target['url'])
            previous = str(self.state.get('conversation_url', ''))
            if not (self.state.get('pending') and '/c/' in previous and not durable_conversation(current)):
                self.state["conversation_url"] = current
        self.state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_json(self.path, self.state)

    @contextlib.contextmanager
    def page(self, target_id: str | None = None):
        before = frontmost_process_id()
        try:
            with connect_cdp_target(self.browser.port, target_id or self.browser.target_id, timeout=60) as connection:
                yield connection
        finally:
            after = frontmost_process_id()
            activated = bool(before and self.browser.browser_pid and before != self.browser.browser_pid
                             and after == self.browser.browser_pid)
            with (self.output / "focus.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"before": before, "after": after,
                                         "browser_activated": activated}) + "\n")
            if activated:
                restore_frontmost_process_if_browser_active(before, self.browser.port)
                self.state.update(status="needs_ops", reason="foreground_changed")
                self.persist()
                raise RuntimeError("Chrome became foreground; background-only operation stopped")

    @staticmethod
    def eval_on(connection, script: str, *, by_value: bool = True) -> dict[str, Any]:
        result = connection.call("Runtime.evaluate", {"expression": script,
                                 "awaitPromise": True, "returnByValue": by_value})
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"]))
        return result.get("result", {})

    @staticmethod
    def element(selector: str) -> str:
        if re.fullmatch(r"@e[1-9]\d*", selector):
            lookup = f"globalThis.__donaldChatgptRefs?.[{int(selector[2:]) - 1}]"
        else:
            lookup = """(() => {
              const matches = [...document.querySelectorAll(SELECTOR)].filter(node => {
                const rect = node.getBoundingClientRect();
                return node.type === 'file' || (rect.width > 0 && rect.height > 0
                  && getComputedStyle(node).visibility !== 'hidden');
              });
              if (matches.length !== 1) throw Error('Expected one visible control or one file input, got ' + matches.length);
              return matches[0];
            })()""".replace("SELECTOR", json.dumps(selector))
        return f"const el = {lookup}; if (!el?.isConnected) throw Error('Missing or stale element');"

    def browser_command(self, args: list[str]) -> dict[str, Any]:
        if not args or not all(isinstance(arg, str) for arg in args):
            raise ValueError("args must be a nonempty array of strings")
        command = args[0]
        if command == "snapshot":
            return self.evaluate(INSPECT_SCRIPT)
        if command == "open":
            chatgpt_url(args[1])
            if self.state.get("pending"):
                raise ValueError("Resolve the pending turn before navigating")
            with self.page() as connection:
                return connection.call("Page.navigate", {"url": args[1]})
        if command == "get" and args[1] in {"url", "title"}:
            return {"value": self.evaluate("location.href" if args[1] == "url" else "document.title")}
        if command == "screenshot":
            output = Path(args[1]).expanduser().resolve()
            with self.page() as connection:
                result = connection.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(base64.b64decode(result["data"]))
            return {"path": str(output)}
        if command not in {"click", "fill", "upload", "download"}:
            raise ValueError("Supported browser commands: snapshot, get url/title, open, click, fill, upload, download, screenshot")
        resolve = self.element(args[1])
        if command == "click":
            return {"clicked": self.evaluate("(() => {" + resolve + """
              if (el.disabled || el.getAttribute('aria-disabled') === 'true') throw Error('Disabled control');
              if (el.closest('a[target=_blank]')) throw Error('New-tab navigation needs an explicit background target');
              el.click(); return true;
            })()""")}
        if command == "fill":
            with self.page() as connection:
                connection.call("Runtime.enable")
                connection.call("Page.enable")
                connection.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                self.eval_on(connection, "(() => {" + resolve + """
                  if (el.isContentEditable) {
                    el.focus(); const range = document.createRange(); range.selectNodeContents(el);
                    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
                    document.execCommand('delete');
                  } else if (el.matches('textarea,input')) {
                    el.focus(); el.select();
                  } else throw Error('Element is not editable');
                })()""")
                connection.call("Input.insertText", {"text": args[2]})
            return {"filled": args[1]}
        if command == "upload":
            files = [str(Path(value).expanduser().resolve()) for value in args[2:]]
            if not files or any(not Path(value).is_file() for value in files):
                raise ValueError("Upload requires existing local files")
            with self.page() as connection:
                remote = self.eval_on(connection, "(() => {" + resolve
                                      + "if (el.type !== 'file') throw Error('Not a file input'); return el;})()",
                                      by_value=False)
                connection.call("DOM.setFileInputFiles", {"objectId": remote["objectId"], "files": files})
            return {"uploaded": files}
        return self.download(args[1], Path(args[2]).expanduser().resolve())

    def evaluate(self, script: str) -> Any:
        with self.page() as connection:
            return self.eval_on(connection, script).get("value")

    def download(self, selector: str, destination: Path) -> dict[str, Any]:
        if destination.exists():
            raise ValueError("Choose a new download path; existing files are preserved")
        directory = self.output / "downloads" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        websocket = str((_cdp_version(self.browser.port) or {}).get("webSocketDebuggerUrl") or "")
        with self.page() as page, _CDPConnection.connect(websocket, timeout=60) as browser:
            frame_id = page.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
            browser.call("Browser.setDownloadBehavior", {"behavior": "allowAndName",
                         "downloadPath": str(directory), "eventsEnabled": True})
            try:
                self.eval_on(page, "(() => {" + self.element(selector) + """
                  const target = el.getAttribute('target'); el.removeAttribute('target');
                  try { el.click(); } finally { if (target !== null) el.setAttribute('target', target); }
                })()""")
                guid = ""
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    for event in browser.poll_events(timeout=0.2):
                        params = event.get("params", {})
                        if event.get("method") == "Browser.downloadWillBegin" and params.get("frameId") == frame_id:
                            guid = params["guid"]
                        if (event.get("method") == "Browser.downloadProgress" and guid
                                and params.get("guid") == guid and params.get("state") == "completed"):
                            source = directory / guid
                            if not source.is_file() or not source.stat().st_size:
                                raise RuntimeError("Downloaded file is missing or empty")
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            source.replace(destination)
                            return {"path": str(destination), "bytes": destination.stat().st_size}
                    time.sleep(0.1)
                raise TimeoutError(f"Download not confirmed; inspect {directory} without regenerating")
            finally:
                browser.call("Browser.setDownloadBehavior", {"behavior": "default"})

    def capture(self, *, recover: bool = True) -> dict[str, Any]:
        observed = self.evaluate(READ_SCRIPT)
        if not isinstance(observed, dict) or not isinstance(observed.get("messages"), list):
            raise ValueError("Could not read conversation DOM; inspect the owned page")
        pending = self.state.get("pending")
        recovery = (pending or {}).get('recovery', {})
        if pending and recovery.get('history_identity_failed'):
            identity = json.loads(self.evaluate(original_turn_script(
                pending['prompt'], pending.get('baseline_user_message_count', 0), recovery.get('original_turn_key', ''))))
            if identity.get('verified'):
                recovery.pop('history_identity_failed')
            elif (observed.get('gate') or {}).get('kind') != 'verification':
                observed['gate'] = {'kind': 'page_error', 'error_type': 'original_conversation_required'}
        if pending:
            for message in observed["messages"]:
                if (message["role"] == "user" and message["id"]
                        and message["id"] not in pending["baseline_ids"]
                        and message["text"].strip() == pending["prompt"].strip()):
                    pending.update(submission_committed=True, user_message_id=message["id"])
        self.observed = observed
        self.observed_at = time.monotonic()
        self.state["gate"] = observed.get("gate")
        if self.state["gate"]:
            self.state.update(status="recoverable_error", reason=self.state["gate"]["error_type"])
        else:
            self.state["status"] = "awaiting_reply" if pending else "ready"
            self.state.pop("reason", None)
        save_json(self.output / "observed.json", observed)
        self.persist()
        recovery = (pending or {}).get("recovery", {})
        missing_after_retry = bool(recovery.get("retry_started") and not self.reply(observed)
                                   and not observed["generating"] and pending.get("user_message_id")
                                   and pending["user_message_id"] not in {m["id"] for m in observed["messages"]})
        if recover and (observed.get("gate") or missing_after_retry):
            return self.recover(observed)
        return observed

    def heartbeat_probe(self) -> dict[str, Any]:
        # Independent, bounded, read-only connection; never share the worker's command stream.
        with connect_cdp_target(self.browser.port, self.browser.target_id, timeout=5) as connection:
            observed = self.eval_on(connection, READ_SCRIPT).get('value')
        if not isinstance(observed, dict) or not isinstance(observed.get('messages'), list):
            raise ValueError('Could not read conversation DOM')
        messages = observed['messages']
        last = messages[-1] if messages else {}
        return {**observed.get('access', {}), 'url': observed.get('url'), 'generating': observed.get('generating'),
                'gate': observed.get('gate'), 'message_count': len(messages),
                'last_message_id': last.get('id'),
                'last_message_hash': hashlib.sha256(last.get('text', '').encode()).hexdigest()}

    def reply_ready(self, previous: dict[str, Any], previous_at: float) -> bool:
        reply = self.reply(self.observed)
        return bool(reply and not self.observed.get('gate')
                    and not self.observed.get('generating', True)
                    and not self.observed.get('continuation_required', True)
                    and reply.get('turn_state', '') in {'', 'complete', 'completed', 'finished'}
                    and not previous.get('generating', True)
                    and not previous.get('continuation_required', True)
                    and self.reply(previous) == reply and time.monotonic() - previous_at >= 2)

    def recovery_event(self, recovery: dict[str, Any], action: str, **details: Any) -> None:
        recovery.setdefault("actions", []).append({"action": action, "at": time.time(), **details})
        self.persist()

    def wait_and_capture(self, seconds: int = 3) -> dict[str, Any]:
        time.sleep(seconds)
        return self.capture(recover=False)

    def probe_conversation(self, recovery: dict[str, Any]) -> dict[str, Any] | None:
        url = str(self.state.get("conversation_url") or "")
        if not re.search(r"/c/[^/?#]+", url) or recovery.get("probe_started"):
            return None
        recovery["probe_started"] = True
        self.recovery_event(recovery, "check_original_in_background", url=url)
        target = create_background_page(self.browser.port, url)
        self.browser.owned_target_ids.add(target)
        try:
            healthy = 0
            for _ in range(6):
                time.sleep(3)
                try:
                    with self.page(target) as connection:
                        observed = self.eval_on(connection, READ_SCRIPT).get("value") or {}
                except (OSError, ProfileConfigError, RuntimeError) as error:
                    healthy = 0
                    self.recovery_event(recovery, "probe_page_not_ready", error=str(error))
                    continue
                healthy = healthy + 1 if (observed.get("ready_state") == "complete"
                          and observed.get("has_composer") and not observed.get("gate")
                          and not observed.get("generating")) else 0
                save_json(self.output / "recovery" / "conversation-probe.json", observed)
                if healthy >= 2:
                    return observed
            return None
        finally:
            close_background_page(self.browser.port, target)
            self.browser.owned_target_ids.discard(target)

    def recover(self, observed: dict[str, Any]) -> dict[str, Any]:
        pending = self.state.get("pending")
        owner = pending if pending is not None else self.state
        recovery = owner.setdefault("recovery", {})
        if recovery.get("exhausted"):
            self.state.update(status="needs_ops", reason="automatic_recovery_exhausted")
            self.persist()
            return observed
        if not recovery.get("settle_waited"):
            recovery["settle_waited"] = True
            self.recovery_event(recovery, "wait_for_normal_browser_recovery")
            observed = self.wait_and_capture()
        gate = observed.get("gate") or {}
        while gate.get('kind') == 'page_error' and recovery.get('page_attempts', 0) < DEFAULT_PAGE_RECOVERY_ATTEMPTS:
            url = str(self.state.get('conversation_url') or '')
            attempt = recovery.get('page_attempts', 0) + 1
            action = page_recovery_action(attempt, submitted=bool(pending),
                                          preserve_target=bool(pending and not durable_conversation(url)))
            if action == 'preserve':
                break
            recovery['page_attempts'] = attempt
            self.recovery_event(recovery, 'recover_loading_page', attempt=attempt, method=action, url=url)
            clicked = action == 'retry' and json.loads(self.evaluate(CLICK_PAGE_ERROR_RETRY_JS)).get('clicked')
            if not clicked:
                with self.page() as connection:
                    connection.call('Page.navigate', {'url': chatgpt_url(url)})
            observed = self.wait_and_capture()
            gate = observed.get('gate') or {}
        if pending and not durable_conversation(str(self.state.get('conversation_url') or '')) and gate.get('kind') != 'verification':
            prompt = pending['prompt']
            identity = json.loads(self.evaluate(original_turn_script(
                prompt, pending.get('baseline_user_message_count', 0), recovery.get('original_turn_key', ''))))
            if identity.get('verified'):
                recovery['original_turn_key'] = identity.get('turn_key', '')
                self.persist()
            elif (recovery.get('history_attempts', 0) < DEFAULT_PAGE_RECOVERY_ATTEMPTS
                  and json.loads(self.evaluate(original_history_script(prompt))).get('found')):
                recovery['history_attempts'] = recovery.get('history_attempts', 0) + 1
                self.recovery_event(recovery, 'reopen_original_history_control')
                self.evaluate(original_history_script(prompt, click=True))
                observed = self.wait_and_capture()
                gate = observed.get('gate') or {}
                identity = json.loads(self.evaluate(original_turn_script(
                    prompt, pending.get('baseline_user_message_count', 0), recovery.get('original_turn_key', ''))))
                if not identity.get('verified') and gate.get('kind') != 'verification':
                    recovery['history_identity_failed'] = True
                    observed['gate'] = {'kind': 'page_error', 'error_type': 'original_conversation_required'}
                    self.state.update(status='needs_ops', reason='original_conversation_required')
                    self.recovery_event(recovery, 'original_history_turn_unverified')
                    return observed
                if identity.get('verified'):
                    recovery['original_turn_key'] = identity.get('turn_key', '')
                self.persist()
        if not gate and not recovery.get('retry_started'):
            return observed
        if gate.get("kind") == "verification":
            # Let the browser's normal verification scripts finish without any foreground action.
            while recovery.get("verification_checks", 0) < 3:
                recovery["verification_checks"] = recovery.get("verification_checks", 0) + 1
                self.persist()
                observed = self.wait_and_capture(5)
                if not observed.get("gate"):
                    self.recovery_event(recovery, "verification_cleared")
                    return observed
                gate = observed["gate"]
                if gate.get("kind") != "verification":
                    break
            if gate.get("kind") == "verification":
                started = recovery.setdefault('verification_started_at', time.time())
                expired = time.time() - started >= self.human_wait_timeout
                self.state.update(status='needs_ops' if expired else 'waiting_for_human',
                                  reason='login_required' if gate.get('error_type') == 'login_required' else 'human_verification_required')
                self.persist()
                return observed
        if (not self.reconcile_only and pending and gate.get("kind") == "service_error" and not recovery.get("retry_started")
                and gate.get("retry_for_user_id") == pending.get("user_message_id")
                and not self.reply(observed) and not observed["generating"]):
            recovery["retry_started"] = True
            recovery["failed_user_message_id"] = pending["user_message_id"]
            save_json(self.output / "recovery" / "failed-before-retry.json", observed)
            self.recovery_event(recovery, "retry_failed_reply", error=gate)
            # Identity and budget are persisted before the single click, including on a timeout.
            self.evaluate("""(() => {
              const retry = globalThis.__donaldChatgptRetry;
              if (!retry?.element?.isConnected || retry.userId !== USER_ID) throw Error('Failed-turn Retry changed');
              retry.element.click();
            })()""".replace("USER_ID", json.dumps(pending["user_message_id"])))
            observed = self.wait_and_capture()
        if not observed.get("gate") and (self.reply(observed) or observed["generating"]):
            return observed
        if not self.reconcile_only and pending and recovery.get("retry_started") and not recovery.get("restore_started"):
            probe = self.probe_conversation(recovery)
            if probe:
                matches = [m for m in probe["messages"] if m["role"] == "user" and m["id"] not in pending["baseline_ids"]
                           and m["text"].strip() == pending["prompt"].strip()]
                baseline = set(pending["baseline_ids"])
                persisted_ids = {m["id"] for m in probe["messages"]}
                if matches or (baseline and baseline <= persisted_ids and not pending["attachments"]):
                    with self.page() as connection:
                        connection.call("Page.navigate", {"url": self.state["conversation_url"]})
                    observed = self.wait_and_capture()
                    if matches:
                        self.recovery_event(recovery, "reopened_persisted_original_turn")
                        return observed
                    # A confirmed failed text follow-up disappeared after Retry. The reopened
                    # original conversation has its complete baseline and no such user turn.
                    if (not observed.get("gate") and not observed["generating"]
                            and baseline <= {m["id"] for m in observed["messages"]}
                            and not any(m["role"] == "user" and m["id"] not in pending["baseline_ids"]
                                        and m["text"].strip() == pending["prompt"].strip()
                                        for m in observed["messages"])):
                        recovery["restore_started"] = True
                        self.recovery_event(recovery, "restore_failed_text_followup_once")
                        self.browser_command(["fill", '[contenteditable=true][role=textbox],#prompt-textarea,textarea',
                                              pending["prompt"]])
                        draft = self.capture(recover=False)
                        if draft["composer_text"].strip() != pending["prompt"].strip():
                            raise ValueError("Could not restore the failed prompt exactly")
                        pending.update(baseline_ids=[m["id"] for m in draft["messages"]],
                                       submission_committed="unknown")
                        pending.pop("user_message_id", None)
                        self.persist()
                        self.evaluate("""(() => {
                          const buttons = [...document.querySelectorAll('button')].filter(el =>
                            el.getBoundingClientRect().height > 0 && !el.disabled
                            && /^(send|send message|发送)$/i.test(el.getAttribute('aria-label') || el.innerText));
                          if (buttons.length !== 1) throw Error('Send control is ambiguous');
                          buttons[0].click();
                        })()""")
                        return self.wait_and_capture()
        if not observed.get("gate"):
            return observed  # Healthy but still unresolved: continue collecting, never blind Send.
        recovery["exhausted"] = True
        self.state.update(status="needs_ops", reason="automatic_recovery_exhausted")
        self.recovery_event(recovery, "automatic_recovery_exhausted", remaining_error=observed["gate"])
        return observed

    def reply(self, observed: dict[str, Any]) -> dict[str, Any] | None:
        user_id = (self.state.get("pending") or {}).get("user_message_id")
        after_user = False
        answer = None
        for message in observed.get("messages", []):
            if message["role"] == "user":
                after_user = message["id"] == user_id
                if after_user:
                    answer = None
            elif after_user and message["role"] == "assistant" and message["text"]:
                answer = message
        return answer

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if self.reconcile_only and op not in {'capture', 'complete-turn', 'finish', 'handoff'}:
            raise ValueError('Reconciliation only permits collecting and finishing the original turn')
        if op == "browser":
            args = list(request["args"])
            if "text_file" in request:
                args.append(Path(request["text_file"]).expanduser().read_text(encoding="utf-8"))
            return self.browser_command(args)
        if op == "eval":
            script = (Path(request["script_file"]).expanduser().read_text(encoding="utf-8")
                      if "script_file" in request else request["script"])
            return {"result": self.evaluate(script)}
        if op == "capture":
            return self.capture()
        if op == "send":
            if self.state.get("pending"):
                raise ValueError("A turn is unresolved; collect it instead of sending again")
            attachments = [str(Path(value).expanduser().resolve()) for value in request.get("attachments", [])]
            if any(not Path(value).is_file() for value in attachments):
                raise ValueError("An attachment source is missing")
            observed = self.capture()
            if observed.get("gate"):
                raise ValueError("Browser recovery did not clear the error; no new Send was clicked")
            if not observed["composer_text"].strip() or observed["generating"]:
                raise ValueError("The draft must contain text and the previous answer must be finished")
            selector = request["selector"]
            self.state["pending"] = {
                "prompt": observed["composer_text"], "attachments": attachments,
                "baseline_ids": [item["id"] for item in observed["messages"]],
                "baseline_user_message_count": sum(item['role'] == 'user' for item in observed['messages']),
                "dispatch_started": True, "submission_committed": "unknown",
            }
            self.state["status"] = "awaiting_reply"
            self.persist()  # A failed click is ambiguous; never automatically replay it.
            return self.browser_command(["click", selector])
        if op == "complete-turn":
            previous, previous_at = self.observed, self.observed_at
            current = self.capture()
            if current.get("gate"):
                raise ValueError("Browser recovery did not clear the error; retain the original conversation")
            reply = self.reply(current)
            if (not reply or current["generating"] or current["continuation_required"]
                    or reply.get("turn_state", "") not in {"", "complete", "completed", "finished"}
                    or previous.get("generating", True) or previous.get("continuation_required", True)
                    or self.reply(previous) != reply or time.monotonic() - previous_at < 2):
                raise ValueError("The pending answer is not confirmed complete and stable; inspect and capture again")
            number = len(self.state["turns"]) + 1
            stem = self.output / "turns" / f"{number:04d}"
            record = {**self.state["pending"], "assistant": reply,
                      "conversation_url": self.state.get("conversation_url")}
            save_json(stem.with_suffix(".json"), record)
            stem.with_suffix(".txt").write_text(reply["text"] + "\n", encoding="utf-8")
            stem.with_suffix(".html").write_text(reply["html"], encoding="utf-8")
            self.state["turns"].append(str(stem.with_suffix(".json")))
            self.state["pending"] = None
            self.state["status"] = "ready"
            self.persist()
            return {"status": "turn_complete", "reply_path": str(stem.with_suffix(".txt")),
                    "turn_path": str(stem.with_suffix(".json"))}
        if op in {"finish", "handoff"}:
            if op == "finish" and self.state.get("pending"):
                raise ValueError("Cannot finish an unresolved turn; collect it or hand it off")
            self.state["status"] = "complete" if op == "finish" else "needs_ops"
            self.state['handoff_requested'] = op == 'handoff'
            self.state["reason"] = request.get("reason", "")
            if op == "handoff" and request.get("human"):
                self.browser.retain_target_for_human(self.browser.target_id)
            self.persist()
            return {"status": self.state["status"]}
        raise ValueError(f"Unknown op: {op}")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="conversation")
    parser.add_argument("--url", type=chatgpt_url)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument('--human-wait-timeout', type=float, default=600)
    parser.add_argument('--session-path', type=Path, help=argparse.SUPPRESS)
    add_supervisor_arguments(parser)
    return parser


def new_session_path(args: argparse.Namespace) -> Path:
    if args.resume or args.session_path:
        return (args.resume or args.session_path).expanduser().resolve()
    name = re.sub(r'[^\w-]+', '-', args.name).strip('-')[:70] or 'conversation'
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
    return resolve_tool_output_root('chatgpt', args.output_root) / name / stamp / 'session.json'


def run_commands(conversation: Conversation, health: RunHeartbeat) -> int:
    requests: queue.Queue = queue.Queue()
    def read_requests() -> None:
        for line in sys.stdin:
            requests.put(line)
        requests.put(None)
    threading.Thread(target=read_requests, daemon=True).start()
    ready_announced = False
    while True:
        active = bool(conversation.state.get('pending') or conversation.state['status'] == 'waiting_for_human')
        if not active or ready_announced:
            health.set_phase('waiting_input')
        elif conversation.state['status'] == 'waiting_for_human':
            health.set_phase('waiting_for_human')
        else:
            health.set_phase('awaiting_reply')
        try:
            line = requests.get(timeout=5)
        except queue.Empty:
            if not active or ready_announced:
                continue
            line = '{"op":"capture"}'
            automatic = True
        else:
            automatic = False
        if line is None:
            return 0  # EOF preserves unresolved state in main's cleanup.
        try:
            previous, previous_at = conversation.observed, conversation.observed_at
            request = json.loads(line)
            if not automatic:
                health.set_phase('command', str(request.get('op', '')))
                ready_announced = False
            result = conversation.handle(request)
            if not automatic:
                emit({'ok': True, 'result': result})
            if conversation.state.get('pending') and conversation.reply_ready(previous, previous_at):
                ready_announced = True
                emit({'event': 'reply_ready', 'session_path': str(conversation.path),
                      'observed_path': str(conversation.output / 'observed.json')})
            if request.get('op') in {'finish', 'handoff'}:
                return 0 if conversation.state['status'] == 'complete' else 2
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            emit({'ok': False, 'error': str(error), 'session_path': str(conversation.path),
                  'pending': conversation.state.get('pending')})
        if conversation.state['status'] == 'needs_ops':
            emit({'status': 'needs_ops', 'reason': conversation.state.get('reason'),
                  'session_path': str(conversation.path), 'pending': conversation.state.get('pending')})
            return 2


def main() -> int:
    args = argument_parser().parse_args()
    browser = None
    conversation = None
    health = None
    try:
        path = new_session_path(args)
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if args.resume and not path.exists():
            raise ValueError('The saved session does not exist')
        if saved.get("pid") and process_is_alive(int(saved["pid"])):
            raise ValueError("The saved session still has an active owner")
        if args.resume and args.url and args.url != saved.get("conversation_url"):
            raise ValueError("Resume the saved conversation without replacing its URL")
        config = configured_browser(scope=BINDING_SCOPE)
        browser = ChatBrowserSession(saved=saved, recover_worker=args.recover_worker, config=config, scope=OWNER_SCOPE,
                                     owner_path=path.parent / 'chatgpt_browser_owner.json',
                                     session="donald-chatgpt", verify_agent_browser=False, url=chatgpt_url(
                                         saved.get('conversation_url') or args.url or 'https://chatgpt.com/'))
        browser.open()
        conversation = Conversation(browser, path, saved)
        conversation.human_wait_timeout = args.human_wait_timeout
        conversation.reconcile_only = args.reconcile_only
        health = RunHeartbeat(path.parent, conversation.heartbeat_probe)
        health.start()
        emit(conversation.state)
        return run_commands(conversation, health)
    except (ValueError, OSError, ProfileConfigError) as error:
        emit({"status": "needs_ops", "error": str(error), "binding_scope": BINDING_SCOPE,
              "session_path": str(args.resume) if args.resume else None})
        return 2
    finally:
        stopped = sys.exc_info()[1]
        if health is not None:
            health.close(conversation.state['status'])
        if conversation is not None:
            if isinstance(stopped, WorkerStopped) and not stopped.preserve_browser:
                conversation.state.update(status='interrupted', reason='cancelled')
            elif conversation.state["status"] not in {"complete", "needs_ops"}:
                conversation.state.update(status="needs_ops", reason="session_interrupted")
            conversation.state["pid"] = None
            try:
                conversation.persist()
            except OSError as error:
                emit({"status": "needs_ops", "error": f"Could not save final session state: {error}"})
        if browser is not None:
            preserve = conversation is not None and conversation.state["status"] != "complete"
            if isinstance(stopped, WorkerStopped):
                preserve = stopped.preserve_browser
            cleanup = browser.close(preserve=preserve)
            if conversation is not None:
                conversation.state["cleanup"] = cleanup
                save_json(conversation.path, conversation.state)
            emit({"cleanup": cleanup})


def entrypoint() -> int:
    args = argument_parser().parse_args()
    if args.worker:
        with worker_signals(new_session_path(args).parent):
            return main()
        return 130
    path = new_session_path(args)
    worker_args = [*sys.argv[1:], '--session-path', str(path)]
    return supervise(Path(__file__).resolve(), worker_args, path.parent, path, args, interactive=True)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
