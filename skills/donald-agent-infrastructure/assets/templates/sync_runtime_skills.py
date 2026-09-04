#!/usr/bin/env python3
"""Generate runtime skill mirrors from canonical root skills/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO / "skills"
STATE_DIR = REPO / ".agent-infra"
MANIFEST = STATE_DIR / "runtime-skill-mirrors.json"
MANIFEST_VERSION = 1
EXPECTED_LAYOUT = "__SKILL_LAYOUT__"
DEFAULT_TARGETS = [
    REPO / ".claude" / "skills",
    REPO / ".agents" / "skills",  # Shared by Codex, Kimi Code, and OpenCode.
    REPO / ".codebuddy" / "skills",  # CodeBuddy Code and CodeBuddy IDE.
    REPO / ".workbuddy" / "skills",  # Tencent WorkBuddy project Skills.
]
NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def display(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def encoded_path(path: Path) -> str:
    return display(absolute(path))


def decoded_path(value: str) -> Path:
    path = Path(value)
    return absolute(path if path.is_absolute() else REPO / path)


def discover_skills() -> tuple[str, dict[str, Path]]:
    found: dict[str, Path] = {}
    depths: set[int] = set()
    for skill_md in sorted(SKILLS_ROOT.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        parts = skill_dir.relative_to(SKILLS_ROOT).parts
        if len(parts) not in {1, 2}:
            sys.exit(
                f"invalid skill depth: {display(skill_md)}; use skills/<skill>/SKILL.md or "
                "skills/<category>/<skill>/SKILL.md"
            )
        for part in parts:
            if not NAME_RE.fullmatch(part):
                sys.exit(f"invalid kebab-case directory name: {display(SKILLS_ROOT.joinpath(*parts))}")
        depths.add(len(parts))
        name = parts[-1]
        if name in found:
            sys.exit(f"duplicate skill name: {name} ({display(found[name])} vs {display(skill_dir)})")
        found[name] = skill_dir
    if len(depths) > 1:
        sys.exit("mixed skill layout is not supported; choose flat or categorized")
    layout = EXPECTED_LAYOUT if not depths else ("categorized" if depths == {2} else "flat")
    if layout != EXPECTED_LAYOUT:
        sys.exit(
            f"skill layout is {layout}, but this project is configured for {EXPECTED_LAYOUT}; "
            "do not introduce a mixed or alternate layout"
        )
    return layout, found


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"donald-agent-infrastructure-tree-v1\0")
    if not root.is_dir():
        return ""
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def is_junction(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    checker = getattr(path, "is_junction", None)
    if checker is not None:
        try:
            return bool(checker())
        except OSError:
            return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and not path.is_symlink()


def path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink() or is_junction(path)


def actual_mode(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if is_junction(path):
        return "junction"
    if path.is_dir():
        return "copy"
    return None


def remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif is_junction(path):
        os.rmdir(path)
    elif path.is_dir():
        shutil.rmtree(path)


def same_target(mirror: Path, source: Path) -> bool:
    try:
        return os.path.samefile(mirror, source)
    except (FileNotFoundError, OSError):
        return False


def load_manifest() -> list[dict[str, str]]:
    if not MANIFEST.exists():
        return []
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"invalid mirror manifest {display(MANIFEST)}: {exc}")
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("mirrors"), list):
        sys.exit(f"unsupported mirror manifest format: {display(MANIFEST)}")
    required = {"target", "source", "mode", "digest"}
    seen: set[str] = set()
    for entry in data["mirrors"]:
        if not isinstance(entry, dict) or not required.issubset(entry):
            sys.exit(f"invalid mirror entry in {display(MANIFEST)}")
        if any(not isinstance(entry[key], str) for key in required):
            sys.exit(f"mirror entry fields must be strings in {display(MANIFEST)}")
        if entry["mode"] not in {"junction", "symlink", "copy"}:
            sys.exit(f"invalid mirror mode {entry['mode']} in {display(MANIFEST)}")
        key = encoded_path(decoded_path(entry["target"]))
        if key in seen:
            sys.exit(f"duplicate mirror target {entry['target']} in {display(MANIFEST)}")
        seen.add(key)
    return data["mirrors"]


def save_manifest(entries: list[dict[str, str]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    content = {
        "version": MANIFEST_VERSION,
        "mirrors": sorted(entries, key=lambda entry: (entry["target"], entry["source"])),
    }
    temporary = MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MANIFEST)


def mirror_matches(path: Path, source: Path, entry: dict[str, str], digest: str) -> bool:
    mode = entry.get("mode")
    if decoded_path(entry.get("source", "")) != absolute(source):
        return False
    if entry.get("digest") != digest or actual_mode(path) != mode:
        return False
    if mode in {"junction", "symlink"}:
        return same_target(path, source)
    if mode == "copy":
        return tree_digest(path) == digest
    return False


def managed_orphan_is_unchanged(path: Path, entry: dict[str, str]) -> bool:
    mode = entry.get("mode")
    if actual_mode(path) != mode:
        return False
    if mode in {"junction", "symlink"}:
        source = decoded_path(entry.get("source", ""))
        return not source.exists() or same_target(path, source)
    if mode == "copy":
        return tree_digest(path) == entry.get("digest")
    return False


def create_junction(path: Path, source: Path) -> None:
    if sys.platform != "win32":
        raise OSError("NTFS junctions are available only on Windows")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path), str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(detail or f"mklink exited with {result.returncode}")
    if not is_junction(path):
        raise OSError("mklink returned success but did not create a junction")


def create_symlink(path: Path, source: Path) -> None:
    relative = Path(os.path.relpath(source, start=path.parent))
    path.symlink_to(relative, target_is_directory=True)
    if not path.is_symlink():
        raise OSError("symlink creation did not produce a directory symlink")


def create_copy(path: Path, source: Path) -> None:
    shutil.copytree(source, path)


def try_create(path: Path, source: Path, mode: str) -> None:
    if mode == "junction":
        create_junction(path, source)
    elif mode == "symlink":
        create_symlink(path, source)
    elif mode == "copy":
        create_copy(path, source)
    else:
        raise ValueError(f"unknown mirror mode: {mode}")


def create_mirror(path: Path, source: Path, forced_mode: str | None) -> tuple[str, list[str]]:
    if forced_mode:
        try:
            try_create(path, source, forced_mode)
        except (OSError, shutil.Error):
            if path_present(path):
                remove_existing(path)
            raise
        return forced_mode, []

    attempts = ["junction", "symlink", "copy"] if sys.platform == "win32" else ["symlink", "copy"]
    failures: list[str] = []
    for mode in attempts:
        try:
            try_create(path, source, mode)
            return mode, failures
        except (OSError, shutil.Error) as exc:
            if path_present(path):
                remove_existing(path)
            failures.append(f"{mode}: {exc}")
    raise OSError("; ".join(failures))


def selected_mode(args: argparse.Namespace) -> str | None:
    if args.junction:
        return "junction"
    if args.symlink:
        return "symlink"
    if args.copy:
        return "copy"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize generated runtime skill mirrors.")
    parser.add_argument("--check", action="store_true", help="verify without changing files")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--junction", action="store_true", help="require Windows NTFS junctions")
    modes.add_argument("--symlink", action="store_true", help="require directory symlinks")
    modes.add_argument("--copy", action="store_true", help="require copied directories")
    parser.add_argument("--replace-existing", action="store_true", help="replace unmanaged conflicting paths")
    parser.add_argument("--target", action="append", help="mirror target directory; may be repeated")
    args = parser.parse_args()

    layout, skills = discover_skills()
    targets = [absolute(Path(value).expanduser()) for value in args.target] if args.target else DEFAULT_TARGETS
    targets = [absolute(path) for path in targets]
    forced_mode = selected_mode(args)
    manifest_exists = MANIFEST.exists()
    old_entries = load_manifest()
    old_by_target = {encoded_path(decoded_path(entry["target"])): entry for entry in old_entries}
    selected_roots = {encoded_path(target) for target in targets}
    planned = {
        encoded_path(target / name): (target / name, source)
        for target in targets
        for name, source in skills.items()
    }
    stale: list[str] = []
    if not manifest_exists:
        stale.append(f"{display(MANIFEST)} (missing manifest)")

    for key, (mirror, source) in planned.items():
        entry = old_by_target.get(key)
        digest = tree_digest(source)
        if entry is None:
            stale.append(f"{display(mirror)} (missing manifest entry)")
        elif forced_mode and entry.get("mode") != forced_mode:
            stale.append(f"{display(mirror)} (expected {forced_mode}, found {entry.get('mode', 'unknown')})")
        elif not mirror_matches(mirror, source, entry, digest):
            stale.append(f"{display(mirror)} (content or target drift)")

    orphan_entries = []
    for entry in old_entries:
        target = decoded_path(entry["target"])
        if encoded_path(target.parent) in selected_roots and encoded_path(target) not in planned:
            orphan_entries.append(entry)
            stale.append(f"{display(target)} (managed orphan)")

    if args.check:
        if stale:
            print(
                "Out of sync (rerun scripts/agent-skills/sync_runtime_skills.py):\n  "
                + "\n  ".join(stale)
            )
            return 1
        modes_used = sorted({entry["mode"] for entry in old_entries if encoded_path(decoded_path(entry["target"])) in planned})
        print(
            f"In sync: layout={layout}, {len(skills)} skills mirrored to {len(targets)} "
            f"runtime target(s) using {', '.join(modes_used) or 'no mirrors'}."
        )
        return 0

    for key, (mirror, _) in planned.items():
        if path_present(mirror) and key not in old_by_target and not args.replace_existing:
            raise SystemExit(
                f"{display(mirror)} exists and is not managed; rerun with --replace-existing "
                "after preserving any hand edits"
            )
    for entry in orphan_entries:
        orphan = decoded_path(entry["target"])
        if path_present(orphan) and not managed_orphan_is_unchanged(orphan, entry):
            raise SystemExit(
                f"managed orphan {display(orphan)} has been modified; preserve it manually before cleanup"
            )

    for entry in orphan_entries:
        orphan = decoded_path(entry["target"])
        if path_present(orphan):
            remove_existing(orphan)

    retained = [
        entry
        for entry in old_entries
        if encoded_path(decoded_path(entry["target"]).parent) not in selected_roots
    ]
    new_entries: list[dict[str, str]] = []
    preferred_mode = forced_mode or ("junction" if sys.platform == "win32" else "symlink")

    for key, (mirror, source) in planned.items():
        digest = tree_digest(source)
        existing_entry = old_by_target.get(key)
        keep_existing = (
            existing_entry is not None
            and existing_entry.get("mode") == preferred_mode
            and mirror_matches(mirror, source, existing_entry, digest)
        )
        if keep_existing:
            mode = existing_entry["mode"]
            failures: list[str] = []
        else:
            if path_present(mirror):
                if existing_entry is not None or args.replace_existing:
                    remove_existing(mirror)
                else:
                    raise SystemExit(
                        f"{display(mirror)} exists and is not managed; rerun with --replace-existing "
                        "after preserving any hand edits"
                    )
            mirror.parent.mkdir(parents=True, exist_ok=True)
            try:
                mode, failures = create_mirror(mirror, source, forced_mode)
            except (OSError, shutil.Error) as exc:
                raise SystemExit(f"failed to create {display(mirror)}: {exc}") from exc
        if failures:
            print(f"warning: {display(mirror)} fallback after {'; '.join(failures)}", file=sys.stderr)
        if mode == "copy" and forced_mode != "copy":
            print(
                f"warning: {display(mirror)} uses copy fallback; rerun sync after canonical changes",
                file=sys.stderr,
            )
        print(f"{display(mirror)} -> {display(source)} [{mode}]")
        new_entries.append(
            {
                "target": encoded_path(mirror),
                "source": encoded_path(source),
                "mode": mode,
                "digest": digest,
            }
        )

    save_manifest(retained + new_entries)
    print(f"Mirrored layout={layout}, {len(skills)} skills to {len(targets)} runtime target(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
