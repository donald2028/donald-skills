"""Exercise generated project infrastructure in disposable repositories."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
INITIALIZER = Path(__file__).with_name("init_project_agent_infra.py")
MIRROR_TEMPLATE = SKILL_ROOT / "assets" / "templates" / "sync_runtime_skills.py"
TARGETS = (".claude", ".agents", ".codebuddy", ".workbuddy")
try:
    import yaml  # noqa: F401

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


class RuntimeScaffoldTests(unittest.TestCase):
    def run_script(self, script: Path, *args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def initialize(self, repo: Path, *args: object) -> subprocess.CompletedProcess[str]:
        result = self.run_script(INITIALIZER, repo, *args, cwd=repo)
        self.assert_success(result)
        return result

    def add_skill(self, repo: Path, layout: str, name: str = "example") -> Path:
        relative = Path(name) if layout == "flat" else Path("collection") / name
        skill = repo / "skills" / relative
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Example workflow.\n---\n\n# Example\n",
            encoding="utf-8",
        )
        (skill / "references" / "guide.md").write_text("original\n", encoding="utf-8")
        return skill

    def mirror_script(self, repo: Path) -> Path:
        return repo / "scripts" / "agent-skills" / "sync_runtime_skills.py"

    def test_layout_and_feature_switch_matrix(self):
        for layout in ("flat", "categorized"):
            for entry in (False, True):
                for governance in (False, True):
                    for subagents in (False, True):
                        with self.subTest(
                            layout=layout,
                            entry=entry,
                            governance=governance,
                            subagents=subagents,
                        ), tempfile.TemporaryDirectory() as directory:
                            repo = Path(directory)
                            args: list[str] = ["--layout", layout]
                            if entry:
                                args.append("--with-entry")
                            if governance:
                                args.append("--with-governance")
                            if subagents:
                                args.append("--with-subagents")
                            self.initialize(repo, *args)

                            prefix = Path("skills") if layout == "flat" else Path("skills/application")
                            review_prefix = Path("skills") if layout == "flat" else Path("skills/development")
                            self.assertEqual((repo / prefix / "enter-project" / "SKILL.md").exists(), entry)
                            self.assertEqual(
                                (repo / review_prefix / "review-skill-best-practices" / "SKILL.md").exists(),
                                governance,
                            )
                            self.assertEqual((repo / "agents" / "registry.yaml").exists(), subagents)
                            self.assertTrue(self.mirror_script(repo).is_file())
                            self.assertEqual((repo / "skills" / "README.md").exists(), layout == "categorized")
                            contract = (repo / "AGENTS.md").read_text(encoding="utf-8")
                            self.assertEqual("## Entry Routing" in contract, entry)
                            self.assertEqual("## Skill Governance" in contract, governance)
                            self.assertEqual("## Project Subagents" in contract, subagents)
                            ignored = (repo / ".gitignore").read_text(encoding="utf-8")
                            self.assertEqual(".claude/agents/" in ignored, subagents)
                            self.assertEqual(".codebuddy/agents/" in ignored, subagents)
                            self.assertEqual("agents/INDEX.md" in ignored, subagents)

    def test_old_profile_capability_mapping(self):
        mappings = {
            "minimal": ["--layout", "flat"],
            "categorized": ["--layout", "categorized"],
            "pipeline": ["--layout", "categorized", "--with-entry"],
            "subagents": ["--layout", "categorized", "--with-entry", "--with-subagents"],
        }
        for old_name, args in mappings.items():
            with self.subTest(old_name=old_name), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                result = self.initialize(repo, *args)
                self.assertIn(f"layout={args[1]}", result.stdout)
                self.assertTrue((repo / "AGENTS.md").is_file())
                self.assertTrue((repo / "CLAUDE.md").is_file())
                self.assertTrue((repo / "CODEBUDDY.md").is_file())
                self.assertTrue(self.mirror_script(repo).is_file())
                self.assertEqual(
                    (repo / "skills" / "application" / "enter-project" / "SKILL.md").exists(),
                    old_name in {"pipeline", "subagents"},
                )
                self.assertEqual((repo / "agents" / "sync_agents.py").exists(), old_name == "subagents")

    def test_profile_argument_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            result = self.run_script(INITIALIZER, repo, "--profile", "minimal", cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_dry_run_and_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            result = self.run_script(INITIALIZER, repo, "--dry-run", cwd=repo)
            self.assert_success(result)
            self.assertEqual(list(repo.iterdir()), [])

            existing = ("AGENTS.md", "CODEBUDDY.md", "scripts/agent-skills/sync_runtime_skills.py")
            for relative in existing:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("user-owned content", encoding="utf-8")
            (repo / ".gitignore").write_text("custom-rule/\n", encoding="utf-8")
            workbuddy_memory = repo / ".workbuddy" / "memory" / "session.md"
            workbuddy_memory.parent.mkdir(parents=True)
            workbuddy_memory.write_text("user-owned runtime state\n", encoding="utf-8")
            self.initialize(repo)
            for relative in existing:
                self.assertEqual((repo / relative).read_text(encoding="utf-8"), "user-owned content")
            gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("custom-rule/", gitignore)
            self.assertIn("# BEGIN donald-agent-infrastructure generated", gitignore)
            self.assertIn(".workbuddy/skills/", gitignore)
            self.assertNotIn("\n.workbuddy/\n", gitignore)
            self.assertEqual(
                workbuddy_memory.read_text(encoding="utf-8"), "user-owned runtime state\n"
            )

    def test_initializer_rejects_layout_mismatch_and_mixed_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.add_skill(repo, "flat", "flat-skill")
            nested = repo / "skills" / "category" / "nested-skill"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text(
                "---\nname: nested-skill\ndescription: Nested.\n---\n\n# Nested\n",
                encoding="utf-8",
            )
            result = self.run_script(INITIALIZER, repo, "--layout", "flat", cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match --layout flat", result.stderr)
            self.assertFalse((repo / "AGENTS.md").exists())

    def test_runtime_mirrors_default_mode_and_live_content(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo)
            skill = self.add_skill(repo, "flat")
            sync = self.mirror_script(repo)
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            result = self.run_script(sync, cwd=repo)
            self.assert_success(result)
            self.assert_success(self.run_script(sync, "--check", cwd=repo))

            manifest = json.loads(
                (repo / ".agent-infra" / "runtime-skill-mirrors.json").read_text(encoding="utf-8")
            )
            expected_mode = "junction" if sys.platform == "win32" else "symlink"
            self.assertEqual({entry["mode"] for entry in manifest["mirrors"]}, {expected_mode})
            (skill / "references" / "guide.md").write_text("updated\n", encoding="utf-8")
            for target in TARGETS:
                mirror = repo / target / "skills" / "example"
                self.assertEqual(mirror.resolve(), skill.resolve())
                self.assertEqual((mirror / "references" / "guide.md").read_text(encoding="utf-8"), "updated\n")
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, cwd=repo))

    def test_categorized_runtime_mirrors_flatten_by_skill_name(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo, "--layout", "categorized")
            skill = self.add_skill(repo, "categorized")
            sync = self.mirror_script(repo)
            self.assert_success(self.run_script(sync, cwd=repo))
            for target in TARGETS:
                self.assertEqual((repo / target / "skills" / "example").resolve(), skill.resolve())
            self.assert_success(self.run_script(sync, "--check", cwd=repo))

    def test_empty_categorized_layout_remains_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo, "--layout", "categorized")
            sync = self.mirror_script(repo)
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            result = self.run_script(sync, cwd=repo)
            self.assert_success(result)
            self.assertIn("layout=categorized", result.stdout)
            self.assert_success(self.run_script(sync, "--check", cwd=repo))

    def test_generated_sync_rejects_layout_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo, "--layout", "flat")
            self.add_skill(repo, "categorized")
            result = self.run_script(self.mirror_script(repo), cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("configured for flat", result.stderr)

    def test_copy_mode_detects_drift_and_cleans_managed_orphans(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo)
            skill = self.add_skill(repo, "flat")
            sync = self.mirror_script(repo)
            self.assert_success(self.run_script(sync, "--copy", cwd=repo))
            self.assert_success(self.run_script(sync, "--copy", "--check", cwd=repo))
            (skill / "references" / "guide.md").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(self.run_script(sync, "--copy", "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, "--copy", cwd=repo))
            shutil.rmtree(skill)
            self.assertNotEqual(self.run_script(sync, "--copy", "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, "--copy", cwd=repo))
            for target in TARGETS:
                self.assertFalse((repo / target / "skills" / "example").exists())

    def test_default_links_clean_managed_orphans_after_source_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo)
            skill = self.add_skill(repo, "flat")
            sync = self.mirror_script(repo)
            self.assert_success(self.run_script(sync, cwd=repo))
            shutil.rmtree(skill)
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, cwd=repo))
            for target in TARGETS:
                mirror = repo / target / "skills" / "example"
                self.assertFalse(mirror.exists())
                self.assertFalse(mirror.is_symlink())

    def test_unmanaged_conflict_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo)
            self.add_skill(repo, "flat")
            existing = repo / ".codebuddy" / "skills" / "example" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("user-owned skill", encoding="utf-8")
            result = self.run_script(self.mirror_script(repo), cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exists and is not managed", result.stderr)
            self.assertEqual(existing.read_text(encoding="utf-8"), "user-owned skill")
            self.assertFalse((repo / ".claude" / "skills" / "example").exists())
            self.assertFalse((repo / ".agent-infra" / "runtime-skill-mirrors.json").exists())
            self.assert_success(self.run_script(self.mirror_script(repo), "--replace-existing", cwd=repo))
            self.assertNotEqual(existing.read_text(encoding="utf-8"), "user-owned skill")
            self.assert_success(self.run_script(self.mirror_script(repo), "--check", cwd=repo))

    def test_forced_modes_do_not_fallback(self):
        spec = importlib.util.spec_from_file_location("runtime_mirror_template", MIRROR_TEMPLATE)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        calls: list[str] = []

        def fail_junction(path, source):
            calls.append("junction")
            raise OSError("junction unavailable")

        def fail_symlink(path, source):
            calls.append("symlink")
            raise OSError("symlink unavailable")

        def copy(path, source):
            calls.append("copy")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(module.sys, "platform", "win32"), mock.patch.object(
            module, "create_junction", fail_junction
        ), mock.patch.object(module, "create_symlink", fail_symlink), mock.patch.object(module, "create_copy", copy):
            root = Path(directory)
            mode, failures = module.create_mirror(root / "mirror", root / "source", None)
            self.assertEqual(mode, "copy")
            self.assertEqual(calls, ["junction", "symlink", "copy"])
            self.assertEqual(len(failures), 2)
            calls.clear()
            with self.assertRaises(OSError):
                module.create_mirror(root / "forced", root / "source", "junction")
            self.assertEqual(calls, ["junction"])
            calls.clear()
            with self.assertRaises(OSError):
                module.create_mirror(root / "forced-symlink", root / "source", "symlink")
            self.assertEqual(calls, ["symlink"])
            calls.clear()
            mode, failures = module.create_mirror(root / "forced-copy", root / "source", "copy")
            self.assertEqual((mode, failures), ("copy", []))
            self.assertEqual(calls, ["copy"])

    @unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required for generated subagent tests")
    def test_subagents_generate_check_update_and_safe_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo, "--with-subagents")
            skill = self.add_skill(repo, "flat")
            spec = skill / "references" / "reviewer-agent.md"
            spec.write_text("# Reviewer\n", encoding="utf-8")
            registry = repo / "agents" / "registry.yaml"
            registry.write_text(
                textwrap.dedent(
                    """\
                    model_tiers:
                      default:
                        claude: sonnet
                        codex: gpt-5
                        codex_reasoning_effort: low
                        codebuddy: inherit
                        codebuddy_effort: medium
                    agents:
                      - id: reviewer
                        kind: reviewer
                        description: "Review project output: quality and safety."
                        spec: skills/example/references/reviewer-agent.md
                        tier: default
                        role_note: Review only the provided artifact.
                        output: Return a concise review.
                        iron_rule: Do not modify source files.
                        codebuddy_tools: [Read, Grep]
                    """
                ),
                encoding="utf-8",
            )
            sync = repo / "agents" / "sync_agents.py"
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            codebuddy_conflict = repo / ".codebuddy" / "agents" / "reviewer.md"
            codebuddy_conflict.parent.mkdir(parents=True)
            codebuddy_conflict.write_text("user-owned\n", encoding="utf-8")
            conflict_result = self.run_script(sync, cwd=repo)
            self.assertNotEqual(conflict_result.returncode, 0)
            self.assertIn("exists and is not generated", conflict_result.stderr)
            self.assertEqual(codebuddy_conflict.read_text(encoding="utf-8"), "user-owned\n")
            self.assertFalse((repo / ".claude" / "agents" / "reviewer.md").exists())
            codebuddy_conflict.unlink()
            self.assert_success(self.run_script(sync, cwd=repo))
            self.assert_success(self.run_script(sync, "--check", cwd=repo))
            self.assertTrue((repo / ".claude" / "agents" / "reviewer.md").is_file())
            self.assertTrue((repo / ".codex" / "agents" / "reviewer.toml").is_file())
            codebuddy_agent = repo / ".codebuddy" / "agents" / "reviewer.md"
            self.assertTrue(codebuddy_agent.is_file())
            codebuddy_content = codebuddy_agent.read_text(encoding="utf-8")
            self.assertIn('model: "inherit"', codebuddy_content)
            self.assertIn("effort: medium", codebuddy_content)
            self.assertIn("tools: Read, Grep", codebuddy_content)
            codebuddy_frontmatter = codebuddy_content.split("---", 2)[1]
            self.assertEqual(yaml.safe_load(codebuddy_frontmatter)["name"], "reviewer")

            updated_registry = registry.read_text(encoding="utf-8").replace(
                "Review only the provided artifact.", "Review the provided artifact and evidence."
            )
            registry.write_text(updated_registry, encoding="utf-8")
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, cwd=repo))
            self.assertIn(
                "artifact and evidence",
                (repo / ".claude" / "agents" / "reviewer.md").read_text(encoding="utf-8"),
            )

            unknown = repo / ".claude" / "agents" / "user-owned.md"
            unknown.write_text("user-owned\n", encoding="utf-8")
            codebuddy_unknown = repo / ".codebuddy" / "agents" / "user-owned.md"
            codebuddy_unknown.write_text("user-owned\n", encoding="utf-8")
            registry.write_text(
                textwrap.dedent(
                    """\
                    model_tiers:
                      default:
                        claude: sonnet
                        codex: gpt-5
                        codex_reasoning_effort: low
                    agents: []
                    """
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
            self.assert_success(self.run_script(sync, cwd=repo))
            self.assertFalse((repo / ".claude" / "agents" / "reviewer.md").exists())
            self.assertFalse((repo / ".codex" / "agents" / "reviewer.toml").exists())
            self.assertFalse((repo / ".codebuddy" / "agents" / "reviewer.md").exists())
            self.assertTrue(unknown.is_file())
            self.assertTrue(codebuddy_unknown.is_file())
            self.assert_success(self.run_script(sync, "--check", cwd=repo))

    @unittest.skipUnless(YAML_AVAILABLE, "PyYAML is required for generated subagent tests")
    def test_subagent_registry_errors_and_user_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(repo, "--with-subagents")
            skill = self.add_skill(repo, "flat")
            (skill / "references" / "reviewer-agent.md").write_text("# Reviewer\n", encoding="utf-8")
            registry = repo / "agents" / "registry.yaml"
            registry.write_text(
                "model_tiers:\n  default:\n    claude: sonnet\nagents:\n  - id: reviewer\n",
                encoding="utf-8",
            )
            sync = repo / "agents" / "sync_agents.py"
            result = self.run_script(sync, cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing", result.stderr)

            invalid_registries = {
                "duplicate id": """\
model_tiers:
  default:
    claude: sonnet
agents:
  - &reviewer
    id: reviewer
    kind: reviewer
    description: Review.
    spec: skills/example/references/reviewer-agent.md
    tier: default
    role_note: Review.
    output: Report.
    iron_rule: Do not edit.
  - *reviewer
""",
                "unknown tier": """\
model_tiers:
  default:
    claude: sonnet
agents:
  - id: reviewer
    kind: reviewer
    description: Review.
    spec: skills/example/references/reviewer-agent.md
    tier: missing-tier
    role_note: Review.
    output: Report.
    iron_rule: Do not edit.
""",
                "missing skill-owned spec": """\
model_tiers:
  default:
    claude: sonnet
agents:
  - id: reviewer
    kind: reviewer
    description: Review.
    spec: skills/example/references/missing-agent.md
    tier: default
    role_note: Review.
    output: Report.
    iron_rule: Do not edit.
""",
            }
            for expected, content in invalid_registries.items():
                with self.subTest(expected=expected):
                    registry.write_text(textwrap.dedent(content), encoding="utf-8")
                    result = self.run_script(sync, cwd=repo)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

            registry.write_text(
                textwrap.dedent(
                    """\
                    model_tiers:
                      default:
                        claude: sonnet
                    agents: []
                    """
                ),
                encoding="utf-8",
            )
            conflict = repo / "agents" / "INDEX.md"
            conflict.write_text("user-owned\n", encoding="utf-8")
            result = self.run_script(sync, cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exists and is not generated", result.stderr)
            self.assertEqual(conflict.read_text(encoding="utf-8"), "user-owned\n")


if __name__ == "__main__":
    unittest.main()
