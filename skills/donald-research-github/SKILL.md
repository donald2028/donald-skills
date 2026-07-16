---
name: donald-research-github
description: Resolve GitHub repository URLs, clone or safely refresh repositories in a configurable owner/repo research library, and inspect them for requested analysis. Use whenever the user supplies a GitHub repository URL or asks to clone, download, pull, inspect, study, or analyze a GitHub project.
---

# Research A GitHub Repository

Acquire GitHub repositories into a predictable research library without tying the workflow to one
person's filesystem. Keep each repository under `<research-root>/<owner>/<repo>` unless the user
explicitly provides an exact destination.

## Resolve The Destination

Choose the research root in this order:

1. Use the directory or exact destination stated in the current request.
2. Otherwise inspect `DONALD_GITHUB_RESEARCH_ROOT`. When it is set and non-empty, use it as the
   exact research root.
3. When it is unset or empty, stop before cloning and tell the user that no global GitHub research
   root is configured. Ask whether they want to configure one or skip configuration.
4. If the user skips, use `<system Documents>/Donald Skills/Data/github-research` for this
   operation.

When the user chooses to configure a global root, ask for the directory, expand it to an absolute
path, and show the exact value that will be assigned to `DONALD_GITHUB_RESEARCH_ROOT`. Explain that
changing only the current process is not persistent. Before editing a shell startup file or an
operating-system environment setting, show the exact target and change and obtain explicit
approval. Update only this variable and do not rewrite unrelated configuration.

Treat skipping as a one-operation choice: do not set the environment variable and do not create a
hidden marker file. Ask again on a later operation if the variable is still unconfigured.

Resolve the operating system's real Documents directory instead of assuming a particular home
path. On Linux, honor the configured XDG Documents directory; on Windows, honor the system Known
Folder location. Expand user-provided paths and environment variables, then use the resulting
absolute path for all commands. Never write a locally resolved path back into this skill.

Interpret an explicitly named repository destination as exact. Interpret a directory described as
a root or library as a root and append `<owner>/<repo>`. When that distinction is materially
ambiguous, show the proposed target before cloning and ask the user to confirm it.

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
