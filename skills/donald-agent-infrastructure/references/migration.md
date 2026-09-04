# Migration Process

Use this process for an existing repository. The initializer is designed for new scaffolds and does
not migrate old generated projects or accept the removed `--profile` interface.

## Read-Only Inventory

1. Inventory project contracts, root `skills/`, runtime Skill mirrors including
   `.workbuddy/skills/`, `agents/`, and generated Claude/Codex/CodeBuddy agent directories.
2. Compare names and content across canonical and runtime-specific copies.
3. Identify the actual layout as flat, categorized, mixed, or invalid.
4. Read Git status and treat uncommitted changes as user-owned.

## Establish Ownership

- Prefer root `skills/` as canonical.
- Preserve divergent runtime content before replacing any mirror.
- Choose flat or categorized as the target; mixed layout is only an intermediate migration state.
- Keep Skill resources self-contained and replace repository-relative sibling imports with
  explicit workflow invocation where appropriate.
- Validate required sub-skills for missing targets, self-reference, and cycles.

## Adopt Generated Mirrors

Place the new helper at `scripts/agent-skills/sync_runtime_skills.py`. Before its first run, treat
all existing runtime paths as unknown and preserve them. Use `--replace-existing` only after
canonical content is complete and divergent files have been saved.

The first successful sync writes `.agent-infra/runtime-skill-mirrors.json`; later cleanup is limited
to entries recorded there. Do not fabricate manifest ownership for directories you did not verify.

## Add Optional Layers

- Add one layout-appropriate `enter-project` Skill only for real routing or mode selection.
- Add the layout-appropriate governance Skill only when the project adopts local quality gates.
- Add `agents/registry.yaml` only for recurring isolated roles. Keep role behavior in an owning
  Skill's references and generate Claude/Codex/CodeBuddy adapters.
- Do not infer that enabling one optional layer requires another.

## Verify

From the target repository, run the applicable checks:

```bash
python <governance-skill>/scripts/audit_project_skills.py --skills-root skills --strict
python scripts/agent-skills/sync_runtime_skills.py
python scripts/agent-skills/sync_runtime_skills.py --check
python agents/sync_agents.py
python agents/sync_agents.py --check
```

Run the project's normal tests afterward. Report any runtime whose discovery or subagent format was
not actually validated.
