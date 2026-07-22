---
name: donald-chatgpt-imagegen
description: Generate and download images through an external ChatGPT Web session using a visible Chrome browser, with optional local reference images, multiple candidates, aspect ratios, resumable conversations, and recovery artifacts. Use when the user requests ChatGPT external image generation, 外部出图, browser-based ChatGPT image generation, or wants to avoid the current runtime's built-in image generator. Also use it as an external fallback when Codex's built-in image generation is blocked by safety restrictions, fails repeatedly, or otherwise cannot complete the requested image generation, subject to ChatGPT's own policies.
---

# Generate Images With ChatGPT

Prepare a deterministic job manifest, run it through a headed ChatGPT Web session, and preserve
enough session evidence to resume or recover downloads without resubmitting the prompt.

## Prerequisites

- **REQUIRED SUB-SKILL:** Invoke `donald-config-browser` for first-time setup or repair, not as a
  routine per-run gate (`donald-skills:donald-config-browser` when the runtime namespaces plugin
  skills). First confirm that it exists in the runtime's already available skill catalog; this
  discovery must not invoke it or run any configuration command. If it is unavailable, tell the
  user that this workflow requires the setup/repair dependency and ask permission to install it
  into the same skill scope and agent target. After approval, use the runtime's normal installer;
  with Skills CLI, run
  `npx skills add donald2028/donald-skills --skill donald-config-browser --yes`, adding `--global`
  only when this skill is installed globally and preserving the current agent target when needed.
  Discover the installed dependency, then continue. If the user declines, installation fails, or
  the runtime cannot load it, report `needs_dependency` with exact install/retry guidance and stop
  before business execution. On the normal path, do not invoke the available dependency and do not
  run separate `environment`, `profiles`, `check`, or `preflight` commands; prepare the job and run
  the bundled image runner directly. The runner reads the saved `donald-chatgpt-imagegen` binding
  and owns the live Chrome/CDP attach check. Invoke the dependency for that scope only when the user
  asks to configure, inspect, change, reset, or repair the binding, or when the runner reports
  `browser_profile_unconfigured` or identifies a missing, stale, or incomplete browser
  configuration. If the runtime has no native skill-invocation action, use its normal Agent Skills
  discovery/read fallback. The dependency owns first-time environment setup, Profile confirmation,
  shared Cookie state, recommendation rules, and repair; do not reproduce those steps here.
- Runtime requirements are `agent-browser`, Python Pillow, and Google Chrome/Chromium on macOS or
  Linux. Do not probe them separately on the normal path; run the bundled command and handle only
  the missing layer it reports. Invoke browser setup or repair only for browser-specific failures.
- Resolve `SKILL_DIR` to the directory containing this `SKILL.md`.
- After setup, the image runner starts and owns the generation browser lifecycle. Do not run a
  separate configuration preflight before it.

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
activating Chrome because they are not an interactive login or verification state. After an
ordinary terminal result, the runner closes its tab and exits the Donald Chrome when no other run
is active. `needs_ops` and explicit `--keep-browser-open` runs keep Chrome open.

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
