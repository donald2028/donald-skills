---
name: donald-agent-infrastructure
description: "Set up, migrate, or structurally audit cross-runtime Agent infrastructure for a project: its canonical AGENTS.md and skills source, generated runtime mirrors, optional routing and governance, and Claude/Codex/CodeBuddy subagent registry. Do not use for routine edits to individual skills or AGENTS.md, single-runtime configuration, or maintenance and release of a reusable Skill collection."
---

# Donald Agent Infrastructure

## Overview

Give a project one canonical operating contract, one canonical `skills/` source, generated runtime
adapters, and independently selected entry, governance, and subagent layers. Do not describe these
dimensions as cumulative profiles.

## First Pass

1. Read the target repo's `AGENTS.md`, runtime adapters, canonical `skills/`, runtime skill mirrors,
   `.workbuddy/skills/`, `.codex/agents/`, `.claude/agents/`, `.codebuddy/agents/`, and `agents/`
   when present.
2. Choose one skill layout:
   - `flat`: `skills/<skill>/SKILL.md`; default for small and ordinary projects.
   - `categorized`: `skills/<category>/<skill>/SKILL.md`; use only for genuine skill families.
3. Decide independently whether the project needs:
   - one entry skill for capability discovery, modes, or cross-skill routing;
   - governance for project-local review and structural audit;
   - a subagent registry for recurring isolated Claude/Codex/CodeBuddy roles.
4. Preserve user changes. Existing runtime directories are user-owned until generated-state
   evidence proves otherwise.
5. If the user requests only a plan, stop before writing files.

Read `references/architecture.md` before designing a project structure. Read
`references/migration.md` before changing an existing repository. Read
`references/workbuddy.md` when Tencent WorkBuddy or CodeBuddy Code is a target runtime.

## Initialize

Use `scripts/init_project_agent_infra.py` for new-project scaffolding. It writes only missing files
unless `--force` is passed.

```bash
python scripts/init_project_agent_infra.py <repo> \
  --layout flat \
  --with-entry \
  --with-governance \
  --with-subagents
```

`--layout` defaults to `flat`. The three `--with-*` switches are independent and opt-in. The
initializer validates any existing canonical skills before writing, appends a managed `.gitignore`
block, and places repository tooling under `scripts/agent-skills/` rather than `skills/`.

All layouts support `.claude/skills/`, `.agents/skills/`, `.codebuddy/skills/`, and
`.workbuddy/skills/` mirrors. Run the generated `scripts/agent-skills/sync_runtime_skills.py` to
create them. Windows defaults to NTFS junctions, then symlinks, then copy; Linux/macOS defaults to
symlinks, then copy.

`--with-subagents` creates `agents/registry.yaml` and `agents/sync_agents.py`. It does not invent
project roles. Add real recurring roles to the registry, keep their behavior specifications in the
owning skill's `references/`, then run the generator and its `--check` mode. Generated
`.codebuddy/agents/` definitions target CodeBuddy Code. Do not infer a WorkBuddy Agent path from
its `.workbuddy/skills/` discovery convention.

After initialization, replace generic project descriptions, observations, routing rules, and red
lines with real project facts.

## Audit

Use `scripts/audit_project_skills.py --skills-root <skills-root> --strict` for structural and
repository-convention checks. It reports layout, categories, dependencies, mirror state, and
frontmatter or path failures. The audit does not replace semantic agent judgment.

After any canonical Skill change, run the generated mirror sync and its `--check` mode. When
subagents are enabled, also run `agents/sync_agents.py --check` after registry or spec changes.

## Core Contract

- `AGENTS.md` is the canonical cross-runtime project contract. Runtime adapters contain only
  runtime-specific additions.
- Root `skills/` is the only hand-maintained project Skill source.
- Flat and categorized trees are Donald project-management conventions; the Agent Skills standard
  defines the individual Skill package rather than the repository taxonomy.
- New scaffolds use exactly one layout. Mixed layout exists only as a migration observation.
- Skill names are globally unique and every Skill is self-contained.
- Required workflow dependencies use `**REQUIRED SUB-SKILL:** Invoke <skill-name>` and must exist
  without self-reference or cycles.
- Runtime skill mirrors and generated agent definitions are disposable output, never hand-edited.
- `agents/registry.yaml` is the canonical subagent wiring source. The bundled generator supports
  Claude, Codex, and CodeBuddy Code; WorkBuddy Skill support does not imply WorkBuddy
  subagent-format support.
- Audit and runtime output provide observations; the main agent owns semantic decisions.

## References

- `references/architecture.md` - layouts, optional layers, runtime mirrors, and subagents.
- `references/migration.md` - non-destructive treatment of existing repositories.
- `references/internal-runtime.md` - validation commands for this Skill.
- `references/output-contract.md` - planning and implementation evidence.
- `references/workbuddy.md` - Tencent WorkBuddy/CodeBuddy project adapters and product boundary.
- `scripts/init_project_agent_infra.py` - new-project initializer.
- `scripts/audit_project_skills.py` - portable structural audit.
