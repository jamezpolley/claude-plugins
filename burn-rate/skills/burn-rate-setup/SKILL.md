---
name: burn-rate-setup
description: Wire the burn-rate quota strip into the Claude Code statusline. Use when setting up burn-rate on a fresh machine, when the statusline isn't showing the quota strip, or after moving to a new statusline. The data collection and /burn-rate report work automatically on install; this only sets up the statusline display.
---

# Burn Rate — Statusline Setup

The burn-rate plugin is **mostly portable on its own**:

- The **Stop hook** (`hooks/log-usage.py`) registers automatically on install and
  snapshots usage to `~/.claude/state/usage-log.sqlite`.
- The **`/burn-rate` report** runs from `${CLAUDE_PLUGIN_ROOT}` — no setup needed.

The one piece that needs wiring is the **statusline strip** (the `5h:…│7d:…│duty:…`
segment). The statusline command is the *user's own* file (`settings.json` →
`statusLine.command`), so a plugin can't install it for you. This skill does that.

## Process

### Step 1: Dry run (see the plan, change nothing)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate-setup/setup-statusline.py
```

It detects one of three situations and prints exactly what it would do:

- **No statusline configured** → would create `~/.claude/statusline-command.sh`
  rendering the strip, and point `settings.json` at it.
- **Statusline is a script file** → if it already renders the strip (our markers
  or a `render-rates.py` call), reports "already wired"; otherwise shows the block
  it would append.
- **Statusline is an inline command** → can't splice safely; prints the snippet to
  add by hand.

### Step 2: Apply

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate-setup/setup-statusline.py --apply
```

Every modified file is backed up to `<file>.burnrate-bak` first. Idempotent —
re-running is a no-op once wired.

### Step 3: Reload

Restart Claude Code (or reload the statusline) to see the strip.

## Notes

- The injected block is **marketplace-agnostic** — it matches any
  `burn-rate@<marketplace>` install path, so it survives version bumps and
  re-installs.
- It uses `jq` to read the install path (same as the reference statusline). If
  `jq` is missing the strip silently no-ops; the rest of the statusline is
  unaffected. Install `jq` to enable it.
- When appending to an **existing** statusline script, the block pipes the
  statusline stdin JSON via a variable named `input`. If your script stores stdin
  under a different name, edit the appended block's `"$input"` to match.
