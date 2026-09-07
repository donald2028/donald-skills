# Tencent WorkBuddy And CodeBuddy

## Shared Project Skill Discovery

Tencent WorkBuddy's agent engine is the CodeBuddy CLI, so WorkBuddy and CodeBuddy resolve
project-level Skills from the same directory:

```text
.codebuddy/skills/<skill-name>/SKILL.md
```

Measured on WorkBuddy 5.5.2, build commit `2b0177c3`. In that build the CLI Skill loader references
`.codebuddy/skills/` at project level and never references `.workbuddy/skills/` at project level;
every `.workbuddy/skills/` occurrence is either the user-level `~/.workbuddy/skills/` sandbox
allowlist or the desktop shell's artifact-presentation allowlist. The bundled documentation states
the same rule. Do not claim a separate WorkBuddy discovery path without re-measuring the installed
build.

CodeBuddy Code and CodeBuddy IDE own the contract adapter:

```text
CODEBUDDY.md
.codebuddy/skills/<skill-name>/SKILL.md
.codebuddy/agents/<agent-id>.md
```

`CODEBUDDY.md` is only the CodeBuddy adapter to canonical `AGENTS.md`. WorkBuddy has no generated
rule adapter; it reads canonical `AGENTS.md`.

## Source Selection

For ordinary cross-runtime authoring, keep root `skills/` unregistered. When the user explicitly
chooses a repo-native CodeBuddy or WorkBuddy Skill source, use `.codebuddy/skills/` as the only
hand-maintained source. Do not also maintain root `skills/`, and do not create `.workbuddy/skills/`
as a compatibility mirror.

The desktop shell currently lists `.workbuddy/skills/` only in its artifact-presentation allowlist
and treats `.workbuddy/` as a hidden path. Do not generate that directory speculatively because a
future product version might use it; verify the installed build when the actual request depends on
new behavior.

## Verifying Discovery

A runtime's injected Skill list is not sufficient evidence. It normally contains only user-level and
built-in plugin Skills, not project Skills. Invoke the Skill by its bare name and read the
`Base directory for this skill:` line the runtime reports; the directory it resolves to is the
source or explicit installation that runtime actually loads.

## Agent Mapping

CodeBuddy agent definitions use Markdown with YAML frontmatter. The shared registry maps:

- `id` to `name`;
- `description` to routing metadata;
- `tools` to the default tool allowlist, overridable with `codebuddy_tools`;
- tier `codebuddy` to `model`, defaulting to `inherit`;
- optional tier `codebuddy_effort` to `effort`;
- the shared role body to the agent system prompt.

Generated `.codebuddy/agents/` files have the same ownership, drift checks, collision protection,
and generated-orphan cleanup as Claude and Codex outputs. No `.workbuddy/agents/` output is
generated until WorkBuddy's project Agent discovery path and schema are separately established.

## Product Boundary

WorkBuddy marketplace Skills, Experts, and Expert Teams are product-account installations rather
than a project-local `.codebuddy/skills/` source. Do not write to WorkBuddy user state, invent
marketplace metadata, or claim marketplace installation from project initialization.

When distribution through WorkBuddy's plugin UI is requested, treat that as a separate packaging
operation. Use the documented WorkBuddy/CodeBuddy plugin manifest and validate it with the current
CodeBuddy plugin validator instead of changing the project workspace contract.
