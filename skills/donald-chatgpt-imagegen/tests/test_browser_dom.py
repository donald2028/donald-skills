"""Opt-in DOM regressions: set DONALD_IMAGEGEN_BROWSER_TESTS=1 to use the bound Chrome lane."""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
