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
  Keep diagnosis and repair on this runner's agent-browser/CDP route and saved browser binding so
  observations refer to the same environment and preserve the original request's ownership.

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
screenshots for a fully hidden application. Keep Chrome behind the user's active app during
startup, attachment, navigation, recovery, and human handoff. When the runner detects login or
anti-automation verification, it preserves the configured CDP Chrome in the background and observes
the task page for up to 10 minutes. Tell the user what requires attention; bring Chrome forward
only when the user explicitly requests it. The runner never clicks verification controls or refreshes an
active challenge. Two healthy checks five seconds apart resume the original task. A timeout returns
`needs_ops` and leaves the browser open in the background; rerun the same job to recover its original
request. After an
ordinary terminal result, the runner closes its tab and exits the Donald Chrome when no other run
is active. Cleanup releases the run's agent-browser connection and confirms the macOS Chrome process
has exited, with bounded termination of only the verified Donald process if normal shutdown stalls.
`needs_ops` and explicit `--keep-browser-open` runs keep Chrome open while releasing that connection.

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

Treat the command's final JSON as the result contract even when the process exits nonzero. Do not
abandon the image request from the exit code or a single transient browser error alone. The runner
owns the first recovery tier: it retries confirmed page failures three times by default, visible
ChatGPT generation failures twice by default (`--max-failure-retries`), and authenticated image
downloads four times. Keep those bounded defaults enabled for normal runs.

The runner also owns one outer recovery cycle. A confirmed unsubmitted failure retries the same
job; an unknown or committed submission switches to collection of the original request. It never
replays Send for either state. It waits through one queue cooldown (up to five minutes), then
checks the paused task's account health in a separate tab. Recorded verification owners let later
jobs perform that check without executing the owners' prompts. Transient health-page errors get
bounded retries and backoff; a visible human gate stops those automatic clicks and reloads.

If the original target redirects to the home page, the runner waits for page loading to settle and
examines that target's navigation history. It may recover a canonical URL only from post-dispatch
history and after verifying the submitted prompt and user-turn baseline in a separate tab. It
preserves ambiguous or unavailable requests for inspection rather than sending again.

Read `automatic_recovery_actions` and `automatic_recovery_exhausted` in the final JSON. When the
runner has consumed its recovery cycle, report the remaining obstacle and retained evidence; do
not add another caller retry loop. A real login/verification gate requires the user to complete it.
Submit, follow-up, and collection exit successfully only after all requested images are downloaded.
An empty collection is an unresolved request, and exhausted automatic collection requires inspecting
the original conversation before a manual recovery decision.
For an unrecoverable original conversation, preserve the job and its commit state. For an explicit
terminal generation failure, prepare a separate replacement job only after the user authorizes a
new submission. `policy_refused` and account restrictions remain terminal for that request.

The normal invocation handles these transitions without separate recovery commands from the
caller. `collect-current` and `health-check` remain available for explicit recovery and diagnostics.
Rendered message counts may shrink while ChatGPT updates or virtualizes a conversation. The runner
uses the submitted prompt to identify its turn and images; a count mismatch alone must not cause
resubmission. Only errors and Retry controls belonging to that request count as generation errors.
A failed command must end in either a downloaded result or an explicit handoff with `status`,
`error_type`, completed recovery actions, conversation URL, retained artifact paths, and
`recommended_next_action`. See `references/output-contract.md` for the result contract.

For every fresh submission, the runner first selects and verifies the top-level `Chat` surface,
never `Work`, using ChatGPT's `Composer mode` button group (`aria-pressed`) or the older
`Select chat surface` radio group (`aria-checked`/`data-state`). Merely seeing Chat and Work labels
does not prove which surface is selected. It then explicitly selects ChatGPT's
`Create image` mode and verifies the selected composer token or image-prompt surface before it
uploads references or sends the prompt. It opens `Add files and more` first when the Create image
control is nested in that menu, including the newer sectioned suggestion list. Visible page-level
errors such as `Failed to load subscription`,
`Something went wrong`, or network/load failures are pre-submit recovery signals. A temporarily
missing Create image control is also recoverable because ChatGPT may omit the tool while such an
error is active; reload and replay the complete preparation transaction within the normal bounded
page-recovery budget. A temporarily disabled control or an unverified selection after a click
also gets a bounded wait (about 15 seconds) before entering page recovery. Recovery first tries a
visible page-loading-error Retry/Try again control, excluding conversation messages and history;
if that does not restore the page, it reloads the target URL. Repeated preparation failures and the
outer unsubmitted retry create a fresh background target on the same browser binding, after
checking that dispatch has not started. The new target must exist before closing the old owned
target. Preparation clears retained draft attachments and rejects any remaining old attachments
before uploading again. Pending attachment controls, their filenames when identifiable, and card
readiness stay in the upload evidence; previews alone do not establish completed uploads.
Persistent failures return
recovery-exhausted evidence to the caller. Never silently submit through Work or fall back to ordinary chat. Reference
uploads likewise require visible composer-attachment evidence before submission.

The runner queues references in manifest-array order without waiting for each file to finish
uploading before adding the next. Each selection only waits for its attachment card to appear so
the composer accepts it before the file input changes again. After all files have been added, it
waits until every attachment is visible and no upload progress remains, then verifies that
visible attachment filenames or numbered labels match the compiled `Reference Image 1..N` map.
Only after both checks pass does it paste and send the prompt. Missing, extra, reordered, or
unverifiable attachments block submission; file selection alone is not proof of attachment order.
The runner creates byte-identical, job-specific numbered upload aliases in `upload_refs/` and records
source paths plus SHA-256 hashes in `upload_refs/mapping.json`. Visible filenames may add a numeric
or `YYYYMMDD-HHMMSS` parenthesized suffix; unknown renames and ambiguous mappings still fail. Aliases
never enter the model-facing reference map.

The runner treats `--aspect-ratio` as an end-to-end output contract. If the current ChatGPT UI
exposes a visible exact ratio control, it clicks that control and records the result. When the UI
has no such control, it records `delivery=prompt_text`, includes the ratio in the submitted image
request, and validates every downloaded image's actual dimensions against the requested ratio with
a 2% tolerance. Never report a ratio UI click when the control was unavailable.

The runner reuses an existing session URL by default. Do not use `--no-resume` unless the saved
conversation is unavailable or the user explicitly requests a fresh conversation.

A read-only observer records the entire run in `chatgpt_heartbeat.json` and
`chatgpt_heartbeat.jsonl`, immediately on phase changes and every 20 seconds during long operations.
It uses a separate bounded CDP connection, so a busy worker cannot stop status reporting. It never
clicks or navigates. Heartbeats distinguish process liveness, page-read failures, phase age, last
observable progress, and submission state. Three consecutive unreadable checks request recovery;
a slow but healthy generation continues until its normal timeout. Browser calls have absolute
response deadlines even when unrelated CDP events keep arriving.

Inspect a running or exited job without opening a browser:

```bash
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest>" --mode status
```

After a restart loses the original target, check the account without touching that request:

```bash
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest>" --mode health-check
```

This opens a separate tab, uploads nothing, and sends nothing. Two healthy checks five seconds
apart clear only that job's human-verification pause; other verification owners and service-failure
pauses remain. The original session and its unknown submission state stay unchanged. A healthy
account does not prove the old prompt was unsubmitted or authorize sending it again.

The existing `chatgpt_progress.jsonl` additionally records generation-specific observations:
conversation continuity, message counts, current-turn errors, and Retry controls.
Message counts and generated-image ownership support both legacy author-role markers and the newer
turn-key, user-bubble, and assistant-heading structure. A missing legacy marker alone is not
evidence that the submitted turn disappeared.
Page recovery is state-aware and bounded (three attempts by default):

- before submission, a confirmed page failure reopens ChatGPT and replays the complete preparation
  transaction: select `Chat`, select `Create image`, apply the ratio control when available, upload
  and verify every reference, and restore the prompt. Confirmed failures include visible
  page-level load/network errors, a missing or persistently disabled Create image control, and a
  clicked mode that remains unverified after the bounded wait. Each recovery uses at most one
  page-error Retry click before reloading. Chat is checked again after uploads and before submit;
- before invoking the submit control, the session records `dispatch_started=true` and
  `submission_committed=unknown`; confirmed submission changes it to `true`. After dispatch,
  recovery may only observe or collect the original conversation, never replay the prompt;
- a normally slow generation is not a recovery signal. Time-based generation refresh is disabled
  by default; post-submit recovery begins only after three consecutive page/DOM heartbeat failures
  or an explicit browser error page;
- after candidates exist, download recovery operates on the existing conversation and never starts
  generation again;
- login, CAPTCHA, Turnstile, Cloudflare, or other human verification suspends new submissions and
  enters the bounded manual-verification wait above. It never causes automatic verification clicks.
  After submission, recovery observes the retained original target or reopens a verified canonical
  conversation. It must not reopen a temporary conversation URL as though it were durable.

Queue health is shared across projects using the same browser data directory and Profile. Two
distinct jobs that exhaust pre-submit page recovery within 10 minutes pause new submissions. A
same-job caller retry counts once. Attachment and content errors do not trip this circuit. A paused
queue still allows recovery/collection of already-submitted requests. The cooldown is five minutes;
only one live process may probe the page and image mode, without sending a prompt. Human-verification
pauses clear only after that task's verification resolves. Circuit files live in the existing native
`chatgpt-web` state directory; they do not contain browser credentials.

After the bounded attempts fail, return `chatgpt_page_recovery_exhausted` to the caller instead of
waiting indefinitely. If the submit action itself errors and the runner cannot prove whether the
click committed, return `chatgpt_submission_state_unknown` and require inspection or
`collect-current` before any resubmission, avoiding duplicate jobs. `--page-recovery-attempts`
changes the bounded recovery count; `--stale-generation-refresh-interval` is diagnostic opt-in and
defaults to disabled.
An explicit ChatGPT generation error interrupts the wait at the next heartbeat. The runner first
uses the visible Retry control up to `--max-failure-retries` times (two by default). Only after
those in-page retries fail does it return structured `generation_failed` with
`recommended_next_action=submit_new_request`; it must not wait until the image timeout or surface a
raw traceback.
A `cloudflare_challenge` response error belongs to this recovery path when it follows the submitted
user turn, including an error bar outside the assistant wrapper. The runner records
`chatgpt_response_service_error` and retries only that request's visible Retry control within the
same budget. A visible verification frame or page-level human-verification instruction takes
priority and enters the manual-verification path. Error codes alone, old messages, prompt text,
and hidden verification frames do not establish an active human gate. The independent heartbeat
records the response error and available Retry so automatic recovery remains observable.
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

Collection reclaims the saved live target without navigating it. A different tab with the same
conversation URL is only a recovery candidate: verify the submitted prompt and its generated image
before adopting it. `Could not load this ChatGPT conversation` and its page-level Try again are
page loading failures, not generation failures. A loaded generated image can be collected without
a composer, viewer, or Download button when no explicit generation control is active. Read visible
Blob assets in their owning browser target; retain that target after an unresolved download and
avoid reloading it. Valid native PNGs keep their original bytes, with SHA-256 and
`image_validation.native_bytes_preserved` recorded after download.

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
