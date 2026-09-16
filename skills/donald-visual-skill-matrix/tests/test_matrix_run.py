from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import matrix_run  # noqa: E402
import output_paths  # noqa: E402


class MatrixRunTests(unittest.TestCase):
    def _skill(self, root: Path, name: str) -> Path:
        skill = root / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test visual skill.\n---\n\n# {name}\n",
            encoding="utf-8",
        )
        return skill

    def _prepared(self, root: Path) -> dict[str, object]:
        brief = root / "brief.md"
        brief.write_text("Design one English learning card.", encoding="utf-8")
        reference = root / "reference.png"
        reference.write_bytes(b"reference")
        first = self._skill(root, "visual-alpha")
        second = self._skill(root, "visual-beta")
        return matrix_run.prepare_run(
            brief_file=brief,
            candidates=[first, second],
            inputs=[reference],
            output_root=root / "runs",
            run_id="test-run",
        )

    def test_prepare_gives_each_candidate_its_own_prompt_and_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            cells = manifest["cells"]
            self.assertEqual(len(cells), 2)

            first_prompt = Path(cells[0]["worker_prompt"]).read_text(encoding="utf-8")
            second_prompt = Path(cells[1]["worker_prompt"]).read_text(encoding="utf-8")
            self.assertIn("visual-alpha", first_prompt)
            self.assertNotIn("visual-beta", first_prompt)
            self.assertIn("visual-beta", second_prompt)
            self.assertNotIn("visual-alpha", second_prompt)
            for cell in cells:
                cell_root = Path(cell["cell_root"])
                self.assertEqual(len(list((cell_root / "candidate").rglob("SKILL.md"))), 1)
                self.assertEqual(
                    (cell_root / "inputs" / "brief.md").read_text(encoding="utf-8"),
                    "Design one English learning card.",
                )
                self.assertTrue((cell_root / "inputs" / "reference.png").is_file())

    def test_verified_unique_workers_can_build_unscored_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            for index, cell in enumerate(manifest["cells"], start=1):
                cell_root = Path(cell["cell_root"])
                preview = cell_root / "outputs" / "preview.png"
                preview.write_bytes(b"preview")
                matrix_run.record_result(
                    cell_root=cell_root,
                    worker_id=f"worker-{index}",
                    status="completed",
                    observed_skills=[cell["target_skill"]],
                    provenance_source="runtime_trace",
                    previews=["outputs/preview.png"],
                    artifacts=["outputs/preview.png"],
                )

            verification = matrix_run.verify_run(Path(manifest["run_root"]))
            self.assertEqual(verification["status"], "verified")
            gallery = matrix_run.build_gallery(Path(manifest["run_root"]))
            document = gallery.read_text(encoding="utf-8")
            self.assertIn("visual-alpha", document)
            self.assertIn("visual-beta", document)
            self.assertIn("target_only", document)
            self.assertIn("runtime_trace", document)
            self.assertNotIn("score", document.lower())
            self.assertNotIn("rank", document.lower())

    def test_reused_worker_id_is_disclosed_without_blocking_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            for cell in manifest["cells"]:
                cell_root = Path(cell["cell_root"])
                preview = cell_root / "outputs" / "preview.png"
                preview.write_bytes(b"preview")
                matrix_run.record_result(
                    cell_root=cell_root,
                    worker_id="same-worker",
                    status="completed",
                    observed_skills=[cell["target_skill"]],
                    provenance_source="runtime_trace",
                    previews=["outputs/preview.png"],
                    artifacts=[],
                )

            verification = matrix_run.verify_run(Path(manifest["run_root"]))
            self.assertEqual(verification["status"], "needs_attention")
            self.assertTrue(any("worker ID was reused" in error for error in verification["errors"]))
            gallery = matrix_run.build_gallery(Path(manifest["run_root"]))
            self.assertIn("worker_reused", gallery.read_text(encoding="utf-8"))

    def test_supporting_skill_is_disclosed_as_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            cell = manifest["cells"][0]
            preview = Path(cell["cell_root"]) / "outputs" / "preview.png"
            preview.write_bytes(b"preview")
            result = matrix_run.record_result(
                cell_root=Path(cell["cell_root"]),
                worker_id="worker-1",
                status="completed",
                observed_skills=[cell["target_skill"], "helper-renderer"],
                provenance_source="runtime_trace",
                previews=["outputs/preview.png"],
                artifacts=["outputs/preview.png"],
            )
            self.assertEqual(result["skill_usage"], "mixed")
            verification = matrix_run.verify_run(Path(manifest["run_root"]))
            self.assertEqual(verification["status"], "verified")
            self.assertTrue(any("supporting skills observed" in warning for warning in verification["warnings"]))
            gallery = matrix_run.build_gallery(Path(manifest["run_root"]))
            document = gallery.read_text(encoding="utf-8")
            self.assertIn("helper-renderer", document)
            self.assertIn("mixed", document)

    def test_worker_report_is_kept_with_a_provenance_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            for index, cell in enumerate(manifest["cells"], start=1):
                result = matrix_run.record_result(
                    cell_root=Path(cell["cell_root"]),
                    worker_id=f"worker-{index}",
                    status="not_independent",
                    observed_skills=[cell["target_skill"]],
                    provenance_source="worker_report",
                    previews=[],
                    artifacts=[],
                )
                self.assertEqual(result["skill_usage"], "target_only")
            self.assertEqual(
                matrix_run.verify_run(Path(manifest["run_root"]))["status"],
                "verified",
            )

    def test_pending_cell_does_not_block_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            gallery = matrix_run.build_gallery(Path(manifest["run_root"]))
            document = gallery.read_text(encoding="utf-8")
            self.assertIn("result is pending", document)
            self.assertIn("pending", document)

    def test_staged_skill_fingerprint_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._prepared(Path(temporary))
            first = manifest["cells"][0]
            staged = Path(first["staged_skill_dir"]) / "SKILL.md"
            staged.write_text(staged.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
            verification = matrix_run.verify_run(Path(manifest["run_root"]))
            self.assertEqual(verification["status"], "needs_attention")
            self.assertTrue(any("entry skill changed" in error for error in verification["errors"]))

    def test_candidate_with_nested_skill_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief = root / "brief.md"
            brief.write_text("Design a logo.", encoding="utf-8")
            first = self._skill(root, "visual-alpha")
            second = self._skill(root, "visual-beta")
            nested = first / "nested"
            nested.mkdir()
            (nested / "SKILL.md").write_text("---\nname: hidden\ndescription: Hidden.\n---\n", encoding="utf-8")

            with self.assertRaisesRegex(matrix_run.MatrixError, "exactly one top-level SKILL.md"):
                matrix_run.prepare_run(
                    brief_file=brief,
                    candidates=[first, second],
                    output_root=root / "runs",
                    run_id="bad-run",
                )

    def test_output_root_uses_shared_override_and_tool_directory(self) -> None:
        root, source = output_paths.resolve_output_root(
            platform_name="darwin",
            home=Path("/Users/tester"),
            env={"DONALD_SKILLS_OUTPUT_ROOT": "/tmp/donald-output"},
        )
        self.assertEqual(source, "environment")
        self.assertEqual(root, Path("/tmp/donald-output/visual-skill-matrix").resolve())


if __name__ == "__main__":
    unittest.main()
