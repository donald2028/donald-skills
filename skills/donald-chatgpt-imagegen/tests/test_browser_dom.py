"""Opt-in DOM regressions: set DONALD_IMAGEGEN_BROWSER_TESTS=1 to use the bound Chrome lane."""

from __future__ import annotations

import argparse
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


@unittest.skipUnless(
    os.environ.get("DONALD_IMAGEGEN_BROWSER_TESTS") == "1",
    "requires opt-in and a configured Chrome lane",
)
class BrowserDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.browser = runner.BrowserSession(
            scope="donald-chatgpt-imagegen",
            session="imagegen-dom-tests",
            url="about:blank",
            timeout=30,
            lock_timeout=10,
        ).open()
        cls.addClassCleanup(cls.browser.close)
        cls.args = argparse.Namespace(_owned_tab_cdp_url=cls.browser.target_url)
        cls.cwd = Path.cwd()

    def load_html(self, html: str) -> None:
        runner._eval_js(
            self.args,
            self.cwd,
            "document.body.innerHTML = " + json.dumps(html),
        )

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
        runner._eval_js(self.args, self.cwd, "document.querySelector('[role=progressbar]').remove()")
        observation = runner._reference_upload_observation(self.args, self.cwd, ["/refs/reference.png"])
        self.assertTrue(observation["ready"])
        self.assertEqual(observation["attachment_filenames"], ["reference(2).png"])
        self.assertEqual(observation["blob_image_count"], 0)
        runner._eval_js(self.args, self.cwd, "document.querySelector('[data-composer-attachments]').remove()")
        self.assertFalse(
            runner._reference_upload_observation(self.args, self.cwd, ["/refs/reference.png"])["ready"]
        )

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

    def test_visible_cloudflare_error_without_frame_blocks_retry_after_long_prompt(self) -> None:
        self.load_html(
            '<div data-message-author-role="user">' + 'Long prompt ' * 800 + '</div>'
            '<div data-message-author-role="assistant"><div role="alert">cloudflare_challenge</div>'
            '<button onclick="document.body.dataset.retried=1">Retry</button></div>'
            '<form><div contenteditable="true">Ask ChatGPT</div></form>'
        )
        state = runner._page_access_state(self.args, self.cwd)
        self.assertEqual(state["humanReason"], "anti_automation_verification")
        health = runner._generation_page_health(self.args, self.cwd, None, runner._page_text(self.args, self.cwd))
        self.assertEqual(health["human_reason"], "anti_automation_verification")
        with (
            mock.patch.object(runner, "_activate_for_human_attention",
                              side_effect=runner.HumanAttentionRequired("anti_automation_verification", {})),
            self.assertRaises(runner.HumanAttentionRequired),
        ):
            runner._click_try_again(self.args, self.cwd)
        self.assertNotEqual(runner._eval_js(self.args, self.cwd, "document.body.dataset.retried || ''"), "1")

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
