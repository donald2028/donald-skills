---
name: donald-manage-skills
description: Use when adding, updating, removing, auditing, validating, packaging, versioning, or publishing skills in a reusable multi-runtime Agent Skills repository.
---

# Manage Skill Repository

Keep one canonical skill source while preserving installer compatibility across supported agent
runtimes.

## Workflow

1. Read the repository contract files, installation documentation, plugin manifests, canonical
   `skills/` tree, runtime mirrors, and Git status.
2. State the requested outcome and the checks that will prove it.
3. Change only canonical skill files and the minimum repository metadata required by the request.
4. Run the repository build to synchronize channel manifests and runtime mirrors.
5. Run the available skill, plugin, and repository validators.
6. Review the diff and sensitive-file status before any requested commit or push.

## Repository Rules

- Prefer root `skills/<skill-name>/` as the canonical source.
- Use lowercase kebab-case for the directory and frontmatter `name`.
- Keep each skill's code and resources self-contained; do not import sibling skill paths.
- Allow workflow dependencies only when the aggregate plugin ships both skills. Declare them as
  `**REQUIRED SUB-SKILL:** Invoke <skill-name>` and keep the instruction runtime-neutral.
- Put only required instructions in `SKILL.md`; add `scripts/`, `references/`, `assets/`, or
  `agents/` only when they support the workflow.
- Treat `.claude/skills/` and `.agents/skills/` as generated mirrors when a sync script owns them.
- Treat `package.json` as the canonical source for shared channel metadata and version when the
  repository build owns those fields.
- Do not add subagents, hooks, MCP servers, categories, or release tooling without a concrete need.
- Never commit credentials, local settings, caches, generated output, or session-only plans.

## Operation Guidance

### Add Or Update

Inspect nearby skills for repository conventions. For a new skill, create a valid `SKILL.md` with
only `name` and `description` in frontmatter, then implement the smallest useful body and resources.
For an existing skill, preserve unrelated content and remove only artifacts made obsolete by the
change.

When one skill provides a shared workflow for others, cross-reference it instead of duplicating
its instructions. Use the canonical bare skill name; mention the plugin-qualified name only as a
runtime lookup aid. Verify the required skill exists and is included by every aggregate plugin
installation surface.

### Remove Or Rename

Search manifests, documentation, mirrors, and scripts for references. Update only real consumers,
regenerate mirrors, and verify that no stale symlink or duplicate skill name remains.

### Release Or Publish

Change versions only when a release is requested. Set the version once through the repository
build, verify that every generated manifest and marketplace is synchronized, validate installation
discovery, then perform the repository's sensitive-file check before committing or pushing.

## Verification

Read `references/internal-runtime.md` before running validation. Use the checks supported by the
repository and installed tools, and do not claim cross-runtime support when an applicable manifest
or discovery check failed.

Return the concise evidence described in `references/output-contract.md`.
