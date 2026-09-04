#!/usr/bin/env python3
"""Initialize agent-led skill infrastructure in a new project repository."""

from __future__ import annotations

import argparse
import re
import stat
import textwrap
from pathlib import Path


NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
GITIGNORE_BEGIN = "# BEGIN donald-agent-infrastructure generated"
GITIGNORE_END = "# END donald-agent-infrastructure generated"
TEMPLATES = Path(__file__).resolve().parents[1] / "assets" / "templates"


def project_title(repo: Path, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    return repo.name.replace("-", " ").replace("_", " ").title()


def write_file(
    path: Path,
    content: str,
    *,
    force: bool,
    dry_run: bool,
    executable: bool = False,
) -> str:
    if path.exists() and not force:
        return f"skipped existing {path}"
    if dry_run:
        return f"would write {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"wrote {path}"


def template_text(name: str) -> str:
    path = TEMPLATES / name
    if not path.is_file():
        raise SystemExit(f"missing scaffold template: {path}")
    return path.read_text(encoding="utf-8")


def validate_existing_layout(skills_root: Path, expected_layout: str) -> None:
    if not skills_root.exists():
        return
    expected_depth = 1 if expected_layout == "flat" else 2
    seen: dict[str, Path] = {}
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        parts = skill_md.parent.relative_to(skills_root).parts
        if len(parts) != expected_depth:
            raise SystemExit(
                f"existing skill {skill_md} does not match --layout {expected_layout}; "
                "mixed and deeper layouts are not supported for new scaffolds"
            )
        for part in parts:
            if not NAME_RE.fullmatch(part):
                raise SystemExit(f"existing skill path is not kebab-case: {skill_md.parent}")
        name = parts[-1]
        if name in seen:
            raise SystemExit(f"duplicate skill name: {name} ({seen[name]} vs {skill_md})")
        seen[name] = skill_md


def managed_gitignore(has_subagents: bool) -> str:
    lines = [
        GITIGNORE_BEGIN,
        ".claude/skills/",
        ".agents/skills/",
        ".codebuddy/skills/",
        ".workbuddy/skills/",
        ".agent-infra/",
    ]
    if has_subagents:
        lines.extend([".claude/agents/", ".codex/agents/", ".codebuddy/agents/", "agents/INDEX.md"])
    lines.append(GITIGNORE_END)
    return "\n".join(lines)


def update_gitignore(repo: Path, *, has_subagents: bool, dry_run: bool) -> str:
    path = repo / ".gitignore"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if (GITIGNORE_BEGIN in current) != (GITIGNORE_END in current):
        raise SystemExit(f"incomplete managed block in {path}")
    block = managed_gitignore(has_subagents)
    if GITIGNORE_BEGIN in current:
        start = current.index(GITIGNORE_BEGIN)
        end = current.index(GITIGNORE_END, start) + len(GITIGNORE_END)
        updated = current[:start] + block + current[end:]
    else:
        separator = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
        updated = current + separator + block + "\n"
    if updated == current:
        return f"unchanged {path}"
    if dry_run:
        return f"would update {path}"
    path.write_text(updated, encoding="utf-8")
    return f"updated {path}"


def skill_path(layout: str, name: str, category: str) -> Path:
    return Path(name) if layout == "flat" else Path(category) / name


def agents_md(
    title: str,
    *,
    layout: str,
    entry_path: Path | None,
    governance_path: Path | None,
    has_subagents: bool,
) -> str:
    expected = "skills/<skill>/SKILL.md" if layout == "flat" else "skills/<category>/<skill>/SKILL.md"
    content = textwrap.dedent(
        f"""\
        # Agent Instructions

        This is {title}. This file is the canonical cross-runtime operating contract for the
        repository.

        ## Project Skills

        - Treat root `skills/` as the canonical project skill source.
        - This project uses the `{layout}` layout: `{expected}`.
        - Do not introduce mixed layouts or duplicate skill names.
        - Treat `.claude/skills/`, `.agents/skills/`, `.codebuddy/skills/`, and
          `.workbuddy/skills/` as generated output.
        - Kimi Code and OpenCode reuse `.agents/skills/`; CodeBuddy uses `.codebuddy/skills/`;
          Tencent WorkBuddy uses `.workbuddy/skills/`.
        - Keep every skill self-contained. Do not import or read a sibling skill by repository path.

        ## Repo Boundaries

        - Work from the repo root.
        - Do not hand-edit generated runtime mirrors.
        - After changing canonical skills, run
          `python scripts/agent-skills/sync_runtime_skills.py` and then its `--check` mode.
        - Do not commit runtime data as source; promote small stable examples into tests or fixtures.
        """
    )
    if entry_path:
        content += textwrap.dedent(
            f"""

            ## Entry Routing

            Use `{entry_path.as_posix()}/SKILL.md` when a request needs project introduction,
            capability inventory, mode selection, or cross-skill routing. Requests that already map
            cleanly to one project skill do not need to pass through the entry skill.
            """
        )
    if governance_path:
        content += textwrap.dedent(
            f"""

            ## Skill Governance

            Use `{governance_path.as_posix()}/SKILL.md` after changing canonical skills or runtime
            mirrors. Treat audit output as evidence for agent judgment, not semantic routing.
            """
        )
    if has_subagents:
        content += textwrap.dedent(
            """

            ## Project Subagents

            Treat `agents/registry.yaml` and its skill-owned behavior specifications as canonical.
            Run `python agents/sync_agents.py` after registry changes and verify with `--check`.
            Treat `.claude/agents/`, `.codex/agents/`, `.codebuddy/agents/`, and
            `agents/INDEX.md` as generated output.
            """
        )
    return content


def claude_md() -> str:
    return textwrap.dedent(
        """\
        # Claude Code Instructions

        Read and follow `AGENTS.md`; it is the canonical project operating contract.

        Keep only Claude-specific additions here. Do not duplicate shared project rules.
        """
    )


def codebuddy_md() -> str:
    return textwrap.dedent(
        """\
        # WorkBuddy / CodeBuddy Code Instructions

        Read and follow `AGENTS.md`; it is the canonical project operating contract.

        Keep only CodeBuddy-specific additions here. Do not duplicate shared project rules.
        """
    )


def skills_readme(title: str) -> str:
    return textwrap.dedent(
        f"""\
        # Project Skills

        Root `skills/` is the canonical source for {title}'s categorized agent workflows. Every
        skill uses `skills/<category>/<skill>/SKILL.md`; mixed and deeper layouts are invalid.

        Categories are project-defined. Common examples include `application/`, `collection/`,
        `distillation/`, `review/`, `orchestration/`, and `development/`.

        A skill requires only `SKILL.md`. Add `references/`, `scripts/`, `assets/`, `evals/`, or
        `agents/openai.yaml` only when the workflow needs them. Keep every skill self-contained.

        Synchronize and verify runtime mirrors from the repository root:

        ```bash
        python scripts/agent-skills/sync_runtime_skills.py
        python scripts/agent-skills/sync_runtime_skills.py --check
        ```
        """
    )


def enter_skill(title: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        name: enter-project
        description: Use when a user enters {title}, asks what it can do, or gives a request that needs project capability discovery, mode selection, or cross-skill routing.
        ---

        # Enter Project

        Classify the request, read lightweight project observations only when needed, and route to
        the narrowest project skill.

        1. Classify the request as introduction, capability inventory, user workflow, builder work,
           review, maintenance, orchestration, or unsupported.
        2. Read only the observations needed for that classification.
        3. Route internally to the narrowest downstream skill.
        4. Answer with outcomes; hide raw commands unless the user asks for them.
        5. Explain capability gaps honestly when no project skill fits.

        Use project skills before generic skills when project-specific evidence, safety, runtime
        state, or workflow boundaries matter. Runtime observations inform agent judgment but do not
        make semantic routing decisions.

        Read `references/internal-runtime.md` for observations and
        `references/output-contract.md` for the response contract.
        """
    )


def enter_internal_runtime() -> str:
    return textwrap.dedent(
        """\
        # Internal Runtime

        Add only lightweight observation commands here. Replace these examples with project facts:

        ```bash
        git status --short
        find skills -name SKILL.md -print | sort
        python scripts/agent-skills/sync_runtime_skills.py --check
        ```
        """
    )


def enter_output_contract() -> str:
    return textwrap.dedent(
        """\
        # Enter Project Output Contract

        For introductions or inventory questions, return a short project summary, current facts
        from relevant observations, and available next outcomes. For concrete requests, identify
        the intent, observations used, next skill, and user-facing result. Do not expose raw CLI
        JSON or internal file dumps as the main answer.
        """
    )


def review_skill() -> str:
    return textwrap.dedent(
        """\
        ---
        name: review-skill-best-practices
        description: Use when creating, editing, reviewing, or accepting project skills, especially after adding, renaming, moving, or deleting a canonical skill.
        ---

        # Review Skill Best Practices

        Review canonical project skills under root `skills/`. Confirm valid frontmatter, concise
        instructions, self-contained resources, valid required sub-skill dependencies, and generated
        runtime mirrors.

        Read `references/internal-runtime.md` for exact checks and
        `references/output-contract.md` for the review result.
        """
    )


def review_internal_runtime(review_path: Path, layout: str) -> str:
    audit = review_path / "scripts" / "audit_project_skills.py"
    return textwrap.dedent(
        f"""\
        # Internal Runtime

        Run from the repository root:

        ```bash
        python {audit.as_posix()} --skills-root skills --layout {layout} --strict
        python scripts/agent-skills/sync_runtime_skills.py --check
        ```

        The audit checks the project layout and repository conventions. It does not replace agent
        judgment about workflow quality.
        """
    )


def review_output_contract() -> str:
    return textwrap.dedent(
        """\
        # Review Output Contract

        Return status, skills checked, blocking issues, warnings, runtime mirror status, and the
        verification commands that ran.
        """
    )


def agent_registry_yaml() -> str:
    return textwrap.dedent(
        """\
        # Project Subagent Registry. Keep behavior in skill-owned reference files.

        model_tiers:
          default:
            claude: sonnet
            codex: gpt-5
            codex_reasoning_effort: low
            codebuddy: inherit

        agents: []
        """
    )


def agents_readme() -> str:
    return textwrap.dedent(
        """\
        # Project Subagents

        `agents/registry.yaml` is the canonical metadata and runtime-wiring source. Keep each
        subagent's behavior in an owning skill's `references/*-agent.md` or
        `references/*-prompt.md` file.

        Generate and verify Claude, Codex, and Tencent WorkBuddy/CodeBuddy Code definitions from
        the repository root:

        ```bash
        python agents/sync_agents.py
        python agents/sync_agents.py --check
        ```

        `.claude/agents/*.md`, `.codex/agents/*.toml`, `.codebuddy/agents/*.md`, and
        `agents/INDEX.md` are generated. An empty registry is valid and intentionally creates no
        fictional worker roles. CodeBuddy defaults to the current session model; set a tier's
        `codebuddy` or `codebuddy_effort`, or an agent's `codebuddy_tools`, only when that role
        needs a runtime-specific override.
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize project agent infrastructure.")
    parser.add_argument("repo", help="Target repository path.")
    parser.add_argument("--project-name", help="Human-readable project name.")
    parser.add_argument(
        "--layout",
        choices=("flat", "categorized"),
        default="flat",
        help="Canonical skill directory layout (default: flat).",
    )
    parser.add_argument("--with-entry", action="store_true", help="Scaffold the project entry skill.")
    parser.add_argument(
        "--with-governance",
        action="store_true",
        help="Scaffold the project skill review and audit layer.",
    )
    parser.add_argument(
        "--with-subagents",
        action="store_true",
        help="Scaffold the Claude/Codex/CodeBuddy subagent registry and generator.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned writes without changes.")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"repo does not exist: {repo}")
    validate_existing_layout(repo / "skills", args.layout)

    title = project_title(repo, args.project_name)
    entry_relative = skill_path(args.layout, "enter-project", "application") if args.with_entry else None
    governance_relative = (
        skill_path(args.layout, "review-skill-best-practices", "development")
        if args.with_governance
        else None
    )
    entry_project_path = Path("skills") / entry_relative if entry_relative else None
    governance_project_path = Path("skills") / governance_relative if governance_relative else None

    writes = [
        write_file(
            repo / "AGENTS.md",
            agents_md(
                title,
                layout=args.layout,
                entry_path=entry_project_path,
                governance_path=governance_project_path,
                has_subagents=args.with_subagents,
            ),
            force=args.force,
            dry_run=args.dry_run,
        ),
        write_file(repo / "CLAUDE.md", claude_md(), force=args.force, dry_run=args.dry_run),
        write_file(repo / "CODEBUDDY.md", codebuddy_md(), force=args.force, dry_run=args.dry_run),
    ]

    if args.layout == "categorized":
        writes.append(
            write_file(repo / "skills" / "README.md", skills_readme(title), force=args.force, dry_run=args.dry_run)
        )

    if entry_relative:
        entry_dir = repo / "skills" / entry_relative
        writes.extend(
            [
                write_file(entry_dir / "SKILL.md", enter_skill(title), force=args.force, dry_run=args.dry_run),
                write_file(
                    entry_dir / "references" / "internal-runtime.md",
                    enter_internal_runtime(),
                    force=args.force,
                    dry_run=args.dry_run,
                ),
                write_file(
                    entry_dir / "references" / "output-contract.md",
                    enter_output_contract(),
                    force=args.force,
                    dry_run=args.dry_run,
                ),
            ]
        )

    if governance_relative:
        review_dir = repo / "skills" / governance_relative
        audit_source = Path(__file__).with_name("audit_project_skills.py").read_text(encoding="utf-8")
        writes.extend(
            [
                write_file(review_dir / "SKILL.md", review_skill(), force=args.force, dry_run=args.dry_run),
                write_file(
                    review_dir / "references" / "internal-runtime.md",
                    review_internal_runtime(Path("skills") / governance_relative, args.layout),
                    force=args.force,
                    dry_run=args.dry_run,
                ),
                write_file(
                    review_dir / "references" / "output-contract.md",
                    review_output_contract(),
                    force=args.force,
                    dry_run=args.dry_run,
                ),
                write_file(
                    review_dir / "scripts" / "audit_project_skills.py",
                    audit_source,
                    force=args.force,
                    dry_run=args.dry_run,
                    executable=True,
                ),
            ]
        )

    writes.append(
        write_file(
            repo / "scripts" / "agent-skills" / "sync_runtime_skills.py",
            template_text("sync_runtime_skills.py").replace("__SKILL_LAYOUT__", args.layout),
            force=args.force,
            dry_run=args.dry_run,
            executable=True,
        )
    )

    if args.with_subagents:
        writes.extend(
            [
                write_file(repo / "agents" / "README.md", agents_readme(), force=args.force, dry_run=args.dry_run),
                write_file(
                    repo / "agents" / "registry.yaml",
                    agent_registry_yaml(),
                    force=args.force,
                    dry_run=args.dry_run,
                ),
                write_file(
                    repo / "agents" / "sync_agents.py",
                    template_text("sync_agents.py"),
                    force=args.force,
                    dry_run=args.dry_run,
                    executable=True,
                ),
            ]
        )

    writes.append(update_gitignore(repo, has_subagents=args.with_subagents, dry_run=args.dry_run))
    print("\n".join(writes))
    print(
        f"Initialized layout={args.layout} entry={'enabled' if args.with_entry else 'disabled'} "
        f"governance={'enabled' if args.with_governance else 'disabled'} "
        f"subagents={'enabled' if args.with_subagents else 'disabled'} for {repo}"
    )
    print(
        "Next: customize generated contracts and skills, run "
        "scripts/agent-skills/sync_runtime_skills.py, then verify it with --check."
    )
    if args.with_subagents:
        print("Define project roles in agents/registry.yaml, then run agents/sync_agents.py --check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
