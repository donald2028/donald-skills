# Tencent WorkBuddy And CodeBuddy

## Separate Project Contracts

Tencent WorkBuddy discovers project Skills from its own runtime directory:

```text
.workbuddy/skills/<skill-name>/SKILL.md
```

CodeBuddy Code and CodeBuddy IDE use a separate vendor directory:

```text
CODEBUDDY.md
.codebuddy/skills/<skill-name>/SKILL.md
.codebuddy/agents/<agent-id>.md
```

Both Skill directories are generated flat from canonical root `skills/`. They are independent
mirrors because neither runtime directory is an alias for the other. `CODEBUDDY.md` is only the
CodeBuddy adapter to canonical `AGENTS.md`.

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
than the project-local `.workbuddy/skills/` mirror. Do not write to WorkBuddy user state, invent
marketplace metadata, or claim marketplace installation from project initialization.

When distribution through WorkBuddy's plugin UI is requested, treat that as a separate packaging
operation. Use the documented WorkBuddy/CodeBuddy plugin manifest and validate it with the current
CodeBuddy plugin validator instead of changing the project workspace contract.
