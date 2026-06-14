#!/usr/bin/env python3
"""Wire the burn-rate quota strip into the user's Claude Code statusline.

The burn-rate plugin's data collection (the Stop hook) and the `/burn-rate`
report are portable automatically — the plugin system registers the hook and
resolves `${CLAUDE_PLUGIN_ROOT}` on install. The one piece that is NOT portable
is the *statusline strip*: the statusline command is the user's own file
(settings.json -> statusLine.command), and a plugin cannot write it for them.
This script does that wiring, idempotently.

Three cases:
  1. No statusLine configured        -> create a minimal statusline script that
                                         renders the burn-rate strip, and point
                                         settings.json at it.
  2. statusLine -> a script file      -> if already wired (a render-rates.py call
                                         or our markers are present) report OK;
                                         otherwise append our block (with markers).
  3. statusLine -> an inline command  -> cannot safely splice; print the snippet
                                         and manual instructions.

Default is a DRY RUN (prints the plan, writes nothing). Pass --apply to write.
Every file we modify is backed up to `<file>.burnrate-bak` first.

The injected block is marketplace-agnostic (matches any `burn-rate@<marketplace>`)
and depends on `jq` to read the install path — the same approach the reference
statusline uses. If `jq` is unavailable the strip silently no-ops (the rest of
the statusline is unaffected).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

BEGIN = "# >>> burn-rate statusline (managed by burn-rate setup-statusline.py) >>>"
END = "# <<< burn-rate statusline <<<"

# Marketplace-agnostic install-path lookup + render. `$1` is the var name the
# host script stored the statusline stdin JSON in (default: input).
BLOCK_TEMPLATE = """{begin}
# Renders the Claude Code quota burn-rate strip. Safe to delete this whole block.
# Requires `jq`; no-ops quietly if jq or the plugin is missing.
__br_root=$(jq -r '[.plugins // {{}} | to_entries[] | select(.key|startswith("burn-rate@")) | .value[0].installPath] | map(select(.!=null)) | .[0] // empty' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null)
if [ -n "$__br_root" ] && [ -x "$__br_root/skills/burn-rate/render-rates.py" ]; then
  printf '%s' "${stdin_var}" | "$__br_root/skills/burn-rate/render-rates.py" 2>/dev/null
fi
{end}"""

CREATED_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Claude Code statusline — created by the burn-rate plugin's setup-statusline.py.
# Reads the statusline JSON from stdin and renders the burn-rate quota strip.
# Add your own segments around the block below as you like.
input=$(cat)

{block}
"""

DEFAULT_STATUSLINE_PATH = Path.home() / ".claude" / "statusline-command.sh"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def block(stdin_var: str = "input") -> str:
    return BLOCK_TEMPLATE.format(begin=BEGIN, end=END, stdin_var=stdin_var)


def already_wired(text: str) -> bool:
    """True if this statusline already renders the burn-rate strip (our markers,
    or a hand-rolled render-rates.py call like the reference statusline uses)."""
    return BEGIN in text or "render-rates.py" in text


def parse_statusline_command(settings: dict) -> tuple[str | None, Path | None]:
    """Return (raw_command, resolved_script_path_or_None)."""
    sl = settings.get("statusLine")
    if not isinstance(sl, dict):
        return None, None
    cmd = sl.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None, None
    # Find a script-file token in the command (e.g. `bash /path/to/foo.sh`).
    for tok in cmd.split():
        tok = tok.strip("'\"")
        p = Path(os.path.expandvars(os.path.expanduser(tok)))
        if p.is_file():
            return cmd, p
    return cmd, None


def backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".burnrate-bak")
    shutil.copy2(path, bak)
    return bak


def load_settings() -> dict:
    if SETTINGS_PATH.is_file():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            print(f"  ! {SETTINGS_PATH} is not valid JSON ({e}); cannot proceed safely.")
            sys.exit(2)
    return {}


def plugin_installed() -> bool:
    if not INSTALLED_PLUGINS.is_file():
        return False
    try:
        plugins = json.loads(INSTALLED_PLUGINS.read_text()).get("plugins", {})
    except json.JSONDecodeError:
        return False
    return any(k.startswith("burn-rate@") and v for k, v in plugins.items())


def main() -> int:
    ap = argparse.ArgumentParser(description="Wire the burn-rate strip into the statusline.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()
    dry = not args.apply
    tag = "[dry-run] " if dry else ""

    print("burn-rate statusline setup")
    print("=" * 40)
    if not plugin_installed():
        print("  ! burn-rate plugin not found in installed_plugins.json.")
        print("    Install it first:  claude plugin install burn-rate@<marketplace>")
        # Not fatal — wiring still works once it is installed — but warn loudly.

    settings = load_settings()
    raw_cmd, script_path = parse_statusline_command(settings)

    # --- Case 1: no statusLine at all -> create one + wire settings.json ---
    if raw_cmd is None:
        target = DEFAULT_STATUSLINE_PATH
        print(f"  No statusLine configured. Will create: {target}")
        print(f"  …and point {SETTINGS_PATH} -> statusLine at it.")
        if dry:
            print(f"\n{tag}Would write this statusline script:\n")
            print(CREATED_SCRIPT_TEMPLATE.format(block=block()))
            print(f"{tag}Re-run with --apply to write. Nothing changed.")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            print(f"  ! {target} already exists but settings.json doesn't reference it.")
            print("    Refusing to overwrite. Inspect it, or point statusLine.command at it manually.")
            return 2
        target.write_text(CREATED_SCRIPT_TEMPLATE.format(block=block()))
        target.chmod(0o755)
        settings.setdefault("statusLine", {})
        settings["statusLine"] = {"type": "command", "command": f"bash {target}"}
        if SETTINGS_PATH.exists():
            backup(SETTINGS_PATH)
        else:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"  ✓ Created {target} and wired settings.json.")
        print("  Restart Claude Code (or reload) to see the strip.")
        return 0

    # --- Case 3: inline command (not a script file) -> advise, don't splice ---
    if script_path is None:
        print(f"  statusLine.command is inline (no script file found in it):")
        print(f"    {raw_cmd}")
        print("  Can't safely splice into an inline command. Add this to your")
        print("  statusline, referencing the variable that holds the stdin JSON:\n")
        print(block(stdin_var="input"))
        print("\n  (Replace `input` with your script's stdin variable.)")
        return 0

    # --- Case 2: statusLine -> a script file ---
    print(f"  statusLine -> {script_path}")
    text = script_path.read_text()
    if already_wired(text):
        print("  ✓ Already renders the burn-rate strip (markers or render-rates.py present).")
        print("    Nothing to do.")
        return 0

    print("  Not wired yet. Will append the burn-rate block.")
    print("  ! Heads-up: the block pipes the statusline stdin JSON to render-rates.py")
    print("    via a variable named `input`. If your script stores stdin under a")
    print("    different name, edit the appended block's `\"$input\"` accordingly.")
    appended = text.rstrip("\n") + "\n\n" + block(stdin_var="input") + "\n"
    if dry:
        print(f"\n{tag}Would append:\n")
        print(block(stdin_var="input"))
        print(f"\n{tag}Re-run with --apply to write. Nothing changed.")
        return 0
    backup(script_path)
    script_path.write_text(appended)
    print(f"  ✓ Appended the burn-rate block to {script_path} (backup: {script_path.name}.burnrate-bak).")
    print("    Verify it renders, and reposition the block if your statusline needs it elsewhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
