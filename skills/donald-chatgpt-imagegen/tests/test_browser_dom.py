"""Opt-in DOM regressions: set DONALD_IMAGEGEN_BROWSER_TESTS=1 to use the bound Chrome lane."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_browser_runner as runner  # noqa: E402
from PIL import Image, PngImagePlugin  # noqa: E402


@unittest.skipUnless(
    os.environ.get("DONALD_IMAGEGEN_BROWSER_TESTS") == "1",
    "requires opt-in and a configured Chrome lane",
)
class BrowserDomTests(unittest.TestCase):
    def test_conversation_loading_failure_without_alert_has_a_scoped_page_retry(self):
        self.load_html('<main><div>Could not load this ChatGPT conversation<button id="retry">Try again</button></div></main>')
        runner._eval_js(self.args, self.cwd, "document.getElementById('retry').onclick = () => {"
                        "document.body.dataset.retried = 'yes'; document.body.innerHTML = '<div contenteditable=\"true\"></div>';}")
        self.assertEqual(runner._heartbeat_probe(self.args, self.cwd)['crashReason'], 'chatgpt_transient_page_error')
        self.assertEqual(runner._generation_page_health(self.args, self.cwd, None, '')['status'], 'page_error')
        self.assertTrue(runner._click_page_error_retry(self.args, self.cwd))
        self.assertEqual(runner._eval_js(self.args, self.cwd, 'document.body.dataset.retried'), 'yes')
        self.assertFalse(runner._heartbeat_probe(self.args, self.cwd)['crashReason'])
        self.load_html('<main><div data-message-author-role="user">Question</div>'
                       '<aside role="alert">cloudflare_challenge<button>Retry</button></aside>'
                       '<div contenteditable="true"></div></main>')
        self.assertEqual(runner._heartbeat_probe(self.args, self.cwd)['responseError']['error_type'],
                         'chatgpt_response_service_error')

    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = runner._FreshImageBrowserSession(
            scope="donald-chatgpt-imagegen",
            session="imagegen-dom-tests",
            url="about:blank",
            timeout=30,
            lock_timeout=10,
        ).open()
        cls.addClassCleanup(cls.browser.close)
        cls.args = argparse.Namespace(_owned_tab_cdp_url=cls.browser.target_url, cdp=str(cls.browser.port))
        cls.cwd = Path.cwd()

    def load_html(self, html: str) -> None:
        runner._eval_js(
            self.args,
            self.cwd,
            "document.body.innerHTML = " + json.dumps(html),
        )

    def test_collapsed_prompt_expands_before_health_and_url_verification(self) -> None:
        prompt = 'Reference image map with exact roles and constraints. ' * 20 + 'Distinct final instruction.'
        self.load_html('<div data-turn-key="ours"><div data-user-message-bubble>'
            '<span id="message">' + prompt[:210] + '...</span><button id="expand">Show more</button>'
            '</div><div data-conversation-role="assistant">ChatGPT said:</div>'
            '<button aria-label="Generated image 1"><img alt="Generated image 1" src="/output.png" width="300" height="420"></button>'
            '</div><div contenteditable="true"></div>')
        runner._eval_js(self.args, self.cwd, 'document.getElementById("expand").onclick = () => {'
            'document.getElementById("message").textContent = ' + json.dumps(prompt) + ';};')
        args = argparse.Namespace(**vars(self.args), _submitted_prompt=prompt)
        self.assertTrue(runner._generation_page_health(args, self.cwd, None, '')['submitted_prompt_present'])
        runner._eval_js(self.args, self.cwd, 'document.getElementById("message").textContent = '
            + json.dumps(prompt[:210] + '...'))
        canonical = 'https://chatgpt.com/c/canonical'
        with mock.patch.object(runner, '_current_url', return_value=canonical), mock.patch.object(
            runner, '_open_conversation', side_effect=AssertionError('Must verify in the original target')
        ):
            event = runner._ensure_expected_conversation(args, self.cwd,
                'https://chatgpt.com/c/local-chatgpt%3Atest', 0)
        self.assertTrue(event['canonicalized'])
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'collapsed', 'prompt_card': 'prompt.md', 'download_dir': temporary}
            runner._persist_canonical_conversation(job, 'batch', event, None)
            saved = runner._read_json_if_exists(runner._session_path(job))
            self.assertEqual(saved['canonical_conversation_url'], canonical)
        health = runner._generation_page_health(args, self.cwd, None, '')
        self.assertTrue(health['submitted_prompt_present'])
        self.assertTrue(any(item.get('inSubmittedTurn') for item in runner._image_inventory(args, self.cwd)))

    def test_full_dom_text_matches_hidden_tail_without_expanding(self) -> None:
        prompt = 'Draw a fox with a distinct instruction hidden in the collapsed section.'
        self.load_html('<div data-turn-key="ours"><div data-user-message-bubble>'
            'Draw a fox <span style="display:none">with a distinct instruction hidden in the collapsed section.</span>'
            '</div></div><div contenteditable="true"></div>')
        args = argparse.Namespace(**vars(self.args), _submitted_prompt=prompt)
        self.assertTrue(runner._generation_page_health(args, self.cwd, None, '')['submitted_prompt_present'])

    def test_expansion_never_accepts_only_a_prefix_or_ambiguous_turns(self) -> None:
        prefix = 'Reference image map with exact roles and constraints. ' * 4
        for copies in (1, 2):
            self.load_html(''.join('<div data-turn-key="turn-' + str(i) + '"><div data-user-message-bubble>'
                + prefix + '<button onclick="this.textContent=\'Show less\'">Show more</button></div></div>'
                for i in range(copies)) + '<div contenteditable="true"></div>')
            args = argparse.Namespace(**vars(self.args), _submitted_prompt=prefix + 'Different hidden instruction.')
            self.assertEqual(runner._expand_submitted_prompt(args, self.cwd), copies == 1)
            self.assertFalse(runner._generation_page_health(args, self.cwd, None, '')['submitted_prompt_present'])

    def test_conversation_load_error_retry_is_page_scoped(self) -> None:
        self.load_html('<main><p>Could not load this ChatGPT conversation</p><button>Try again</button></main>')
        access = runner._page_access_state(self.args, self.cwd)
        health = runner._generation_page_health(self.args, self.cwd, None, 'Try again')
        self.assertEqual(access['crashReason'], 'chatgpt_transient_page_error')
        self.assertEqual(health['status'], 'page_error')
        self.assertNotIn('generation_error', health)

    def test_native_blob_is_collected_without_composer_viewer_or_download_button(self) -> None:
        native = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('native-blob', 'preserve original compressed bytes')
        Image.new('RGB', (300, 420), 'coral').save(native, format='PNG', pnginfo=metadata)
        data = native.getvalue()
        self.load_html('<div data-turn-key="current"><div data-user-message-bubble>Draw a fox</div>'
                       '<div data-conversation-role="assistant">ChatGPT said:</div>'
                       '<button aria-label="Generated image 1"><img alt="Generated image 1"></button></div>')
        runner._eval_js(self.args, self.cwd, '''(async () => {
            const data = Uint8Array.from(atob(''' + json.dumps(base64.b64encode(data).decode()) + '''), c => c.charCodeAt(0));
            const image = document.querySelector('img');
            image.src = URL.createObjectURL(new Blob([data], {type: 'image/png'}));
            await image.decode(); return 'loaded';
        })()''')
        args = argparse.Namespace(**vars(self.args))
        args._submitted_prompt = 'Draw a fox'
        args.timeout, args.progress_interval = 5, 5
        url = 'https://chatgpt.com/c/fixture'
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'blob', 'prompt_card': 'fixture.md', 'download_dir': temporary, 'variant_count': 1}
            with (
                mock.patch.object(runner, '_ensure_expected_conversation', return_value={'current_url': url}),
                mock.patch.object(runner, '_persist_canonical_conversation', return_value=url),
                mock.patch.object(runner, '_write_timing_sample'),
                mock.patch.object(runner, '_screenshot'),
                mock.patch.object(runner, '_submit_prompt') as submit,
                mock.patch.object(runner.time, 'sleep', side_effect=AssertionError('A loaded idle asset must enter download immediately')),
            ):
                result = runner._collect_generated_images_with_retries(args, job, self.cwd, Path(temporary),
                    set(), [], label='batch', mode='collect_current', resumed=True, max_failure_retries=0,
                    expected_conversation_url=url, baseline_user_message_count=0)
            self.assertEqual(result['status'], 'downloaded')
            self.assertEqual(Path(result['images'][0]['path']).read_bytes(), data)
            self.assertTrue(result['images'][0]['image_validation'].get('ok'))
            submit.assert_not_called()

    def test_generation_controls_skip_hidden_editor_before_visible_composer(self) -> None:
        self.load_html('<div contenteditable="true" style="display:none"></div>'
                       '<div contenteditable="true">Message</div><button>Edit</button>')
        state = runner._checked_generation_button_state(self.args, self.cwd)
        self.assertTrue(state["composerVisible"])
        self.assertFalse(runner._generation_active_from_button_state(state))
        self.load_html('<div contenteditable="true">Message</div><button>Stop generating</button>')
        state = runner._checked_generation_button_state(self.args, self.cwd)
        self.assertTrue(runner._generation_active_from_button_state(state))

    def test_chat_surface_structures_and_ambiguous_selection(self) -> None:
        cases = [
            ('group', 'Composer mode', 'aria-pressed="true"', 'aria-pressed="false"', 'chat'),
            ('group', 'Composer mode', 'aria-pressed="false"', 'aria-pressed="true"', 'work'),
            ('radiogroup', 'Select chat surface', 'aria-checked="true"', 'aria-checked="false"', 'chat'),
            ('radiogroup', 'Select chat surface', 'data-state="off"', 'data-state="on"', 'work'),
            ('group', 'Composer mode', '', '', 'unknown'),
            ('group', 'Composer mode', 'aria-pressed="true"', 'aria-pressed="true"', 'unknown'),
            ('group', 'Other', 'aria-pressed="true"', 'aria-pressed="false"', 'unknown'),
        ]
        for role, label, chat_state, work_state, expected in cases:
            with self.subTest(role=role, label=label, expected=expected, chat_state=chat_state):
                self.load_html(
                    f'<div role="{role}" aria-label="{label}">'
                    f'<button {chat_state}>Chat</button>'
                    f'<button {work_state}><span>Work</span></button></div>'
                )
                state = runner._chat_surface_state(self.args, self.cwd)
                self.assertEqual(state["selected_surface"], expected)

        self.load_html(
            '<div style="display:none" role="group" aria-label="Composer mode">'
            '<button aria-pressed="true">Chat</button>'
            '<button aria-pressed="false">Work</button></div>'
        )
        self.assertFalse(runner._chat_surface_state(self.args, self.cwd)["control_available"])

    def test_switch_from_work_to_chat(self) -> None:
        self.load_html(
            '<div role="group" aria-label="Composer mode">'
            '<button aria-pressed="false" onclick="'
            "this.setAttribute('aria-pressed','true');"
            "this.nextElementSibling.setAttribute('aria-pressed','false')"
            '">Chat</button><button aria-pressed="true">Work</button></div>'
        )
        result = runner._ensure_chat_surface(self.args, self.cwd)
        self.assertTrue(result["verified"])
        self.assertTrue(result["clicked_chat"])
        self.assertEqual(result["selected_surface"], "chat")

    def test_independent_heartbeat_counts_current_user_turn_structure(self) -> None:
        self.load_html(
            '<div data-turn-key="1"><h5>You said:</h5>'
            '<div data-user-message-bubble>draw a fox</div>'
            '<h6>ChatGPT said:</h6><div>Generating image</div></div>'
        )
        state = runner._heartbeat_probe(self.args, self.cwd)
        self.assertEqual(state['userTurnCount'], 1)

    def test_create_image_menu_structures(self) -> None:
        cases = [
            (
                '<div data-mention-section-items="true">'
                '<button data-list-navigation-item="true"><div data-menu-row-content="true">'
                '<span style="display:block">Create image</span>'
                '<span style="display:block">Visualize anything</span></div></button></div>',
                True,
            ),
            ('<div role="menu"><button role="menuitem">Create image</button></div>', True),
            ('<button>Create image</button>', False),
            ('<div data-mention-section-items="true"><button>Sketch</button></div>', False),
        ]
        for html, expected in cases:
            with self.subTest(html=html):
                self.load_html(html)
                self.assertEqual(
                    runner._find_create_image_control(self.args, self.cwd)["found"],
                    expected,
                )

    def test_create_image_ignores_sidebar_conversation_titles(self) -> None:
        sidebar = (
            '<div data-sidebar-chatgpt-conversation-key="chatgpt:conversation:old" role="listitem">'
            '<div role="group"><a aria-label="Generate image" href="/c/old">'
            'Generate image</a></div></div>'
        )
        self.load_html(sidebar)
        self.assertFalse(runner._find_create_image_control(self.args, self.cwd)["found"])
        self.load_html(
            sidebar + '<div data-mention-section-items="true"><button>Create image</button></div>'
        )
        control = runner._find_create_image_control(self.args, self.cwd)
        self.assertTrue(control["found"])
        self.assertIn("Create image", control["label"])

    def test_create_image_reports_disabled_menu_entry(self) -> None:
        self.load_html(
            '<div data-mention-section-items="true">'
            '<div aria-disabled="true"><div data-menu-row-content="true">'
            '<span>Create image</span></div></div></div>'
        )
        control = runner._find_create_image_control(self.args, self.cwd)
        self.assertFalse(control["found"])
        self.assertTrue(control.get("disabled"))

    def test_current_reference_cards_wait_for_upload_and_ignore_other_images(self) -> None:
        self.load_html(
            '<nav><img src="blob:https://chatgpt.com/unrelated" width="100" height="100"></nav>'
            '<form><div contenteditable="true">Create image</div>'
            '<div data-composer-attachments><div class="composer-attachment-surface" '
            'role="button" aria-label="reference(2).png">'
            '<img alt="reference(2).png" width="100" height="100">'
            '<span role="progressbar" aria-label="Uploading reference(2).png">Uploading</span>'
            '<button aria-label="Remove reference(2).png">Remove</button>'
            '</div></div></form>'
        )
        observation = runner._reference_upload_observation(self.args, self.cwd, ["/refs/reference.png"])
        self.assertFalse(observation["ready"])
        self.assertEqual(observation["attachment_count"], 1)
        self.assertTrue(observation["filename_mentions"]["reference.png"])
        self.assertEqual(observation["pending_controls"][0]["attachmentFilename"], "reference(2).png")
        self.assertTrue(observation["attachment_details"][0]["pending"])
        runner._eval_js(self.args, self.cwd, "document.querySelector('[role=progressbar]').remove()")
        observation = runner._reference_upload_observation(self.args, self.cwd, ["/refs/reference.png"])
        self.assertTrue(observation["ready"])
        self.assertEqual(observation["attachment_filenames"], ["reference(2).png"])
        self.assertEqual(observation["blob_image_count"], 0)
        runner._eval_js(self.args, self.cwd, "document.querySelector('[data-composer-attachments]').remove()")
        self.assertFalse(
            runner._reference_upload_observation(self.args, self.cwd, ["/refs/reference.png"])["ready"]
        )

    def test_new_chat_clears_retained_attachments_before_reupload(self) -> None:
        self.load_html('<form><div contenteditable="true"></div>'
            '<div data-composer-attachments><div role="button" aria-label="old.png"><img width="50" height="50"></div></div>'
            '</form><button onclick="document.querySelector(\'[data-composer-attachments]\').remove()">New chat</button>')
        with mock.patch.object(runner, "_open_url"), mock.patch.object(runner, "_wait_ms"):
            runner._open_new_chat(self.args, self.cwd)
        self.assertEqual(runner._reference_upload_observation(self.args, self.cwd, ["/refs/new.png"])["attachment_count"], 0)

    def test_unscoped_composer_progress_is_reported_and_still_blocks_ready(self) -> None:
        self.load_html('<form><div contenteditable="true"></div><div data-composer-attachments>'
            '<div role="button" aria-label="ref.png"><img width="50" height="50"></div></div>'
            '<span role="progressbar" aria-label="Processing">Processing</span></form>')
        observed = runner._reference_upload_observation(self.args, self.cwd, ["/refs/ref.png"])
        self.assertFalse(observed["ready"])
        self.assertIsNone(observed["pending_controls"][0]["attachmentFilename"])
        self.assertFalse(observed["attachment_details"][0]["pending"])

    def test_pre_submit_replacement_uses_a_new_background_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job = {"job_name": "replace", "prompt_card": "prompt.md", "download_dir": temporary}
            runner._write_session_patch(job, {"submission_committed": False})
            self.args._health_job = job
            self.args._browser_session = self.browser
            self.args._owned_tab_cdp_target_id = self.browser.target_id
            previous = self.browser.target_id
            try:
                runner._replace_unsubmitted_tab(self.args, self.cwd, "about:blank")
                self.assertNotEqual(self.args._owned_tab_cdp_target_id, previous)
                self.assertEqual(self.args._owned_tab_cdp_target_id, self.browser.target_id)
                self.assertEqual(runner._current_url(self.args, self.cwd), "about:blank")
                self.assertIsNone(runner.cdp_target(self.browser.port, previous))
                saved = json.loads(runner._session_path(job).read_text())
                self.assertFalse(saved["submission_committed"])
            finally:
                del self.args._health_job

    def test_reference_upload_selects_enabled_non_camera_input(self) -> None:
        self.load_html(
            '<form><div contenteditable="true"></div>'
            '<input type="file" capture="environment" aria-label="Take a photo">'
            '<input type="file" disabled aria-label="Unavailable">'
            '<input type="file" multiple aria-label="Attach files"></form>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.png"
            reference.write_bytes(b"test upload selection")
            runner._upload_files(self.args, self.cwd, [str(reference)])
            result = runner._eval_json(
                self.args, self.cwd,
                "JSON.stringify({names:Array.from(document.querySelectorAll('input')).map(el=>"
                "Array.from(el.files).map(file=>file.name))})",
            )
        self.assertEqual(result["names"], [[], [], ["reference.png"]])

    def test_ordered_selections_overlap_uploads_and_wait_for_all(self) -> None:
        self.load_html(
            '<form><div contenteditable="true"></div>'
            '<input type="file" multiple><div data-composer-attachments></div></form>'
        )
        runner._eval_js(self.args, self.cwd, """
window.selectionPendingCounts = [];
document.querySelector('input').onchange = (event) => {
  window.selectionPendingCounts.push(document.querySelectorAll('[role=progressbar]').length);
  const card = document.createElement('div');
  card.setAttribute('role', 'button');
  card.setAttribute('aria-label', event.target.files[0].name);
  card.innerHTML = '<img width="100" height="100"><span role="progressbar">Uploading</span>';
  document.querySelector('[data-composer-attachments]').append(card);
  setTimeout(() => card.querySelector('[role=progressbar]').remove(), 3000);
};
""")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(runner, "_page_access_state", return_value={}),
        ):
            references = [str(Path(temporary) / name) for name in ['third.png', 'first.png', 'second.png']]
            for path in references:
                Path(path).write_bytes(b'test attachment')
            observation = runner._upload_references_in_order(self.args, self.cwd, references)
        counts = runner._eval_json(
            self.args, self.cwd, 'JSON.stringify({counts: window.selectionPendingCounts})',
        )
        self.assertEqual(counts["counts"], [0, 1, 2])
        self.assertEqual(observation['attachment_filenames'], ['third.png', 'first.png', 'second.png'])
        self.assertEqual(observation['pending_attachment_count'], 0)
        self.assertTrue(observation['ready'])
        self.assertTrue(observation['order_verified'])

    def test_sidebar_history_error_does_not_fail_or_retry_a_healthy_conversation(self) -> None:
        self.load_html(
            '<div id="app"><main><div data-turn-key="current">'
            '<div data-user-message-bubble>Review these images</div>'
            '<div><h4 data-conversation-role="assistant">ChatGPT said:</h4>Response complete</div>'
            '</div><form><div contenteditable="true"></div></form></main>'
            '<nav aria-label="Home"><section><div role="alert">Unable to load history</div>'
            '<button onclick="document.body.dataset.retried=1">Retry</button></section></nav></div>'
        )
        args = argparse.Namespace(**vars(self.args), _submitted_prompt="Review these images")
        state = runner._page_access_state(args, self.cwd)
        self.assertTrue(state['hasPrompt'])
        self.assertNotEqual(state['crashReason'], 'chatgpt_transient_page_error')
        self.assertEqual(state['transientPageErrorText'], '')
        health = runner._generation_page_health(args, self.cwd, None, runner._page_text(args, self.cwd))
        self.assertNotIn('generation_error', health)
        self.assertEqual(health['retry_control_labels'], [])
        self.assertFalse(runner._click_page_error_retry(args, self.cwd))
        self.assertFalse(runner._click_try_again(args, self.cwd))
        self.assertNotEqual(runner._eval_js(args, self.cwd, "document.body.dataset.retried || ''"), '1')

    def test_page_retry_ignores_history_and_current_message_buttons(self) -> None:
        self.load_html(
            '<div id="history"><p>Something went wrong</p><button onclick="document.body.dataset.wrong=1">Retry</button></div>'
            '<div data-turn-key="assistant"><p>Something went wrong</p>'
            '<button onclick="document.body.dataset.wrong=1">Try again</button></div>'
            '<div role="alert"><p>Failed to load subscription</p>'
            '<button onclick="document.body.dataset.recovered=1">Try again</button></div>'
        )
        self.assertTrue(runner._click_page_error_retry(self.args, self.cwd))
        result = runner._eval_json(self.args, self.cwd, 'JSON.stringify({...document.body.dataset})')
        self.assertEqual(result.get("recovered"), "1")
        self.assertNotIn("wrong", result)
        runner._eval_js(self.args, self.cwd, "document.querySelector('[role=alert]').remove()")
        self.assertFalse(runner._click_page_error_retry(self.args, self.cwd))

    def test_page_recovery_clicks_retry_or_reloads_in_real_chrome(self) -> None:
        healthy = (
            '<div role="group" aria-label="Composer mode">'
            '<button aria-pressed="true">Chat</button><button aria-pressed="false">Work</button></div>'
            '<form><div contenteditable="true">Ask ChatGPT</div></form>'
        )
        requests = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                requests[handler.path] = requests.get(handler.path, 0) + 1
                if handler.path == '/retry':
                    html = '<div role="alert">Network error <button onclick="document.body.innerHTML=' + json.dumps(healthy).replace('"', '&quot;') + '">Try again</button></div>'
                elif requests[handler.path] == 1:
                    html = '<div role="alert">Failed to load subscription</div>'
                else:
                    html = healthy
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/html; charset=utf-8')
                handler.end_headers()
                handler.wfile.write(html.encode())

            def log_message(handler, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, method in [('/retry', 'page_error_retry'), ('/refresh', 'navigate_owned_tab')]:
                with self.subTest(path=path):
                    url = f'http://127.0.0.1:{server.server_port}{path}'
                    self.assertTrue(runner._navigate_owned_cdp_tab(self.args, url))
                    result = runner._recover_chatgpt_page(
                        self.args, self.cwd, url,
                        phase='pre_submit_prepare', attempt=1, reason='injected load failure',
                    )
                    self.assertEqual(result['method'], method)
                    self.assertTrue(runner._ensure_chat_surface(self.args, self.cwd)['verified'])
            self.assertGreaterEqual(requests['/refresh'], 2)
        finally:
            runner._navigate_owned_cdp_tab(self.args, 'about:blank')
            server.shutdown()
            server.server_close()
            thread.join()

    def test_manual_verification_after_send_keeps_original_page_and_single_submission(self) -> None:
        self.load_html(
            '<div role="group" aria-label="Composer mode"><button aria-pressed="true">Chat</button>'
            '<button aria-pressed="false">Work</button></div>'
            '<form onsubmit="return false"><div id="prompt-textarea" role="textbox" contenteditable="true">draw a fox</div>'
            '<button type="button" data-testid="send-button">Send</button></form>'
            '<div id="verification" style="display:none">Verify you are human</div>'
        )
        runner._eval_js(self.args, self.cwd, """
window.submissionCount = 0;
document.querySelector('[data-testid=send-button]').onclick = () => {
  window.submissionCount++;
  document.querySelector('#verification').style.display = 'block';
  document.querySelector('#prompt-textarea').textContent = '';
  // Test fixture simulates an external human finishing verification; runner never clicks it.
  setTimeout(() => document.querySelector('#verification').remove(), 1200);
};
""")
        actual_access = runner._page_access_state
        verification_observations = []

        def access(args, cwd):
            state = actual_access(args, cwd)
            # This fixture uses about:blank; retain real DOM verification detection.
            if state.get('crashReason') == 'unexpected_blank_page':
                state['crashReason'] = ''
            verification_observations.append(bool(state.get('humanReason')))
            return state

        with tempfile.TemporaryDirectory() as temporary:
            queue_path = Path(temporary) / 'queue.json'
            args = argparse.Namespace(
                _owned_tab_cdp_url=self.browser.target_url,
                _owned_tab_cdp_target_id=self.browser.target_id,
                _health_job={'job_name': 'injected-challenge', 'prompt_card': 'prompt.md', 'download_dir': temporary},
                _queue_path=queue_path, _job_id='injected-challenge', _queue_owner='test-runner',
                _original_keep_browser_open=False, human_wait_timeout=30,
            )
            with (
                mock.patch.object(runner, '_page_access_state', side_effect=access),
                mock.patch.object(runner, 'show_browser_without_focus', return_value={'status': 'visible_in_background'}),
                mock.patch.object(runner, 'configured_browser', return_value={}),
                mock.patch.object(runner, '_cdp_launch_port', return_value=1),
                mock.patch.object(runner, '_current_url', return_value='https://chatgpt.com/c/injected-original'),
                mock.patch.object(runner, '_open_conversation') as navigate,
            ):
                runner._submit_prompt(args, self.cwd, 'draw a fox', timeout_s=30)
            count = runner._eval_json(args, self.cwd, 'JSON.stringify({count: window.submissionCount})')
            self.assertEqual(count['count'], 1)
            self.assertIn(True, verification_observations)
            self.assertEqual(verification_observations[-2:], [False, False])
            self.assertEqual(runner.SubmissionCircuit(queue_path).read()['state'], 'running')
            self.assertTrue(runner._ensure_chat_surface(args, self.cwd)['verified'])
            navigate.assert_not_called()
            self.assertEqual(args._owned_tab_cdp_target_id, self.browser.target_id)

    def test_failed_browser_check_with_reload_is_not_an_active_verification(self) -> None:
        self.load_html(
            '<div role="alert"><h2>We couldn’t load your account</h2>'
            '<p>The browser check couldn’t finish. Reload this page to try again</p>'
            '<button onclick="document.body.dataset.reloadClicked=1">Reload</button></div>'
        )
        state = runner._page_access_state(self.args, self.cwd)
        self.assertFalse(state['humanReason'])
        self.assertEqual(state['crashReason'], 'chatgpt_transient_page_error')
        self.assertTrue(runner._click_page_error_retry(self.args, self.cwd))
        clicked = runner._eval_json(self.args, self.cwd, 'JSON.stringify({...document.body.dataset})')
        self.assertEqual(clicked['reloadClicked'], '1')

    def test_cloudflare_response_error_uses_original_turn_retry_after_long_prompt(self) -> None:
        prompt = 'Long prompt ' * 800
        self.load_html(
            '<div data-turn-key="current"><div data-user-message-bubble>' + prompt + '</div></div>'
            '<div role="alert">cloudflare_challenge'
            '<button onclick="document.body.dataset.cloudflareRetried=1">Retry</button></div>'
            '<form><div contenteditable="true">Ask ChatGPT</div></form>'
        )
        args = argparse.Namespace(**vars(self.args), _submitted_prompt=prompt)
        state = runner._page_access_state(args, self.cwd)
        self.assertEqual(state["humanReason"], "")
        self.assertEqual(state['responseError']['matched_marker'], 'cloudflare_challenge')
        heartbeat = runner._heartbeat_probe(args, self.cwd)
        self.assertEqual(heartbeat['responseError']['matched_marker'], 'cloudflare_challenge')
        health = runner._generation_page_health(args, self.cwd, None, runner._page_text(args, self.cwd))
        self.assertEqual(health["status"], "generation_error")
        self.assertEqual(health['generation_error']['matched_marker'], 'cloudflare_challenge')
        with mock.patch.object(runner, '_activate_for_human_attention', side_effect=AssertionError('No human gate')):
            self.assertTrue(runner._click_try_again(args, self.cwd))
        self.assertEqual(runner._eval_js(args, self.cwd, "document.body.dataset.cloudflareRetried || ''"), "1")

    def test_real_challenge_overrides_response_error_and_never_clicks_retry(self) -> None:
        runner._eval_js(self.args, self.cwd, "delete document.body.dataset.retried")
        self.load_html('<section><div data-message-author-role="user">Draw a fox</div>'
            '<div role="alert">cloudflare_challenge<button onclick="document.body.dataset.retried=1">Retry</button></div>'
            '</section><iframe title="Cloudflare Turnstile" src="about:blank"></iframe>'
            '<div contenteditable="true">Ask ChatGPT</div>')
        state = runner._page_access_state(self.args, self.cwd)
        self.assertEqual(state['humanReason'], 'anti_automation_verification')
        health = runner._generation_page_health(self.args, self.cwd, None, '')
        self.assertEqual(health['status'], 'needs_ops')
        with (
            mock.patch.object(runner, "_activate_for_human_attention",
                              side_effect=runner.HumanAttentionRequired("anti_automation_verification", {})),
            self.assertRaises(runner.HumanAttentionRequired),
        ):
            runner._click_try_again(self.args, self.cwd)
        self.assertNotEqual(runner._eval_js(self.args, self.cwd, "document.body.dataset.retried || ''"), "1")

    def test_cloudflare_recovery_stops_at_retry_budget_without_resending_prompt(self) -> None:
        self.load_html('<div data-turn-key="current"><div data-user-message-bubble>Draw a fox</div></div>'
            '<div role="alert">cloudflare_challenge<button onclick="document.body.dataset.retries = '
            '(Number(document.body.dataset.retries) || 0) + 1">Retry</button></div>'
            '<div contenteditable="true">Ask ChatGPT</div>')
        runner._eval_js(self.args, self.cwd, "delete document.body.dataset.retries")
        args = argparse.Namespace(**vars(self.args), _submitted_prompt='Draw a fox', timeout=30, progress_interval=1)
        url = 'https://chatgpt.com/c/fixture'
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_name': 'cloudflare', 'prompt_card': 'fixture.md', 'download_dir': temporary, 'variant_count': 1}
            runner._write_session_patch(job, {'submission_committed': 'unknown', 'conversation_url': url})
            with (
                mock.patch.object(runner, '_ensure_expected_conversation', return_value={'current_url': url}),
                mock.patch.object(runner, '_persist_canonical_conversation', return_value=url),
                mock.patch.object(runner, '_wait_for_conversation_url', return_value=url),
                mock.patch.object(runner, '_screenshot'),
                mock.patch.object(runner, '_submit_prompt', side_effect=AssertionError('Never replay Send')),
                mock.patch.object(runner, '_activate_for_human_attention', side_effect=AssertionError('No human gate')),
            ):
                result = runner._collect_generated_images_with_retries(args, job, self.cwd, Path(temporary),
                    set(), [], label='batch', mode='single_batch_submit', resumed=True, max_failure_retries=2,
                    expected_conversation_url=url, baseline_user_message_count=0)
            self.assertEqual(result['status'], 'generation_failed')
            self.assertEqual(result['error_type'], 'chatgpt_response_service_error')
            self.assertEqual(len(result['failure_retries']), 3)
            self.assertEqual(runner._read_json_if_exists(runner._session_path(job))['submission_committed'], 'unknown')
        self.assertEqual(runner._eval_js(args, self.cwd, "document.body.dataset.retries"), '2')

    def test_challenge_mentions_and_hidden_frames_do_not_block_current_request(self) -> None:
        self.load_html('<div data-turn-key="old"><div data-user-message-bubble>Old request</div>'
            '<div role="alert">cloudflare_challenge<button>Retry</button></div></div>'
            '<div data-turn-key="current"><div data-user-message-bubble>'
            'Explain cloudflare_challenge and verify you are human</div></div>'
            '<iframe title="Cloudflare Turnstile" style="display:none" src="about:blank"></iframe>'
            '<div contenteditable="true">Ask ChatGPT</div>')
        args = argparse.Namespace(**vars(self.args), _submitted_prompt='Explain cloudflare_challenge and verify you are human')
        state = runner._page_access_state(args, self.cwd)
        self.assertEqual(state['humanReason'], '')
        self.assertFalse(state.get('responseError'))
        health = runner._generation_page_health(args, self.cwd, None, '')
        self.assertEqual(health['human_reason'], '')
        self.assertNotIn('generation_error', health)

    def test_conversation_counts_health_and_current_turn_images(self) -> None:
        source = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "width='512' height='512'%3E%3C/svg%3E"
        )
        for layout in ("legacy", "current", "overlap"):
            with self.subTest(layout=layout):
                turns = []
                for index in (1, 2):
                    user_role = 'data-message-author-role="user"' if layout != "current" else ""
                    assistant_role = 'data-message-author-role="assistant"' if layout != "current" else ""
                    current = layout != "legacy"
                    user = (
                        f'<div {user_role} {"data-user-message-bubble" if current else ""}>'
                        f'Prompt {index}<img src="{source}#{index}-reference" alt="Reference"></div>'
                    )
                    assistant = (
                        f'<div {assistant_role}>'
                        f'<h4 {"data-conversation-role=assistant" if current else ""}>ChatGPT said:</h4>'
                        f'{"Something went wrong" if index == 1 else "Current image"}'
                        f'<img src="{source}#{index}-generated" alt="Generated image"></div>'
                    )
                    if current:
                        turns.append(f'<div data-turn-key="{index}">{user}{assistant}</div>')
                    else:
                        turns.extend([
                            f'<section data-testid="conversation-turn-{index}-user">{user}</section>',
                            f'<section data-testid="conversation-turn-{index}-assistant">{assistant}</section>',
                        ])
                self.load_html(
                    "".join(turns)
                    + f'<form><div contenteditable="true"></div><img src="{source}#composer"></form>'
                    + f'<nav><img src="{source}#mascot"></nav>'
                )
                runner._eval_js(
                    self.args,
                    self.cwd,
                    "Promise.all(Array.from(document.images).map(img => img.decode())).then(() => 'loaded')",
                )
                self.assertEqual(
                    runner._conversation_message_counts(self.args, self.cwd),
                    {"user_message_count": 2, "assistant_message_count": 2},
                )
                health = runner._generation_page_health(self.args, self.cwd, None, "")
                self.assertEqual(health["user_message_count"], 2)
                self.assertEqual(health["assistant_message_count"], 2)
                self.assertNotIn("generation_error", health)
                self.assertIn("Current image", health["latest_turn_excerpt"])
                self.assertNotIn("Something went wrong", health["latest_turn_excerpt"])
                candidates = runner._generated_image_candidates(
                    runner._image_inventory(self.args, self.cwd),
                    set(),
                    baseline_user_message_count=1,
                )
                self.assertEqual(len(candidates), 1)
                self.assertTrue(candidates[0]["src"].endswith("#2-generated"))
                self.assertEqual(candidates[0]["role"], "assistant")
                self.assertEqual(candidates[0]["messageUserTurn"], 2)

    def test_navigation_image_is_not_a_generated_candidate_during_loading(self) -> None:
        self.load_html(
            '<nav><img src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' '
            'width=\'512\' height=\'512\'%3E%3C/svg%3E"></nav>'
        )
        runner._eval_js(
            self.args,
            self.cwd,
            "Promise.all(Array.from(document.images).map(img => img.decode())).then(() => 'loaded')",
        )
        self.assertEqual(
            runner._generated_image_candidates(runner._image_inventory(self.args, self.cwd), set()),
            [],
        )

    def test_submitted_prompt_owns_images_despite_virtualized_message_count(self) -> None:
        source = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='512' height='512'%3E%3C/svg%3E"
        self.load_html(
            '<div data-turn-key="old"><div data-message-author-role="user">Old prompt</div>'
            '<div data-message-author-role="assistant">Something went wrong'
            '<button>Try again</button><img alt="Generated image" src="' + source + '#old"></div></div>'
            '<div data-turn-key="current"><div data-user-message-bubble>Create image <p>Revise page</p><p>Remove boxes</p></div>'
            '<div><h4 data-conversation-role="assistant">ChatGPT said:</h4>Adding final touches 90%'
            '<img alt="Generated image" src="' + source + '#new"></div></div>'
            '<aside><button>Try again</button></aside><form><div contenteditable="true"></div></form>'
        )
        args = argparse.Namespace(**vars(self.args), _submitted_prompt="Revise page\n\nRemove boxes")
        runner._eval_js(args, self.cwd, "Promise.all(Array.from(document.images).map(img => img.decode())).then(() => 'loaded')")
        health = runner._generation_page_health(args, self.cwd, None, "Something went wrong Try again")
        self.assertEqual(health["user_message_count"], 2)
        self.assertTrue(health["submitted_prompt_present"])
        self.assertEqual(health["submitted_turn_key"], "current")
        self.assertNotIn("generation_error", health)
        self.assertIsNone(runner._missing_submitted_turn_error(
            health, baseline_user_message_count=4, candidate_count=0, consecutive_heartbeats=2,
        ))
        candidates = runner._generated_image_candidates(runner._image_inventory(args, self.cwd), set(), 4)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["src"].endswith("#new"))
        self.assertTrue(candidates[0]["inSubmittedTurn"])
        self.assertFalse(runner._click_try_again(args, self.cwd))

    def test_current_turn_retry_is_scoped_to_submitted_prompt(self) -> None:
        self.load_html(
            '<div data-turn-key="old"><div data-user-message-bubble>Old</div>'
            '<button onclick="document.body.dataset.wrong=1">Retry</button></div>'
            '<div data-turn-key="current"><div data-user-message-bubble>Current</div>'
            '<div><h4 data-conversation-role="assistant">ChatGPT said:</h4>'
            '<button onclick="document.body.dataset.correct=1">Retry</button></div></div>'
        )
        args = argparse.Namespace(**vars(self.args), _submitted_prompt="Current")
        health = runner._generation_page_health(args, self.cwd, None, "")
        self.assertEqual(health["generation_error"]["matched_marker"], "Retry")
        with mock.patch.object(runner, "_wait_ms"):
            self.assertTrue(runner._click_try_again(args, self.cwd))
        state = runner._eval_json(args, self.cwd, 'JSON.stringify({...document.body.dataset})')
        self.assertEqual(state.get("correct"), "1")
        self.assertNotIn("wrong", state)

    def test_duplicate_prompt_nodes_in_one_turn_do_not_hide_its_image(self) -> None:
        source = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='512' height='512'%3E%3C/svg%3E"
        current = (
            '<div data-turn-key="current"><div data-message-author-role="user">Draw a brace</div>'
            '<div data-user-message-bubble>Draw a brace</div>'
            '<div><h4 data-conversation-role="assistant">ChatGPT said:</h4>'
            '<img alt="Generated image" src="' + source + '"></div></div>'
        )
        self.load_html(current + '<section role="alert">Couldn’t check Lockdown mode'
                       '<button onclick="document.body.dataset.retried=1">Retry</button></section>')
        args = argparse.Namespace(**vars(self.args), _submitted_prompt="Draw a brace")
        runner._eval_js(args, self.cwd, "Promise.all(Array.from(document.images).map(img => img.decode())).then(() => 'loaded')")
        health = runner._generation_page_health(args, self.cwd, None, runner._page_text(args, self.cwd))
        self.assertTrue(health['submitted_prompt_present'])
        self.assertNotIn('generation_error', health)
        self.assertEqual(health['retry_control_labels'], [])
        candidates = runner._generated_image_candidates(runner._image_inventory(args, self.cwd), set(), 0)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(runner._click_try_again(args, self.cwd))
        self.assertNotEqual(runner._eval_js(args, self.cwd, "document.body.dataset.retried || ''"), '1')
        self.load_html(current + '<div data-turn-key="another"><div data-user-message-bubble>Draw a brace</div></div>')
        health = runner._generation_page_health(args, self.cwd, None, '')
        self.assertFalse(health['submitted_prompt_present'])
        self.assertEqual(runner._generated_image_candidates(runner._image_inventory(args, self.cwd), set(), 0), [])

    def test_canonical_url_matches_prompt_across_rendered_blocks(self) -> None:
        self.load_html(
            '<div data-message-author-role="user"><p>Create a fox</p><p>Beside a tree</p></div>'
        )
        args = argparse.Namespace(
            _owned_tab_cdp_url=self.browser.target_url,
            _submitted_prompt="Create a fox\nBeside a tree",
        )
        with mock.patch.object(runner, "_current_url", return_value="https://chatgpt.com/c/canonical"):
            event = runner._ensure_expected_conversation(
                args, self.cwd, "https://chatgpt.com/c/local-chatgpt%3Atest", 0,
            )
        self.assertTrue(event["canonicalized"])


if __name__ == "__main__":
    unittest.main()
