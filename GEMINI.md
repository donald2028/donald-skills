# Donald Skills

This extension provides reusable Agent Skills in the `skills/` directory.

Use Gemini CLI's native skill activation flow to load the relevant skill when a task matches it.
Skills are on-demand; do not load unrelated skills. If a loaded skill requires another Donald
skill, activate that dependency before continuing.
