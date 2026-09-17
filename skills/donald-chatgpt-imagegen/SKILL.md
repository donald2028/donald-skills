---
name: donald-chatgpt-imagegen
description: Generate and download images through an external ChatGPT Web session using a visible Chrome browser, with optional local reference images, multiple candidates, aspect ratios, resumable conversations, and recovery artifacts. Use when the user requests ChatGPT external image generation, 外部出图, browser-based ChatGPT image generation, or wants to avoid the current runtime's built-in image generator. Also use it as an external fallback when Codex's built-in image generation is blocked by safety restrictions, fails repeatedly, or otherwise cannot complete the requested image generation, subject to ChatGPT's own policies.
---

# Generate Images With ChatGPT

Prepare a deterministic job manifest, run it through a headed ChatGPT Web session, and preserve
enough session evidence to resume or recover downloads without resubmitting the prompt.

Use `python3` to run the bundled scripts on macOS/Linux. On Windows use `python` (or `py -3`),
substituting it for `python3` in the command examples below.

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
- Runtime requirements are `agent-browser`, Python Pillow, and Google Chrome/Chromium on macOS,
  Windows, or Linux. Do not probe them separately on the normal path; run the bundled command and
  handle only the missing layer it reports. Invoke browser setup or repair only for browser-specific
  failures.
- Resolve `SKILL_DIR` to the directory containing this `SKILL.md`.
- After setup, the image runner starts and owns the generation browser lifecycle. Do not run a
  separate configuration preflight before it. Startup, target ownership, cross-skill active-run
  tracking, and teardown use the shared Donald `BrowserSession`; ChatGPT-specific code begins only
  after that runtime returns an owned target ID.

- Log in to ChatGPT in the selected visible Profile.
- Passing `--user-data-dir` or `CHATGPT_WEB_USER_DATA_DIR` selects an explicit CDP data directory;
  pair it with `--profile` or `CHATGPT_WEB_PROFILE`. `CHATGPT_WEB_CHROME_EXECUTABLE` overrides only
  the Chrome binary. In every case the runner launches Chrome over CDP and attaches agent-browser
  with `--cdp`.

The first run may open Chrome and require interactive login. Return `needs_ops` instead of bypassing
login, MFA, captcha, policy refusals, or account restrictions.

On macOS, keep the automatically launched headed Chrome visible behind the active app during
normal automation; this is not headless mode and does not make Chrome frontmost. The shared
runtime launches the window in the background without hiding it because macOS stops servicing CDP
screenshots for a fully hidden application. Do not activate it during normal
generation. When the runner detects login or anti-automation verification, it returns `needs_ops`,
activates only the configured CDP Chrome, and leaves it open for the user. Explain the required
action and continue after the user confirms completion. Policy refusals are reported without
activating Chrome because they are not an interactive login or verification state. After an
ordinary terminal result, the runner closes its tab and exits the Donald Chrome when no other run
is active. `needs_ops` and explicit `--keep-browser-open` runs keep Chrome open.

## Prepare A Job

By default, each job is written under the system Documents folder at
`Donald Skills/Data/chatgpt-images/<job-name>/<UTC-timestamp>/`. Persist a user-defined shared Data
root with `python3 "$SKILL_DIR/scripts/output_paths.py" set "<shared-output-root>"`; use `show` or
`reset` to inspect or remove it. This setting applies to all Donald output skills.
`DONALD_SKILLS_OUTPUT_ROOT` remains a process-level compatibility override. Pass
`--output-root <path>` to replace the ChatGPT image root for one job; it has the highest precedence.
Never default beside the prompt file, inside the installed skill, or in the current working
directory.

Runner locks, submit throttling, and timing metrics are machine state rather than user output. They
live under `~/Library/Application Support/Donald Skills/state/chatgpt-web/` on macOS,
`%LOCALAPPDATA%\Donald Skills\state\chatgpt-web\` on Windows, and
`${XDG_STATE_HOME:-~/.local/state}/donald-skills/chatgpt-web/` on Linux.

For a plain prompt, place the entire prompt in a `.txt` file. For Markdown, put executable prompt
text in `## Prompt`. Define the reference map before preparing any job that uses local images. Each
reference needs four model-facing fields: what it represents, its whole-image or positional map,
what visual evidence may be used, and what must be ignored. This applies to one image as well as to
multiple images. Use `Whole image` or `Single subject` when positions do not distinguish content.

```markdown
## Required Reference Images

1. `refs/style.png`
   - Role: Page visual style anchor.
   - Spatial map: Whole image; no subject identity mapping.
   - Use: Illustration style, palette, and rendering only.
   - Ignore: People, text, digits, and original layout.
2. `refs/identity.png`
   - Role: Identity sheet for Mia, Ryan, and Owen.
   - Spatial map: Left is Mia, center is Ryan, right is Owen.
   - Use: Facial identity, hair, and body proportions.
   - Ignore: Clothing, background, captions, and composition.

## Prompt

Create a studio portrait...
```

The numbered path list is uploader metadata; local paths and filenames never identify images to the
model. `prepare_job.py` converts the structured entries into two separate artifacts:

- `ordered_upload_paths`, whose array order is the only source for numbering; and
- `model_reference_map` plus `compiled_model_reference_map`, which contain only `Reference Image
  1..N` semantics and are inserted into every submitted message.

The compiler rejects missing or duplicate numbers, missing fields, path/filename-only roles,
placeholders such as `Reference Image 1: Reference Image 1`, stale compiled maps, and spatial maps
that do not identify the whole image, a single subject, or positions such as left/center/right and
top/bottom. Do not collapse several inputs into only `Images 3-5 are character references`; each
number still needs its own entry. Keep style responsibility separate from identity, clothing,
accessory, or composition responsibility, and make `Ignore` override old captions, numbers,
layouts, relationships, unrelated people, or conflicting clothing where relevant.

For a plain prompt plus repeated `--reference` paths, also repeat `--reference-role`,
`--reference-spatial-map`, `--reference-use`, and `--reference-ignore` once per image in exactly the
same order. A path-only `--reference` is invalid. These numbered-map rules are a general
model-facing principle for reference-image generation; this Skill implements and validates only
the external ChatGPT browser path. Do not route the built-in `image_gen` path through this runner.

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

For every fresh submission, the runner first selects and verifies the top-level `Chat` surface,
never `Work`, using ChatGPT's `Select chat surface` control. It then explicitly selects ChatGPT's
`Create image` mode and verifies the selected composer token or image-prompt surface before it
uploads references or sends the prompt. It opens `Add files and more` first when the Create image
control is nested in that menu. A missing or unverifiable Chat surface or image mode is terminal;
do not silently submit through Work or fall back to ordinary chat. Reference uploads likewise
require visible composer-attachment evidence before submission.

The runner uploads references one at a time in manifest-array order. After every upload it requires
a monotonic attachment count, records the numbered local-path sequence, and refuses to submit if it
cannot prove that UI attachment order matches the compiled `Reference Image 1..N` map. Batch file
selection is not used because its attachment order is not treated as reliable.

The runner treats `--aspect-ratio` as an end-to-end output contract. If the current ChatGPT UI
exposes a visible exact ratio control, it clicks that control and records the result. When the UI
has no such control, it records `delivery=prompt_text`, includes the ratio in the submitted image
request, and validates every downloaded image's actual dimensions against the requested ratio with
a 2% tolerance. Never report a ratio UI click when the control was unavailable.

The runner reuses an existing session URL by default. Do not use `--no-resume` unless the saved
conversation is unavailable or the user explicitly requests a fresh conversation.

During generation the runner records a structured page-health observation every 20 seconds in
`chatgpt_progress.jsonl`: target-conversation continuity, composer/challenge state, message counts,
the compact latest-turn excerpt, and any recognized current-turn error text, visible error surface,
or Retry control. Deep page-health inspection runs on that heartbeat, not on the shorter
candidate-collection loop.
Page recovery is state-aware and bounded (three attempts by default):

- before submission, a confirmed page failure reopens ChatGPT and replays the complete preparation
  transaction: select `Chat`, select `Create image`, apply the ratio control when available, upload
  and verify every reference, and restore the prompt;
- after the submit control has been invoked, the session records `submission_committed=true` and
  recovery may only reopen the same conversation and continue observing or collecting it. It must
  never replay the prompt or submit a replacement request;
- a normally slow generation is not a recovery signal. Time-based generation refresh is disabled
  by default; post-submit recovery begins only after three consecutive page/DOM heartbeat failures
  or an explicit browser error page;
- after candidates exist, download recovery operates on the existing conversation and never starts
  generation again;
- login, CAPTCHA, Turnstile, Cloudflare, or other human-verification evidence bypasses automatic
  recovery, activates and preserves the visible browser, and returns `needs_ops` for a human click.

After the bounded attempts fail, return `chatgpt_page_recovery_exhausted` to the caller instead of
waiting indefinitely. If the submit action itself errors and the runner cannot prove whether the
click committed, return `chatgpt_submission_state_unknown` and require inspection or
`collect-current` before any resubmission, avoiding duplicate jobs. `--page-recovery-attempts`
changes the bounded recovery count; `--stale-generation-refresh-interval` is diagnostic opt-in and
defaults to disabled.
An explicit ChatGPT generation error ends the wait at the next heartbeat with structured
`generation_failed` and `recommended_next_action=submit_new_request`; it must not wait until the
image timeout or surface a raw traceback.
If the expected conversation URL remains open but the submitted user turn is missing for two
consecutive heartbeats, the runner reports `generation_failed` with
`error_type=chatgpt_submitted_turn_missing`. This covers blank, unrecoverable conversation shells
without treating a single slow page render as terminal.
After a request is fully downloaded, routine browser checkpoint screenshots are removed by default
while reports, session state, progress logs, generated PNGs, and failure-specific screenshots stay
in place. Pass `--keep-success-trace-screenshots` only when a debugging run needs every successful
checkpoint. Partial, failed, timed-out, login, and verification outcomes retain their screenshots.
Generated-image downloads use the logged-in browser and retry transient failures four times. An
authenticated ChatGPT URL must not fall back to an unauthenticated HTTP request. If all attempts
fail, preserve the conversation and candidates and return structured `download_failed` with
`recommended_next_action=collect_current_first` instead of raising a traceback.

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
- Confirm `chat_surface.verified` with `selected_surface=chat`, `image_mode.verified`, reference
  `upload_observation.ready`, and
  `aspect_ratio_validation.all_match` for fresh requests that use those inputs.
- Validate `chatgpt_web_run_summary.json`, session JSON, conversation URL, and trace reports.
- Report `partial_downloaded`, `policy_refused`, timeout, or login-required states honestly.
- Keep session state, progress logs, reports, and failure-specific screenshots until the caller
  accepts the outputs; remove them only when the user no longer needs resume or audit evidence.

See `references/output-contract.md` for the artifact layout and status semantics.
