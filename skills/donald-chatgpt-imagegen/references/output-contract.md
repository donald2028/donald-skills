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
Generation heartbeats record the composer controls used for the download decision in
`page_health.composer_controls`; `generation_active` uses that same decision, while
`text_generation_active` retains the separate text heuristic. `recognized_candidates` identifies
candidate ownership and dimensions; a candidate count alone does not prove generation is idle.
`submitted_prompt_present` verifies the complete normalized prompt in one user message and
`submitted_turn_key` identifies its rendered turn. Virtualization and mixed message markup can
reduce DOM counts during generation, so a matching prompt takes precedence over the saved count.
Duplicate prompt nodes within one recognized turn count as one message identity; matching prompts
in distinct turns remain ambiguous. Mixed markup must not assign an assistant image the role of a
different user element elsewhere in the same turn.
Candidate `inSubmittedTurn` binds images to that request even when counts shrink; images in other
turns remain excluded. Generation errors and Retry controls are scoped to that request, including
during `collect-current`; page-wide Retry text alone is not a generation failure.
`Could not load this ChatGPT conversation` is a page loading failure. A same-URL copy cannot
override a prompt-verified generated asset in a live target. Collection reuses the saved live
target even without a retained-target entry, and may recover another target only after checking
the submitted prompt and asset ownership. Loaded assets take priority over Retry text; unresolved
Blob downloads retain their owning page and never cause a new-request recommendation.
Valid downloaded PNGs remain byte-identical to the browser asset;
`image_validation.native_bytes_preserved=true` and `image_validation.sha256` record that output.
Page-level load failures during collection use the bounded post-submit page recovery path and
reopen only the original conversation. Collection writes current image counts and the image list
to both stdout and session. Successful collection clears active failure fields while retaining
failure attempts and logs.
Sidebar/navigation failures such as `Unable to load history` are excluded from page and generation
failure detection and Retry selection. Error text aggregated by their ancestor containers must not
trigger recovery of a readable conversation; account/page failures outside those regions still do.
When recognized turn containers exist, unrelated article/section containers cannot own generation
Retry controls. A page-level feature-check Retry outside the request is not a generation failure.

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

Important terminal or recoverable states include:

- `downloaded`: requested outputs were recognized and saved;
- `partial_downloaded`: fewer images were returned; collect the current conversation before a
  follow-up;
- `policy_refused`: revise the prompt rather than retrying unchanged;
- `timeout_no_images`: retain traces and session state for diagnosis or resume;
- `collect_current_no_new_images`: the bounded collection window ended without a verified image.
  `error_type=chatgpt_collection_no_images` replaces stale generation errors in the session while
  preserving failure attempts. The request remains unconfirmed (`terminal=false`); the result
  recommends `inspect_original_conversation_before_manual_recovery`, with `retryable=false` and
  `should_collect_current_first=false`, rather than asking the caller to repeat collection;
- `generation_failed`: ChatGPT explicitly reported a generation-tool error; use the original
  response's bounded automatic Retry path without another approval. After recovery is exhausted,
  report failure without waiting for permission to repeat it. `error_type=chatgpt_submitted_turn_missing` instead means that the
  submitted prompt could not be identified across two healthy heartbeats. It recommends
  `collect_current_first` and enters the outer collection recovery without clicking Retry or
  sending again; rendered counts do not prove that a request was lost. This status also includes
  `error_type=chatgpt_chat_surface_unavailable`
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
  operator in the preserved background browser. The runner requests attention without activating
  Chrome; foreground activation requires an explicit user request. It never refreshes or
  attempts the verification itself. A visible verification frame or page-level verification
  instruction establishes this gate and takes priority over response-level Retry. The result is
  `retryable=false` until human action, retains the committed conversation binding, and recommends
  inspection or `collect-current` after resolution rather than a new submission. A homepage
  observation must not overwrite an existing committed conversation URL in session state.
- A current-response `cloudflare_challenge` error without an active verification gate is
  `chatgpt_response_service_error`. The runner observes and retries the original request's visible
  Retry control up to `--max-failure-retries` times (two by default), then returns structured
  `generation_failed` if it persists. The error bar may sit after the user turn outside an assistant
  wrapper. Old-turn errors, prompt mentions, and hidden challenge frames do not trigger this path.
  `chatgpt_heartbeat.json` exposes `page.responseError` with the marker, excerpt, and
  `retry_available`; `recovery_required=true` makes the outstanding recovery visible. Real human
  verification retains its manual-completion requirement.

Temporary-to-canonical conversation migration requires both a post-submission user-message count
and the full submitted prompt in the latest user turn. Record that evidence before replacing the
session URL; message counts alone must not authorize an unrelated conversation change.

## Caller Recovery Contract

A nonzero runner exit is not, by itself, a final workflow decision. Read the final JSON and saved
commit state. Recovery within the authorized task and its existing budgets needs no additional
user approval. Keep monitoring the normal supervised command until it returns a checked result
or an explicit saved failure; do not replace its recovery with an approval question. Each worker
owns internal retries and one outer recovery cycle:

| Evidence | Automatic action |
| --- | --- |
| Confirmed unsubmitted page failure | Retry the same job once after internal recovery. |
| Unknown/committed submit or pending download | Collect the original request once; never replay Send. |
| Paused account | Wait through one cooldown, then check the recorded owner's health in a separate tab without executing its prompt. |
| Original target redirected home | Inspect post-dispatch history, then a unique exact-prompt sidebar control including role=button without href. Verify the original main user turn, baseline, saved turn key, and owned target before collecting or scoped Retry, even without a canonical URL. |
| Fresh login/verification gate | Preserve the request and request human completion; do not click or reload the challenge. |
| Explicit generation error | Apply the original response's bounded Retry automatically; report a terminal failure when recovery is exhausted. |

`automatic_recovery_actions` records the worker's outer cycle. The default supervisor may restart
a failed or stalled worker within its separate process/time budget, preserving commit state;
`automatic_recovery_exhausted=true` describes this worker's cycle, not business completion or a
human gate. The supervisor owns bounded worker restarts and the persistent continuation below;
follow its final result rather than treating a worker's manual recommendation as the caller's task end.
For submit, follow-up, and collect modes, exit code 0 requires all requested images to be downloaded,
including every selected variant. Empty, partial, and failed results exit nonzero. An incomplete
result after the outer recovery sets `automatic_recovery_exhausted=true`; stdout and session both
disable blind caller resubmission while preserving the supervisor's continuation. Health checks
and upload-only preparation have their own success criteria and do not require generated images.
Standalone `health-check` never sends a prompt or modifies the saved request session. Unavailable or
ambiguous original-conversation identity never authorizes a replacement submission.
For exhausted unsubmitted preparation, stdout and session both retain the consumed recovery cycle
and `recommended_next_action=repair_environment_then_resume_unsubmitted_job`, with `retryable=false`.
The supervisor may replay confirmed-unsubmitted preparation within its bounded budget. Once
supervision is exhausted, use its checkpoint rather than a new job or reset retry loop.

The independent supervisor writes `chatgpt_recovery.json` when ordinary failures exhaust one run:

- `recovery_pending`: unfinished business (`business_complete=false`, `requires_user_action=false`).
  `continuation.argv` is the exact same task's `--resume-recovery` invocation and `not_before` is
  its cooldown deadline. The caller executes it automatically without another approval. Each run
  keeps its own 300-second active budget; at most two continuation runs are admitted. Run counters
  are saved before launch and ordinary reinvocation cannot reset them. Response Retry counts are
  separately persisted before clicking and never reset by a worker/run restart.
- `recovery_blocked`: the retry cap is spent, not a human-verification classification. Keep the
  task, original target, and logs. `continuation.reconcile_argv` is a bounded `collect-current
  --reconcile-only` invocation that cannot Send or click response Retry. Use it when new service
  or original-output evidence justifies collection, rather than looping on unchanged failure.
- `needs_ops`: a real human/input/runtime permission obstacle. A visible verification gate at the
  run deadline also retains this handoff instead of scheduling another automatic retry.

A checkpoint does not itself schedule a wakeup or guarantee that the upstream service will recover.
Do not claim business completion until the requested original outputs are downloaded and checked.

Repeated pre-submit recovery creates a fresh background target on the same binding before closing
the old owned target. Committed/unknown submissions and dispatch evidence block that reset.
Upload observations include `pending_controls` and `attachment_details` to distinguish an
identified pending reference card from an unscoped busy control. All visible pending controls
continue to block submission; the diagnostic fields do not relax upload or ordering checks.

## Whole-run health and recovery

The acceptance scope is ordinary runtime failures: network interruption, page loading errors,
missing/stale controls, stalled generation, failed transfers, worker exceptions, and blocked command
or result pipes. Each active task must converge to a checked result or an explicit saved failure.
Unclassified worker failures share the bounded restart budget; an unexpected supervision error
stops the owned worker and records `unexpected_supervisor_error`. A zero exit code with an invalid
result is not completion. Login, verification, and account restrictions retain their human handoff.

| Ordinary failure | Fallback |
| --- | --- |
| Page error while waiting for a composer/history | Interrupt the wait and run bounded page recovery. |
| Browser call, upload, download, or generation stops progressing | Apply operation retries and the independent heartbeat/progress deadline; restart the worker within budget. |
| Worker crashes or returns malformed/unexpected output | Preserve evidence, restart within budget, then return an explicit failure if unresolved. |
| Command/result pipe blocks | Forward outside the monitoring loop; supervision and its deadlines continue. Full worker output stays in attempt logs. |
| Pending reply is incorrectly reported as idle | Verify the saved pending state and `reply_ready` event before granting idle time. |
| Recovery repeats without success | Stop that bounded run; persist `recovery_pending` with executable continuation, or `recovery_blocked` with an original-result reconciliation path after the cap. |

`chatgpt_supervisor.json.result` retains the terminal result even when the caller is not reading
stdout; the status command can retrieve it after the process exits.
When a deadline/worker error races with artifact commit, reconcile the original manifest, prompt
hash, expected image count, task-owned output paths, stored hashes when present, and decoded PNGs
before reporting failure. A fully verified set returns exit 0 and `status=downloaded` with
`completion_source=verified_saved_artifacts`; takeover logs retain the preceding timeout and worker
exit code. `downloaded` text without valid files is insufficient. Partial/corrupt/mismatched files
remain unfinished and use the recovery checkpoint.

- Text and image workers use the same `chatgpt_page_health.py` classifiers, scoped page Retry,
  and recovery policy. A readable loading-error page sets `page.crashReason` and interrupts
  conversation waits even when `consecutive_page_failures=0`; it is not a verification gate.
  The supervisor consumes this same classification, with a 30-second phase grace before takeover.
- `chatgpt_heartbeat.json` is the latest atomic snapshot; `chatgpt_heartbeat.jsonl` is the event
  journal. Both cover browser startup, throttle waits, mode selection, upload, prompt insertion,
  submission, generation, download, manual verification, and terminal outcomes.
- `--mode status` reads the heartbeat and session without controlling Chrome. It reports stopped
  processes and stale observations separately from completed or paused tasks, and includes
  `chatgpt_supervisor.json` as `supervisor`.
- `chatgpt_supervisor.json` records the parent PID, restart count, log directory, and takeover
  actions. Final results include `supervisor_path` and `worker_restarts`. Worker logs live under
  `supervisor/<run>/` and survive later invocations. Defaults are a 60-second heartbeat deadline,
  180-second progress stall, two restarts, and a 300-second active run budget. Process termination
  never kills Chrome or replays an uncertain Send. Ordinary exhaustion returns `recovery_pending`
  or `recovery_blocked`, the reason, session/supervisor paths, and `last_worker_report`;
  concrete human/input gates retain `needs_ops`. The deadline includes retries and backoff.
  The five-minute default targets ordinary requests: roughly one minute of preparation, two of
  generation, and two of recovery as planning allowances under a single deadline. It is an
  operating policy, not a measured service guarantee. Explicit long/multi-image jobs can increase
  `--task-timeout` and `--timeout`; a rare slow success may otherwise be cut off by this policy.
- `--mode health-check` uses a separate tab and returns `health_checked` after two healthy account
  checks five seconds apart, without uploads or submission. It clears only this job's verification
  pause and preserves its session, including unknown submission state. Remaining pause owners or
  service-failure pauses return `queue_paused`; an unusable account returns `needs_ops`.
  Confirmed page errors receive at most `--page-recovery-attempts` retries (three by default) in
  that separate health tab, within the 30-second check window. An observed human gate stops
  recovery without clicks or reloads. Results include `page_observation` and `page_recoveries`;
  exhausted page recovery returns `needs_ops` with `reason=health_check_page_recovery_exhausted`.
- Sessions persist `dispatch_started`, `dispatch_started_at`, `owned_target_id`, `prompt_sha256`,
  and `canonical_conversation_url` as evidence becomes available. Send is attempted once; unknown
  outcomes require original-conversation inspection. A retained temporary target can be observed
  in place, but a temporary URL alone is never reopened for recovery. With no saved URL, the
  retained target also requires the submitted prompt in a user turn beyond the saved baseline.
  Unfinished submissions without a durable URL retain their tab and browser; fresh jobs and health
  checks open separate targets so they cannot overwrite that recovery evidence.
- `waiting_for_human` continues observation within both its 600-second verification limit and the
  remaining total task budget (300 seconds by default). Two healthy checks five seconds
  apart resume the same task. `needs_ops` means observation ended and human action/recovery remains.
  Resuming a saved verification first enters `checking_verification`; only a fresh challenge or
  login signal requests human attention while Chrome remains in the background. Checks during `readyState=loading`
  do not count as healthy. Two completed healthy checks clear only this job's verification owner
  before checking original-request identity, so a lost request cannot keep an otherwise healthy
  account paused under that owner. Other verification owners and service-failure pauses remain.
  `verification_observation` records the gate and its frame/text-marker evidence;
  `verification_resolution` records the completed healthy check. Unconfirmed page health stays
  paused and is distinguished from a currently observed challenge. Original-conversation handoffs
  persist `needs_ops` to the session as well as stdout.
- `queue_paused` blocks fresh submissions, with `queue_health` describing the reason and next check.
  Two distinct exhausted pre-submit jobs within 600 seconds pause the lane; a cooldown of 300 seconds
  permits one health probe. Already-submitted collection is permitted while the queue is paused.
- `upload_refs/mapping.json` maps each original reference to its byte-identical upload alias and full
  SHA-256. Upload observations keep original order and separately record `effective_upload_paths`
  and `upload_aliases`; filename normalization never replaces attachment-count/order verification.
