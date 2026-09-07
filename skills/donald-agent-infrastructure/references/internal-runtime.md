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

Smoke-test a generated categorized project with an unregistered authoring source:

```bash
python scripts/init_project_agent_infra.py <temporary-repo> \
  --project-name SmokeProject \
  --layout categorized \
  --with-entry \
  --with-governance \
  --with-subagents

python <temporary-repo>/agents/sync_agents.py
python <temporary-repo>/agents/sync_agents.py --check
```

Confirm `.agents/skills/`, `.claude/skills/`, `.codebuddy/skills/`, and `.workbuddy/skills/` were not
created. Tests cover both layouts, explicit native-root selection, independent feature switches,
removed profile CLI, conservative legacy-mirror cleanup, and Claude/Codex/CodeBuddy Subagent
generation and safe cleanup. They verify filesystem contracts, not live agent sessions.

## Verifying Runtime Skill Discovery

Filesystem checks do not prove a runtime registers a Skill. When a runtime-native source or an
explicit consumer installation is requested, invoke a Skill by its bare name in that runtime and
read the `Base directory for this skill:` line it reports. Record the runtime version and build.

The runtime's injected Skill list is not evidence on its own: it normally contains only user-level
and built-in plugin Skills, so a project Skill can resolve and activate by name while absent from
that list. Discovery paths are product behavior and can change between releases; see
`references/workbuddy.md` for the current WorkBuddy measurement.
