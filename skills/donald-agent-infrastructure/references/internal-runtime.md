# Internal Runtime

Run these checks from the `donald-agent-infrastructure` Skill directory:

```bash
python scripts/audit_project_skills.py --skills-root .. --strict
python scripts/test_audit_project_skills.py
python scripts/test_init_project_agent_infra.py
python -m py_compile scripts/*.py assets/templates/*.py
```

The Subagent integration tests require PyYAML. Install it in an isolated test environment before
claiming the complete Subagent path passed.

Smoke-test a generated categorized project:

```bash
python scripts/init_project_agent_infra.py <temporary-repo> \
  --project-name SmokeProject \
  --layout categorized \
  --with-entry \
  --with-governance \
  --with-subagents

python <temporary-repo>/scripts/agent-skills/sync_runtime_skills.py
python <temporary-repo>/scripts/agent-skills/sync_runtime_skills.py --check
python <temporary-repo>/agents/sync_agents.py
python <temporary-repo>/agents/sync_agents.py --check
```

Tests cover the two layouts, independent feature switches, removed profile CLI, mirror drift and
ownership, Windows junction-first fallback order, forced modes, and Claude/Codex/CodeBuddy
Subagent generation and safe cleanup. They verify filesystem contracts, not live agent sessions.
