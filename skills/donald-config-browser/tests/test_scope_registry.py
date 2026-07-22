from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "profile_config.py"
SPEC = importlib.util.spec_from_file_location("donald_config_browser_scopes", SCRIPT_PATH)
assert SPEC and SPEC.loader
profile_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = profile_config
SPEC.loader.exec_module(profile_config)


class ScopeRegistryTests(unittest.TestCase):
    def test_accepts_a_future_donald_skill_scope(self) -> None:
        self.assertEqual(
            profile_config.resolve_scope("donald-collect-linkedin"),
            "donald-collect-linkedin",
        )

    def test_rejects_a_scope_that_could_escape_the_config_directory(self) -> None:
        with self.assertRaises(profile_config.ProfileConfigError):
            profile_config.resolve_scope("../other-skill")

    def test_discovers_saved_future_scopes_for_profile_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_root = Path(temporary)
            source = config_root / "Chrome"
            runtime = config_root / "Chrome CDP" / "Profile 19"
            future = config_root / "donald-collect-linkedin.json"
            future.write_text(
                json.dumps(
                    {
                        "schema_version": profile_config.SCHEMA_VERSION,
                        "scope": "donald-collect-linkedin",
                        "profile": {"directory": "Profile 19"},
                        "chrome": {
                            "source_user_data_dir": str(source),
                            "cdp_user_data_dir": str(runtime),
                            "default_cdp_port": 9345,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {profile_config.CONFIG_ROOT_ENV: str(config_root)},
            ):
                scopes = profile_config.configured_scope_names()
                binding = profile_config._existing_profile_binding("Profile 19", source)

        self.assertIn("donald-collect-linkedin", scopes)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding[0], "donald-collect-linkedin")
        self.assertEqual(binding[1]["chrome"]["default_cdp_port"], 9345)

    def test_migrates_known_legacy_scope_without_reselecting_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_root = Path(temporary)
            legacy_path = config_root / "donald-collect-wechat-accounts.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "schema_version": profile_config.SCHEMA_VERSION,
                        "scope": "donald-collect-wechat-accounts",
                        "profile": {"directory": "Profile 22"},
                        "chrome": {
                            "source_user_data_dir": str(config_root / "Chrome"),
                            "cdp_user_data_dir": str(config_root / "Chrome CDP" / "Profile 22"),
                            "default_cdp_port": 9244,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {profile_config.CONFIG_ROOT_ENV: str(config_root)},
            ):
                migrated = profile_config.configured_browser(scope="donald-collect-wechat")
                canonical_path = config_root / "donald-collect-wechat.json"
                canonical_created = canonical_path.is_file()

        self.assertEqual(migrated["scope"], "donald-collect-wechat")
        self.assertEqual(migrated["profile"]["directory"], "Profile 22")
        self.assertEqual(migrated["chrome"]["default_cdp_port"], 9244)
        self.assertTrue(canonical_created)


if __name__ == "__main__":
    unittest.main()
