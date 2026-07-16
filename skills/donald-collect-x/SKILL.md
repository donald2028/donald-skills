---
name: donald-collect-x
description: Collect, refresh, or backfill an X/Twitter account's original posts, self-threads, long-form Articles, and media through a visible logged-in Chrome session. Use for X posts collection, Twitter account archiving, thread capture, 按账号采集推文、增量采集、补采历史内容, or exporting a single X post and its self-thread.
---

# Collect X Posts

Collect an account's own content from browser-produced X GraphQL responses. Use a visible Chrome
session over CDP, preserve raw response runs, and compile deterministic JSONL/Markdown outputs.

## Prerequisites

- **REQUIRED SUB-SKILL:** Invoke `donald-config-browser` before this workflow
  (`donald-skills:donald-config-browser` when the runtime namespaces plugin skills). Configure the
  `donald-collect-x` scope and continue only after its check and preflight report `ready`. That
  skill owns environment setup, Profile selection, shared Cookie state, and one-off CDP proof; do
  not reproduce those steps here. If the runtime has no native skill-invocation action, load the
  installed dependency through its normal Agent Skills discovery/read fallback. If it is not
  installed, report `needs_dependency` with aggregate Donald plugin installation guidance and stop
  before running the collector.
- Use Python 3.10+, `agent-browser`, and Google Chrome.
- Resolve `SKILL_DIR` to the directory containing this `SKILL.md`.
- The configuration preflight closes any Chrome it starts before returning. The collector runner
  then starts and owns the headed Chrome/CDP session used for X and stops with `needs_ops` if any
  runtime layer is unavailable.

- Log in to X in the selected visible Profile.
- `X_COLLECTOR_CHROME_EXECUTABLE` and `X_COLLECTOR_CHROME_DATA_DIR` remain explicit legacy
  overrides; the collector still verifies `agent-browser --cdp` attach before collecting.
- Install `curl`; install `ffmpeg` and `yt-dlp` when video/media download is required.

On macOS, keep the automatically launched headed Chrome hidden during normal automation; this is
not headless mode. Do not activate it during normal
collection. When the collector detects a login wall, rate limit, or blocking error page, it returns
`needs_ops`, activates only the configured CDP Chrome, and leaves it open for the user. Explain the
required action and continue after the user confirms completion.

Do not forge cookies, replay GraphQL endpoints, run headless, or bypass login walls, captcha, rate
limits, or challenges. Return `needs_ops` when the visible session needs human action.

## Collect An Account

By default, account and post outputs are written under the system Documents folder at
`Donald Skills/Data/x/`. Set `DONALD_SKILLS_OUTPUT_ROOT` to change the shared Data root for all
Donald tools, or pass `--output-root <path>` to replace the X root for one command. The CLI override
wins over the environment setting. Never default to the installed skill or current working
directory.

Always choose the post and scroll budgets explicitly:

```bash
python3 "$SKILL_DIR/scripts/research_user.py" \
  --handle <handle> \
  --mode full \
  --max-posts 200 \
  --max-scrolls 200
```

Use `--no-media` for a faster text/metadata pass. Posts are the primary stream; Articles are
supplementary and do not replace a requested Posts quota.

Choose one collection mode:

- `--mode head`: collect new posts and stop after reconnecting with known status IDs.
- `--mode backfill`: extend the older tail across repeated sessions.
- `--mode full`: perform a first or unconstrained historical pass.

`--incremental` is an alias for `--mode head`. Use `--since YYYY-MM-DD` for a date boundary. If a
run stops at `max_scrolls_reached` while `post_count` was still rising, increase the budget and rerun;
do not report the account as exhausted.

## Collect One Post Or Thread

```bash
python3 "$SKILL_DIR/scripts/research_post.py" \
  --url "https://x.com/<handle>/status/<status-id>" \
  --max-scrolls 15
```

Omit `--cdp` to use this skill's configured shared Profile port. Pass it only as an explicit
legacy override.

Keep only the target author's self-thread in the compiled thread. Do not treat unrelated replies,
quotes, or reposts as the account's own evidence.

## Completion Check

- Confirm `<handle>/_user/timeline.jsonl` and `timeline.meta.json` exist for account runs.
- Inspect `stop_reason`, `count`, `section_counts`, newest/oldest status IDs, and
  `capture_debug.jsonl` when growth looks suspicious.
- Confirm X Articles include their structured body blocks, not only a `t.co` link.
- Report media as downloaded, reused, skipped, or blocked.
- Report the handle, mode, count, stop reason, output root, and any operator action required.

See `references/output-contract.md` for the artifact layout.
