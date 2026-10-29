---
name: donald-chatgpt-imagegen
description: Generate and download images through an external ChatGPT Web session using a visible Chrome browser, with optional local reference images, multiple candidates, aspect ratios, resumable conversations, and recovery artifacts. Use when the user requests ChatGPT external image generation, 外部出图, browser-based ChatGPT image generation, or wants to avoid the current runtime's built-in image generator. Also use it as an external fallback when Codex's built-in image generation is blocked by safety restrictions, fails repeatedly, or otherwise cannot complete the requested image generation, subject to ChatGPT's own policies.
---

# Generate Images With ChatGPT

Prepare a deterministic job manifest, run it through a headed ChatGPT Web session, and preserve
enough session evidence to resume or recover downloads without resubmitting the prompt.

Use `python3` to run the bundled scripts on macOS/Linux. On Windows use `python` (or `py -3`),
substituting it for `python3` in the command examples below.

## Automatic Recovery Authorization

Treat authorization to generate the user's images as authorization for the same task's bounded
recovery. On a recoverable error or timeout, immediately use the runner's recovery and supervision
without asking whether to retry, collect, resume, or restart the worker. This includes inspecting
saved state, reconnecting the saved browser binding, reopening the original durable conversation,
retrying confirmed-unsubmitted preparation, the original failed response's bounded Retry action,
and downloading existing candidates. Both the calling agent and the runner may restart failed
workers/pages and rebuild an unusable request within this authorization. Use the normal supervised
entrypoint; never pass `--worker` directly or disable supervision. Keep the task inputs and browser
binding. Collect committed/uncertain requests first; if bounded lookup proves the original is
unreachable, preserve its record and create a linked replacement as described below.

Keep monitoring the running command through its final result. A progress update may describe
recovery but must not end the task with “shall I retry?” or a pending approval question. An exhausted
worker/run is not the end of the business task: consume the saved continuation below automatically,
without discarding request history. A per-run deadline or saved submission marker does not end the
business task. User action is needed only for a concrete remaining requirement, such as
login, a human challenge, a new Profile choice, or missing input. If the host runtime actually
blocks a tool call on its permission policy, report that exact action and block separately; skill
instructions cannot bypass a runtime approval requirement.

## Prerequisites

- **REQUIRED SUB-SKILL:** Invoke `donald-config-browser` for first-time setup or repair, not as a
  routine per-run gate (`donald-skills:donald-config-browser` when the runtime namespaces plugin
  skills). Only when setup/repair is needed, confirm that it exists in the runtime's skill catalog; this
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
the task page within `--human-wait-timeout` and the remaining run budget (five minutes per run by
default for ordinary requests). Tell the user what requires attention; bring Chrome forward
only when the user explicitly requests it. The runner never clicks verification controls or refreshes an
active challenge. Two healthy checks five seconds apart resume the original task. A timeout returns
`needs_ops`; the supervisor closes this run's owned tabs when it exits. Rerun the saved job to
reopen the durable conversation or use linked recovery for an unreachable temporary request.
Success, failure, timeout, and cancellation all release the task's tabs. Mid-run worker takeovers
retain the original page only until recovery continues or the supervisor exits. Donald Chrome
exits when no other run or intentionally retained page needs it. Cleanup releases the run's
agent-browser connection and confirms the macOS Chrome process
has exited, with bounded termination of only the verified Donald process if normal shutdown stalls.
Only explicit `--keep-browser-open` retains tabs after a non-cancelled supervised exit. Login and
verification pages remain available during the active observation window; `needs_ops` alone does
not request indefinite browser retention.

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
  --timeout 300
```

Treat the command's final JSON as the result contract even when the process exits nonzero. Do not
abandon the image request from the exit code or a single transient browser error alone. The runner
owns the first recovery tier: it retries confirmed page failures three times by default, visible
ChatGPT generation failures twice by default (`--max-failure-retries`), and authenticated image
downloads four times. Keep those bounded defaults enabled for normal runs.

Each worker attempt also owns one outer recovery cycle. A confirmed unsubmitted failure retries the same
job; an unknown or committed submission switches to collection of the original request before
considering a linked replacement. It waits through one queue cooldown (up to five minutes), then
checks the paused task's account health in a separate tab. Recorded verification owners let later
jobs perform that check without executing the owners' prompts. Transient health-page errors get
bounded retries and backoff; a visible human gate stops those automatic clicks and reloads.

If the original target redirects to the home page, the runner waits for page loading to settle and
examines that target's navigation history. It may recover a canonical URL only from post-dispatch
history and after verifying the submitted prompt and user-turn baseline in a separate tab. It
preserves the original lookup evidence. On the same owned target, it also opens a unique sidebar
history control whose full accessible label matches the submitted
prompt, including `role=button` entries without an href. It then verifies the full prompt, the
post-dispatch user-turn baseline, and any saved turn key in the main conversation. That verified
live turn can be collected and its scoped response Retry used even while the URL remains home or
`local-chatgpt:`; it must not be navigated/reloaded as though its URL were durable.

Read `automatic_recovery_actions`, `automatic_recovery_exhausted`, and `chatgpt_supervisor.json`.
The normal invocation also has the bounded process supervision described below. After supervision
stops, consume `recovery_pending` as described below; a worker's failure is not a human handoff.
A real login/verification gate requires the user to complete it.
Submit, follow-up, and collection exit successfully only after all requested images are downloaded.
An empty collection remains unresolved and must retain the original request and continuation.
For an unrecoverable original conversation, preserve the job and its commit state. An explicit
generation failure uses the original response's bounded automatic Retry path. A history entry that
opens home without the original user turn is an unsuccessful lookup, even if its label matches.
After bounded lookup fails, the runner archives the old session and creates a new request identity,
preserving prompt, references, old Retry counts, lineage, and distinct image/trace paths. It permits
two automatic replacements per task; upstream may authorize one more per takeover using the
existing generation authorization. Rebuilding requires complete local inputs; a follow-up relying
on unavailable conversation references needs those files supplied first. `policy_refused` and
account restrictions remain terminal for that request.

A current-response gallery with placeholders, idle composer, no Stop, and no loaded generated
image is `asset_hydration`, not active generation. After 30 seconds the runner may reload that same
verified canonical conversation, at most twice across workers. It preserves loaded image/Blob
assets and never reloads a temporary URL. If hydration still fails (or an unreloadable temporary
gallery stays empty for 90 seconds), it can rebuild a linked request after checking existing results.
Only validated native image bytes count as success; Edit/Share controls and placeholders do not.

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
observable progress, and submission state. Three consecutive unreadable checks request worker
recovery; the supervisor separately enforces progress and total deadlines. Browser calls have absolute
response deadlines even when unrelated CDP events keep arriving.

Page/error detection, page Retry selection, and recovery policy are shared with the text skill in
`scripts/chatgpt_page_health.py`; the build vendors this source alongside the common heartbeat and
supervisor. A readable `Could not load this ChatGPT conversation` error is an immediate recovery
signal, not a failed CDP read or a human gate. Conversation-composer, history, and idle waits consume
that signal so they enter bounded page recovery instead of waiting out their full timeout. A
worker that does not act on an explicit page error can also be taken over after its phase has
remained active for 30 seconds.

The normal invocation runs the worker under a separate supervisor process. It takes over after
60 seconds without a heartbeat, three minutes without observable progress, persistent unreadable
page checks, or an unexpected worker exit. It terminates only that Python worker, retains Chrome
and task evidence, and restarts at most twice with 5/10-second backoffs. Confirmed unsubmitted
preparation starts again; committed or unknown submissions resume the original request without
replaying Send. A stalled generation may reopen its durable conversation; unresolved downloads
retain their live asset target. Active verification keeps its bounded observation loop and resumes
when healthy; an exhausted human gate is handed off rather than restarted in a loop.

`--heartbeat-timeout`, `--stall-timeout`, `--max-worker-restarts`, and `--task-timeout` control
supervision. Each supervised run has a 300-second active budget across all its worker
attempts, including preparation, generation, recovery, and backoff. Retries do not extend it.
This is a responsive operating budget, not a service latency guarantee: approximately one minute
for preparation, two for ordinary generation, and two for recovery, sharing one deadline rather
than fixed stage allocations. Deliberately long or multi-image jobs may explicitly increase both
`--task-timeout` and the generation-stage `--timeout`. `chatgpt_supervisor.json` records the
parent PID and takeover history; `supervisor/<run>/` retains each attempt's stdout/stderr. The final
result includes `supervisor_path` and `worker_restarts`.
Before returning an interrupted/failed run, the supervisor checks the same manifest's saved
`downloaded` session and PNGs. Matching prompt hash, complete expected count, task-owned paths,
stored hashes when present, and full PNG decoding are required. Valid saved outputs return success
with `completion_source=verified_saved_artifacts`, even if the deadline interrupted the worker
before its final stdout. A label alone, missing/corrupt files, or partial outputs cannot complete it.
Task cancellation is separate from automatic takeover. SIGTERM/interrupt stops the owned worker,
releases its recorded browser ownership, and closes the runtime-owned Chrome only when no other
run or retained target needs it. If a worker must be killed before cleanup, the parent uses
`chatgpt_browser_owner.json` to finish that same ownership cleanup. Planned recovery preserves the
original live target. A browser process disappearing during an image run triggers bounded worker
restart on the same binding; it alone is not evidence of user cancellation. SIGTERM/interrupt of
the task or direct worker cancellation remains `interrupted`, with no automatic relaunch. Resume
cancelled work only on explicit instruction. Ordinary service failures use the continuation below
without another approval.
Command and result forwarding stay outside the monitoring loop, including caller stdout/stderr.
Unexpected worker output cannot count as success or disable the deadline. Unclassified worker
failures consume the same restart budget; unexpected supervision errors stop the worker and save
an explicit failure checkpoint. Ordinary runtime errors must retain an executable continuation;
`needs_ops` is reserved for a concrete remaining operator requirement.
The supervisor stays alive independently of the worker; it is not an OS service. If the host or
supervisor is terminated, explicitly resume the saved job. `status` and standalone `health-check`
remain direct diagnostic commands.

### Continue After A Run Budget Is Exhausted

`recovery_pending` means the business task is unfinished, with `business_complete=false` and
`requires_user_action=false`. The supervisor persists `chatgpt_recovery.json`, the exact original
manifest/session, worker logs, a cooldown deadline, and `continuation.argv`. After `not_before`,
execute that argv through the normal tool automatically; it adds `--resume-recovery`. Do not ask
the user, prepare a new manifest, pass `--worker`, or end the caller task merely because this run
exited nonzero. The command also enforces its own cooldown if invoked early.

At most two additional supervised runs are admitted, each with its own bounded active budget.
The checkpoint counts these runs before launch; ordinary reinvocation cannot reset the cap. Scoped
response Retry counts survive every worker/run, including uncertain click timeouts. These are
separate, observable recovery runs, not a silent extension of the original five-minute deadline.

If those runs cannot progress, `recovery_blocked` means the automatic cycle is spent. The upstream
agent owns the next decision; it may execute `continuation.takeover_argv` (`--restart-task`) to
start another bounded cycle after diagnosing/repairing the failure, or `continuation.rebuild_argv`
to collect first and permit one additional linked replacement. These are executable recovery
actions under the existing authorization, not requests for another user approval. Total run and
replacement history remain recorded. Do not repeat unchanged failures indefinitely.

For late results, `continuation.reconcile_argv` uses `collect-current --reconcile-only`; it cannot
Send, click response Retry, or rebuild. It remains available after exhaustion. A checkpoint itself
does not schedule a wakeup or establish that an image exists.

The same takeover entrypoint works while a supervisor is alive:

```bash
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest>" --mode single-batch-submit --restart-task
# Collect first, then allow one additional linked replacement if needed:
python3 "$SKILL_DIR/scripts/agent_browser_runner.py" "<job_manifest>" --mode single-batch-submit --rebuild-request
```

A live supervisor receives an ownership-scoped restart request and reaps its worker before
launching the next. `recovery_requested` acknowledges control delivery, not image completion;
continue monitoring the original command/status. An exited task gets a fresh bounded cycle.

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
  recovery first observes/collects that request; an unreachable original may then be archived and
  replaced through the linked-request path above;
- within the worker, time-based generation refresh is disabled by default; post-submit page
  recovery begins after three consecutive page/DOM heartbeat failures or an explicit browser
  error page. The separate supervisor can restart a worker that exceeds its progress deadline,
  preserving the original request;
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
Reference duplicates are rejected only by identical file hashes or identical full-resolution RGBA
pixels. A small genuine generated edit must retain its native bytes even if it visually resembles
an uploaded reference.

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
