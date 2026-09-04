# Output Contract

When using this skill, return one of these deliverables.

## Planning-Only Result

- current layout: contract files, canonical skills, runtime mirrors, subagents
- target profile: minimal, categorized, pipeline, or subagents
- optional layers: governance and OpenAI-specific UI metadata when applicable
- migration plan: ordered non-destructive steps
- risks: existing divergent mirrors, dirty worktree, missing metadata, or unknown runtime behavior
- verification: commands or artifacts that will prove the migration

## Implemented Result

- files created or changed
- profile initialized
- mirror targets configured
- audit result summary
- sync check result summary
- runtime coverage: configured discovery paths versus actual in-app checks; subagent support separately
- any skipped work and why

Do not claim the infrastructure is ready until the audit and sync check have run or you clearly say
which check could not run.
