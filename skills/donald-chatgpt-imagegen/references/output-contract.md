# Output Contract

A prepared and executed job uses:

```text
<system Documents>/Donald Skills/Data/chatgpt-images/<job-name>/<UTC-timestamp>/
├── chatgpt-job.json
├── chatgpt_session.json                    # single-batch session
├── variant_XX_chatgpt_session.json         # independent-variant sessions
├── chatgpt_web_run_summary.json
├── chatgpt_progress.jsonl
├── agent_browser_trace*/                   # reports plus retained diagnostic screenshots
└── *.png                                   # generated candidates
```

The persistent shared output setting replaces `<system Documents>/Donald Skills/Data`.
`DONALD_SKILLS_OUTPUT_ROOT` remains a process-level compatibility override; command-level
`--output-root` replaces the ChatGPT image root itself and has the highest precedence.

CDP locks, submit-throttle counters, and timing metrics are stored in the platform-native Donald
Skills application-state directory. They are not part of this user-facing output contract.

`chatgpt-job.json` keeps uploader metadata and model semantics separate:

- `reference_images` and `ordered_upload_paths` contain the exact local attachment array in
  `Reference Image 1..N` order;
- `model_reference_map` contains per-image `model_role`, `spatial_map`, `use`, and `ignore` fields
  without local paths or filenames; and
- `compiled_model_reference_map` is the exact pure-numbered map inserted into every model-facing
  message. It is empty when the job has no references.

Session files record the conversation URL, attempts, resume state, outputs, and the same separated
`reference_upload_order`, `ordered_upload_paths`, `model_reference_map`, and
`compiled_model_reference_map` audit fields.
They also record `submission_committed` at the submit boundary plus the latest `page_recovery` and
accumulated `page_recoveries` evidence. Before that boundary, recovery replays the whole composer
setup and reference upload. After that boundary, recovery is read-only with respect to submission:
it may reopen the saved conversation to observe or collect, but it must never send the prompt again.
The run summary records request mode, variant results, image paths, status, and the same reference
audit fields so numbering can be checked against local inputs without exposing those identifiers to
the model. Each fresh upload observation records `upload_sequence`, `order_verified`, and
`order_verification_method`. It queues files in order without per-file completion waits, then waits
for the exact attachment count with no pending upload indicators. Visible attachment filenames or
numbered labels must prove manifest order before the prompt is pasted or sent; missing order
evidence or a mismatch blocks submission.
Upload observations include Unix-second `all_uploads_ready_at`, `prompt_pasted_at`, and
`submit_started_at` timestamps for fresh submissions; their order must be nondecreasing.
Fresh-submit reports and summaries also record the verified `chat_surface` (`Chat`, never `Work`),
the verified `image_mode`, reference-upload evidence, requested aspect-ratio delivery
(`ui_control_and_prompt_text` or `prompt_text`), and an actual-dimensions ratio check for every
downloaded image. The ratio check uses a 2% tolerance and must not claim a UI click when ChatGPT
exposes no exact visible ratio control.
`chatgpt_progress.jsonl` records 20-second page-health heartbeats during generation so a run proves
that it remained on the expected conversation and reports a compact latest-turn excerpt,
current-turn error text, visible error surfaces, and Retry controls without waiting for the image
timeout. Deep page-health inspection is performed on the heartbeat rather than the shorter
candidate-collection loop.

Before submission, visible ChatGPT page-level failures such as `Failed to load subscription`,
`Something went wrong`, network/load errors, or an opened tool menu that temporarily omits the
Create image control enter the bounded page-recovery path. The runner reloads and replays the full
preparation transaction. A persistent missing control is reported as page recovery exhaustion;
it is not silently downgraded to ordinary chat.

Routine browser checkpoints are temporary diagnostics. After a request reaches fully
`downloaded`, the runner removes its routine trace screenshots and records
`trace_retention.policy=failures_only` plus the removed count and reclaimed bytes in the run
summary. Trace reports, session state, progress logs, generated PNGs, and failure-specific
screenshots remain. Pass `--keep-success-trace-screenshots` for an explicit debugging run that
must retain every checkpoint. Partial, failed, timed-out, login, and verification outcomes never
trigger successful-trace cleanup.

Collection reuses the saved live target without navigating it. Another same-URL target is
a recovery candidate only after verifying the submitted prompt and generated asset ownership.
`Could not load this ChatGPT conversation` and its page-level Retry are loading failures.
Existing assets take priority over stale Retry text; unresolved Blob downloads retain their
owning page and never cause a new-request recommendation. Loaded assets can be collected
without a composer, viewer, or Download button when no explicit generation control is active.
Valid native PNGs preserve their bytes; `image_validation.native_bytes_preserved=true` and
`image_validation.sha256` record that output.

Important terminal or recoverable states include:

- `downloaded`: requested outputs were recognized and saved;
- `partial_downloaded`: fewer images were returned; collect the current conversation before a
  follow-up;
- `policy_refused`: revise the prompt rather than retrying unchanged;
- `timeout_no_images`: retain traces and session state for diagnosis or resume;
- `generation_failed`: ChatGPT explicitly reported a generation-tool error; start a new request
  instead of repeatedly collecting the failed conversation. This also includes
  `error_type=chatgpt_submitted_turn_missing` when the expected conversation remains blank across
  two consecutive heartbeats after submission, and `error_type=chatgpt_chat_surface_unavailable`
  when the runner cannot prove that Chat rather than Work is selected before submission;
- `generation_failed` with `error_type=chatgpt_page_recovery_exhausted`: the page or heartbeat
  remained unusable after the bounded recovery attempts. The result includes phase, target URL,
  attempt count, individual recovery events, and a retry recommendation instead of waiting forever;
- `submission_state_unknown`: the submit action failed at its commit boundary, so the runner cannot
  safely decide whether resubmission would duplicate the request. Inspect or use `collect-current`
  before sending anything again;
- `download_failed`: ChatGPT produced candidates but the authenticated download stayed unavailable
  after bounded retries; preserve the conversation and retry with `collect-current`;
- `needs_ops` with `error_type=human_attention_required`: login or human verification requires an
  operator in the activated visible browser. The runner preserves that tab and never refreshes or
  attempts the verification itself. A visible `cloudflare_challenge` error is sufficient even
  without a verification frame or clickable widget; it must not trigger Retry. The result is
  `retryable=false` until human action, retains the committed conversation binding, and recommends
  inspection or `collect-current` after resolution rather than a new submission. A homepage
  observation must not overwrite an existing committed conversation URL in session state.

Temporary-to-canonical conversation migration requires both a post-submission user-message count
and the full submitted prompt in the latest user turn. Record that evidence before replacing the
session URL; message counts alone must not authorize an unrelated conversation change.

## Caller Recovery Contract

A nonzero runner exit is not, by itself, a final workflow decision. The caller must read the final
JSON and use `retryable`, `submission_committed`, `recommended_next_action`, the session file, and
the retained conversation URL to choose the next action. The runner's internal recovery remains
the first tier; after it is exhausted, the caller gets one bounded recovery cycle:

| Result evidence | Caller action |
| --- | --- |
| `rerun_same_job` and `submission_committed=false` | Rerun the same command once. |
| `collect_current_first` or `collect_current_or_inspect_conversation` | Run `collect-current` once; do not resubmit first. |
| `submission_state_unknown` or `submission_committed=unknown` | Inspect the session and conversation; use `collect-current` when a conversation URL exists, and never assume the prompt was not sent. |
| `timeout_no_images` | Preserve the existing conversation and run `collect-current` once before considering a new request. |
| `submit_new_request` after explicit `generation_failed` | Start one fresh request from the same manifest with `--no-resume`. |
| `inspect_session_and_rerun_if_not_submitted` | Inspect `chatgpt_session.json`; rerun only if it proves no submission occurred, otherwise collect first. |
| `policy_refused`, `retryable=false`, or human/account intervention | Do not retry unchanged; report or request the required action immediately. |

After that caller recovery fails, or when no safe automatic action exists, return control promptly
with the terminal `status`, `error_type`, internal retry history (`page_recoveries`,
`failure_retries`, or download failures), the caller action attempted, conversation URL, retained
artifact paths, and `recommended_next_action`. Do not silently stop at the first transient error,
wait indefinitely, or create an unbounded retry loop.

## Whole-run health and recovery

- `chatgpt_heartbeat.json` is the latest atomic snapshot; `chatgpt_heartbeat.jsonl` is the event
  journal. Both cover browser startup, throttle waits, mode selection, upload, prompt insertion,
  submission, generation, download, manual verification, and terminal outcomes.
- `--mode status` reads the heartbeat and session without controlling Chrome. It reports stopped
  processes and stale observations separately from completed or paused tasks.
- `--mode health-check` uses a separate tab and returns `health_checked` after two healthy account
  checks five seconds apart, without uploads or submission. It clears only this job's verification
  pause and preserves its session, including unknown submission state. Remaining pause owners or
  service-failure pauses return `queue_paused`; an unusable account returns `needs_ops`.
- Sessions persist `dispatch_started`, `dispatch_started_at`, `owned_target_id`, `prompt_sha256`,
  and `canonical_conversation_url` as evidence becomes available. Send is attempted once; unknown
  outcomes require original-conversation inspection. A retained temporary target can be observed
  in place, but a temporary URL alone is never reopened for recovery.
- `waiting_for_human` continues observation for at most 600 seconds. Two healthy checks five seconds
  apart resume the same task. `needs_ops` means observation ended and human action/recovery remains.
- `queue_paused` blocks fresh submissions, with `queue_health` describing the reason and next check.
  Two distinct exhausted pre-submit jobs within 600 seconds pause the lane; a cooldown of 300 seconds
  permits one health probe. Already-submitted collection is permitted while the queue is paused.
- `upload_refs/mapping.json` maps each original reference to its byte-identical upload alias and full
  SHA-256. Upload observations keep original order and separately record `effective_upload_paths`
  and `upload_aliases`; filename normalization never replaces attachment-count/order verification.
