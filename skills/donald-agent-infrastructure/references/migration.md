# Migration Process

Use this process when a repo already has skills or runtime-specific copies.

## Read-Only Inventory

1. List `AGENTS.md`, `CLAUDE.md`, `skills/`, `.claude/skills/`, `.agents/skills/`,
   `.codex/agents/`, and `agents/`.
2. Compare skill names and file contents across canonical and mirror folders.
3. Identify whether the repo is flat, categorized, mixed, or already generated.
4. Check git status and treat uncommitted changes as user-owned.

## Decide Ownership

Pick one canonical source:

- Prefer root `skills/` for project-local skills.
- Treat `.claude/skills/` and `.agents/skills/` as generated mirrors.
- If only a runtime mirror exists, copy the best version into root `skills/` before replacing the
  mirror with symlinks.

Do not delete divergent files until you have either preserved them in canonical `skills/` or shown
the user the divergence. After ownership is established, runtime mirrors are disposable generated
output and may be replaced on every sync.

## Introduce Governance

1. Add `skills/sync_runtime_skills.py`.
2. Add `skills/development/review-skill-best-practices/` only when the project adopts the optional
   governance layer.
3. Run the audit script when governance is present and fix hard errors.
4. Run the sync script in `--check` mode before replacing mirrors.
5. Convert mirrors to generated links or copies only after the canonical source is correct.

## Add Entry Routing

Add `skills/application/enter-project/` only when the project needs routing or modes. Categorizing
skills alone does not require an entry skill. The entry skill
should read the live state it needs and route internally. It should not duplicate every downstream
skill.

## Optional Subagents

Add `agents/registry.yaml` only if the repo needs generated subagent definitions. Do not create a
registry just because one exists in a reference project.

## Verification

Run these from the target repo when available:

```bash
python3 skills/sync_runtime_skills.py --check
python3 skills/development/review-skill-best-practices/scripts/audit_project_skills.py --skills-root skills
```

Then run the repo's normal tests or at least a smoke command that proves the generated scripts can
execute.
