# Agent-Led Multi-Skill Infrastructure

This architecture separates repository layout from optional workflow capabilities. Agent Skills
standardizes each `SKILL.md` package; the repository taxonomy, entry routing, mirrors, governance,
and subagent registry below are Donald project-management conventions.

## Independent Layers

1. Project contract: `AGENTS.md`, with thin `CLAUDE.md` and `CODEBUDDY.md` adapters.
2. Canonical skills: one flat or categorized root `skills/` tree.
3. Runtime mirrors: generated `.claude/skills/`, `.agents/skills/`, `.codebuddy/skills/`, and
   `.workbuddy/skills/`.
4. Entry routing: an optional single `enter-project` Skill.
5. Governance: an optional review Skill and structural audit.
6. Subagents: an optional registry and Claude/Codex/CodeBuddy generator.

The layers are independent. Categorization does not imply routing; routing does not imply
governance; subagents do not require either.

## Canonical Skill Layout

Flat, the default:

```text
skills/
  make-post/
    SKILL.md
```

Categorized, for genuine families:

```text
skills/
  application/run-workflow/SKILL.md
  collection/collect-source/SKILL.md
  review/review-package/SKILL.md
```

New projects use exactly one shape. Category and Skill directories use kebab-case, Skill names are
globally unique, and no Skill may import or read a sibling by repository-relative path. Mixed or
deeper trees are invalid for new scaffolds; mixed layout is recognized only while planning a
non-destructive migration.

A Skill requires only `SKILL.md`. Add `references/`, `scripts/`, `assets/`, `evals/`, or
`agents/openai.yaml` only when needed.

## Optional Entry And Governance

Use one entry Skill when requests require capability discovery, user/builder mode selection,
project safety boundaries, or cross-Skill routing. Its path depends on layout:

```text
flat:         skills/enter-project/
categorized:  skills/application/enter-project/
```

The entry classifies the request, reads only lightweight observations, routes to the narrowest
Skill, and explains unsupported work. It is not a workflow engine.

Governance is independently enabled at:

```text
flat:         skills/review-skill-best-practices/
categorized:  skills/development/review-skill-best-practices/
```

Its audit checks structure and repository conventions; the main agent still judges workflow
quality and semantics.

## Runtime Skill Mirrors

Canonical source:

```text
skills/<skill>/
skills/<category>/<skill>/
```

Generated flat runtime views:

```text
.claude/skills/<skill-name>
.agents/skills/<skill-name>
.codebuddy/skills/<skill-name>
.workbuddy/skills/<skill-name>
```

The generated `scripts/agent-skills/sync_runtime_skills.py` embeds the layout selected during
initialization, rejects later layout drift, flattens the canonical tree by globally unique Skill
name, and records managed output in
`.agent-infra/runtime-skill-mirrors.json`.

Default modes:

- Windows: NTFS junction, then directory symlink, then copy.
- Linux/macOS: relative directory symlink, then copy.

Junctions are preferred on Windows because mirrors are local disposable output and junctions do
not normally require elevated symlink privileges. Forced `--junction`, `--symlink`, and `--copy`
modes never fall back. Copy mode hashes the full tree so `--check` detects drift.

Only manifest-managed orphans are removed. Unknown runtime paths are preserved unless the user
explicitly passes `--replace-existing` after preserving hand edits.

| Runtime | Project contract | Skill mirror |
|---|---|---|
| Claude Code | `CLAUDE.md` reads `AGENTS.md` | `.claude/skills/` |
| Codex | `AGENTS.md` | `.agents/skills/` |
| CodeBuddy Code / CodeBuddy IDE | `CODEBUDDY.md` reads `AGENTS.md` | `.codebuddy/skills/` |
| Tencent WorkBuddy | No generated rule adapter | `.workbuddy/skills/` |
| Kimi Code | `AGENTS.md` | `.agents/skills/` |
| OpenCode | `AGENTS.md` | `.agents/skills/` and compatible `.claude/skills/` |

## Subagent Registry

Enable subagents only for recurring isolated roles such as workers, reviewers, or synthesizers.
The initializer creates infrastructure, not fictional roles:

```text
agents/
  README.md
  registry.yaml
  sync_agents.py
```

Canonical sources:

```text
agents/registry.yaml
skills/<skill>/references/<role>-agent.md
skills/<category>/<skill>/references/<role>-agent.md
```

Generated output:

```text
.claude/agents/<id>.md
.codex/agents/<id>.toml
.codebuddy/agents/<id>.md
agents/INDEX.md
```

The registry owns metadata and runtime wiring; the Skill-owned spec owns behavior. An empty
registry is valid. `sync_agents.py --check` detects missing, changed, and stale generated files.
Synchronization removes only files carrying its generated notice, preserves unknown files, and
requires `--replace-existing` for a same-path user file. The generator targets Claude, Codex, and
CodeBuddy Code. CodeBuddy definitions default to `model: inherit`; a model tier
may set `codebuddy` and `codebuddy_effort`, and an agent may set `codebuddy_tools` when its
CodeBuddy tool allowlist differs from Claude's.

WorkBuddy project Skills use their own `.workbuddy/skills/` mirror. WorkBuddy marketplace Skills,
Experts, and Expert Teams remain installed product-level artifacts, so the initializer neither
fabricates their package metadata nor writes into WorkBuddy's user-level state.

## CLI Composition

```text
--layout flat|categorized
--with-entry
--with-governance
--with-subagents
```

The switches map one-to-one to output and may be combined independently. There are no cumulative
profiles or implicit capability bundles.

## Red Lines

- Do not hand-edit generated mirrors or agent definitions.
- Do not let runtime observations choose semantic routing by themselves.
- Do not duplicate shared rules across contract adapters or workflow behavior across files.
- Do not commit runtime state as canonical source.
- Do not hard-code mutable capability claims.
