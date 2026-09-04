#!/usr/bin/env python3
"""Audit project-local agent skills.

The audit intentionally supports both flat and categorized skill trees:

  skills/<skill>/SKILL.md
  skills/<category>/<skill>/SKILL.md

It also tolerates mixed layouts during migration. Duplicate skill names are always errors.
Optional resources stay optional; --strict enforces recommended authoring limits.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
}


@dataclass(frozen=True)
class Issue:
    severity: str
    skill: str
    path: str
    rule: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "skill": self.skill,
            "path": self.path,
            "rule": self.rule,
            "message": self.message,
        }


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    values: dict[str, str] = {}
    body_start = 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = idx + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[body_start:])
    return values, body


def parse_simple_yaml(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "interface:":
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def local_markdown_links(markdown: str) -> list[str]:
    links = re.findall(r"\]\(([^)]+)\)", markdown)
    return [
        link
        for link in links
        if not link.startswith(("http://", "https://", "#", "mailto:"))
    ]


def discover_skills(skills_root: Path, skip_dirs: set[str]) -> tuple[list[Path], list[Issue]]:
    issues: list[Issue] = []
    skill_files = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        rel_parts = skill_md.relative_to(skills_root).parts
        if any(part in skip_dirs for part in rel_parts):
            continue
        skill_files.append(skill_md)
    by_name: dict[str, Path] = {}
    for skill_md in skill_files:
        name = skill_md.parent.name
        if name in by_name:
            issues.append(
                Issue(
                    "error",
                    name,
                    rel(skill_md, skills_root.parent),
                    "duplicate_name",
                    f"Duplicate skill name also found at {rel(by_name[name], skills_root.parent)}.",
                )
            )
        else:
            by_name[name] = skill_md
    return skill_files, issues


def audit_skill(skill_md: Path, repo_root: Path, strict: bool) -> list[Issue]:
    issues: list[Issue] = []
    skill_dir = skill_md.parent
    skill_name = skill_dir.name
    skill_path = rel(skill_md, repo_root)
    frontmatter, body = parse_frontmatter(skill_md)

    def issue(severity: str, rule: str, message: str, path: Path = skill_md) -> None:
        issues.append(Issue(severity, skill_name, rel(path, repo_root), rule, message))

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not frontmatter:
        issue("error", "frontmatter", "Missing YAML frontmatter.")
    if not name:
        issue("error", "frontmatter.name", "Missing name.")
    elif name != skill_name:
        issue("error", "frontmatter.name", "Name must match the skill directory.")
    elif not NAME_RE.match(name):
        issue("error", "frontmatter.name", "Use lowercase hyphen-case under 64 characters.")

    if not description:
        issue("error", "frontmatter.description", "Missing description.")
    else:
        if strict and "use when" not in description.lower():
            issue(
                "warning",
                "frontmatter.description.trigger",
                "Describe both what the skill does and when it should be used.",
            )
        if len(description) > 1024:
            issue(
                "error",
                "frontmatter.description.length",
                "Description exceeds the 1024-character specification limit.",
            )

    if len(body.splitlines()) > 500:
        issue(
            "error" if strict else "warning",
            "body.length",
            "SKILL.md exceeds 500 lines; move conditional detail to focused references/.",
        )

    openai_yaml = skill_dir / "agents" / "openai.yaml"
    if openai_yaml.exists():
        values = parse_simple_yaml(openai_yaml)
        default_prompt = values.get("default_prompt", "")
        if default_prompt and f"${skill_name}" not in default_prompt:
            issue(
                "error",
                "agents.default_prompt",
                "When present, default_prompt must reference the skill as $<skill-name>.",
                openai_yaml,
            )

    for link in local_markdown_links(body):
        target = (skill_dir / link).resolve()
        if not target.exists():
            issue("error", "links.local", f"Linked local reference does not exist: {link}")

    if not body.strip():
        issue("error", "body.empty", "SKILL.md body is empty.")

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit project-local skills.")
    parser.add_argument("--skills-root", default="skills", help="Path to canonical skills root.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce recommended size limits and check trigger guidance; optional resources remain optional.",
    )
    parser.add_argument(
        "--include-vendored",
        action="store_true",
        help="Do not skip node_modules, build outputs, virtualenvs, or cache directories.",
    )
    args = parser.parse_args(argv)

    skills_root = Path(args.skills_root).expanduser().resolve()
    repo_root = skills_root.parent
    if not skills_root.exists():
        print(json.dumps({"status": "failed", "errors": [{"message": f"skills root not found: {skills_root}"}]}, indent=2))
        return 1

    skip_dirs = set() if args.include_vendored else DEFAULT_SKIP_DIRS
    skill_files, issues = discover_skills(skills_root, skip_dirs)
    for skill_md in skill_files:
        issues.extend(audit_skill(skill_md, repo_root, args.strict))

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    report = {
        "status": "failed" if errors else "passed",
        "skills_root": str(skills_root),
        "skills_checked": len(skill_files),
        "skipped_directory_names": sorted(skip_dirs),
        "errors": [issue.as_dict() for issue in errors],
        "warnings": [issue.as_dict() for issue in warnings],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
