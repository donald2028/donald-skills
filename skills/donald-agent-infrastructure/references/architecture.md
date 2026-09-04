# Agent-Led Skill Infrastructure

Use this reference to design a project-local skill management structure that can scale from a
small repo to a multi-stage agent pipeline.

## Universal Pieces

An agent-led project has five separable layers:

1. Project contract: `AGENTS.md` is the canonical cross-runtime contract. `CLAUDE.md` is a thin
   Claude adapter or contains only Claude-specific additions.
2. Canonical skills: `skills/` contains the source of truth for project-local workflows.
3. Runtime mirrors: `.claude/skills/` and `.agents/skills/` expose the same skills to different
   runtimes without duplicate hand maintenance.
4. Governance: an optional project-local review skill and audit script provide quality gates.
5. Subagents: an optional canonical registry generates runtime-specific agent definitions.

Keep these layers independent. A simple repo may use only the first three. A complex pipeline may
use all five.

## Supported Skill Layouts

Flat layout:

```text
skills/
  make-post/
    SKILL.md
```

Use flat layout for small projects with a few user-facing workflows.

Categorized layout:

```text
skills/
  application/run-workflow/SKILL.md
  collection/collect-source/SKILL.md
  distillation/build-package/SKILL.md
  review/review-package/SKILL.md
```

Use categorized layout when the repo has internal skill families, modes, or multi-step pipelines.

Mixed layout is allowed during migration. Discovery scripts should treat every directory with a
`SKILL.md` below `skills/` as a skill, then fail on duplicate skill names.

## Entry Skill Pattern

Use an entry skill when a repo has any of these:

- user mode vs builder mode
- published/current capability inventory
- workflow routing
- project-specific safety or evidence boundaries
- generic skills that should not bypass project rules

The entry skill should:

1. classify the request
2. read only the lightweight runtime observations needed
3. route to the narrowest downstream skill
4. answer with outcomes, not raw commands
5. explain capability gaps honestly

Do not make every skill an entry skill. Keep exactly one default entry point unless a runtime
forces another shape.

## Skill Anatomy

Base project skill structure:

```text
skill-name/
  SKILL.md
```

Add supporting resources only when they improve the workflow:

```text
skill-name/
  SKILL.md                    required
  references/                 optional detailed guidance
  scripts/                    optional deterministic helpers
  assets/                     optional output templates or resources
  evals/                      optional behavioral evaluations
  agents/openai.yaml          optional OpenAI UI metadata
```

Keep short commands and output expectations in `SKILL.md`. Use focused reference files when the
detail is conditional or substantial; names such as `internal-runtime.md` and
`output-contract.md` are conventions, not requirements.

## Runtime Mirror Pattern

Canonical source:

```text
skills/<maybe-category>/<skill-name>/
```

Disposable generated mirrors:

```text
.claude/skills/<skill-name> -> ../../skills/<maybe-category>/<skill-name>
.agents/skills/<skill-name> -> ../../skills/<maybe-category>/<skill-name>
```

Prefer relative directory symlinks so references and scripts resolve the same way from the skill
root. Use copy mode only for environments that cannot use symlinks. After adoption, runtime
mirrors are compiler output: regenerate them from `skills/` and never maintain them by hand.

## Subagent Registry Pattern

Use a subagent registry only when a project has recurring isolated roles, such as cleaners,
reviewers, synthesizers, or data workers.

Single source:

```text
agents/registry.yaml
skills/<owner>/<skill>/references/<agent-spec>.md
```

Generated runtime files:

```text
.claude/agents/<id>.md
.codex/agents/<id>.toml
agents/INDEX.md
```

Keep behavior in the spec file, wiring in the registry, and generated runtime files disposable.

## Profiles

`minimal`:

- canonical `AGENTS.md`
- thin `CLAUDE.md` adapter
- `skills/`
- `skills/sync_runtime_skills.py`

`categorized`:

- minimal profile
- `skills/README.md`
- categorized canonical skill tree

`pipeline`:

- categorized profile
- `skills/application/enter-project/`
- explicit user/builder modes
- runtime observation references

`subagents`:

- pipeline profile
- `agents/registry.yaml`
- `agents/sync_agents.py`
- generated `.claude/agents` and `.codex/agents`

Choose the smallest profile that keeps the project understandable.

Governance is an independent optional layer available with `--with-governance`. It adds
`skills/development/review-skill-best-practices/` and its audit helper without forcing the project
to adopt pipeline routing or subagents.

## Red Lines

- Do not let runtime output choose semantic routing by itself.
- Do not hand-edit generated mirrors once a sync script owns them.
- Do not duplicate shared project rules between `AGENTS.md` and `CLAUDE.md`, or workflow behavior
  between contract files and `SKILL.md`.
- Do not commit runtime data as source. Promote small fixtures into tests instead.
- Do not hide mutable state behind stale hard-coded capability claims.
