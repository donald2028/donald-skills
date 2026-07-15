---
name: donald-collect-wechat
description: Collect or refresh a WeChat Official Account's article list, public article bodies, publish timestamps, digests, images, and audio markers through a headed logged-in Chrome session that stays hidden during normal automation. Use for 微信公众号内容采集、公众号文章抓取、导出公众号历史文章、增量补采, or building a traceable local archive from a WeChat account.
---

# Collect WeChat Accounts

Collect article metadata from the WeChat Official Account backend's visible article picker, then
optionally fetch public `mp.weixin.qq.com` article bodies. Keep the workflow read-only and preserve
the captured network responses as evidence.

## Prerequisites

- Install `agent-browser` and Google Chrome.
- Resolve `SKILL_DIR` to the directory containing this `SKILL.md`.
- Check this skill's independent Profile binding before collecting:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" check
```

If it reports `needs_initialization`, `stale_profile`, or `incomplete_user_data_dir`, run
`environment`, then `profiles`; present the choices and wait for the user to confirm one before
running `set --profile <choice>`. This bundled script automatically uses the
`donald-collect-wechat` config. If another skill selects the same Profile, both configs
reuse the same CDP User Data, cookies, login state, and port.

- Launch the selected headed Profile in the background with the CDP port used below:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" preflight \
  --session <agent-browser-session> \
  --url about:blank
```

Continue only when preflight reports `ready`; it proves both Chrome CDP and
`agent-browser --cdp` attach before the article-picker workflow starts. Reuse the returned
`cdp_port` in every collection command below.

- Log in to the WeChat Official Account backend in that Profile.

On macOS, keep the automatically launched headed Chrome hidden throughout normal collection; this
is not headless mode and prevents delayed page work from promoting Chrome to the foreground. When
login, verification, risk control, or anti-automation requires operator interaction, run
`python3 "$SKILL_DIR/scripts/profile_config.py" activate`, explain the required action, and keep
Chrome open until the user confirms completion. Public-body collection performs this activation
automatically when it recognizes a WeChat login or verification page.

After preflight proves `agent-browser --cdp` can attach, the bundled collectors create dedicated
browser-level CDP targets with `background: true`, enable focus emulation, and use trusted CDP input.
They never call focus-stealing `agent-browser tab new`, `tab`, `open`, or input commands on the
normal path. Treat Chrome becoming frontmost as a collection failure.

Do not bypass login, captcha, risk prompts, or account permissions. Return `needs_ops` when human
interaction is required.

## Collect Article Metadata

By default, outputs are written under the system Documents folder at
`Donald Skills/Data/wechat/<account-slug>/`. Set `DONALD_SKILLS_OUTPUT_ROOT` to change the shared
Data root for all Donald tools, or pass `--output-root <path>` to replace the WeChat root for one
capture. The CLI override wins over the environment setting. Never default to the installed skill
or current working directory.

The collector starts from the backend home page and drives this visible UI flow in a background CDP
target: open `文章`, open `超链接`, click `选择其他账号`, search the exact account nickname (or its
WeChat ID when supplied), and select the exact result. It then captures the browser-produced
`appmsgpublish` responses while paging. It never replays that endpoint itself.

The command creates a unique UTC run directory, merges prior runs by URL, writes a small
`index.json`, monthly JSONL indexes, and one `article.json` per article, then returns the run as
`run_dir`:

```bash
python3 "$SKILL_DIR/scripts/collect_account_articles.py" \
  --session <agent-browser-session> \
  --cdp <configured-cdp-port> \
  --account "<account name>" \
  --wechat-id "<optional exact WeChat ID>" \
  --pages 20
```

Verify that `page_begins` advances across pages. A click without a new `begin` value does not prove
pagination. Stop cleanly if the next-page control disappears or repeated clicks yield no new page.

For a refresh, rerun the same collector with the required page count. It automatically merges every
`runs/*` evidence file and deduplicates articles by URL. Read `index.json` first, then only the
needed `indexes/YYYY-MM.jsonl` shard and per-article record; never rebuild a monolithic article
array. Do not delete older evidence unless the user explicitly requests replacement.

## Fetch Public Article Bodies

Fetch selected recent bodies through the same headed Chrome/CDP profile:

```bash
python3 "$SKILL_DIR/scripts/fetch_public_article_bodies.py" \
  "<account-root>" \
  --cdp <configured-cdp-port> \
  --session <agent-browser-session> \
  --limit 12
```

Use repeated `--title-contains` for targeted articles or repeated `--url` for explicit public URLs.
Bodies are written beside their metadata as
`<account-root>/articles/YYYY/MM/<article-id>/article.md`; in-body images are downloaded to the
same article's `images/` directory and Markdown links are rewritten to relative local paths. The
Markdown is a compact reading copy: an article title when one exists, one source byline, and the
DOM-derived article body.
Fetch diagnostics and interaction fields stay in `article.json`, not in the Markdown. The latest
selection manifest is written to `<account-root>/body-fetch-manifest.json`.
Record blocked or unavailable bodies honestly; do not replace them with search-engine snippets or
third-party reposts. Model ordinary articles as `content_type=article` and WeChat
`item_show_type=10` 图文 as `content_type=image_text`. When `is_user_title=0`, the 图文 `title` is
only a WeChat-generated copy/preview of `content`: leave the semantic title empty, use a short
`display_title` only in indexes, and write the complete backend `content` once to `article.md`.

Each ordinary article opens in a dedicated `background: true` CDP target. The body script closes
that target in a `finally` block and verifies its target ID is gone before continuing. It
deliberately keeps only a recognized login or verification target open, activates Chrome for the operator, and records
`browser_tab_cleanup: kept_open_for_human`. Public interaction counts are best-effort: record
reading, like, or share counts only when the public page exposes them; otherwise write
`interaction_metrics.status: unavailable` instead of treating page defaults such as `0` as real
measurements. 图文 already materialized from backend `content` does not open a redundant public
article target. Never call a hidden statistics endpoint directly.

## Completion Check

- Confirm `index.json` exists and has a plausible non-zero `count`.
- Inspect `coverage_status`, all warnings, and the listed shard counts.
- Confirm captured `begin` values are distinct and advancing.
- If bodies were requested, inspect `body-fetch-manifest.json` and report successful, blocked, and
  missing counts separately.
- Report the account, capture time, article count, output root, and any operator action required.

See `references/output-contract.md` for the artifact layout.
