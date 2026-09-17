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
`order_verification_method`. It checks visible numbered attachment labels when available and always
requires sequential file input with a monotonic attachment count; the runner refuses submission
when this evidence does not match the manifest order.
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
  attempts the verification itself.

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
