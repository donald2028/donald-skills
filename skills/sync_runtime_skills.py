#!/usr/bin/env python3
"""Generate runtime skill mirrors from canonical root skills/.

Supports flat, categorized, and mixed skill layouts. The skill name is the parent directory
containing SKILL.md; duplicate names fail.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO / "skills"
DEFAULT_TARGETS = [
    REPO / ".claude" / "skills",
    REPO / ".agents" / "skills",
    REPO / ".codebuddy" / "skills",
    REPO / ".workbuddy" / "skills",
]


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


def is_link_or_junction(path: Path) -> bool:
    """True for symlinks and Windows junction points (both are NTFS reparse points)."""
    if path.is_symlink():
        return True
    try:
        path.readlink()
        return True
    except OSError:
        return False


def remove_existing(path: Path) -> None:
    # A junction is a reparse point, not a plain directory: unlink only, never rmtree,
    # otherwise shutil would follow it and wipe the real skill sources.
    if is_link_or_junction(path) or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _create_junction(mirror_path: Path, skill_dir: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(mirror_path), str(skill_dir)],
        check=True,
        capture_output=True,
    )


def make_link(mirror_path: Path, skill_dir: Path, *, force_junction: bool = False) -> None:
    """Create a directory link to skill_dir.

    - On non-Windows: always a real symlink.
    - On Windows: --junction forces an admin-free junction point. Without it we try a
      true symlink first; if that lacks privilege OR a security layer silently turns it
      into a copy, we detect the failure and fall back to a junction point (no admin).
    """
    if force_junction and sys.platform == "win32":
        _create_junction(mirror_path, skill_dir)
        return
    if sys.platform != "win32":
        mirror_path.symlink_to(rel_target(skill_dir, mirror_path.parent), target_is_directory=True)
        return
    # Windows: attempt a real symlink, then verify it actually linked.
    try:
        mirror_path.symlink_to(rel_target(skill_dir, mirror_path.parent), target_is_directory=True)
    except OSError:
        pass
    if not is_link_or_junction(mirror_path):
        # Privilege denied, or a security product faked a directory copy instead of linking.
        if mirror_path.exists() or mirror_path.is_symlink():
            remove_existing(mirror_path)
        _create_junction(mirror_path, skill_dir)


def sync_target(
    target_dir: Path,
    skills: dict[str, Path],
    *,
    check: bool,
    copy: bool,
    replace_existing: bool,
    force_junction: bool = False,
) -> list[str]:
    stale: list[str] = []
    planned = {target_dir / name: skill_dir for name, skill_dir in skills.items()}

    for mirror_path, skill_dir in planned.items():
        if copy:
            ok = mirror_path.is_dir() and (mirror_path / "SKILL.md").exists()
        else:
            # realpath equality holds for both symlinks and junctions; is_link_or_junction
            # catches junctions, which os.path.islink() reports as False on Windows.
            ok = is_link_or_junction(mirror_path) and os.path.realpath(mirror_path) == os.path.realpath(skill_dir)
        if not ok:
            stale.append(f"{mirror_path.relative_to(REPO)}")

    orphans: list[Path] = []
    if target_dir.exists():
        managed_names = set(skills)
        for candidate in target_dir.iterdir():
            if candidate.name not in managed_names and is_link_or_junction(candidate):
                orphans.append(candidate)

    if check:
        return stale + [f"{o.relative_to(REPO)} (orphan)" for o in orphans]

    target_dir.mkdir(parents=True, exist_ok=True)
    for mirror_path, skill_dir in planned.items():
        if mirror_path.exists() or is_link_or_junction(mirror_path):
            if is_link_or_junction(mirror_path) or replace_existing or copy:
                remove_existing(mirror_path)
            else:
                raise SystemExit(
                    f"{mirror_path.relative_to(REPO)} exists and is not generated; "
                    "rerun with --replace-existing after preserving any hand edits"
                )
        if copy:
            shutil.copytree(skill_dir, mirror_path)
        else:
            make_link(mirror_path, skill_dir, force_junction=force_junction)

    for orphan in orphans:
        orphan.unlink()
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify only")
    mirror_mode = parser.add_mutually_exclusive_group()
    mirror_mode.add_argument(
        "--copy",
        action="store_true",
        help="copy directories instead of linking (default for cross-platform checkouts)",
    )
    mirror_mode.add_argument(
        "--symlink",
        action="store_true",
        help="prefer symlinks; on Windows falls back to junction points when the privilege is missing",
    )
    mirror_mode.add_argument(
        "--junction",
        action="store_true",
        help="always use Windows junction points (admin-free); no-op alias for symlink off Windows",
    )
    parser.add_argument("--replace-existing", action="store_true", help="replace existing non-generated mirrors")
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
                copy=not (args.symlink or args.junction),
                replace_existing=args.replace_existing,
                force_junction=args.junction,
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
