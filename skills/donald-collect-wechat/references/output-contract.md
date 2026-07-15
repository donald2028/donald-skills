# Output Contract

The default account archive is:

```text
<system Documents>/Donald Skills/Data/wechat/<account-slug>/
├── index.json                     # small account summary and shard catalog
├── indexes/YYYY-MM.jsonl          # compact, streamable article lookup rows
├── body-fetch-manifest.json        # optional
├── articles/YYYY/MM/<article-id>/
│   ├── article.json               # metadata, fetch status, metrics, asset manifest
│   ├── article.md                 # optional compact reading copy
│   └── images/                    # optional localized in-body images
└── runs/<timestamp>/               # raw network and browser evidence
```

`DONALD_SKILLS_OUTPUT_ROOT` replaces `<system Documents>/Donald Skills/Data`; command-level
`--output-root` replaces the WeChat root itself and has higher precedence.

`index.json` contains only account-level metadata, total count, coverage status, warnings, and a
catalog of monthly shards. It must not contain an all-article array. Each monthly JSONL row contains
only the fields needed to locate an article: stable ID, date, semantic `title`, synthetic
`display_title`, digest, URL, `content_type`, and relative `article.json` path. Read JSONL line by
line or select one month instead of loading the whole archive.

Each `article.json` is the source of truth for one article's metadata and fetch state. It excludes
the full body; `article.md` stores the readable body once. Set `content_type=article` for ordinary
articles and `content_type=image_text` for `item_show_type=10` 图文. For 图文 with
`is_user_title=0`, keep `title` empty, expose a short synthetic `display_title` only for discovery,
and convert the complete backend `content` to Markdown once without adding a duplicate heading.

`body-fetch-manifest.json` records each selected URL, Markdown output path, fetch status, content
length, localized-image counts, audio markers, best-effort public interaction metrics, article-tab
cleanup status, method, and blocked reason. Markdown contains no diagnostic field dump. Missing
public metrics are `unavailable`, not zero. Keep raw
`wechat-network-request-*.json` files so the compiled index can be audited or rebuilt.
