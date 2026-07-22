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


if __name__ == "__main__":
    unittest.main()
