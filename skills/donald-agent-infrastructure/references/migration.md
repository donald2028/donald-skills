# Migration

## Inventory

Before editing, inspect Git status, root `skills/`, runtime Skill directories,
`scripts/agent-skills/sync_runtime_skills.py`, `.agent-infra/runtime-skill-mirrors.json`, and
AGENTS.md/.gitignore sync rules. Treat uncommitted and divergent runtime content as user-owned.

For ordinary projects, preserve root `skills/` as the sole unregistered authoring source. If the
user explicitly chooses a runtime-native source, reconcile content into that directory before
removing any other source.

## Cleanup

Preview and then apply the bundled migration:

```bash
python scripts/migrate_legacy_runtime_skills.py <repo>
python scripts/migrate_legacy_runtime_skills.py <repo> --apply
```

The migration removes:

- the recognized generated sync script and ownership manifest;
- manifest-owned junctions, symlinks, and unchanged copies in the four legacy runtime paths;
- generated AGENTS.md sync instructions and managed `.gitignore` rules;
- empty directories left by those removals.

It never deletes root `skills/`. If the manifest is invalid, a runtime copy changed, a link points
elsewhere, or the sync script is unrecognized, it stops before deleting anything. Reconcile that
content manually and rerun.

Customized AGENTS.md prose may not match the old generated text exactly. After migration, search for
`sync_runtime_skills.py` and remove any remaining instruction that would recreate runtime links.

## Verify

Run the audit and normal project tests. For default root authoring, confirm these paths are absent:

```text
.agents/skills/
.claude/skills/
.codebuddy/skills/
.workbuddy/skills/
```

Edit a canonical Skill and repeat the check. The normal authoring workflow must not recreate those
paths. Verify subagent generation separately when enabled.
