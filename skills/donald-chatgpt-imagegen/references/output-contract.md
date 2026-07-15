# Output Contract

A prepared and executed job uses:

```text
<system Documents>/Donald Skills/Data/chatgpt-images/<job-name>/<UTC-timestamp>/
├── chatgpt-job.json
├── chatgpt_session.json                    # single-batch session
├── variant_XX_chatgpt_session.json         # independent-variant sessions
├── chatgpt_web_run_summary.json
├── chatgpt_progress.jsonl
├── agent_browser_trace*/                   # screenshots and reports
└── *.png                                   # generated candidates
```

`DONALD_SKILLS_OUTPUT_ROOT` replaces `<system Documents>/Donald Skills/Data`; command-level
`--output-root` replaces the ChatGPT image root itself and has higher precedence.

CDP locks, submit-throttle counters, and timing metrics are stored in the platform-native Donald
Skills application-state directory. They are not part of this user-facing output contract.

Session files record the conversation URL, reference mapping, attempts, resume state, and outputs.
The run summary records request mode, variant results, image paths, and status.

Important terminal or recoverable states include:

- `downloaded`: requested outputs were recognized and saved;
- `partial_downloaded`: fewer images were returned; collect the current conversation before a
  follow-up;
- `policy_refused`: revise the prompt rather than retrying unchanged;
- `timeout_no_images`: retain traces and session state for diagnosis or resume;
- login/challenge states: require operator action in the visible browser.
