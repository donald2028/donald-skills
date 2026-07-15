# Output Contract

Account collection writes:

```text
<system Documents>/Donald Skills/Data/x/<handle>/_user/
├── timeline.jsonl              # one post or self-thread per line
├── timeline.meta.json          # count, sections, coverage endpoints, stop reason
├── timeline.md                 # human-readable view
├── runs/                       # immutable browser response evidence
├── media/                      # optional downloaded media
├── checkpoint.json             # resumable capture state
├── capture_debug.jsonl         # per-round growth and stop diagnostics
├── rejected_responses/         # unparsed response evidence
└── manifest.json               # run history
```

Single-post collection writes under the same X root at `<handle>/<status-id>/`, including
`thread.json`, `post.md`, `runs/`, and optional `media/`.

`DONALD_SKILLS_OUTPUT_ROOT` replaces `<system Documents>/Donald Skills/Data`; command-level
`--output-root` replaces the X root itself and has higher precedence.

Treat JSON/JSONL as the machine-readable interface. Treat Markdown as a review view and `runs/` as
the source evidence for reparsing after X response-shape changes.
