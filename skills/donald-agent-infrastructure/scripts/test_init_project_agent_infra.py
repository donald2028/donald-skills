"""Exercise generated project infrastructure in disposable repositories."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

INITIALIZER = Path(__file__).with_name("init_project_agent_infra.py")
MIGRATOR = Path(__file__).with_name("migrate_legacy_runtime_skills.py")
RUNTIME_SKILL_ROOTS = (".claude/skills", ".agents/skills", ".codebuddy/skills", ".workbuddy/skills")
try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


class RuntimeScaffoldTests(unittest.TestCase):
    def run_script(self, script: Path, *args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=cwd,
            check=False,
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

    def create_directory_link(self, target: Path, source: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return "junction"
        target.symlink_to(source, target_is_directory=True)
        return "symlink"

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
                            self.assertFalse(
                                (repo / "scripts" / "agent-skills" / "sync_runtime_skills.py").exists()
                            )
                            for runtime_root in RUNTIME_SKILL_ROOTS:
                                self.assertFalse((repo / runtime_root).exists())
                            self.assertEqual((repo / "skills" / "README.md").exists(), layout == "categorized")
                            contract = (repo / "AGENTS.md").read_text(encoding="utf-8")
                            self.assertIn("not a runtime discovery path", contract)
                            self.assertNotIn("sync_runtime_skills.py", contract)
                            self.assertEqual("## Entry Routing" in contract, entry)
                            self.assertEqual("## Skill Governance" in contract, governance)
                            self.assertEqual("## Project Subagents" in contract, subagents)
                            gitignore = repo / ".gitignore"
                            ignored = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
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
                self.assertFalse((repo / "scripts" / "agent-skills" / "sync_runtime_skills.py").exists())
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

            existing = ("AGENTS.md", "CODEBUDDY.md")
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
            self.assertNotIn("# BEGIN donald-agent-infrastructure generated", gitignore)
            self.assertNotIn(".workbuddy/skills/", gitignore)
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

    def test_explicit_native_skills_root_is_the_only_source(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.initialize(
                repo,
                "--skills-root",
                ".agents/skills",
                "--layout",
                "categorized",
                "--with-entry",
                "--with-governance",
                "--with-subagents",
            )
            source = repo / ".agents" / "skills"
            self.assertTrue((source / "application" / "enter-project" / "SKILL.md").is_file())
            self.assertTrue(
                (source / "development" / "review-skill-best-practices" / "SKILL.md").is_file()
            )
            self.assertFalse((repo / "skills").exists())
            for runtime_root in (".claude/skills", ".codebuddy/skills", ".workbuddy/skills"):
                self.assertFalse((repo / runtime_root).exists())
            contract = (repo / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("explicitly selected repository Skill source", contract)
            self.assertNotIn("sync_runtime_skills.py", contract)
            subagent_sync = (repo / "agents" / "sync_agents.py").read_text(encoding="utf-8")
            self.assertIn('REPO / ".agents/skills"', subagent_sync)
            self.assertNotIn("__SKILLS_ROOT__", subagent_sync)

    def test_legacy_migration_removes_generated_links_rules_and_regeneration_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            skill = self.add_skill(repo, "flat")
            entries = []
            for runtime_root in RUNTIME_SKILL_ROOTS:
                target = repo / runtime_root / "example"
                mode = self.create_directory_link(target, skill)
                entries.append(
                    {
                        "target": target.relative_to(repo).as_posix(),
                        "source": skill.relative_to(repo).as_posix(),
                        "mode": mode,
                        "digest": "legacy-link-digest",
                    }
                )
            manifest = repo / ".agent-infra" / "runtime-skill-mirrors.json"
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({"version": 1, "mirrors": entries}), encoding="utf-8")
            sync = repo / "scripts" / "agent-skills" / "sync_runtime_skills.py"
            sync.parent.mkdir(parents=True)
            sync.write_text(
                '"""Generate runtime skill mirrors from canonical root skills/."""\n'
                'MANIFEST = "runtime-skill-mirrors.json"\n'
                'DEFAULT_TARGETS = []\n'
                '# sync_runtime_skills.py\n',
                encoding="utf-8",
            )
            (repo / "AGENTS.md").write_text(
                textwrap.dedent(
                    """\
                    # Agent Instructions

                    ## Project Skills

                    - Treat root `skills/` as the canonical project skill source.
                    - Treat `.claude/skills/`, `.agents/skills/`, `.codebuddy/skills/`, and
                      `.workbuddy/skills/` as generated output.
                    - Kimi Code and OpenCode reuse `.agents/skills/`; CodeBuddy uses `.codebuddy/skills/`;
                      Tencent WorkBuddy uses `.workbuddy/skills/`.

                    ## Repo Boundaries

                    - Do not hand-edit generated runtime mirrors.
                    - After changing canonical skills, run
                      `python scripts/agent-skills/sync_runtime_skills.py` and then its `--check` mode.
                    """
                ),
                encoding="utf-8",
            )
            (repo / ".gitignore").write_text(
                textwrap.dedent(
                    """\
                    custom-rule/

                    # BEGIN donald-agent-infrastructure generated
                    .claude/skills/
                    .agents/skills/
                    .codebuddy/skills/
                    .workbuddy/skills/
                    .agent-infra/
                    # END donald-agent-infrastructure generated
                    """
                ),
                encoding="utf-8",
            )

            preview = self.run_script(MIGRATOR, repo, cwd=repo)
            self.assert_success(preview)
            self.assertIn("would remove .agents", preview.stdout)
            self.assertTrue(sync.is_file())
            self.assertTrue(manifest.is_file())

            applied = self.run_script(MIGRATOR, repo, "--apply", cwd=repo)
            self.assert_success(applied)
            self.assertTrue((skill / "SKILL.md").is_file())
            self.assertFalse(sync.exists())
            self.assertFalse(manifest.exists())
            for runtime_root in RUNTIME_SKILL_ROOTS:
                self.assertFalse((repo / runtime_root).exists())
            contract = (repo / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Do not register or mirror it", contract)
            self.assertNotIn("sync_runtime_skills.py", contract)
            gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("custom-rule/", gitignore)
            for ignored in RUNTIME_SKILL_ROOTS:
                self.assertNotIn(f"{ignored}/", gitignore)

            self.initialize(repo)
            (skill / "references" / "guide.md").write_text("edited\n", encoding="utf-8")
            for runtime_root in RUNTIME_SKILL_ROOTS:
                self.assertFalse((repo / runtime_root).exists())
            repeated = self.run_script(MIGRATOR, repo, cwd=repo)
            self.assert_success(repeated)
            self.assertIn("No legacy generated runtime Skill infrastructure found", repeated.stdout)

    def test_legacy_migration_preserves_modified_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            skill = self.add_skill(repo, "flat")
            copied = repo / ".agents" / "skills" / "example"
            copied.mkdir(parents=True)
            (copied / "SKILL.md").write_text("user-modified\n", encoding="utf-8")
            manifest = repo / ".agent-infra" / "runtime-skill-mirrors.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mirrors": [
                            {
                                "target": ".agents/skills/example",
                                "source": "skills/example",
                                "mode": "copy",
                                "digest": "different",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_script(MIGRATOR, repo, "--apply", cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("modified runtime path", result.stderr)
            self.assertEqual((copied / "SKILL.md").read_text(encoding="utf-8"), "user-modified\n")
            self.assertTrue((skill / "SKILL.md").is_file())
            self.assertTrue(manifest.is_file())

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
