from __future__ import annotations

import json
import contextlib
import os
import io
import queue
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
    def test_reconciliation_cannot_send_through_browser_or_eval_commands(self):
        self.conversation.reconcile_only = True
        with mock.patch.object(self.conversation, 'browser_command') as browser, \
                mock.patch.object(self.conversation, 'evaluate') as evaluate:
            for request in ({'op': 'send', 'selector': '#send'},
                            {'op': 'browser', 'args': ['click', '#send']},
                            {'op': 'eval', 'script': 'document.querySelector("#send").click()'}):
                with self.subTest(request=request), self.assertRaisesRegex(ValueError, 'Reconciliation'):
                    self.conversation.handle(request)
            browser.assert_not_called()
            evaluate.assert_not_called()

    def test_unverified_history_turn_cannot_become_a_completed_reply_after_resume(self):
        self.conversation.state['pending'] = {'prompt': 'Question', 'baseline_ids': [], 'user_message_id': 'original',
            'recovery': {'history_identity_failed': True, 'original_turn_key': 'original'}}
        observed = {'messages': [{'role': 'user', 'id': 'different', 'text': 'Question'},
                                {'role': 'assistant', 'id': 'answer', 'text': 'Other reply'}],
                    'gate': None, 'generating': False, 'continuation_required': False}
        with mock.patch.object(self.conversation, 'evaluate', side_effect=[observed, json.dumps({'verified': False})]):
            self.conversation.capture(recover=False)
        self.assertEqual(self.conversation.observed['gate']['error_type'], 'original_conversation_required')
        self.assertFalse(self.conversation.reply_ready(observed, 0))

    def test_pending_loading_failure_reopens_original_without_retrying_or_sending(self):
        self.conversation.state['pending'] = {'prompt': 'Question', 'baseline_ids': [],
                                              'attachments': [], 'submission_committed': 'unknown'}
        failure = {'messages': [], 'generating': False,
                   'gate': {'kind': 'page_error', 'error_type': 'chatgpt_transient_page_error'}}
        healthy = {**failure, 'gate': None}
        connection = mock.Mock()
        with mock.patch.object(self.conversation, 'evaluate', side_effect=[failure, failure, healthy]) as evaluate, \
                mock.patch.object(self.conversation, 'page', return_value=contextlib.nullcontext(connection)), \
                mock.patch.object(self.conversation, 'browser_command') as send, \
                mock.patch.object(session.time, 'sleep'):
            observed = self.conversation.capture()
        self.assertIsNone(observed['gate'])
        connection.call.assert_called_once_with('Page.navigate', {'url': 'https://chatgpt.com/c/example'})
        self.assertTrue(all(call.args[0] == session.READ_SCRIPT for call in evaluate.call_args_list))
        self.assertEqual(self.conversation.state['pending']['submission_committed'], 'unknown')
        send.assert_not_called()

    def test_pending_turn_resumes_after_verification_without_caller_capture_commands(self):
        self.conversation.state['pending'] = {'prompt': 'Question', 'baseline_ids': [],
            'attachments': [], 'submission_committed': 'unknown',
            'recovery': {'settle_waited': True, 'verification_checks': 3, 'verification_started_at': time.time()}}
        observed = {'messages': [{'id': 'user', 'role': 'user', 'text': 'Question'},
                                {'id': 'answer', 'role': 'assistant', 'text': 'Done', 'html': '<p>Done</p>'}],
                    'generating': False, 'continuation_required': False, 'gate': None}
        requests = mock.Mock()
        requests.get.side_effect = [queue.Empty, queue.Empty, queue.Empty, '{"op":"complete-turn"}', '{"op":"finish"}']
        verification = {**observed, 'messages': observed['messages'][:1],
                        'gate': {'kind': 'verification', 'error_type': 'verification_pending'}}
        clock = iter(range(0, 100, 5))
        health = mock.Mock()
        with mock.patch.object(session.queue, 'Queue', return_value=requests), \
                mock.patch.object(sys, 'stdin', io.StringIO('')), \
                mock.patch.object(self.conversation, 'evaluate', side_effect=[verification, observed, observed, observed]) as read, \
                mock.patch.object(session.time, 'monotonic', side_effect=lambda: next(clock)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(session.run_commands(self.conversation, health), 0)
        self.assertGreaterEqual(read.call_count, 3)
        self.assertIn('reply_ready', output.getvalue())
        health.set_phase.assert_any_call('waiting_for_human')
        self.assertIsNone(self.conversation.state['pending'])
        self.assertEqual((self.path.parent / 'turns/0001.txt').read_text(), 'Done\n')

    def test_verification_keeps_polling_and_its_deadline_survives_worker_resume(self):
        observed = {'messages': [], 'generating': False,
                    'gate': {'kind': 'verification', 'error_type': 'verification_pending'}}
        self.conversation.state['recovery'] = {'settle_waited': True, 'verification_checks': 3,
                                              'verification_started_at': 100}
        self.conversation.human_wait_timeout = 10
        with mock.patch.object(self.conversation, 'evaluate', return_value=observed), \
                mock.patch.object(session.time, 'time', return_value=109):
            self.conversation.capture()
        self.assertEqual(self.conversation.state['status'], 'waiting_for_human')
        resumed = session.Conversation(self.conversation.browser, self.path, json.loads(self.path.read_text()))
        resumed.human_wait_timeout = 10
        with mock.patch.object(resumed, 'evaluate', return_value=observed), \
                mock.patch.object(session.time, 'time', return_value=111):
            resumed.capture()
        self.assertEqual(resumed.state['status'], 'needs_ops')
        self.assertEqual(resumed.state['recovery']['verification_started_at'], 100)

    def test_redirect_home_cannot_erase_pending_conversation_binding(self):
        self.conversation.state['pending'] = {'submission_committed': 'unknown'}
        original = self.conversation.state['conversation_url']
        for url in ('https://chatgpt.com/', 'https://chatgpt.com/c/local-chatgpt%3Atest'):
            with mock.patch.object(session, 'cdp_target', return_value={'url': url}):
                self.conversation.persist()
            self.assertEqual(self.conversation.state['conversation_url'], original)

    def test_takeover_preserves_encoded_temporary_target_and_refuses_to_recreate_it(self):
        browser = object.__new__(session.ChatBrowserSession)
        browser.saved = {'target_id': 'original', 'pending': {'submission_committed': 'unknown'}}
        browser.url = 'https://chatgpt.com/c/local-chatgpt%3Atest'
        browser.port, browser.owned_target_ids, browser.recover_worker = 9241, set(), True
        state = {'active_runs': [], 'retained_targets': []}
        with mock.patch.object(session, 'cdp_target', return_value={'id': 'original'}), \
                mock.patch.object(browser, '_navigate') as navigate:
            self.assertEqual(browser._claim_target(state), 'original')
        navigate.assert_not_called()
        with mock.patch.object(session, 'cdp_target', return_value=None), \
                self.assertRaisesRegex(session.ProfileConfigError, 'temporary conversation'):
            browser._claim_target(state)

    def test_human_handoff_retains_the_target_without_activating_chrome(self):
        browser = self.conversation.browser
        browser.retain_target_for_human = mock.Mock()
        browser.preserve_for_human = mock.Mock()
        self.conversation.handle({'op': 'handoff', 'reason': 'login_required', 'human': True})
        browser.retain_target_for_human.assert_called_once_with('owned')
        browser.preserve_for_human.assert_not_called()
        self.assertEqual(self.conversation.state['status'], 'needs_ops')
        self.assertTrue(self.conversation.state['handoff_requested'])

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

    def test_cleared_page_error_does_not_skip_original_failed_turn_probe(self):
        observed = {'messages': [], 'generating': False, 'gate': None}
        self.conversation.state['pending'] = {'prompt': 'Question', 'baseline_ids': [], 'attachments': [],
            'user_message_id': 'missing', 'submission_committed': 'unknown',
            'recovery': {'settle_waited': True, 'retry_started': True}}
        with mock.patch.object(self.conversation, 'evaluate', return_value=observed), \
                mock.patch.object(self.conversation, 'probe_conversation', return_value=None) as probe, \
                mock.patch.object(self.conversation, 'browser_command') as send:
            self.conversation.capture()
        probe.assert_called_once()
        send.assert_not_called()

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
    def test_shared_loading_failure_detection_and_scoped_retry(self):
        config = session.configured_browser(scope=session.BINDING_SCOPE)
        with tempfile.TemporaryDirectory() as directory, session.BrowserSession(
            session='chatgpt-page-health-test', scope='donald-chatgpt-local-test', config=config,
            verify_agent_browser=False,
        ) as browser:
            conversation = session.Conversation(browser, Path(directory) / 'session.json',
                                                {'conversation_url': 'https://chatgpt.com/c/example'})
            html = '<main><div>Could not load this ChatGPT conversation<button id="retry">Try again</button></div></main>'
            conversation.evaluate('document.body.innerHTML = ' + json.dumps(html))
            observed = conversation.capture(recover=False)
            self.assertEqual(observed['gate']['kind'], 'page_error')
            self.assertEqual(observed['access']['crashReason'], 'chatgpt_transient_page_error')
            conversation.evaluate("document.getElementById('retry').onclick = () => {document.body.dataset.retried = 'yes';"
                                  "document.body.innerHTML = '<div contenteditable=\"true\" role=\"textbox\"></div>';}")
            with mock.patch.object(session.time, 'sleep'):
                self.assertIsNone(conversation.capture()['gate'])
            self.assertEqual(conversation.evaluate('document.body.dataset.retried'), 'yes')
            quoted = '<div data-chatgpt-search-unit-key="turn:0:user">Could not load this ChatGPT conversation<button>Try again</button></div>'
            conversation.evaluate('document.body.innerHTML = ' + json.dumps(quoted))
            self.assertIsNone(conversation.capture(recover=False)['gate'])
            self.assertFalse(json.loads(conversation.evaluate(session.CLICK_PAGE_ERROR_RETRY_JS))['clicked'])

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
