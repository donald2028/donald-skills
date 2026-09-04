#!/usr/bin/env python3
"""Audit project-local Agent Skills structure and repository conventions."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
DEPENDENCY_RE = re.compile(
    r"\*\*REQUIRED SUB-SKILL:\*\*\s*Invoke\s+`?([a-z0-9]+(?:-[a-z0-9]+)*)`?"
)
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
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str, bool, bool]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, False, False

    values: dict[str, str] = {}
    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    body = "" if closing_index is None else "\n".join(lines[closing_index + 1 :])
    return values, body, True, closing_index is not None


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
    return [link for link in links if not link.startswith(("http://", "https://", "#", "mailto:"))]


def clean_link(link: str) -> str:
    value = link.strip().strip("<>")
    if " " in value:
        value = value.split(" ", 1)[0]
    return value.split("#", 1)[0]


def discover_skills(
    skills_root: Path,
    repo_root: Path,
    skip_dirs: set[str],
    strict: bool,
) -> tuple[list[Path], list[Issue], str, list[str]]:
    issues: list[Issue] = []
    skill_files: list[Path] = []
    depths: set[int] = set()
    categories: set[str] = set()
    by_name: dict[str, Path] = {}

    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        relative = skill_md.relative_to(skills_root)
        if any(part in skip_dirs for part in relative.parts):
            continue
        skill_files.append(skill_md)
        parts = skill_md.parent.relative_to(skills_root).parts
        name = skill_md.parent.name
        depth = len(parts)
        depths.add(depth)

        severity = "error" if strict else "warning"
        if depth not in {1, 2}:
            issues.append(
                Issue(
                    severity,
                    name,
                    rel(skill_md, repo_root),
                    "layout.depth",
                    "Use skills/<skill>/SKILL.md or skills/<category>/<skill>/SKILL.md.",
                )
            )
        if depth == 2:
            categories.add(parts[0])
            if not NAME_RE.fullmatch(parts[0]):
                issues.append(
                    Issue(
                        "error",
                        name,
                        rel(skill_md.parent, repo_root),
                        "layout.category_name",
                        "Category directory must use lowercase kebab-case under 64 characters.",
                    )
                )
        if name in by_name:
            issues.append(
                Issue(
                    "error",
                    name,
                    rel(skill_md, repo_root),
                    "duplicate_name",
                    f"Duplicate skill name also found at {rel(by_name[name], repo_root)}.",
                )
            )
        else:
            by_name[name] = skill_md

    supported_depths = depths & {1, 2}
    invalid_depths = depths - {1, 2}
    if len(supported_depths) > 1:
        issues.append(
            Issue(
                "error" if strict else "warning",
                "<project>",
                rel(skills_root, repo_root),
                "layout.mixed",
                "Mixed flat and categorized layouts are migration-only; choose one layout.",
            )
        )
    if invalid_depths:
        layout = "invalid"
    elif supported_depths == {1, 2}:
        layout = "mixed"
    elif supported_depths == {2}:
        layout = "categorized"
    elif supported_depths == {1}:
        layout = "flat"
    else:
        layout = "empty"
    return skill_files, issues, layout, sorted(categories)


def audit_skill(skill_md: Path, repo_root: Path, strict: bool) -> tuple[list[Issue], list[str]]:
    issues: list[Issue] = []
    skill_dir = skill_md.parent
    skill_name = skill_dir.name
    frontmatter, body, has_frontmatter, closed = parse_frontmatter(skill_md)

    def issue(severity: str, rule: str, message: str, path: Path = skill_md) -> None:
        issues.append(Issue(severity, skill_name, rel(path, repo_root), rule, message))

    if not has_frontmatter:
        issue("error", "frontmatter", "Missing YAML frontmatter.")
    elif not closed:
        issue("error", "frontmatter.closing", "Missing closing --- delimiter.")

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if not name:
        issue("error", "frontmatter.name", "Missing name.")
    elif name != skill_name:
        issue("error", "frontmatter.name", "Name must match the skill directory.")
    elif not NAME_RE.fullmatch(name):
        issue("error", "frontmatter.name", "Use lowercase kebab-case under 64 characters.")

    if not description:
        issue("error", "frontmatter.description", "Missing description.")
    elif len(description) > 1024:
        issue("error", "frontmatter.description.length", "Description exceeds 1024 characters.")

    if len(body.splitlines()) > 500:
        issue(
            "error" if strict else "warning",
            "body.length",
            "SKILL.md exceeds 500 lines; move conditional detail to focused references/.",
        )
    if not body.strip():
        issue("error", "body.empty", "SKILL.md body is empty.")

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

    skill_root = skill_dir.resolve()
    for raw_link in local_markdown_links(body):
        link = clean_link(raw_link)
        if not link:
            continue
        target = (skill_dir / link).resolve()
        if not target.is_relative_to(skill_root):
            issue(
                "error" if strict else "warning",
                "links.escape",
                f"Local reference escapes the skill directory: {raw_link}",
            )
        elif not target.exists():
            issue("error", "links.local", f"Linked local reference does not exist: {raw_link}")

    dependencies = sorted(set(DEPENDENCY_RE.findall(skill_md.read_text(encoding="utf-8"))))
    if skill_name in dependencies:
        issue("error", "dependency.self", "A skill cannot require itself.")
    return issues, dependencies


def dependency_issues(
    graph: dict[str, list[str]],
    skill_paths: dict[str, Path],
    repo_root: Path,
) -> list[Issue]:
    issues: list[Issue] = []
    names = set(graph)
    for skill, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in names:
                issues.append(
                    Issue(
                        "error",
                        skill,
                        rel(skill_paths[skill], repo_root),
                        "dependency.missing",
                        f"Required sub-skill does not exist: {dependency}.",
                    )
                )

    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(skill: str) -> None:
        state[skill] = 1
        stack.append(skill)
        for dependency in graph.get(skill, []):
            if dependency not in graph:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycles.add(tuple(stack[start:] + [dependency]))
        stack.pop()
        state[skill] = 2

    for skill in sorted(graph):
        if state.get(skill, 0) == 0:
            visit(skill)
    for cycle in sorted(cycles):
        owner = cycle[0]
        issues.append(
            Issue(
                "error",
                owner,
                rel(skill_paths[owner], repo_root),
                "dependency.cycle",
                "Required sub-skill cycle: " + " -> ".join(cycle),
            )
        )
    return issues


def mirror_state(repo_root: Path) -> tuple[dict, list[Issue]]:
    manifest = repo_root / ".agent-infra" / "runtime-skill-mirrors.json"
    if not manifest.exists():
        return {"status": "not_initialized", "manifest": rel(manifest, repo_root), "entries": 0}, []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entries = data.get("mirrors", [])
        if not isinstance(entries, list):
            raise ValueError("mirrors must be a list")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        issue = Issue(
            "error",
            "<project>",
            rel(manifest, repo_root),
            "mirror.manifest",
            f"Invalid runtime mirror manifest: {exc}",
        )
        return {"status": "invalid", "manifest": rel(manifest, repo_root), "entries": 0}, [issue]
    return (
        {
            "status": "recorded",
            "manifest": rel(manifest, repo_root),
            "entries": len(entries),
            "modes": sorted({entry.get("mode", "unknown") for entry in entries if isinstance(entry, dict)}),
            "targets": sorted({entry.get("target", "") for entry in entries if isinstance(entry, dict)}),
        },
        [],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit project-local skills and layout conventions.")
    parser.add_argument("--skills-root", default="skills", help="Path to canonical skills root.")
    parser.add_argument(
        "--layout",
        choices=("flat", "categorized"),
        help="Expected project layout; fail when the detected non-empty layout differs.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Make migration-only layouts, escaping references, and authoring limits fail.",
    )
    parser.add_argument(
        "--include-vendored",
        action="store_true",
        help="Do not skip dependencies, build outputs, virtualenvs, or cache directories.",
    )
    args = parser.parse_args(argv)

    skills_root = Path(args.skills_root).expanduser().resolve()
    repo_root = skills_root.parent
    if not skills_root.exists():
        print(
            json.dumps(
                {"status": "failed", "errors": [{"message": f"skills root not found: {skills_root}"}]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    skip_dirs = set() if args.include_vendored else DEFAULT_SKIP_DIRS
    skill_files, issues, layout, categories = discover_skills(skills_root, repo_root, skip_dirs, args.strict)
    if args.layout and layout not in {"empty", args.layout}:
        issues.append(
            Issue(
                "error",
                "<project>",
                rel(skills_root, repo_root),
                "layout.expected",
                f"Expected {args.layout} layout but detected {layout}.",
            )
        )
    graph: dict[str, list[str]] = {}
    skill_paths: dict[str, Path] = {}
    for skill_md in skill_files:
        skill_name = skill_md.parent.name
        skill_paths.setdefault(skill_name, skill_md)
        skill_issues, dependencies = audit_skill(skill_md, repo_root, args.strict)
        issues.extend(skill_issues)
        graph.setdefault(skill_name, dependencies)
    issues.extend(dependency_issues(graph, skill_paths, repo_root))
    mirrors, mirror_issues = mirror_state(repo_root)
    issues.extend(mirror_issues)

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    report = {
        "status": "failed" if errors else "passed",
        "skills_root": str(skills_root),
        "layout": layout,
        "expected_layout": args.layout,
        "categories": categories,
        "skills_checked": len(skill_files),
        "dependency_graph": graph,
        "mirror_state": mirrors,
        "skipped_directory_names": sorted(skip_dirs),
        "errors": [issue.as_dict() for issue in errors],
        "warnings": [issue.as_dict() for issue in warnings],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
