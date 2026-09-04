"""Exercise generated runtime adapters and mirrors in disposable projects."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


INITIALIZER = Path(__file__).with_name("init_project_agent_infra.py")
TARGETS = (".claude", ".agents", ".codebuddy")


class RuntimeScaffoldTests(unittest.TestCase):
    def run_script(self, script, *args, cwd):
        return subprocess.run(
            [sys.executable, str(script), *map(str, args)],
            cwd=cwd, capture_output=True, text=True,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_profiles_share_complete_skills_across_runtimes(self):
        for profile in ("minimal", "categorized", "pipeline", "subagents"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                self.assert_success(self.run_script(
                    INITIALIZER, repo, "--profile", profile, cwd=repo,
                ))
                for adapter in ("CLAUDE.md", "CODEBUDDY.md"):
                    self.assertIn("`AGENTS.md`", (repo / adapter).read_text())
                skill = repo / "skills" / "collection" / "example"
                (skill / "references").mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "---\nname: example\ndescription: Example workflow.\n---\n"
                )
                reference = skill / "references" / "guide.md"
                reference.write_text("original")
                sync = repo / "skills" / "sync_runtime_skills.py"
                self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
                self.assert_success(self.run_script(sync, cwd=repo))
                self.assert_success(self.run_script(sync, "--check", cwd=repo))
                reference.write_text("updated")
                for target in TARGETS:
                    mirror = repo / target / "skills" / "example"
                    self.assertEqual(mirror.resolve(), skill.resolve())
                    self.assertEqual((mirror / "references" / "guide.md").read_text(), "updated")
                buddy = repo / ".codebuddy" / "skills" / "example"
                buddy.unlink()
                self.assertNotEqual(self.run_script(sync, "--check", cwd=repo).returncode, 0)
                self.assert_success(self.run_script(sync, cwd=repo))
                self.assert_success(self.run_script(sync, "--check", cwd=repo))

    def test_dry_run_and_existing_project_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.assert_success(self.run_script(INITIALIZER, repo, "--dry-run", cwd=repo))
            self.assertEqual(list(repo.iterdir()), [])
            existing = ("AGENTS.md", "CODEBUDDY.md", "skills/sync_runtime_skills.py")
            for relative in existing:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("user-owned content")
            self.assert_success(self.run_script(INITIALIZER, repo, cwd=repo))
            for relative in existing:
                self.assertEqual((repo / relative).read_text(), "user-owned content")

    def test_existing_workbuddy_skill_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.assert_success(self.run_script(INITIALIZER, repo, "--profile", "pipeline", cwd=repo))
            existing = repo / ".codebuddy" / "skills" / "enter-project" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("user-owned skill")
            result = self.run_script(repo / "skills/sync_runtime_skills.py", cwd=repo)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exists and is not generated", result.stderr)
            self.assertEqual(existing.read_text(), "user-owned skill")


if __name__ == "__main__":
    unittest.main()
