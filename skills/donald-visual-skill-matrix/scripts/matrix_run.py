#!/usr/bin/env python3
"""Prepare, record, verify, and present isolated visual-skill matrix runs."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from output_paths import StorageConfigError, resolve_output_root


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
VISUAL_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
RESULT_STATUSES = {
    "completed",
    "failed",
    "needs_input",
    "needs_preview",
    "not_independent",
    "isolation_violation",
}
PROVENANCE_SOURCES = {
    "runtime_trace",
    "coordinator_observation",
    "worker_report",
    "unknown",
}


class MatrixError(RuntimeError):
    """Raised when a matrix run would violate its isolation contract."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MatrixError(f"Cannot read JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise MatrixError(f"Expected a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash all regular files in a staged skill, including their relative paths."""
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise MatrixError(f"Symlinked source is not allowed: {root}")
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_symlink():
                raise MatrixError(f"Symlink inside isolated source is not allowed: {path}")


def _copy_strict(source: Path, destination: Path) -> None:
    _assert_no_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise MatrixError(f"Input does not exist or is not a regular file/directory: {source}")


def _frontmatter_name(skill_md: Path) -> str:
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise MatrixError(f"Skill has no YAML frontmatter: {skill_md}")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise MatrixError(f"Skill frontmatter is not closed: {skill_md}") from error
    for line in lines[1:end]:
        match = re.match(r"^name:\s*[\"']?([^\"']+?)[\"']?\s*$", line)
        if match:
            return match.group(1).strip()
    raise MatrixError(f"Skill frontmatter has no name: {skill_md}")


def _validate_candidate(source: Path) -> tuple[str, Path]:
    resolved = source.expanduser().resolve()
    if not resolved.is_dir():
        raise MatrixError(f"Candidate skill directory does not exist: {resolved}")
    _assert_no_symlinks(resolved)
    skill_files = sorted(resolved.rglob("SKILL.md"))
    if skill_files != [resolved / "SKILL.md"]:
        raise MatrixError(f"Candidate must contain exactly one top-level SKILL.md: {resolved}")
    name = _frontmatter_name(skill_files[0])
    if not NAME_RE.fullmatch(name):
        raise MatrixError(f"Candidate skill name is not kebab-case: {name!r}")
    if resolved.name != name:
        raise MatrixError(f"Candidate folder {resolved.name!r} does not match skill name {name!r}")
    return name, resolved


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _worker_prompt(
    *,
    skill_name: str,
    skill_dir: Path,
    brief_path: Path,
    inputs_dir: Path,
    workspace: Path,
    outputs_dir: Path,
) -> str:
    return f"""You are one isolated cell in a visual-skill matrix.

Isolation contract:
- This must be a brand-new context with no inherited conversation turns.
- Use this staged skill as the entry skill: {skill_name} at {skill_dir / 'SKILL.md'}.
- Supporting skills may be invoked when the entry skill requires them. Do not invoke another matrix candidate or use another cell's work.
- Read only the exact candidate, brief, input, workspace, and output paths named below. Do not inspect the run manifest or any sibling cell, and do not compare your work with another result.
- Start a new external creative conversation or generation job; never resume one from another cell.

Task:
- Read the frozen brief at {brief_path}.
- Read optional copied inputs only from {inputs_dir}.
- Work only in {workspace}.
- Put every deliverable in {outputs_dir}.
- Produce at least one visual preview image. Preserve original image artifacts. For a web result, keep the project and capture a representative screenshot without using another design skill.
- Return the preview paths, original artifact paths, final status, every skill observed during execution, and whether that list came from a runtime trace, coordinator observation, worker report, or is unknown.
"""


def prepare_run(
    *,
    brief_file: Path,
    candidates: Iterable[Path],
    inputs: Iterable[Path] = (),
    output_root: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    brief_source = brief_file.expanduser().resolve()
    if not brief_source.is_file() or brief_source.is_symlink():
        raise MatrixError(f"Brief must be a regular non-symlink file: {brief_source}")

    validated = [_validate_candidate(path) for path in candidates]
    if len(validated) < 2:
        raise MatrixError("A visual matrix requires at least two candidate skills")
    names = [name for name, _ in validated]
    if len(set(names)) != len(names):
        raise MatrixError("Candidate skill names must be unique")

    copied_inputs = [path.expanduser().resolve() for path in inputs]
    input_names = [path.name for path in copied_inputs]
    if len(set(input_names)) != len(input_names):
        raise MatrixError("Input basenames must be unique")
    for path in copied_inputs:
        _assert_no_symlinks(path)
        if not path.exists():
            raise MatrixError(f"Input does not exist: {path}")

    effective_run_id = run_id or _default_run_id()
    if not RUN_ID_RE.fullmatch(effective_run_id):
        raise MatrixError(f"Unsafe run id: {effective_run_id!r}")
    root, root_source = resolve_output_root(output_root)
    run_root = root / effective_run_id
    if run_root.exists():
        raise MatrixError(f"Run root already exists: {run_root}")
    run_root.mkdir(parents=True)
    shutil.copy2(brief_source, run_root / "brief.md")
    brief_hash = _sha256(run_root / "brief.md")

    cells: list[dict[str, Any]] = []
    for index, (name, source) in enumerate(validated, start=1):
        cell_id = f"{index:02d}-{name}"
        cell_root = run_root / "cells" / cell_id
        candidate_dir = cell_root / "candidate" / name
        inputs_dir = cell_root / "inputs"
        workspace = cell_root / "workspace"
        outputs_dir = cell_root / "outputs"
        candidate_dir.parent.mkdir(parents=True)
        inputs_dir.mkdir(parents=True)
        workspace.mkdir(parents=True)
        outputs_dir.mkdir(parents=True)

        _copy_strict(source, candidate_dir)
        skill_hash = _tree_sha256(candidate_dir)
        shutil.copy2(brief_source, inputs_dir / "brief.md")
        for input_path in copied_inputs:
            _copy_strict(input_path, inputs_dir / input_path.name)

        contract = _worker_prompt(
            skill_name=name,
            skill_dir=candidate_dir,
            brief_path=inputs_dir / "brief.md",
            inputs_dir=inputs_dir,
            workspace=workspace,
            outputs_dir=outputs_dir,
        )
        (cell_root / "worker-prompt.md").write_text(contract, encoding="utf-8")
        (workspace / "AGENTS.md").write_text(contract, encoding="utf-8")
        cell = {
            "cell_id": cell_id,
            "target_skill": name,
            "source_skill_dir": str(source),
            "staged_skill_dir": str(candidate_dir),
            "cell_root": str(cell_root),
            "worker_prompt": str(cell_root / "worker-prompt.md"),
            "brief_sha256": brief_hash,
            "entry_skill_sha256": skill_hash,
            "status": "prepared",
        }
        _write_json(cell_root / "cell.json", cell)
        cells.append(cell)

    manifest = {
        "schema_version": 1,
        "run_id": effective_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_root_source": root_source,
        "run_root": str(run_root),
        "brief_path": str(run_root / "brief.md"),
        "brief_sha256": brief_hash,
        "cells": cells,
    }
    _write_json(run_root / "manifest.json", manifest)
    return manifest


def _inside(root: Path, value: str) -> tuple[Path, str]:
    candidate = Path(value)
    unresolved = candidate if candidate.is_absolute() else root / candidate
    if unresolved.is_symlink():
        raise MatrixError(f"Result path may not be a symlink: {unresolved}")
    path = unresolved.resolve()
    root_resolved = root.resolve()
    try:
        relative = path.relative_to(root_resolved)
    except ValueError as error:
        raise MatrixError(f"Result path escapes {root_resolved}: {value}") from error
    return path, relative.as_posix()


def record_result(
    *,
    cell_root: Path,
    worker_id: str,
    status: str,
    observed_skills: list[str],
    provenance_source: str,
    previews: list[str],
    artifacts: list[str],
    note: str | None = None,
) -> dict[str, Any]:
    root = cell_root.expanduser().resolve()
    cell = _read_json(root / "cell.json")
    target = str(cell.get("target_skill", ""))
    if not worker_id.strip():
        raise MatrixError("worker_id is required")
    if status not in RESULT_STATUSES:
        raise MatrixError(f"Unsupported result status: {status}")
    if provenance_source not in PROVENANCE_SOURCES:
        raise MatrixError(f"Unsupported provenance source: {provenance_source}")

    observed: list[str] = []
    for skill in observed_skills:
        normalized = skill.strip()
        if normalized and normalized not in observed:
            observed.append(normalized)
    if not observed:
        skill_usage = "unverified"
    elif observed == [target]:
        skill_usage = "target_only"
    elif target in observed:
        skill_usage = "mixed"
    else:
        skill_usage = "target_unobserved"

    outputs = root / "outputs"
    recorded_previews: list[str] = []
    for value in previews:
        path, relative = _inside(root, value)
        try:
            path.relative_to(outputs.resolve())
        except ValueError as error:
            raise MatrixError(f"Preview must be inside the cell outputs directory: {path}") from error
        if not path.is_file() or path.suffix.lower() not in VISUAL_SUFFIXES:
            raise MatrixError(f"Preview must be an existing visual file: {path}")
        recorded_previews.append(relative)
    if status == "completed" and not recorded_previews:
        raise MatrixError("A completed visual cell requires at least one preview")

    recorded_artifacts: list[str] = []
    for value in artifacts:
        path, relative = _inside(root, value)
        try:
            path.relative_to(outputs.resolve())
        except ValueError as error:
            raise MatrixError(f"Artifact must be inside the cell outputs directory: {path}") from error
        if not path.exists():
            raise MatrixError(f"Artifact does not exist: {path}")
        _assert_no_symlinks(path)
        recorded_artifacts.append(relative)

    result = {
        "schema_version": 2,
        "cell_id": cell["cell_id"],
        "entry_skill": target,
        "entry_skill_sha256": cell.get("entry_skill_sha256", ""),
        "worker_id": worker_id,
        "fresh_context": True,
        "context_status": "fresh_declared",
        "observed_skills": observed,
        "skill_usage": skill_usage,
        "provenance_source": provenance_source,
        "status": status,
        "previews": recorded_previews,
        "artifacts": recorded_artifacts,
        "note": note or "",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(root / "result.json", result)
    return result


def verify_run(run_root: Path) -> dict[str, Any]:
    root = run_root.expanduser().resolve()
    manifest = _read_json(root / "manifest.json")
    errors: list[str] = []
    warnings: list[str] = []
    worker_ids: set[str] = set()
    for declared in manifest.get("cells", []):
        cell_root = Path(declared["cell_root"]).resolve()
        if cell_root.parent != root / "cells":
            errors.append(f"{declared.get('cell_id')}: cell root is outside this run")
            continue
        target = declared.get("target_skill")
        staged = cell_root / "candidate" / str(target)
        try:
            _assert_no_symlinks(staged)
        except MatrixError as error:
            errors.append(f"{declared.get('cell_id')}: {error}")
        skill_files = sorted(staged.rglob("SKILL.md")) if staged.is_dir() else []
        if skill_files != [staged / "SKILL.md"]:
            errors.append(f"{declared.get('cell_id')}: staged cell does not contain exactly one skill")
        elif _tree_sha256(staged) != declared.get("entry_skill_sha256"):
            errors.append(f"{declared.get('cell_id')}: staged entry skill changed")
        brief = cell_root / "inputs" / "brief.md"
        if not brief.is_file() or _sha256(brief) != manifest.get("brief_sha256"):
            errors.append(f"{declared.get('cell_id')}: frozen brief changed or is missing")

        result_path = cell_root / "result.json"
        if not result_path.is_file():
            warnings.append(f"{declared.get('cell_id')}: result is pending")
            continue
        try:
            result = _read_json(result_path)
            worker_id = str(result.get("worker_id", ""))
            if not worker_id:
                errors.append(f"{declared.get('cell_id')}: worker ID is missing")
            elif worker_id in worker_ids:
                errors.append(f"{declared.get('cell_id')}: worker ID was reused")
            worker_ids.add(worker_id)
            if result.get("fresh_context") is not True:
                errors.append(f"{declared.get('cell_id')}: context was not declared fresh")
            if result.get("entry_skill") != target:
                errors.append(f"{declared.get('cell_id')}: entry skill changed")
            if result.get("entry_skill_sha256") != declared.get("entry_skill_sha256"):
                errors.append(f"{declared.get('cell_id')}: entry skill fingerprint changed")
            observed = result.get("observed_skills", [])
            if not isinstance(observed, list):
                errors.append(f"{declared.get('cell_id')}: observed_skills is not a list")
                observed = []
            expected_usage = (
                "unverified"
                if not observed
                else "target_only"
                if observed == [target]
                else "mixed"
                if target in observed
                else "target_unobserved"
            )
            if result.get("skill_usage") != expected_usage:
                errors.append(f"{declared.get('cell_id')}: skill usage classification is inconsistent")
            elif expected_usage == "mixed":
                helpers = ", ".join(str(skill) for skill in observed if skill != target)
                warnings.append(f"{declared.get('cell_id')}: supporting skills observed: {helpers}")
            elif expected_usage == "target_unobserved":
                warnings.append(f"{declared.get('cell_id')}: entry skill was not observed in execution evidence")
            elif expected_usage == "unverified":
                warnings.append(f"{declared.get('cell_id')}: actual skill usage is unverified")
            provenance = result.get("provenance_source")
            if provenance not in PROVENANCE_SOURCES:
                errors.append(f"{declared.get('cell_id')}: invalid provenance source")
            elif provenance == "unknown":
                warnings.append(f"{declared.get('cell_id')}: provenance source is unknown")
            elif provenance == "worker_report":
                warnings.append(f"{declared.get('cell_id')}: skill usage is based on worker self-report")
            if result.get("status") not in RESULT_STATUSES:
                errors.append(f"{declared.get('cell_id')}: invalid result status")
            for field in ("previews", "artifacts"):
                values = result.get(field, [])
                if not isinstance(values, list):
                    errors.append(f"{declared.get('cell_id')}: {field} is not a list")
                    continue
                for value in values:
                    path, _ = _inside(cell_root, str(value))
                    if not path.exists():
                        errors.append(f"{declared.get('cell_id')}: missing {field[:-1]} {value}")
            if result.get("status") == "completed" and not result.get("previews"):
                errors.append(f"{declared.get('cell_id')}: completed cell has no preview")
        except MatrixError as error:
            errors.append(f"{declared.get('cell_id')}: {error}")

    return {
        "status": "verified" if not errors else "needs_attention",
        "run_root": str(root),
        "cell_count": len(manifest.get("cells", [])),
        "errors": errors,
        "warnings": warnings,
    }


def _relative_url(from_dir: Path, target: Path) -> str:
    relative = Path(os.path.relpath(target.resolve(), start=from_dir.parent.resolve()))
    return quote(relative.as_posix(), safe="/")


def build_gallery(run_root: Path) -> Path:
    root = run_root.expanduser().resolve()
    verification = verify_run(root)
    manifest = _read_json(root / "manifest.json")
    gallery_dir = root / "gallery"
    gallery_dir.mkdir(exist_ok=True)
    gallery_path = gallery_dir / "index.html"

    worker_counts: dict[str, int] = {}
    for declared in manifest["cells"]:
        result_path = Path(declared["cell_root"]) / "result.json"
        if result_path.is_file():
            worker_id = str(_read_json(result_path).get("worker_id", ""))
            if worker_id:
                worker_counts[worker_id] = worker_counts.get(worker_id, 0) + 1

    cards: list[str] = []
    for declared in manifest["cells"]:
        cell_root = Path(declared["cell_root"])
        result_path = cell_root / "result.json"
        if result_path.is_file():
            result = _read_json(result_path)
        else:
            result = {
                "status": "pending",
                "worker_id": "",
                "fresh_context": None,
                "observed_skills": [],
                "skill_usage": "unverified",
                "provenance_source": "unknown",
                "previews": [],
                "artifacts": [],
                "note": "Result has not been recorded yet.",
            }

        local_issues: list[str] = []
        worker_id = str(result.get("worker_id", ""))
        if worker_id and worker_counts.get(worker_id, 0) > 1:
            context_status = "worker_reused"
            local_issues.append("Worker ID is shared with another cell.")
        elif result.get("fresh_context") is True:
            context_status = "fresh_declared"
        elif result_path.is_file():
            context_status = "not_verified"
            local_issues.append("Fresh context was not declared.")
        else:
            context_status = "pending"

        previews: list[str] = []
        for value in result.get("previews", []):
            try:
                path, _ = _inside(cell_root, str(value))
                if not path.is_file():
                    raise MatrixError(f"Missing preview: {value}")
                url = _relative_url(gallery_path, path)
                previews.append(
                    f'<a class="preview" href="{url}"><img src="{url}" alt="{html.escape(declared["target_skill"])} preview"></a>'
                )
            except MatrixError as error:
                local_issues.append(str(error))
        artifacts: list[str] = []
        for value in result.get("artifacts", []):
            try:
                path, _ = _inside(cell_root, str(value))
                if not path.exists():
                    raise MatrixError(f"Missing artifact: {value}")
                url = _relative_url(gallery_path, path)
                artifacts.append(f'<a href="{url}">{html.escape(Path(value).name)}</a>')
            except MatrixError as error:
                local_issues.append(str(error))

        observed = result.get("observed_skills", [])
        observed_markup = (
            ", ".join(f"<code>{html.escape(str(skill))}</code>" for skill in observed)
            if isinstance(observed, list) and observed
            else "Not observed"
        )
        skill_usage = html.escape(str(result.get("skill_usage", "unverified")))
        provenance = html.escape(str(result.get("provenance_source", "unknown")))
        fingerprint = html.escape(str(declared.get("entry_skill_sha256", ""))[:12] or "unavailable")
        note = html.escape(str(result.get("note", "")))
        preview_markup = "".join(previews) or '<div class="empty">No preview</div>'
        note_markup = f"<p>{note}</p>" if note else ""
        issue_markup = (
            '<ul class="issues">' + "".join(f"<li>{html.escape(issue)}</li>" for issue in local_issues) + "</ul>"
            if local_issues
            else ""
        )
        artifact_markup = " · ".join(artifacts) or "No artifact links"
        cards.append(
            "<article class=\"card\">"
            f"<header><h2>{html.escape(declared['target_skill'])}</h2>"
            f"<span>{html.escape(str(result.get('status', 'unknown')))}</span></header>"
            '<dl class="provenance">'
            f"<div><dt>Entry skill</dt><dd><code>{html.escape(declared['target_skill'])}</code></dd></div>"
            f"<div><dt>Entry fingerprint</dt><dd><code>{fingerprint}</code></dd></div>"
            f"<div><dt>Observed skills</dt><dd>{observed_markup}</dd></div>"
            f"<div><dt>Usage</dt><dd><strong>{skill_usage}</strong></dd></div>"
            f"<div><dt>Evidence</dt><dd>{provenance}</dd></div>"
            f"<div><dt>Context</dt><dd>{html.escape(context_status)}</dd></div>"
            "</dl>"
            f"{issue_markup}"
            f"<div class=\"previews\">{preview_markup}</div>"
            f"<footer>{artifact_markup}{note_markup}</footer>"
            "</article>"
        )

    brief = html.escape((root / "brief.md").read_text(encoding="utf-8"))
    run_issues = [
        ("error", issue) for issue in verification["errors"]
    ] + [("warning", issue) for issue in verification["warnings"]]
    run_status = (
        '<section class="run-status"><h2>Run evidence</h2><ul>'
        + "".join(
            f'<li class="{level}"><strong>{level.title()}:</strong> {html.escape(issue)}</li>'
            for level, issue in run_issues
        )
        + "</ul></section>"
        if run_issues
        else '<section class="run-status clean">No provenance or context warnings recorded.</section>'
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Visual Skill Matrix</title>
<style>
:root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background:#f4f2ed; color:#171714; }}
body {{ margin:0; padding:32px; }}
main {{ max-width:1600px; margin:auto; }}
h1 {{ margin:0 0 8px; font-size:clamp(28px,4vw,54px); letter-spacing:-.04em; }}
.brief {{ white-space:pre-wrap; max-width:900px; color:#5d5b53; margin:0 0 28px; }}
.run-status {{ background:#fff8e8; border:1px solid #ead4a2; border-radius:14px; padding:14px 18px; margin:0 0 20px; }}
.run-status.clean {{ background:#edf8f3; border-color:#b8dfce; }}
.run-status h2 {{ margin:0 0 8px; }} .run-status ul {{ margin:0; padding-left:20px; }}
.run-status .error {{ color:#982f28; }} .run-status .warning {{ color:#6f5314; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:18px; align-items:start; }}
.card {{ background:#fff; border:1px solid #dad7cf; border-radius:18px; overflow:hidden; box-shadow:0 12px 32px rgba(28,27,23,.07); }}
header {{ display:flex; justify-content:space-between; gap:16px; align-items:center; padding:16px 18px; border-bottom:1px solid #ece9e2; }}
h2 {{ font-size:16px; margin:0; }}
header span {{ font-size:12px; color:#68665e; }}
.provenance {{ padding:14px 18px; margin:0; display:grid; gap:7px; font-size:12px; border-bottom:1px solid #ece9e2; }}
.provenance div {{ display:grid; grid-template-columns:110px 1fr; gap:10px; }}
.provenance dt {{ color:#77736a; }} .provenance dd {{ margin:0; overflow-wrap:anywhere; }}
.issues {{ margin:0; padding:12px 34px; color:#982f28; background:#fff0ee; font-size:12px; }}
.previews {{ display:grid; gap:1px; background:#ece9e2; }}
.preview {{ display:block; background:#f7f6f2; }}
.preview img {{ display:block; width:100%; max-height:680px; object-fit:contain; }}
.empty {{ padding:80px 18px; text-align:center; color:#88857c; }}
footer {{ min-height:22px; padding:14px 18px; font-size:13px; color:#68665e; }}
footer a {{ color:#1f5d50; }} footer p {{ margin:10px 0 0; }}
</style>
</head>
<body><main>
<h1>Visual Skill Matrix</h1>
<p class="brief">{brief}</p>
{run_status}
<section class="grid">{''.join(cards)}</section>
</main></body>
</html>
"""
    gallery_path.write_text(document, encoding="utf-8")
    return gallery_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="create isolated matrix cells")
    prepare.add_argument("--brief-file", required=True)
    prepare.add_argument("--candidate", action="append", required=True)
    prepare.add_argument("--input", action="append", default=[])
    prepare.add_argument("--output-root")
    prepare.add_argument("--run-id")

    record = subparsers.add_parser("record", help="record one worker result")
    record.add_argument("--cell-root", required=True)
    record.add_argument("--worker-id", required=True)
    record.add_argument("--status", required=True, choices=sorted(RESULT_STATUSES))
    record.add_argument("--observed-skill", "--invoked-skill", dest="observed_skill", action="append", default=[])
    record.add_argument("--provenance-source", choices=sorted(PROVENANCE_SOURCES), default="unknown")
    record.add_argument("--preview", action="append", default=[])
    record.add_argument("--artifact", action="append", default=[])
    record.add_argument("--note")

    verify = subparsers.add_parser("verify", help="verify isolation evidence")
    verify.add_argument("--run-root", required=True)

    gallery = subparsers.add_parser("gallery", help="build the neutral visual gallery")
    gallery.add_argument("--run-root", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            payload = prepare_run(
                brief_file=Path(args.brief_file),
                candidates=[Path(value) for value in args.candidate],
                inputs=[Path(value) for value in args.input],
                output_root=Path(args.output_root) if args.output_root else None,
                run_id=args.run_id,
            )
        elif args.command == "record":
            payload = record_result(
                cell_root=Path(args.cell_root),
                worker_id=args.worker_id,
                status=args.status,
                observed_skills=args.observed_skill,
                provenance_source=args.provenance_source,
                previews=args.preview,
                artifacts=args.artifact,
                note=args.note,
            )
        elif args.command == "verify":
            payload = verify_run(Path(args.run_root))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["status"] == "verified" else 2
        else:
            gallery = build_gallery(Path(args.run_root))
            payload = {"status": "created", "gallery": str(gallery), "run_root": str(Path(args.run_root).resolve())}
    except (MatrixError, StorageConfigError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
