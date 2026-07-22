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
`--output-root` replaces the X collection root itself and has higher precedence. Passing a handle
directory or `_user` directory is rejected; the canonical account directory is always
`<output-root>/<handle>/_user/`.

Account commands print one JSON object with `schema_version`, `status`, `reason`, `hint`, `handle`,
`output_root`, `canonical_user_dir`, `stop_reason`, `posts`, `articles`, `total_items`,
`capture_posts_seen`, `capture_new_posts`, `capture_known_overlap_posts`, `known_before_posts`, and
`articles_included`. Browser/operator failures use `status=needs_ops`; stable reasons include
`browser_profile_unconfigured`, `cdp_unavailable`, `login_wall`, `captcha`, and `rate_limited`.

`--capture-max-posts`/`--max-posts`, `--max-scrolls`, and `--since` are capture boundaries. They do
not trim the persisted timeline, which is rebuilt from all immutable `runs/`. Head mode skips
Articles unless `--include-articles` is passed. An interrupted run writes
`status=interrupted`, `stop_reason=interrupted`, captured counts, paths, and `recovery_hint` to
`timeline.meta.json`; rerunning the same command resumes from preserved runs.

Treat JSON/JSONL as the machine-readable interface. Treat Markdown as a review view and `runs/` as
the source evidence for reparsing after X response-shape changes.
