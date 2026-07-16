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

After changing this tree, rebuild the channel manifests and runtime mirrors:

```bash
npm run build
npm run build:check
```

The aggregate Claude, Codex, Cursor, Kimi Code, and Gemini CLI manifests discover skills directly
from this directory. OpenCode discovers the generated `.claude/skills/` and `.agents/skills/`
mirrors according to its native Agent Skills search paths.
