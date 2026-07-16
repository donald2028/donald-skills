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
- Keep each skill's scripts, references, and assets self-contained; they must not import or read a
  sibling skill by repository-relative path.
- A workflow may invoke another skill when both ship in the aggregate Donald plugin. Declare a
  required workflow dependency in `SKILL.md` as `**REQUIRED SUB-SKILL:** Invoke <skill-name>` and
  keep the instruction runtime-neutral. A runtime without native skill invocation may use its
  normal Agent Skills discovery/read fallback. Cross-skill invocation is orchestration, not a code
  import. Resolve dependencies lazily when the caller skill is invoked: if one is unavailable, stop
  before business execution, explain the requirement, and ask the user for permission to install it
  into the same scope and agent target. After an approved successful install, discover and invoke
  the dependency and continue the original request. Report `needs_dependency` with exact
  install/retry guidance only when the user declines, installation fails, or the runtime cannot
  load the installed dependency.
- `.claude/skills/` and `.agents/skills/` are generated runtime mirrors. Never hand-edit them;
  run `npm run build` after adding, renaming, moving, or removing a skill.
- `package.json` is the only hand-maintained source for shared plugin metadata and version. Run
  `npm run build` to project those fields into every committed channel manifest; preserve
  platform-specific fields such as `interface` in their native manifest.
- `.claude-plugin/` and `.codex-plugin/` describe the same aggregate plugin. Their shared metadata
  is generated from `package.json`.
- `.cursor-plugin/plugin.json` and `.kimi-plugin/plugin.json` expose the same aggregate `skills/`
  tree for Cursor and Kimi Code.
- `gemini-extension.json` and `GEMINI.md` are the Gemini CLI extension entry points; keep the
  extension pointed at the canonical root `skills/` tree.
- OpenCode natively discovers the generated `.claude/skills/` and `.agents/skills/` mirrors. Do
  not add a second OpenCode skill copy or plugin bootstrap unless the user explicitly asks for
  OpenCode-specific behavior.
- `.agents/plugins/marketplace.json` and `.claude-plugin/marketplace.json` expose the same plugin
  from the repository root.
- Keep secrets and runtime output out of Git. Commit examples as `.env.example`, never `.env`.

## Skill Quality Gate

For every new or changed skill:

1. Use a kebab-case directory name and matching frontmatter `name`.
2. Make the frontmatter `description` specific enough for reliable triggering.
3. Add `scripts/`, `references/`, `assets/`, or `agents/` only when the skill needs them.
4. Run the build and repository validation commands documented in `README.md`.
5. Confirm `npx skills add . --list` discovers the intended skills and no unrelated directories.
6. Confirm every declared required sub-skill exists in `skills/` and ships through each aggregate
   plugin manifest.
