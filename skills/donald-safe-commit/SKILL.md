---
name: donald-safe-commit
description: Safely review, stage, commit, and optionally push Git changes while preserving unrelated work and screening for secrets. Use when the user asks to commit, 提交, push, 推送, synchronize, or sync code in a Git repository.
---

# Safely Commit Git Changes

Create the smallest commit that matches the user's request. Preserve unrelated working-tree and
index changes, never expose secret contents, and push only when the user requests it.

## Inspect The Repository

Resolve the repository root, then inspect the current state before changing the index:

```bash
git rev-parse --show-toplevel
git status --short --branch
git diff --stat
git diff --cached --stat
git log -5 --oneline
```

Read the relevant unstaged and staged diffs. Identify which files belong to the requested commit.
Do not silently include unrelated changes or replace a user's existing staged selection. If the
requested scope cannot be separated safely, explain the overlap and ask before changing the index.

## Screen For Sensitive Content

Review candidate paths and diffs before staging. Treat these as high-risk:

- `.env` files other than clearly synthetic examples such as `.env.example`
- private keys, certificates with private material, credentials, service-account files, and tokens
- local configuration containing authentication or private endpoints
- generated files that embed environment variables or session data

Do not print detected secret values. Stop before committing a high-risk file, identify only its
path and risk category, and propose the narrowest safe action. Do not broadly rewrite `.gitignore`
or classify `plans/`, `specs/`, IDE settings, or other project files as disposable without evidence
from that repository.

## Stage And Verify

Stage explicit in-scope paths:

```bash
git add -- <path>...
git diff --cached --stat
git diff --cached
```

Use `git add -A` only when every working-tree change has been reviewed and belongs to the requested
commit. Re-run the sensitive-content screen against the final staged diff. If nothing is staged,
report that no commit was created.

## Commit

Match the repository's recent message style. Prefer a concise Conventional Commit message when the
history supports it:

```text
<type>(<optional-scope>): <imperative summary>
```

Use a body only when it explains non-obvious intent or consequences. Commit normally and allow
hooks and signing checks to run; do not bypass them unless the user explicitly requests that and
understands the consequence.

After a successful commit, report the short hash and subject, then inspect `git status --short`
again. If a hook changed files, leave those changes visible and explain whether they were included.

## Push Only When Requested

Inspect the current branch, upstream, and remotes before pushing. Use the existing upstream when it
is configured. If no upstream exists, set one only when the intended remote and branch are
unambiguous; otherwise ask the user.

Never force-push, delete a remote branch, rewrite history, or amend an existing commit unless the
user explicitly requests that operation. Report the pushed remote and branch after success.
