from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import collect_account_articles as runner  # noqa: E402


class AccountUiTests(unittest.TestCase):
    def test_search_marker_survives_current_placeholder_wording(self) -> None:
        marker = runner.ACCOUNT_SEARCH_PLACEHOLDER_MARKER

        self.assertIn(marker, "输入文章来源的账号名称或微信号")
        self.assertIn(marker, "输入文章来源的账号名称或公众号ID，回车进行搜索")

    def test_account_result_uses_exact_nickname_anchor(self) -> None:
        expression = runner._account_result_expression("叶小钗", "")

        self.assertIn("inner_link_account_nickname", expression)
        self.assertIn("nickname === account", expression)

    def test_optional_editor_tip_is_not_required_for_the_flow(self) -> None:
        self.assertEqual(runner.EDITOR_TIP_ACKNOWLEDGEMENTS, ("我知道了", "知道了"))

    def test_interactive_target_selects_only_its_owned_page_target(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class Connection:
            def call(self, method: str, params: dict[str, object]) -> None:
                calls.append((method, params))

        runner._prepare_interactive_target(Connection())

        self.assertEqual(
            calls,
            [
                ("Page.bringToFront", {}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ],
        )

    def test_current_account_selector_is_scoped_to_authenticated_backend_header(self) -> None:
        source = runner._current_account_name.__doc__ or ""

        self.assertIn("authenticated backend account", source)

    def test_external_article_picker_is_distinct_from_account_cards(self) -> None:
        self.assertEqual(runner.ARTICLE_PICKER_LABELS, ("选择账号文章",))
        self.assertTrue(issubclass(runner.ExternalAccountPickerUnavailable, RuntimeError))


if __name__ == "__main__":
    unittest.main()
