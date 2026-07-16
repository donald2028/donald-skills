# Skills

This directory is the canonical source for Donald Skills. Keep each reusable workflow's code and
resources in its own directory:

```text
skills/
  skill-name/
    SKILL.md
    agents/                  # optional runtime metadata
    assets/                  # optional static resources
    references/              # optional detailed instructions
    scripts/                 # optional deterministic automation
```

Use a flat layout until the collection is large enough to need categories. A skill must not read
files outside its own directory. Workflow-level composition is allowed when the aggregate plugin
ships both skills: declare it in `SKILL.md` with `**REQUIRED SUB-SKILL:** Invoke <skill-name>` and
let the agent invoke the dependency instead of importing its files.

After changing this tree, regenerate the Claude Code and Codex project mirrors:

```bash
python3 skills/sync_runtime_skills.py
python3 skills/sync_runtime_skills.py --check
```

The aggregate Claude and Codex plugin manifests discover skills directly from this directory.
