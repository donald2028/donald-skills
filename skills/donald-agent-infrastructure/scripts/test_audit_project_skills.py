"""Regression tests for project skill layout and dependency auditing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


AUDITOR = Path(__file__).with_name("audit_project_skills.py")


class SkillAuditTests(unittest.TestCase):
    def write_skill(self, root: Path, relative: str, body: str = "# Skill\n") -> Path:
        skill = root / relative
        skill.mkdir(parents=True)
        name = skill.name
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 在需要测试时使用这个工作流。\n---\n\n{body}",
            encoding="utf-8",
        )
        return skill

    def audit(self, root: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [sys.executable, str(AUDITOR), "--skills-root", str(root), *args],
            capture_output=True,
            text=True,
        )
        return result, json.loads(result.stdout)

    def test_flat_and_categorized_layout_reports(self):
        for relative, expected, categories in (
            ("one", "flat", []),
            ("collection/one", "categorized", ["collection"]),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "skills"
                self.write_skill(root, relative)
                result, report = self.audit(root, "--strict")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(report["layout"], expected)
                self.assertEqual(report["categories"], categories)
                self.assertEqual(report["dependency_graph"], {"one": []})
                self.assertEqual(report["warnings"], [])

    def test_mixed_and_invalid_depth_fail_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            self.write_skill(root, "flat-skill")
            self.write_skill(root, "collection/nested-skill")
            self.write_skill(root, "too/deep/third-skill")
            result, report = self.audit(root, "--strict")
            self.assertNotEqual(result.returncode, 0)
            rules = {issue["rule"] for issue in report["errors"]}
            self.assertIn("layout.mixed", rules)
            self.assertIn("layout.depth", rules)
            self.assertEqual(report["layout"], "invalid")

    def test_expected_layout_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            self.write_skill(root, "one")
            result, report = self.audit(root, "--layout", "categorized", "--strict")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("layout.expected", {issue["rule"] for issue in report["errors"]})
            self.assertEqual(report["expected_layout"], "categorized")

    def test_invalid_category_and_duplicate_global_name_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            self.write_skill(root, "collection/shared")
            self.write_skill(root, "review/shared")
            self.write_skill(root, "Bad_Category/other")
            result, report = self.audit(root, "--strict")
            self.assertNotEqual(result.returncode, 0)
            rules = {issue["rule"] for issue in report["errors"]}
            self.assertIn("duplicate_name", rules)
            self.assertIn("layout.category_name", rules)

    def test_frontmatter_closing_and_escaping_link_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            broken = root / "broken"
            broken.mkdir(parents=True)
            (broken / "SKILL.md").write_text(
                "---\nname: broken\ndescription: Broken\n# no closing delimiter\n",
                encoding="utf-8",
            )
            linked = self.write_skill(root, "linked", "[escape](../broken/SKILL.md)\n")
            self.assertTrue(linked.is_dir())
            result, report = self.audit(root, "--strict")
            self.assertNotEqual(result.returncode, 0)
            rules = {issue["rule"] for issue in report["errors"]}
            self.assertIn("frontmatter.closing", rules)
            self.assertIn("links.escape", rules)

    def test_missing_self_and_cycle_dependencies_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            self.write_skill(root, "one", "**REQUIRED SUB-SKILL:** Invoke two\n")
            self.write_skill(
                root,
                "two",
                "**REQUIRED SUB-SKILL:** Invoke one\n\n**REQUIRED SUB-SKILL:** Invoke missing\n",
            )
            self.write_skill(root, "selfish", "**REQUIRED SUB-SKILL:** Invoke selfish\n")
            result, report = self.audit(root, "--strict")
            self.assertNotEqual(result.returncode, 0)
            rules = {issue["rule"] for issue in report["errors"]}
            self.assertIn("dependency.missing", rules)
            self.assertIn("dependency.self", rules)
            self.assertIn("dependency.cycle", rules)

    def test_mirror_manifest_summary_and_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            root = repo / "skills"
            self.write_skill(root, "one")
            state = repo / ".agent-infra"
            state.mkdir()
            manifest = state / "runtime-skill-mirrors.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mirrors": [
                            {"target": ".agents/skills/one", "source": "skills/one", "mode": "junction"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result, report = self.audit(root, "--strict")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(report["mirror_state"]["entries"], 1)
            self.assertEqual(report["mirror_state"]["modes"], ["junction"])
            manifest.write_text("{not-json", encoding="utf-8")
            result, report = self.audit(root, "--strict")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mirror.manifest", {issue["rule"] for issue in report["errors"]})


if __name__ == "__main__":
    unittest.main()
