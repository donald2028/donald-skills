---
name: donald-visual-skill-matrix
description: Compare multiple explicitly selected visual-design skills by running one brief in separate fresh worker contexts and presenting their image or webpage previews side by side. Use when, and only when, the user explicitly requests a skill-to-skill comparison or directly names this skill. Do not use for an ordinary logo, illustration, comic, card, UI, or web-design request.
---

# Visual Skill Matrix

Run each candidate skill as an independent visual-design attempt. Preserve the candidate's original
artifacts and build a neutral gallery for human inspection. Never score, rank, recommend, merge, or
rewrite the candidates' work. Attribute every result to its dispatched entry skill and disclose any
supporting skills observed during execution.

## Invocation Guard

Run this workflow only when the user explicitly asks to compare or matrix-test at least two skills,
or directly invokes `$donald-visual-skill-matrix`. Do not infer comparison intent from an ordinary
visual-design request, the presence of several installed design skills, or uncertainty about which
skill to choose.

Use `python3` for bundled scripts on macOS/Linux. On Windows use `python` (or `py -3`). Resolve
`SKILL_DIR` to the directory containing this `SKILL.md`.

## Non-Negotiable Isolation

Treat every candidate as a separate matrix cell and fail closed if the runtime cannot satisfy all
of these requirements:

- Create a brand-new worker, agent, task, or session for every cell. Set inherited conversation
  history to zero or `none`; never fork or reuse a worker that has seen another candidate.
- Give the worker only its frozen brief, copied inputs, exact target skill, cell paths, and the
  isolation contract. Do not send the candidate list, another candidate's name, prior output, or
  the coordinator's design analysis.
- Invoke exactly one staged target as the entry skill. If that entry skill invokes supporting
  skills, allow the cell to finish but record every observed skill and the evidence source. Never
  give it another candidate cell's artifacts or ask it to combine candidate results.
- Keep each worker in its own cell workspace and output directory. Do not let a worker read a
  sibling cell. Start a new browser page, generation job, or external creative conversation when a
  candidate uses an external service; login state may be reused, creative conversation state may
  not.
- End the worker after one cell. Never send follow-up work for a second candidate to it.

Prompt instructions alone are not an isolation mechanism. Use the runtime's native fresh-worker
control. If it cannot create a worker without inherited turns, stop with `isolation_unavailable`;
do not run candidates sequentially in the coordinator context.

## Prepare A Run

Freeze the user's visual brief in a UTF-8 text or Markdown file. Resolve an exact directory for
every candidate; that directory must contain one `SKILL.md` and no nested skill. Then prepare the
run:

```bash
python3 "$SKILL_DIR/scripts/matrix_run.py" prepare \
  --brief-file "<brief.md>" \
  --candidate "<absolute-path-to-skill-a>" \
  --candidate "<absolute-path-to-skill-b>" \
  --input "<optional-reference-file-or-directory>"
```

Use `--output-root "<exact-root>"` only when the user specifies a destination. Otherwise the
script uses the shared Donald Skills output configuration or the system Documents default. It
prints the absolute run root and one cell descriptor per candidate.

Preparation copies the brief, inputs, and one candidate skill into every cell. It rejects unsafe
names, symlinks, duplicate candidates, and candidate trees containing another `SKILL.md`. Do not
replace these copies with shared paths. The cell manifest records the entry skill's name, staged
path, and full-tree SHA-256 fingerprint. This proves what was dispatched and whether its staged
copy changed; it does not by itself prove which instructions the worker actually followed.

## Dispatch The Cells

For every prepared cell:

1. Start a fresh worker with no inherited turns. Run cells concurrently only within the runtime's
   worker limit; use additional waves without reusing workers.
2. Send only that cell's `worker-prompt.md`. The prompt points to the copied brief, inputs, staged
   skill, workspace, and output directory.
3. Require a visual preview. Keep original images unchanged. For an HTML or web result, keep the
   project and capture a representative screenshot with an ordinary browser or screenshot tool,
   not another design skill. For a multi-image result, preserve all pages and select one or more
   representative preview images.
4. Inspect the strongest available evidence for skill usage. Prefer a runtime trace, then a
   coordinator observation, then the worker's own report. Keep the result when supporting skills
   were used, but label it as `mixed` and list them in the gallery.
5. Record the cell using its unique runtime worker ID:

```bash
python3 "$SKILL_DIR/scripts/matrix_run.py" record \
  --cell-root "<cell-root>" \
  --worker-id "<unique-worker-id>" \
  --status completed \
  --observed-skill "<target-skill-name>" \
  --observed-skill "<optional-supporting-skill>" \
  --provenance-source runtime_trace \
  --preview "outputs/<preview.png>" \
  --artifact "outputs/<original-file-or-project>"
```

Use `failed`, `needs_input`, or `needs_preview` when the isolated worker cannot complete. Those
statuses may omit `--preview`. Use repeated `--observed-skill` arguments for the complete observed
chain. Set `--provenance-source` to `runtime_trace`, `coordinator_observation`, `worker_report`, or
`unknown`; never present a self-report as runtime-verified provenance.

Never infer skill usage from the visual style, filename, image metadata, or generated artifact.
Treat a runtime trace that identifies the loaded `SKILL.md` or native skill invocation as the
strongest execution evidence. If the runtime exposes no such trace, record the coordinator's direct
observation or the worker's self-report and show that weaker source in the gallery.

## Verify And Present

Normally build the gallery after all cells finish; it can also render an in-progress run:

```bash
python3 "$SKILL_DIR/scripts/matrix_run.py" verify --run-root "<run-root>"
python3 "$SKILL_DIR/scripts/matrix_run.py" gallery --run-root "<run-root>"
```

Verification checks for one unique worker ID per cell, a fresh-context declaration, an unchanged
brief and staged entry skill, and result paths contained by the cell's `outputs/` directory. It
reports reused workers, altered inputs, missing provenance, and mixed skill usage, but it does not
block the gallery merely because a cell is mixed, pending, or incomplete.

The gallery shows each entry skill, every observed supporting skill, provenance source, context
isolation status, execution status, visual previews, and links to original artifacts. It visibly
marks `target_only`, `mixed`, `target_unobserved`, and `unverified` cells without hiding them. Return
the absolute `gallery/index.html` path and the run root. Do not add commentary about quality; the
user performs the comparison.
