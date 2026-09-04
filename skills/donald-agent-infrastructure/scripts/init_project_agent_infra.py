#!/usr/bin/env python3
"""Initialize agent-led skill infrastructure in a project repo.

This script is intentionally conservative. It writes missing files, but does not overwrite
existing project contracts or mirrors unless --force or --replace-existing is passed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import textwrap
from pathlib import Path


SKILL_NAME_RE = re.compile(r"[^a-z0-9]+")


def clean_name(value: str) -> str:
    value = value.lower().strip()
    value = SKILL_NAME_RE.sub("-", value).strip("-")
    return value or "project"


def project_title(repo: Path, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    return repo.name.replace("-", " ").replace("_", " ").title()


def write_file(path: Path, content: str, *, force: bool, dry_run: bool, executable: bool = False) -> str:
    if path.exists() and not force:
        return f"skipped existing {path}"
    if dry_run:
        return f"would write {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    if executable:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"wrote {path}"


def agents_md(title: str, *, has_entry: bool, governance: bool, has_subagents: bool) -> str:
    content = textwrap.dedent(
        f"""\
        # Agent Instructions

        This is {title}. This file is the canonical cross-runtime operating contract for the
        repository.

        ## Project Skills

        - Treat root `skills/` as the canonical project skill source.
        - Treat `.claude/skills/`, `.agents/skills/`, and `.codebuddy/skills/` as generated runtime output.
        - Kimi Code uses `AGENTS.md` and the shared `.agents/skills/` mirror.
        - WorkBuddy / CodeBuddy Code uses `CODEBUDDY.md` and `.codebuddy/skills/`.
        - Keep a skill self-contained and add references, scripts, assets, evaluations, or
          runtime-specific metadata only when the workflow needs them.

        ## Repo Boundaries

        - Work from the repo root.
        - Do not hand-edit generated runtime skill mirrors.
        - Do not commit runtime data as source; promote small stable examples into tests or fixtures.
        """
    )
    if has_entry:
        content += textwrap.dedent(
            """

            ## Entry Routing

            Use `skills/application/enter-project/SKILL.md` only when a request needs project
            introduction, capability inventory, mode selection, or cross-skill routing. A request
            that already maps cleanly to one project skill does not need to pass through the entry
            skill first.
            """
        )
    if governance:
        content += textwrap.dedent(
            """

            ## Skill Governance

            Run the project skill review workflow after changing canonical skills or their runtime
            mirrors. Treat audit output as evidence for agent judgment, not as semantic routing.
            """
        )
    if has_subagents:
        content += textwrap.dedent(
            """

            ## Project Subagents

            Treat `agents/registry.yaml` and its referenced specifications as canonical source.
            Treat `.claude/agents/`, `.codex/agents/`, and `agents/INDEX.md` as generated output.
            """
        )
    return content


def claude_md() -> str:
    return textwrap.dedent(
        """\
        # Claude Code Instructions

        Read and follow `AGENTS.md`; it is the canonical project operating contract.

        Keep only Claude-specific additions in this file. Do not duplicate shared project rules.
        """
    )


def codebuddy_md() -> str:
    return textwrap.dedent(
        """\
        # WorkBuddy / CodeBuddy Code Instructions

        Read and follow `AGENTS.md`; it is the canonical project operating contract.

        Keep only CodeBuddy-specific additions in this file. Do not duplicate shared project rules.
        """
    )


def skills_readme(title: str) -> str:
    return textwrap.dedent(
        f"""\
        # Project Skills

        Root `skills/` is the canonical source of truth for {title}'s agent-led workflows.

        ## Recommended Categories

        - `application/`: project entry, request routing, user-facing application flows.
        - `collection/`: source gathering, import, refresh, or backfill workflows.
        - `distillation/`: synthesis, packaging, compiling, or transforming workflows.
        - `review/`: human or agent review workflows.
        - `orchestration/`: multi-skill or scheduled workflows.
        - `development/`: project skill maintenance and quality gates.

        Every project skill requires:

        - `SKILL.md`

        Add supporting resources only when they improve the workflow:

        - `references/` for substantial or conditional guidance
        - `scripts/` for repeated deterministic operations
        - `assets/` for templates or files used in generated output
        - `evals/` for behavioral evaluation of complex or risky skills
        - `agents/openai.yaml` for optional OpenAI UI metadata

        ## Runtime Mirrors

        `.claude/skills/` (Claude Code), `.agents/skills/` (Codex, Kimi Code, OpenCode), and
        `.codebuddy/skills/` (WorkBuddy / CodeBuddy Code) are generated mirrors.
        Change canonical skills under `skills/`, then run:

        ```bash
        python3 skills/sync_runtime_skills.py
        ```

        Verify before committing:

        ```bash
        python3 skills/sync_runtime_skills.py --check
        ```
        """
    )


def enter_skill(title: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        name: enter-project
        description: Use when any user enters {title}, asks what the project can do, gives a request that needs routing, or asks for current project capability, workflow, or builder guidance.
        ---

        # Enter Project

        Use this as the project entry skill. It classifies the request, reads lightweight project
        observations only when needed, and routes to the narrowest project skill.

        ## What You Do

        1. Classify the request: introduction, capability inventory, user workflow, builder work,
           review, maintenance, orchestration, or unsupported.
        2. Read only the lightweight runtime observations needed for that classification.
        3. Route internally to the correct downstream skill.
        4. Answer in user-facing language and hide raw commands unless the user asks for them.
        5. If no project skill fits, say what is missing and offer the closest valid next path.

        ## Routing Rules

        - Use project skills before generic skills when project-specific evidence, brand, safety,
          runtime state, or workflow boundaries matter.
        - Do not hard-code mutable capability inventory in final answers.
        - Do not let runtime output make semantic decisions by itself. Runtime output is observation
          material for the agent.

        ## References

        - `references/internal-runtime.md` - lightweight observations for this project.
        - `references/output-contract.md` - entry answer and routing contract.
        """
    )


def enter_internal_runtime() -> str:
    return textwrap.dedent(
        """\
        # Internal Runtime

        Add only lightweight observation commands here. These commands are for agent use and should
        not be exposed as the main user-facing answer.

        Examples to replace:

        ```bash
        git status --short
        find skills -name SKILL.md -print | sort
        python3 skills/sync_runtime_skills.py --check
        ```
        """
    )


def enter_output_contract() -> str:
    return textwrap.dedent(
        """\
        # Enter Project Output Contract

        For introductions or inventory questions, return:

        - `project_summary`: one or two sentences.
        - `current_inventory`: compact facts from runtime observations when relevant.
        - `available_paths`: next actions expressed as outcomes.

        For concrete requests, return or route with:

        - `intent`
        - `runtime_observations_used`
        - `next_skill`
        - `user_facing_result`

        Do not include raw CLI JSON or internal file dumps in user-facing answers.
        """
    )


def review_skill() -> str:
    return textwrap.dedent(
        """\
        ---
        name: review-skill-best-practices
        description: Use when creating, editing, reviewing, or accepting project skills, especially before saying a project skill is production-ready or after adding, renaming, moving, or deleting a skill under root skills/.
        ---

        # Review Skill Best Practices

        Use this project-local skill to review canonical skills under root `skills/`.

        ## What Good Looks Like

        - The skill frontmatter has `name` and a trigger-focused `description`.
        - `SKILL.md` is the concise agent-facing contract.
        - Detailed commands, flags, schemas, and examples live in `references/`.
        - Repeated or fragile operations live in `scripts/`.
        - Supporting references, scripts, assets, evaluations, and runtime metadata exist only
          when they materially improve the skill.
        - Runtime mirrors are generated from canonical `skills/`.

        ## Review Flow

        1. Read the changed skill's `SKILL.md` and only the references needed.
        2. Run the audit command from `references/internal-runtime.md`.
        3. Run `python3 skills/sync_runtime_skills.py --check`.
        4. Fix hard failures first.
        5. Re-run the audit and the repo's relevant tests before reporting success.

        ## References

        - `references/internal-runtime.md` - exact audit commands.
        - `references/output-contract.md` - review report shape.
        """
    )


def review_internal_runtime() -> str:
    return textwrap.dedent(
        """\
        # Internal Runtime

        Run from the repository root:

        ```bash
        python3 skills/development/review-skill-best-practices/scripts/audit_project_skills.py --skills-root skills
        python3 skills/sync_runtime_skills.py --check
        ```

        Use `--strict` when recommended authoring limits should fail validation. Supporting
        references, scripts, assets, evaluations, and runtime metadata remain optional.
        """
    )


def review_output_contract() -> str:
    return textwrap.dedent(
        """\
        # Review Output Contract

        Return:

        - `status`: passed, failed, or blocked.
        - `skills_checked`: count from the audit.
        - `blocking_issues`: hard failures with file paths.
        - `warnings`: important non-blocking risks.
        - `sync_status`: runtime mirror check result.
        - `tests_run`: verification commands and outcomes.
        """
    )


def sync_runtime_skills_script() -> str:
    return textwrap.dedent(
        '''\
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
        DEFAULT_TARGETS = [
            REPO / ".claude" / "skills",
            REPO / ".agents" / "skills",  # Shared by Codex, Kimi Code, and OpenCode.
            REPO / ".codebuddy" / "skills",  # WorkBuddy / CodeBuddy Code.
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
                    print("Out of sync (rerun skills/sync_runtime_skills.py):\\n  " + "\\n  ".join(stale))
                    return 1
                print(f"In sync: {len(skills)} skills mirrored to {len(targets)} runtime target(s).")
                return 0

            print(f"Mirrored {len(skills)} skills to {len(targets)} runtime target(s).")
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        '''
    )


def agent_registry_yaml() -> str:
    return textwrap.dedent(
        """\
        # Project Subagent Registry.
        #
        # Add entries under agents only when a recurring isolated role should be generated into
        # both runtime-specific agent formats. Keep behavior in each spec file.

        model_tiers:
          default:
            claude: sonnet
            codex: gpt-5
            codex_reasoning_effort: low

        agents: []
        """
    )


def agents_readme() -> str:
    return textwrap.dedent(
        """\
        # Project Subagents

        Project subagents are optional. Use them only for recurring isolated worker or reviewer
        roles that should exist consistently across runtimes.

        Single source of truth:

        - `agents/registry.yaml` for metadata and runtime wiring.
        - skill-owned `references/*-agent.md` or `references/*-prompt.md` files for behavior.

        Generated files:

        - `.claude/agents/*.md`
        - `.codex/agents/*.toml`
        - `agents/INDEX.md`

        Do not hand-edit generated files.
        """
    )


def sync_agents_script() -> str:
    return textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        """Generate project subagents from agents/registry.yaml."""

        from __future__ import annotations

        import argparse
        import sys
        from pathlib import Path

        try:
            import yaml
        except ImportError:
            sys.exit("PyYAML is required for agents/sync_agents.py")


        REPO = Path(__file__).resolve().parents[1]
        REGISTRY = REPO / "agents" / "registry.yaml"
        CLAUDE_DIR = REPO / ".claude" / "agents"
        CODEX_DIR = REPO / ".codex" / "agents"
        INDEX = REPO / "agents" / "INDEX.md"
        NOTICE = "Generated by agents/sync_agents.py from agents/registry.yaml; do not edit by hand."
        REQUIRED = ["id", "kind", "description", "spec", "tier", "role_note", "output", "iron_rule"]


        def load_registry() -> tuple[dict, list[dict]]:
            data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
            tiers = data.get("model_tiers", {})
            agents = data.get("agents", [])
            seen: set[str] = set()
            for agent in agents:
                missing = [key for key in REQUIRED if not agent.get(key)]
                if missing:
                    sys.exit(f"registry error: agent {agent.get('id', '?')} missing {missing}")
                if agent["id"] in seen:
                    sys.exit(f"registry error: duplicate id {agent['id']}")
                seen.add(agent["id"])
                if agent["tier"] not in tiers:
                    sys.exit(f"registry error: unknown tier {agent['tier']} for {agent['id']}")
                if not (REPO / agent["spec"]).exists():
                    sys.exit(f"registry error: missing spec {agent['spec']} for {agent['id']}")
                tier = tiers[agent["tier"]]
                agent["_claude_model"] = str(tier.get("claude", "sonnet"))
                agent["_codex_model"] = str(tier.get("codex", "gpt-5"))
                agent["_codex_reasoning_effort"] = str(tier.get("codex_reasoning_effort", "low"))
            return tiers, agents


        def body(agent: dict) -> str:
            return (
                f"{agent['role_note'].strip()}\\n\\n"
                f"1. Read `{agent['spec']}` in full and follow it exactly.\\n"
                "2. Read only the inputs the caller provides.\\n"
                "3. Follow the spec output format and red lines.\\n"
                f"4. {agent['output'].strip()}\\n\\n"
                f"Iron rule: {agent['iron_rule'].strip()}"
            )


        def render_claude(agent: dict) -> str:
            tools = ", ".join(agent.get("tools", []))
            lines = [f"name: {agent['id']}", f"description: {agent['description'].strip()}"]
            if tools:
                lines.append(f"tools: {tools}")
            lines.append(f"model: {agent['_claude_model']}")
            return "---\\n" + "\\n".join(lines) + "\\n---\\n" + f"<!-- {NOTICE} -->\\n\\n" + body(agent) + "\\n"


        def render_codex(agent: dict) -> str:
            desc = agent["description"].strip().replace("\\\\", "\\\\\\\\").replace('"', '\\"')
            rendered_body = body(agent)
            if '"""' in rendered_body:
                sys.exit(f"agent {agent['id']} body contains triple quotes")
            lines = [
                f"# {NOTICE}",
                f"name = \\"{agent['id']}\\"",
                f"description = \\"{desc}\\"",
                f"model = \\"{agent['_codex_model']}\\"",
                f"model_reasoning_effort = \\"{agent['_codex_reasoning_effort']}\\"",
            ]
            if agent.get("sandbox"):
                lines.append(f"sandbox_mode = \\"{agent['sandbox']}\\"")
            lines.append('developer_instructions = """\\n' + rendered_body + '\\n"""')
            return "\\n".join(lines) + "\\n"


        def render_index(agents: list[dict]) -> str:
            rows = [
                "# Subagent Index (generated)",
                "",
                f"<!-- {NOTICE} -->",
                "",
                "| Agent | Kind | Tier | Spec | Dispatched by |",
                "|---|---|---|---|---|",
            ]
            for agent in agents:
                rows.append(
                    f"| `{agent['id']}` | {agent['kind']} | {agent['tier']} | "
                    f"`{agent['spec']}` | {agent.get('dispatched_by', '-')} |"
                )
            return "\\n".join(rows) + "\\n"


        def planned(agents: list[dict]) -> dict[Path, str]:
            files = {INDEX: render_index(agents)}
            for agent in agents:
                files[CLAUDE_DIR / f"{agent['id']}.md"] = render_claude(agent)
                files[CODEX_DIR / f"{agent['id']}.toml"] = render_codex(agent)
            return files


        def main() -> int:
            parser = argparse.ArgumentParser()
            parser.add_argument("--check", action="store_true")
            args = parser.parse_args()

            _, agents = load_registry()
            files = planned(agents)
            stale = []
            for path, content in files.items():
                current = path.read_text(encoding="utf-8") if path.exists() else None
                if current != content:
                    stale.append(str(path.relative_to(REPO)))
            if args.check:
                if stale:
                    print("Out of sync:\\n  " + "\\n  ".join(stale))
                    return 1
                print(f"In sync: {len(agents)} agents.")
                return 0

            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            CODEX_DIR.mkdir(parents=True, exist_ok=True)
            INDEX.parent.mkdir(parents=True, exist_ok=True)
            for path, content in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            print(f"Generated {len(agents)} agents.")
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        '''
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize project agent infrastructure.")
    parser.add_argument("repo", help="Target repository path.")
    parser.add_argument("--project-name", help="Human-readable project name.")
    parser.add_argument(
        "--profile",
        choices=["minimal", "categorized", "pipeline", "subagents"],
        default="minimal",
        help="Smallest structural profile to scaffold (default: minimal).",
    )
    parser.add_argument(
        "--with-governance",
        action="store_true",
        help="Add the independent project skill review and audit layer.",
    )
    parser.add_argument("--with-subagents", action="store_true", help="Also scaffold agents/ registry.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned writes without changing files.")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    title = project_title(repo, args.project_name)
    if not repo.exists():
        raise SystemExit(f"repo does not exist: {repo}")

    has_entry = args.profile in {"pipeline", "subagents"}
    has_subagents = args.with_subagents or args.profile == "subagents"
    writes: list[str] = []
    writes.append(
        write_file(
            repo / "AGENTS.md",
            agents_md(
                title,
                has_entry=has_entry,
                governance=args.with_governance,
                has_subagents=has_subagents,
            ),
            force=args.force,
            dry_run=args.dry_run,
        )
    )
    writes.append(write_file(repo / "CLAUDE.md", claude_md(), force=args.force, dry_run=args.dry_run))
    writes.append(write_file(repo / "CODEBUDDY.md", codebuddy_md(), force=args.force, dry_run=args.dry_run))

    if args.profile in {"categorized", "pipeline", "subagents"}:
        writes.append(
            write_file(
                repo / "skills" / "README.md",
                skills_readme(title),
                force=args.force,
                dry_run=args.dry_run,
            )
        )

    if has_entry:
        enter_dir = repo / "skills" / "application" / "enter-project"
        writes.append(write_file(enter_dir / "SKILL.md", enter_skill(title), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(enter_dir / "references" / "internal-runtime.md", enter_internal_runtime(), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(enter_dir / "references" / "output-contract.md", enter_output_contract(), force=args.force, dry_run=args.dry_run))

    if args.with_governance:
        review_dir = repo / "skills" / "development" / "review-skill-best-practices"
        writes.append(write_file(review_dir / "SKILL.md", review_skill(), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(review_dir / "references" / "internal-runtime.md", review_internal_runtime(), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(review_dir / "references" / "output-contract.md", review_output_contract(), force=args.force, dry_run=args.dry_run))

        audit_source = Path(__file__).with_name("audit_project_skills.py").read_text(encoding="utf-8")
        writes.append(
            write_file(
                review_dir / "scripts" / "audit_project_skills.py",
                audit_source,
                force=args.force,
                dry_run=args.dry_run,
                executable=True,
            )
        )

    writes.append(
        write_file(
            repo / "skills" / "sync_runtime_skills.py",
            sync_runtime_skills_script(),
            force=args.force,
            dry_run=args.dry_run,
            executable=True,
        )
    )

    if has_subagents:
        writes.append(write_file(repo / "agents" / "README.md", agents_readme(), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(repo / "agents" / "registry.yaml", agent_registry_yaml(), force=args.force, dry_run=args.dry_run))
        writes.append(write_file(repo / "agents" / "sync_agents.py", sync_agents_script(), force=args.force, dry_run=args.dry_run, executable=True))

    print("\n".join(writes))
    print(
        f"Initialized profile={args.profile} governance={'enabled' if args.with_governance else 'disabled'} "
        f"subagents={'enabled' if has_subagents else 'disabled'} for {repo}"
    )
    print(
        "Next: customize AGENTS.md and any generated project skills, run "
        "skills/sync_runtime_skills.py, then verify with skills/sync_runtime_skills.py --check."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
