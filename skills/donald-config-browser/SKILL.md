---
name: donald-config-browser
description: Install and verify agent-browser, list local Chrome Profiles with login emails, bind each Donald browser skill to its own selected Profile, initialize shared per-Profile Chrome user-data-dirs, and prove headed Chrome control through agent-browser over CDP. Use before Donald browser collection or image-generation skills, or to inspect, change, reset, or repair one skill's browser Profile selection.
---

# Configure Agent Browser Profile

Prepare each browser skill independently before collection or image-generation work. A successful
setup means the skill has its own Profile binding, real headed Chrome is running from that
Profile's persistent `user-data-dir`, and `agent-browser --cdp` can inspect a background CDP page.

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

## Phase 1: Check The Environment

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`. List the independently configurable
targets, then check the common environment:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" targets
python3 "$SKILL_DIR/scripts/profile_config.py" environment
```

If `agent-browser` is missing, the command installs it automatically using npm (all platforms) or
Homebrew (macOS fallback), then runs its official browser setup. It never invokes `sudo`. If neither
installer exists or global installation needs administrator action, report `needs_ops` with the
returned error.

This phase also requires Google Chrome and its normal User Data directory. Do not continue to a
browser task until it reports `ready`.

## Phase 2: Choose A Skill And Profile

Ask which target to initialize or change. Use exactly one of these scope values:

- `donald-chatgpt-imagegen`
- `donald-collect-wechat`
- `donald-collect-x`

List usable Profiles without changing config:

```bash
python3 "$SKILL_DIR/scripts/profile_config.py" \
  --scope <skill-name> \
  profiles
```

Present each `directory`, `name`, and login `email` to the user. Show `not available` when Chrome's
local Profile metadata has no email. **Do not initialize or write config until the user explicitly
confirms one.** On Windows, ask the user to close Chrome before the first Profile copy so locked
files do not omit login state.

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

## Phase 3: Prove Chrome Over CDP

Check files and selection, then perform a real headed preflight:

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
