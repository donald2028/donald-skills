---
name: donald-config-browser
description: "Perform first-time setup or repair for Donald browser skills: install and verify agent-browser, select a local Chrome Profile, persist a per-skill binding, initialize shared per-Profile Chrome user-data-dirs, and prove headed Chrome control over CDP. Use when no binding exists, the user asks to inspect, change, or reset a binding, or a browser runner reports a configuration or environment failure; do not use as a gate before every routine browser task."
---

# Configure Agent Browser Profile

Prepare each browser skill independently once, then reuse its saved binding. A successful setup
means the skill has its own Profile binding and a real headed Chrome launched from that Profile's
persistent `user-data-dir` has passed an `agent-browser --cdp` control check. The binding remains
valid for later tasks until the user changes it or a runner reports a configuration failure.

Other Donald browser workflows invoke this skill only for first-time setup or repair. When invoked
that way, use the caller's skill name as `--scope`. Do not duplicate the caller's business workflow
or import files from its skill directory.

Keep bindings separate from browser state:

- Each skill has an independent config and can select or reselect its own Chrome Profile.
- The CDP `user-data-dir` belongs to the Chrome Profile, not the skill. Skills selecting the same
  Profile reuse the same directory, CDP port, cookies, and login state.
- Profiles that differ use different runtime directories. Do not make a second copy merely because
  another skill selected the same Profile.

## Storage

- Config JSON directory: `~/Library/Application Support/Donald Skills/config/agent-browser/` on
  macOS, `%LOCALAPPDATA%\Donald Skills\config\agent-browser\` on Windows, and
  `${XDG_CONFIG_HOME:-~/.config}/donald-skills/agent-browser/` on Linux. It contains one JSON file
  per configured skill. Set `DONALD_AGENT_BROWSER_CONFIG_DIR` only when an explicit config-root
  override is required.
- Runtime browser data: `~/Library/Application Support/Donald Skills/Chrome CDP/` on macOS,
  `%LOCALAPPDATA%\Donald Skills\Chrome CDP\` on Windows, and
  `${XDG_DATA_HOME:-~/.local/share}/donald-skills/chrome-cdp/` on Linux, with one directory per
  Chrome Profile.

The JSON contains paths and Profile metadata, not passwords or tokens. The runtime browser data is
sensitive because it contains copied login state. Never commit, upload, or inspect it unnecessarily.

## Invocation Policy

Routine browser tasks take the fast path: the caller runs its bundled business runner directly.
That runner reads the saved binding and owns the live Chrome/CDP/agent-browser startup check. Do
not invoke this skill, enumerate Profiles, or run a separate preflight merely because a new task
started.

When this skill is invoked, resolve `SKILL_DIR` to the directory containing this `SKILL.md`, resolve
the caller's scope, and inspect the saved binding first:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> show
```

- If it reports `ready` and the user requested at most an inspection, report the saved binding when
  requested and return control immediately. Do not run `environment`, `profiles`, `check`, or
  `preflight`.
- If the user requested a reset, run `reset` directly. Do not check the environment or list Profiles.
- If no binding exists, perform Phases 1–3.
- If the user explicitly requests a Profile change, skip the cached binding and perform Phases 1–3.
- If a runner reported a configuration or environment failure, use `check` to diagnose it, then run
  only the repair steps indicated by the result. Run `preflight` once after a repair.

A saved binding is sticky. Never ask the user to select a Profile again solely because this is a
new task or session.

## Phase 1: Check The Environment For Setup Or Repair

When this skill is used standalone and the target is unknown, list the independently configurable
targets:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" targets
```

When a caller supplied its exact scope, do not run `targets` or ask for a target. For first-time
setup or repair, check the common environment:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" environment
```

If `agent-browser` is missing, the command installs it automatically using npm (all platforms) or
Homebrew (macOS fallback), then runs its official browser setup. It never invokes `sudo`. If neither
installer exists or global installation needs administrator action, report `needs_ops` with the
returned error.

This phase also requires Google Chrome and its normal User Data directory. Do not continue with
setup or repair until it reports `ready`.

## Phase 2: Choose A Profile Only When Needed

If no caller supplied a scope, ask which target to initialize or change. Do not ask again when the
caller already supplied its exact scope. Existing browser workflows use these scope values:

- `donald-chatgpt-imagegen`
- `donald-collect-wechat`
- `donald-collect-x`

A future Donald browser skill may use its own lowercase kebab-case `donald-*` skill name as the
scope without changing this script. The first saved binding makes it appear in `targets`. Use the
exact caller skill name so its runner reads the same config file.

List usable Profiles without changing config only for those cases:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" \
  --scope <skill-name> \
  profiles
```

Present each `directory`, `name`, and login `email` to the user. Show `not available` when Chrome's
local Profile metadata has no email. **Do not initialize or write config until the user explicitly
confirms one.** On Windows, ask the user to close Chrome before the first Profile copy so locked
files do not omit login state.

Do not label a Profile as recommended based on its name, email, directory, list order, or prior
guess. A recommendation is allowed only after that exact Profile's login state for the caller's
service has been verified in a real browser session. For `donald-chatgpt-imagegen`, this means a
verified ChatGPT login. The `profiles` command does not verify service authentication, so present
its choices without a recommendation. If the interface requires one option to be marked
recommended, use a plain list instead and ask for the exact Profile directory.

After confirmation:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" \
  --scope <skill-name> \
  set --profile "<directory-or-unique-name>"
```

Initialization copies the selected Chrome Profile and `Local State` into a persistent, non-default
CDP User Data directory. It follows agent-browser's own Profile snapshot exclusions for caches and
other large non-auth data. Existing valid runtime data for that Profile is reused instead of
overwritten, including its shared CDP port. Only the selected skill's config binding is written.

To migrate a known dedicated CDP directory, pass `--user-data-dir <path>`. It must contain the
selected Profile directory and must not be Chrome's normal User Data root. If another target is
already bound to that Profile, initialization reuses its existing directory and port and rejects a
conflicting override.

## Phase 3: Prove Chrome Over CDP Once

After a new binding or repair, check files and selection, then perform one real headed preflight:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> show
python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> check

python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> preflight \
  --session donald-browser-preflight \
  --url about:blank
```

The preflight follows this exact sequence:

1. Start real Chrome with `--remote-debugging-address=127.0.0.1`,
   `--remote-debugging-port`, `--user-data-dir`, `--profile-directory`, and
   `--no-startup-window`. Do not add `--headless` unless the user explicitly requests it.
2. Wait for `http://127.0.0.1:<port>/json/version`.
3. Create the page through browser-level CDP with `Target.createTarget({background: true})`.
4. Run `agent-browser --session <name> --cdp <port> get url` to prove agent-browser control.
5. If preflight started Chrome, close that exact CDP browser and wait for its port to stop listening
   before returning `ready`. The downstream runner must start and own its own Chrome lifecycle.

On macOS, launch the headed Chrome instance with `open -g -j -n -a "Google Chrome"`: `-g`
keeps it in the background and `-j` keeps the application hidden. This remains headed Chrome, not
headless Chrome. Keep it hidden during normal automation because a visible Chrome window can
promote itself after delayed page work even when a CDP target was created with `background=true`.
Never activate Chrome or take keyboard focus. If login, MFA, captcha, risk confirmation,
or another anti-automation challenge requires a human, return `needs_ops`, run the controlled
activation command below, and keep that Chrome open:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> activate
```

`activate` verifies that the CDP listener belongs to the configured Profile before bringing only
that Chrome process to the foreground. Tell the user what needs attention and continue after they
confirm completion. Do not activate for ordinary background collection.

If the port belongs to a different or unverifiable Chrome process, fail instead of attaching to a
possibly wrong account. Downstream skills may proceed only when preflight reports `ready`.

## Reinitialize

Run `profiles`, ask the user again, and run `set` for only the target being changed. Other skill
bindings remain untouched. To remove only one target's selection and force setup on its next run:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" --scope <skill-name> reset
```

`reset` intentionally preserves runtime browser data. Do not delete a user's logged-in Profile data
without a separate explicit request.

Never bypass login, MFA, captcha, risk prompts, or account permissions. A first copied Profile may
still require the user to sign in interactively; after that, the persistent CDP directory is reused.
