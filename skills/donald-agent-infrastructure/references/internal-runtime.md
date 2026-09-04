# Internal Runtime

Run these commands from the `donald-agent-infrastructure` skill directory. Keep every skill-local
path relative to that directory so the checks work regardless of the user name or installation
root.

```bash
python3 scripts/audit_project_skills.py --skills-root . --strict
python3 scripts/test_init_project_agent_infra.py

python3 -m py_compile \
  scripts/audit_project_skills.py \
  scripts/init_project_agent_infra.py

DONALD_INFRA_SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/donald-agent-infra-smoke.XXXXXX")"
python3 scripts/init_project_agent_infra.py \
  "$DONALD_INFRA_SMOKE_ROOT" \
  --project-name SmokeProject \
  --profile pipeline \
  --with-governance

cd "$DONALD_INFRA_SMOKE_ROOT"
python3 skills/development/review-skill-best-practices/scripts/audit_project_skills.py \
  --skills-root skills \
  --strict
python3 skills/sync_runtime_skills.py
python3 skills/sync_runtime_skills.py --check
```

The regression tests exercise all four profiles, shared Kimi discovery through `.agents/skills/`,
WorkBuddy adapters and mirrors, supporting-file resolution, drift detection, dry-run behavior,
and preservation of existing project files and WorkBuddy skills. They verify filesystem contracts,
not live agent sessions. For Windows without symlink privileges, sync and check with `--copy`;
preserve existing mirror edits before choosing copy mode, which replaces existing directories.
