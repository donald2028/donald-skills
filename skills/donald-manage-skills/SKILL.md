---
name: donald-manage-skills
description: Use when adding, updating, removing, auditing, validating, packaging, versioning, or publishing skills in a reusable Agent Skills repository that supports Skills CLI, Claude Code, and Codex.
---

# Manage Skill Repository

Keep one canonical skill source while preserving installer compatibility across supported agent
runtimes.

## Workflow

1. Read the repository contract files, installation documentation, plugin manifests, canonical
   `skills/` tree, runtime mirrors, and Git status.
2. State the requested outcome and the checks that will prove it.
3. Change only canonical skill files and the minimum repository metadata required by the request.
4. Regenerate runtime mirrors with the repository's sync script when one exists.
5. Run the available skill, plugin, and repository validators.
6. Review the diff and sensitive-file status before any requested commit or push.

## Repository Rules

- Prefer root `skills/<skill-name>/` as the canonical source.
- Use lowercase kebab-case for the directory and frontmatter `name`.
- Keep each published skill self-contained so it can be copied or installed independently.
- Put only required instructions in `SKILL.md`; add `scripts/`, `references/`, `assets/`, or
  `agents/` only when they support the workflow.
- Treat `.claude/skills/` and `.agents/skills/` as generated mirrors when a sync script owns them.
- Keep Claude and Codex plugin names and versions aligned.
- Do not add subagents, hooks, MCP servers, categories, or release tooling without a concrete need.
- Never commit credentials, local settings, caches, generated output, or session-only plans.

## Operation Guidance

### Add Or Update

Inspect nearby skills for repository conventions. For a new skill, create a valid `SKILL.md` with
only `name` and `description` in frontmatter, then implement the smallest useful body and resources.
For an existing skill, preserve unrelated content and remove only artifacts made obsolete by the
change.

### Remove Or Rename

Search manifests, documentation, mirrors, and scripts for references. Update only real consumers,
regenerate mirrors, and verify that no stale symlink or duplicate skill name remains.

### Release Or Publish

Change versions only when a release is requested. Keep aggregate plugin manifests and marketplace
metadata consistent, validate installation discovery, then perform the repository's sensitive-file
check before committing or pushing.

## Verification

Read `references/internal-runtime.md` before running validation. Use the checks supported by the
repository and installed tools, and do not claim cross-runtime support when an applicable manifest
or discovery check failed.

Return the concise evidence described in `references/output-contract.md`.
