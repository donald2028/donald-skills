# Skills

This directory is the canonical source for Donald Skills. The initial repository contains only
the collection-management skill; add each business workflow as a self-contained directory:

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
files outside its own directory because installers may distribute it independently.

After changing this tree, regenerate the Claude Code and Codex project mirrors:

```bash
python3 skills/sync_runtime_skills.py
python3 skills/sync_runtime_skills.py --check
```

The aggregate Claude and Codex plugin manifests discover skills directly from this directory.
