#!/usr/bin/env python3
"""Generate runtime skill mirrors from canonical root skills/.

Supports flat, categorized, and mixed skill layouts. The skill name is the parent directory
containing SKILL.md; duplicate names fail.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO / "skills"
DEFAULT_TARGETS = [REPO / ".claude" / "skills", REPO / ".agents" / "skills"]


def discover_skills() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for skill_md in sorted(SKILLS_ROOT.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        name = skill_dir.name
        if name in found:
            sys.exit(f"duplicate skill name: {name} ({found[name]} vs {skill_dir})")
        found[name] = skill_dir
    return found


def rel_target(skill_dir: Path, link_parent: Path) -> Path:
    return Path(os.path.relpath(skill_dir, start=link_parent))


def remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def sync_target(target_dir: Path, skills: dict[str, Path], *, check: bool, copy: bool, replace_existing: bool) -> list[str]:
    stale: list[str] = []
    planned = {target_dir / name: skill_dir for name, skill_dir in skills.items()}

    for mirror_path, skill_dir in planned.items():
        expected = rel_target(skill_dir, mirror_path.parent)
        if copy:
            ok = mirror_path.is_dir() and (mirror_path / "SKILL.md").exists()
        else:
            ok = mirror_path.is_symlink() and mirror_path.readlink().as_posix() == expected.as_posix()
        if not ok:
            stale.append(f"{mirror_path.relative_to(REPO)}")

    orphans: list[Path] = []
    if target_dir.exists():
        managed_names = set(skills)
        for candidate in target_dir.iterdir():
            if candidate.name not in managed_names and candidate.is_symlink():
                orphans.append(candidate)

    if check:
        return stale + [f"{o.relative_to(REPO)} (orphan)" for o in orphans]

    target_dir.mkdir(parents=True, exist_ok=True)
    for mirror_path, skill_dir in planned.items():
        if mirror_path.exists() or mirror_path.is_symlink():
            if mirror_path.is_symlink() or replace_existing or copy:
                remove_existing(mirror_path)
            else:
                raise SystemExit(
                    f"{mirror_path.relative_to(REPO)} exists and is not generated; "
                    "rerun with --replace-existing after preserving any hand edits"
                )
        if copy:
            shutil.copytree(skill_dir, mirror_path)
        else:
            mirror_path.symlink_to(rel_target(skill_dir, mirror_path.parent), target_is_directory=True)

    for orphan in orphans:
        orphan.unlink()
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify only")
    parser.add_argument("--copy", action="store_true", help="copy directories instead of symlinking")
    parser.add_argument("--replace-existing", action="store_true", help="replace existing non-symlink mirrors")
    parser.add_argument("--target", action="append", help="mirror target directory; may be repeated")
    args = parser.parse_args()

    skills = discover_skills()
    targets = [Path(t).expanduser().resolve() for t in args.target] if args.target else DEFAULT_TARGETS
    stale: list[str] = []
    for target in targets:
        stale.extend(
            sync_target(
                target,
                skills,
                check=args.check,
                copy=args.copy,
                replace_existing=args.replace_existing,
            )
        )

    if args.check:
        if stale:
            print("Out of sync (rerun skills/sync_runtime_skills.py):\n  " + "\n  ".join(stale))
            return 1
        print(f"In sync: {len(skills)} skills mirrored to {len(targets)} runtime target(s).")
        return 0

    print(f"Mirrored {len(skills)} skills to {len(targets)} runtime target(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
