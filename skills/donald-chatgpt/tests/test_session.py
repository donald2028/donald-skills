from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chatgpt_session as session


class SessionTests(unittest.TestCase):
    def test_human_handoff_retains_the_target_without_activating_chrome(self):
        browser = self.conversation.browser
        browser.retain_target_for_human = mock.Mock()
        browser.preserve_for_human = mock.Mock()
        self.conversation.handle({'op': 'handoff', 'reason': 'login_required', 'human': True})
        browser.retain_target_for_human.assert_called_once_with('owned')
        browser.preserve_for_human.assert_not_called()
        self.assertEqual(self.conversation.state['status'], 'needs_ops')

    def test_unexpected_foreground_change_restores_the_working_app_before_stopping(self):
        connection = mock.MagicMock()
        with (
            mock.patch.object(session, 'frontmost_process_id', side_effect=[111, 222]),
            mock.patch.object(session, 'connect_cdp_target', return_value=connection),
            mock.patch.object(session, 'restore_frontmost_process_if_browser_active', create=True) as restore,
            self.assertRaisesRegex(RuntimeError, 'foreground'),
        ):
            with self.conversation.page():
                pass
        restore.assert_called_once_with(111, 9241)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = mock.patch.object(session, "cdp_target", return_value={"url": "https://chatgpt.com/c/example"})
        self.target.start()
        self.addCleanup(self.target.stop)
        self.foreground = mock.patch.object(session, "frontmost_process_id", return_value=111)
        self.foreground.start()
        self.addCleanup(self.foreground.stop)
        browser = SimpleNamespace(port=9241, target_id="owned", browser_pid=222)
        self.path = Path(self.temporary.name) / "session.json"
        self.conversation = session.Conversation(browser, self.path, {})

    def test_uncertain_send_is_saved_before_click_and_cannot_be_replayed(self):
        observed = {"composer_text": "Read the attachment", "generating": False, "messages": []}
        with mock.patch.object(self.conversation, "capture", return_value=observed), mock.patch.object(
            self.conversation, "browser_command", side_effect=TimeoutError("Click outcome unknown")
        ) as click:
            with self.assertRaises(TimeoutError):
                self.conversation.handle({"op": "send", "selector": "button"})
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved["pending"]["submission_committed"], "unknown")
            self.assertTrue(saved["pending"]["dispatch_started"])
            with self.assertRaisesRegex(ValueError, "unresolved"):
                self.conversation.handle({"op": "send", "selector": "button"})
            click.assert_called_once()

    def test_background_fill_uses_only_the_owned_page_connection(self):
        connection = mock.MagicMock()
        connection.call.return_value = {"result": {"value": True}}
        context = mock.MagicMock()
        context.__enter__.return_value = connection
        with mock.patch.object(session, "connect_cdp_target", return_value=context) as connect:
            self.conversation.browser_command(["fill", "[role=textbox]", "literal $() text\n第二行"])
        connect.assert_called_once_with(9241, "owned", timeout=60)
        methods = {call.args[0] for call in connection.call.call_args_list}
        self.assertFalse(methods & {"Target.activateTarget", "Page.bringToFront", "Input.dispatchKeyEvent"})
        connection.call.assert_any_call("Input.insertText", {"text": "literal $() text\n第二行"})
        focus = json.loads((self.path.parent / "focus.jsonl").read_text())
        self.assertFalse(focus["browser_activated"])

    def test_verification_blocks_send_without_retry_or_activation(self):
        observed = {"composer_text": "Question", "generating": False, "messages": [],
                    "gate": {"error_type": "human_verification_required"}}
        with mock.patch.object(self.conversation, "capture", return_value=observed), mock.patch.object(
            self.conversation, "browser_command"
        ) as click:
            with self.assertRaises(ValueError):
                self.conversation.handle({"op": "send", "selector": "button"})
        click.assert_not_called()
        self.assertIsNone(self.conversation.state.get("pending"))

    def test_inline_cloudflare_failure_retries_its_own_reply_once(self):
        user = {"id": "new-user", "role": "user", "text": "Question"}
        failed = {"composer_text": "", "generating": False, "messages": [user],
                  "gate": {"kind": "service_error", "error_type": "transient_service_error", "retry_for_user_id": "new-user"}}
        healthy = {**failed, "gate": None, "messages": [user,
                   {"id": "answer", "role": "assistant", "text": "Recovered"}]}
        self.conversation.state["pending"] = {"prompt": "Question", "baseline_ids": [],
                                              "attachments": [], "user_message_id": "new-user"}
        reads = iter([failed, failed, healthy])
        actions = []
        def evaluate(script):
            if script == session.READ_SCRIPT:
                return next(reads)
            actions.append(script)
            return True
        with mock.patch.object(self.conversation, "evaluate", side_effect=evaluate), mock.patch.object(session.time, "sleep"):
            result = self.conversation.capture()
        self.assertIsNone(result["gate"])
        self.assertEqual(self.conversation.reply(result)["text"], "Recovered")
        self.assertEqual(len(actions), 1)
        self.assertTrue(self.conversation.state["pending"]["recovery"]["retry_started"])
        self.assertNotEqual(self.conversation.state["status"], "needs_ops")

    def test_old_turns_retry_cannot_replay_the_pending_message(self):
        user = {"id": "new-user", "role": "user", "text": "Question"}
        failed = {"composer_text": "", "generating": False, "messages": [user],
                  "gate": {"kind": "service_error", "error_type": "transient_service_error", "retry_for_user_id": "old-user"}}
        self.conversation.state["pending"] = {"prompt": "Question", "baseline_ids": [],
                                              "attachments": [], "user_message_id": "new-user"}
        with mock.patch.object(self.conversation, "evaluate", return_value=failed) as evaluate, mock.patch.object(session.time, "sleep"):
            self.conversation.capture()
        self.assertTrue(all(call.args[0] == session.READ_SCRIPT for call in evaluate.call_args_list))
        self.assertEqual(self.conversation.state["reason"], "automatic_recovery_exhausted")

    def test_verification_can_clear_in_background_without_user_help(self):
        waiting = {"composer_text": "", "generating": False, "messages": [],
                   "gate": {"kind": "verification", "error_type": "verification_pending"}}
        healthy = {**waiting, "gate": None}
        with mock.patch.object(self.conversation, "evaluate", side_effect=[waiting, waiting, healthy]), mock.patch.object(session.time, "sleep"):
            result = self.conversation.capture()
        self.assertIsNone(result["gate"])
        self.assertEqual(self.conversation.state["status"], "ready")

    def test_exhausted_budget_survives_resume_without_more_clicks(self):
        failed = {"composer_text": "", "generating": False, "messages": [],
                  "gate": {"kind": "service_error", "error_type": "transient_service_error", "retry_for_user_id": "new-user"}}
        self.conversation.state["recovery"] = {"exhausted": True, "retry_started": True}
        resumed = session.Conversation(self.conversation.browser, self.path,
                                       json.loads(self.path.read_text()) | {"recovery": self.conversation.state["recovery"]})
        with mock.patch.object(resumed, "evaluate", return_value=failed) as evaluate, mock.patch.object(session.time, "sleep") as sleep:
            resumed.capture()
        evaluate.assert_called_once_with(session.READ_SCRIPT)
        sleep.assert_not_called()
        self.assertEqual(resumed.state["reason"], "automatic_recovery_exhausted")

    def test_missing_turn_without_explicit_failure_is_never_resent(self):
        observed = {"composer_text": "", "generating": False, "messages": [], "gate": None}
        self.conversation.state["pending"] = {"prompt": "Question", "baseline_ids": [],
            "attachments": [], "user_message_id": "missing", "submission_committed": "unknown"}
        with mock.patch.object(self.conversation, "evaluate", return_value=observed) as evaluate, mock.patch.object(
            self.conversation, "probe_conversation"
        ) as probe:
            self.conversation.capture()
        evaluate.assert_called_once_with(session.READ_SCRIPT)
        probe.assert_not_called()
        self.assertEqual(self.conversation.state["pending"]["submission_committed"], "unknown")

    def test_previous_answer_cannot_complete_a_new_user_turn(self):
        self.conversation.state["pending"] = {"user_message_id": "new-user"}
        observed = {"generating": False, "continuation_required": False, "messages": [
            {"id": "old-user", "role": "user", "text": "old question"},
            {"id": "old-answer", "role": "assistant", "text": "old answer"},
            {"id": "new-user", "role": "user", "text": "new question"},
        ]}
        self.conversation.observed = observed
        self.conversation.observed_at = time.monotonic() - 10
        with mock.patch.object(self.conversation, "capture", return_value=observed):
            with self.assertRaisesRegex(ValueError, "not confirmed complete"):
                self.conversation.handle({"op": "complete-turn"})
        self.assertIsNotNone(self.conversation.state["pending"])

    def test_resume_preserves_another_active_runs_target(self):
        browser = object.__new__(session.ChatBrowserSession)
        browser.saved = {"target_id": "active-target"}
        state = {"active_runs": [{"target_id": "active-target"}], "retained_targets": []}
        with mock.patch.object(session, "create_background_page") as create:
            with self.assertRaisesRegex(session.ProfileConfigError, "active run"):
                browser._claim_target(state)
        create.assert_not_called()


@unittest.skipUnless(os.environ.get("DONALD_CHATGPT_BROWSER_TEST") == "1",
                     "Opt in to a background local-page test using the shared Chrome binding")
class LocalBrowserTests(unittest.TestCase):
    def test_hidden_composer_file_transfers_and_grouped_replies(self):
        config = session.configured_browser(scope=session.BINDING_SCOPE)
        with tempfile.TemporaryDirectory() as directory, session.BrowserSession(
            session="chatgpt-local-test", scope="donald-chatgpt-local-test", config=config,
            verify_agent_browser=False,
        ) as browser:
            root = Path(directory)
            conversation = session.Conversation(browser, root / "session.json", {})
            html = '''<main>
              <div contenteditable="true" role="textbox" style="display:none"></div>
              <div contenteditable="true" role="textbox"><p><br></p></div>
              <input type="file" id="files" multiple>
              <a id="download" download="result.txt" href="data:text/plain,download-verified">Download</a>
              <div data-turn-key="pair" data-talvt-turn-state="complete">
                <div data-chatgpt-search-unit-key="turn:0:user" data-chatgpt-search-message-ids="user-1">
                  <div class="whitespace-pre-wrap">Question</div>
                </div>
                <div data-chatgpt-search-unit-key="turn:1:assistant" data-chatgpt-search-message-ids="answer-1">
                  <h4>ChatGPT said:</h4><div data-markdown-text-style="assistant-message">Answer<pre>code()</pre></div>
                </div>
              </div>
            </main>'''
            conversation.evaluate("document.body.innerHTML = " + json.dumps(html))
            conversation.browser_command(["fill", "[role=textbox]", "Multiline\n第二行 $()"])
            observed = conversation.capture()
            self.assertEqual(observed["composer_text"], "Multiline\n第二行 $()")
            self.assertEqual([item["role"] for item in observed["messages"]], ["user", "assistant"])
            self.assertEqual(observed["messages"][1]["code"], ["code()"])
            snapshot = conversation.browser_command(["snapshot"])
            editor = next(item["ref"] for item in snapshot["controls"] if item["role"] == "textbox")
            conversation.browser_command(["fill", editor, "Ref still works"])
            self.assertEqual(conversation.capture()["composer_text"], "Ref still works")
            sources = [root / "a.txt", root / "b.csv"]
            for source in sources:
                source.write_text("local test input", encoding="utf-8")
            conversation.browser_command(["upload", "#files", *map(str, sources)])
            self.assertEqual(conversation.evaluate("[...document.querySelector('#files').files].map(f => f.name)"),
                             ["a.txt", "b.csv"])
            destination = root / "downloaded.txt"
            conversation.browser_command(["download", "#download", str(destination)])
            self.assertEqual(destination.read_text(), "download-verified")
            conversation.evaluate("document.body.insertAdjacentHTML('beforeend', '<aside role=alert><div>cloudflare_challenge</div><button>Retry</button></aside>')")
            error = conversation.capture(recover=False)
            self.assertEqual(error["gate"]["kind"], "service_error")
            self.assertEqual(conversation.state["status"], "recoverable_error")
            failed_page = '''<main>
              <div data-message-author-role="user" data-message-id="failed-user">Question</div>
              <aside role="alert"><div>cloudflare_challenge</div><button onclick="this.closest('aside').outerHTML =
                '&lt;div data-message-author-role=assistant data-message-id=recovered&gt;Recovered&lt;/div&gt;'">Retry</button></aside>
              <div contenteditable="true" role="textbox"><p><br></p></div>
            </main>'''
            conversation.evaluate("document.body.innerHTML = " + json.dumps(failed_page))
            conversation.state["pending"] = {"prompt": "Question", "baseline_ids": [], "attachments": [],
                                              "user_message_id": "failed-user"}
            recovered = conversation.capture()
            self.assertIsNone(recovered["gate"])
            self.assertEqual(conversation.reply(recovered)["text"], "Recovered")
            self.assertTrue(conversation.state["pending"]["recovery"]["retry_started"])
            focus = [json.loads(line) for line in (root / "focus.jsonl").read_text().splitlines()]
            self.assertTrue(focus)
            self.assertFalse(any(row["browser_activated"] for row in focus))


if __name__ == "__main__":
    unittest.main()
