# Donald Skills Agent Contract

## Scope

This repository is the canonical collection for Donald's reusable agent skills. Keep the
repository runtime-neutral: every production skill must work from the root `skills/` tree and be
installable by supported agents without maintaining separate copies.

Do not add business skills, subagents, hooks, MCP servers, or release automation unless the user
asks for them. Prefer the smallest structure that satisfies the current request.

## Working Principles

### Think Before Coding

- State assumptions and surface ambiguous requirements before implementation.
- Present meaningful alternatives when they change the result.
- Prefer a simpler approach when it meets the same success criteria.

### Simplicity First

- Write only the code and documentation required for the requested outcome.
- Avoid speculative configuration, one-use abstractions, and impossible-case handling.
- Reduce an implementation when a smaller one is equally clear and verifiable.

### Surgical Changes

- Touch only files that trace directly to the request.
- Preserve user changes and match the existing style.
- Remove only imports, variables, or files made obsolete by the current change.

### Goal-Driven Execution

- Translate work into verifiable outcomes before editing.
- For bugs, reproduce first; for refactors, verify behavior before and after.
- Run the narrowest relevant checks and continue until they pass.

## Repository Contract

- `skills/` is the only hand-maintained source of production skills.
- Use a flat `skills/<skill-name>/SKILL.md` layout until categories are genuinely needed.
- A published skill must be self-contained. Its `SKILL.md`, references, and scripts must not
  depend on sibling skills or repository-only documentation.
- `.claude/skills/` and `.agents/skills/` are generated runtime mirrors. Never hand-edit them;
  run `python3 skills/sync_runtime_skills.py` after adding, renaming, moving, or removing a skill.
- `.claude-plugin/` and `.codex-plugin/` describe the same aggregate plugin. Keep their names and
  versions aligned.
- `.agents/plugins/marketplace.json` and `.claude-plugin/marketplace.json` expose the same plugin
  from the repository root.
- Keep secrets and runtime output out of Git. Commit examples as `.env.example`, never `.env`.

## Skill Quality Gate

For every new or changed skill:

1. Use a kebab-case directory name and matching frontmatter `name`.
2. Make the frontmatter `description` specific enough for reliable triggering.
3. Add `scripts/`, `references/`, `assets/`, or `agents/` only when the skill needs them.
4. Run the runtime sync and repository validation commands documented in `README.md`.
5. Confirm `npx skills add . --list` discovers the intended skills and no unrelated directories.
