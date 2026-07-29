---
name: donald-research-github
description: Resolve GitHub repository URLs, clone or safely refresh repositories in a configurable owner/repo research library, and inspect them for requested analysis. Use whenever the user supplies a GitHub repository URL or asks to clone, download, pull, inspect, study, or analyze a GitHub project.
---

# Research A GitHub Repository

Acquire GitHub repositories into a predictable research library without tying the workflow to one
person's filesystem. Keep each repository under `<research-root>/<owner>/<repo>` unless the user
explicitly provides an exact destination.

## Resolve The Destination

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`. If the current request names an
exact destination, use it without changing the shared output setting. If it names a research root,
pass that exact root as the one-operation override:

```bash
python3 "$SKILL_DIR/scripts/output_paths.py" resolve github-research --root "<research-root>"
```

Otherwise resolve the effective root without prompting:

```bash
python3 "$SKILL_DIR/scripts/output_paths.py" resolve github-research
```

The resolver chooses the root in this order:

1. A `--root` value from the current request.
2. `DONALD_GITHUB_RESEARCH_ROOT` when explicitly supplied as a process-level compatibility
   override.
3. `DONALD_SKILLS_OUTPUT_ROOT` as a shared process-level compatibility override.
4. `<saved shared output root>/github-research`.
5. `<system Documents>/Donald Skills/Data/github-research`.

Use the returned absolute `output_root` as the research root and report its `source`. Do not ask the
user to configure a root merely because no environment variable or saved config exists; the
Documents default is a complete normal configuration.

When the user asks to configure, inspect, change, or reset persistent output storage, explain that
the saved root is shared by all Donald output skills, then use:

```bash
python3 "$SKILL_DIR/scripts/output_paths.py" show
python3 "$SKILL_DIR/scripts/output_paths.py" set "<shared-output-root>"
python3 "$SKILL_DIR/scripts/output_paths.py" reset
```

`set` stores the absolute shared root in `storage.json` under the unified Donald Skills config
root. It does not edit `.zshrc`, another shell startup file, or an operating-system environment
setting. A GitHub-only custom root remains a one-operation `--root`; persistent customization is
shared by design.

Interpret an explicitly named repository destination as exact. Interpret a directory described as
a root or library as a root and append `<owner>/<repo>`. When that distinction is materially
ambiguous, show the proposed target before cloning and ask the user to confirm it. Expand every
user-provided path and use its resulting absolute path for Git commands.

## Parse The Repository

Accept common repository forms, including HTTPS URLs, `git@github.com:<owner>/<repo>.git`, and URLs
containing `/tree/`, `/blob/`, issues, pull requests, query strings, or fragments. Extract only the
first repository owner and repository name, strip a trailing `.git`, and reject missing or unsafe
path components such as `.` or `..`.

Use `https://github.com/<owner>/<repo>.git` for cloning unless the user explicitly requests SSH.
Before running a command, show the parsed owner, repository, clone URL, research root, and final
target.

## Clone Or Refresh

Create only the owner directory needed for the target. Do not change the caller's working
directory; use absolute paths and `git -C`.

- If the target does not exist, default to a current-state research checkout with
  `git clone --depth=1 --single-branch --no-tags -- <clone-url> <target>`. Fetch more history only
  when the request needs commit history, blame, revision comparison, or an older commit. Deepen a
  shallow clone incrementally with `git fetch --deepen=<n>`, or use `git fetch --unshallow` only
  when complete history is required.
- For an unusually large repository with a clearly known subdirectory scope, add
  `--filter=blob:none --sparse` and expand the sparse checkout only as analysis requires. Do not
  use sparse checkout when the relevant paths are not yet known.
- If the target is a Git repository, verify that its `origin` identifies the same GitHub
  `<owner>/<repo>`. Inspect `git status --short`, the current branch and upstream, and
  `git rev-parse --is-shallow-repository` before updating.
- If the existing repository is clean and tracks an upstream, fetch only that upstream branch
  into its remote-tracking ref with `--no-tags`, then merge the remote-tracking ref with
  `--ff-only`. Do not run a broad fetch when the exact upstream ref is known, and do not create a
  merge commit during refresh.
- Do not pass `--depth=1` while refreshing an existing checkout: truncating the fetched history at
  the new tip can hide the ancestry needed for a fast-forward. A shallow checkout should retain
  its original shallow boundary and download only commits added since the last refresh. A complete
  checkout should retain its existing history and likewise fetch only missing upstream objects.
- Do not shrink or replace an existing checkout in place. If the user explicitly wants to reclaim
  its historical storage, create and verify a separate shallow clone before asking for approval to
  replace the original.
- If it is dirty, detached, divergent, has no unambiguous upstream, or points at another origin,
  preserve it and report the condition before taking further action.
- If the target exists but is not a Git repository, do not overwrite or delete it.

Use argument separators where supported so an owner, repository, or path cannot be interpreted as
an option. Do not fetch credentials into files or place tokens in clone URLs.

## Inspect And Report

After acquisition, report whether the repository was cloned, refreshed, or left unchanged, plus
its absolute location, current branch, origin, latest commit, and whether it is shallow.

When the user asks for research or analysis, inspect the repository's own instructions first, then
review the README, top-level structure, dependency manifests, tests, and relevant source files.
Tailor the analysis to the user's question instead of producing a fixed generic report.
