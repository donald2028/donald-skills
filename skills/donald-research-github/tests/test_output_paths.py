from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import output_paths  # noqa: E402


class OutputPathsTests(unittest.TestCase):
    def test_all_platforms_use_one_native_config_root(self) -> None:
        home = Path("/Users/tester")

        self.assertEqual(
            output_paths.default_config_root(platform_name="darwin", home=home, env={}),
            home / "Library" / "Application Support" / "Donald Skills" / "config",
        )
        self.assertEqual(
            output_paths.default_config_root(
                platform_name="win32",
                home=Path("C:/Users/tester"),
                env={"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
            ),
            Path("C:/Users/tester/AppData/Local/Donald Skills/config"),
        )
        self.assertEqual(
            output_paths.default_config_root(
                platform_name="linux",
                home=home,
                env={"XDG_CONFIG_HOME": "/tmp/config"},
            ),
            Path("/tmp/config/donald-skills"),
        )

    def test_saved_shared_root_is_reused_by_every_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "storage.json"
            shared_root = Path(temporary) / "outputs"
            output_paths.save_output_root(shared_root, config_path=config_path)

            resolved = output_paths.describe_output_root(
                "x",
                config_path=config_path,
                env={},
            )

        self.assertEqual(resolved["source"], "config")
        self.assertEqual(resolved["output_root"], str((shared_root / "x").resolve()))

    def test_unconfigured_tool_uses_documents_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resolved = output_paths.describe_output_root(
                "wechat",
                config_path=Path(temporary) / "missing.json",
                platform_name="darwin",
                home=Path("/Users/tester"),
                env={},
            )

        self.assertEqual(resolved["source"], "default")
        self.assertEqual(
            resolved["output_root"],
            "/Users/tester/Documents/Donald Skills/Data/wechat",
        )

    def test_explicit_and_compatibility_overrides_beat_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "storage.json"
            output_paths.save_output_root(
                Path(temporary) / "saved",
                config_path=config_path,
            )
            environment_resolved = output_paths.describe_output_root(
                "x",
                config_path=config_path,
                env={"DONALD_SKILLS_OUTPUT_ROOT": str(Path(temporary) / "environment")},
            )
            legacy_resolved = output_paths.describe_output_root(
                "github-research",
                config_path=config_path,
                env={"DONALD_GITHUB_RESEARCH_ROOT": str(Path(temporary) / "legacy")},
            )
            explicit_resolved = output_paths.describe_output_root(
                "x",
                Path(temporary) / "explicit",
                config_path=config_path,
                env={"DONALD_SKILLS_OUTPUT_ROOT": str(Path(temporary) / "environment")},
            )

        self.assertEqual(environment_resolved["source"], "environment")
        self.assertTrue(environment_resolved["output_root"].endswith("/environment/x"))
        self.assertEqual(legacy_resolved["source"], "environment")
        self.assertTrue(legacy_resolved["output_root"].endswith("/legacy"))
        self.assertEqual(explicit_resolved["source"], "explicit")
        self.assertTrue(explicit_resolved["output_root"].endswith("/explicit"))

    def test_save_config_writes_only_version_and_absolute_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "nested" / "storage.json"
            output_paths.save_output_root(
                Path(temporary) / "outputs",
                config_path=config_path,
            )

            payload = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "output_root": str((Path(temporary) / "outputs").resolve()),
            },
        )

    def test_reset_removes_only_the_storage_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "storage.json"
            output_file = root / "outputs" / "result.txt"
            output_file.parent.mkdir()
            output_file.write_text("kept", encoding="utf-8")
            output_paths.save_output_root(output_file.parent, config_path=config_path)

            removed = output_paths.reset_output_root(config_path=config_path)

            self.assertTrue(removed)
            self.assertFalse(config_path.exists())
            self.assertEqual(output_file.read_text(encoding="utf-8"), "kept")


if __name__ == "__main__":
    unittest.main()
