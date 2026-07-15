---
name: donald-chatgpt-imagegen
description: Generate and download images through an external ChatGPT Web session using a visible Chrome browser, with optional local reference images, multiple candidates, aspect ratios, resumable conversations, and recovery artifacts. Use when the user requests ChatGPT external image generation, 外部出图, browser-based ChatGPT image generation, or wants to avoid the current runtime's built-in image generator.
---

# Generate Images With ChatGPT

Prepare a deterministic job manifest, run it through a headed ChatGPT Web session, and preserve
enough session evidence to resume or recover downloads without resubmitting the prompt.

## Prerequisites

- Install `agent-browser` and Python Pillow.
- Use Google Chrome/Chromium on macOS or Linux.
- Resolve `SKILL_DIR` to the directory containing this `SKILL.md`.
- Check this skill's independent Profile binding before generating:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" check
```

If it reports `needs_initialization`, `stale_profile`, or `incomplete_user_data_dir`, run
`environment`, then `profiles`; present the choices and wait for the user to confirm one before
running `set --profile <choice>`. This bundled script automatically uses the
`donald-chatgpt-imagegen` config. If another skill selects the same Profile, both
configs reuse the same CDP User Data, cookies, login state, and port. Before submitting a job, run
`preflight` without overriding its configured port and continue only when real Chrome CDP and
`agent-browser --cdp` both report ready.

- Log in to ChatGPT in the selected visible Profile.
- Passing `--user-data-dir` or `CHATGPT_WEB_USER_DATA_DIR` selects an explicit CDP data directory;
  pair it with `--profile` or `CHATGPT_WEB_PROFILE`. `CHATGPT_WEB_CHROME_EXECUTABLE` overrides only
  the Chrome binary. In every case the runner launches Chrome over CDP and attaches agent-browser
  with `--cdp`.

The first run may open Chrome and require interactive login. Return `needs_ops` instead of bypassing
login, MFA, captcha, policy refusals, or account restrictions.

On macOS, keep the automatically launched headed Chrome hidden during normal automation; this is
not headless mode. Do not activate it during normal
generation. When the runner detects login or anti-automation verification, it returns `needs_ops`,
activates only the configured CDP Chrome, and leaves it open for the user. Explain the required
action and continue after the user confirms completion. Policy refusals are reported without
activating Chrome because they are not an interactive login or verification state.

## Prepare A Job

By default, each job is written under the system Documents folder at
`Donald Skills/Data/chatgpt-images/<job-name>/<UTC-timestamp>/`. Set
`DONALD_SKILLS_OUTPUT_ROOT` to change the shared Data root for all Donald tools, or pass
`--output-root <path>` to replace the ChatGPT image root for one job. The CLI override wins over
the environment setting. Never default beside the prompt file, inside the installed skill, or in
the current working directory.

Runner locks, submit throttling, and timing metrics are machine state rather than user output. They
live under `~/Library/Application Support/Donald Skills/state/chatgpt-web/` on macOS,
`%LOCALAPPDATA%\Donald Skills\state\chatgpt-web\` on Windows, and
`${XDG_STATE_HOME:-~/.local/state}/donald-skills/chatgpt-web/` on Linux.

For a plain prompt, place the entire prompt in a `.txt` file. For Markdown, put executable prompt
text in `## Prompt`. Optional reference images can be supplied with repeated `--reference` flags or
declared in the card:

```markdown
## Required Reference Images

1. `refs/identity.png`
2. `refs/product.png`

- Reference Image 1: controls identity only.
- Reference Image 2: controls product shape and color only.

## Prompt

Create a studio portrait...
```

Prepare the manifest once:

```bash
python3 "$SKILL_DIR/scripts/prepare_job.py" prompt.md \
  --variants 2 \
  --request-mode single_batch \
  --aspect-ratio 1:1
```

Use `single_batch` for natural sampling variations of one prompt. Use `independent_variants` with
repeated `--variant-note` when each candidate needs a distinct direction.

## Run Or Resume

Submit a prepared job:

```bash
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest returned by prepare_job.py>" \
  --mode single-batch-submit \
  --session chatgpt-image-<job-name> \
  --timeout 1200
```

The runner reuses an existing session URL by default. Do not use `--no-resume` unless the saved
conversation is unavailable or the user explicitly requests a fresh conversation.

If ChatGPT completed generation but a candidate was not downloaded, collect from the saved
conversation without sending the prompt again:

```bash
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest returned by prepare_job.py>" \
  --mode collect-current \
  --session chatgpt-image-<job-name>
```

To attach to an already-running Chrome, pass `--cdp <port-or-url> --no-launch-browser`. For a second
prompt in the same conversation, prepare a separate `single_batch` job with
`--reuse-conversation-references`, then run `--mode conversation-followup --conversation-session
<first-output>/chatgpt_session.json`.

## Completion Check

- Confirm the requested candidate count and inspect every PNG.
- Validate `chatgpt_web_run_summary.json`, session JSON, conversation URL, and trace reports.
- Report `partial_downloaded`, `policy_refused`, timeout, or login-required states honestly.
- Keep recoverable artifacts until the caller accepts the outputs; remove them only when the user
  no longer needs resume or audit evidence.

See `references/output-contract.md` for the artifact layout and status semantics.
