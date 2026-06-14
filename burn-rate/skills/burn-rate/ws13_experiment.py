#!/usr/bin/env python3
"""WS13: Duty-weighting soundness comparison — Control C vs Model A vs Model B.

Runs three projection models against live DB data and synthetic scenarios
to determine which produces a principled duty floor:
  - Lower when remaining time is skewed toward idle (weekend ahead)
  - Higher when remaining time is skewed toward active (workday ahead)

Control C = span-rebuild code (this repo)
Model A    = per-active-hour eff_rate (wt-duty-A worktree)
Model B    = wall-rate × all-cell-normalised weights (wt-duty-B worktree)
"""

import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: import projection.py from three locations
# ---------------------------------------------------------------------------

DB_PATH = Path.home() / ".claude" / "state" / "usage-log.sqlite"

SCRIPT_DIR = Path(__file__).parent.resolve()

# Control C: this repo (span-rebuild)
CONTROL_DIR = str(SCRIPT_DIR)

# Model A worktree
WT_A_DIR = str(Path("/home/james/git/claude-plugins-wt-duty-A/burn-rate/skills/burn-rate"))

# Model B worktree
WT_B_DIR = str(Path("/home/james/git/claude-plugins-wt-duty-B/burn-rate/skills/burn-rate"))


def import_projection(path: str, alias: str):
    """Import projection module from a given directory path, return it."""
    import importlib.util
    import types

    # Remove any previously imported 'projection'/'spans' from sys.modules to avoid caching
    for key in list(sys.modules.keys()):
        if key in ('projection', 'spans') or key.startswith(f'projection_{alias}') or key.startswith(f'spans_{alias}'):
            del sys.modules[key]

    # Temporarily modify sys.path so spans.py is also importable from same dir
    orig_path = sys.path.copy()
    sys.path.insert(0, path)

    try:
        # First import spans from this dir (needed by projection.py)
        spans_path = os.path.join(path, "spans.py")
        spans_alias = f"spans_{alias}"
        if os.path.exists(spans_path):
            spans_spec = importlib.util.spec_from_file_location(spans_alias, spans_path)
            spans_mod = importlib.util.module_from_spec(spans_spec)
            # Register as 'spans' so projection.py's `from spans import ...` finds it
            sys.modules['spans'] = spans_mod
            sys.modules[spans_alias] = spans_mod
            spans_spec.loader.exec_module(spans_mod)

        proj_alias = f"projection_{alias}"
        spec = importlib.util.spec_from_file_location(
            proj_alias,
            os.path.join(path, "projection.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[proj_alias] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path = orig_path

    # Clean up the 'spans' alias so next import_projection gets a fresh one
    sys.modules.pop('spans', None)

    return mod


def open_db_ro() -> sqlite3.Connection:
    """Open the usage DB read-only."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_projection(proj_mod, conn, bucket, now, mode='predictive'):
    """Run projection from a module with the given now override."""
    return proj_mod.project(conn, bucket, now=now, mode=mode)


def format_result(r) -> str:
    """Format key fields from a ProjectionResult."""
    return (
        f"rtc={r.projected_pct_at_reset:.1f}%  duty={r.duty_projected_pct:.1f}%  "
        f"eff_rate={r.effective_rate_pp_h:.4f} pp/h  "
        f"active_remaining=? (see notes)"
    )


def get_active_remaining(r) -> str:
    """Extract active_remaining note if present from notes."""
    for note in r.notes:
        if 'weighted active capacity' in note or 'active_remaining' in note.lower():
            return note
    return '(not in notes)'


def main():
    print("=" * 78)
    print("WS13 Duty-Weighting Soundness Experiment")
    print("=" * 78)
    print(f"DB: {DB_PATH}")
    print()

    # Import all three versions
    print("Importing modules...")
    try:
        proj_C = import_projection(CONTROL_DIR, "C")
        print(f"  Control C: {CONTROL_DIR}")
    except Exception as e:
        print(f"  ERROR loading Control C: {e}")
        sys.exit(1)

    try:
        proj_A = import_projection(WT_A_DIR, "A")
        print(f"  Model A:   {WT_A_DIR}")
    except Exception as e:
        print(f"  ERROR loading Model A: {e}")
        sys.exit(1)

    try:
        proj_B = import_projection(WT_B_DIR, "B")
        print(f"  Model B:   {WT_B_DIR}")
    except Exception as e:
        print(f"  ERROR loading Model B: {e}")
        sys.exit(1)

    conn = open_db_ro()
    bucket = 'seven_day'

    # ---------------------------------------------------------------------------
    # Part 1: Live data (now = real now)
    # ---------------------------------------------------------------------------
    print()
    print("=" * 78)
    print("PART 1: Live data projection (now = real clock)")
    print("=" * 78)
    now_real = datetime.now(timezone.utc)
    print(f"now = {now_real.isoformat()}")
    print()

    for label, mod in [("Control C", proj_C), ("Model A", proj_A), ("Model B", proj_B)]:
        r = run_projection(mod, conn, bucket, now_real)
        print(f"  [{label}]")
        print(f"    current={r.current_pct:.1f}%  elapsed={r.elapsed_h:.1f}h  h_to_reset={r.h_to_reset:.1f}h")
        print(f"    rtc={r.projected_pct_at_reset:.1f}%  duty={r.duty_projected_pct:.1f}%  eff_rate={r.effective_rate_pp_h:.4f} pp/h")
        for note in r.notes:
            if any(kw in note for kw in ['[Model', 'Duty surface', 'active capacity', 'Shrinkage', 'shrinkage']):
                print(f"      • {note}")
        print()

    # ---------------------------------------------------------------------------
    # Part 2: Scenario simulation
    # Construct two synthetic `now` times against a fixed reset_dt.
    # ---------------------------------------------------------------------------
    print("=" * 78)
    print("PART 2: Scenario simulation (synthetic now, fixed reset_dt)")
    print("=" * 78)
    print()
    print("Fixed reset_dt = next Saturday 00:00 UTC")
    print("Scenario ACTIVE: now = Wednesday 10:00 UTC (3+ workdays ahead → many active hours)")
    print("Scenario IDLE:   now = Friday   22:00 UTC (weekend ahead → mostly idle hours)")
    print()

    # Find the next Saturday 00:00 UTC from a reference point
    # Use a deterministic future Saturday: 2026-06-20 00:00 UTC
    reset_dt_fixed = datetime(2026, 6, 20, 0, 0, 0, tzinfo=timezone.utc)

    scenario_active_now = datetime(2026, 6, 17, 10, 0, 0, tzinfo=timezone.utc)  # Wed Jun 17 10:00 UTC
    scenario_idle_now   = datetime(2026, 6, 19, 22, 0, 0, tzinfo=timezone.utc)  # Fri Jun 19 22:00 UTC

    print(f"  reset_dt_fixed      = {reset_dt_fixed.isoformat()}")
    print(f"  scenario_active_now = {scenario_active_now.isoformat()}  "
          f"({scenario_active_now.strftime('%A')}, {(reset_dt_fixed - scenario_active_now).total_seconds()/3600:.1f}h to reset)")
    print(f"  scenario_idle_now   = {scenario_idle_now.isoformat()}  "
          f"({scenario_idle_now.strftime('%A')}, {(reset_dt_fixed - scenario_idle_now).total_seconds()/3600:.1f}h to reset)")
    print()

    # For the duty surface comparison we call duty_surface() directly from each module
    # so we can see active_hours_remaining without running the full project() pipeline
    # (which requires current snapshot data).
    print("─" * 78)
    print("Duty surface active_hours_remaining (direct call to duty_surface())")
    print("─" * 78)
    print()

    rows = []
    for scenario_label, s_now in [
        ("ACTIVE (Wed 10:00)", scenario_active_now),
        ("IDLE   (Fri 22:00)", scenario_idle_now),
    ]:
        row = {"scenario": scenario_label}
        for model_label, mod in [("Control C", proj_C), ("Model A", proj_A), ("Model B", proj_B)]:
            try:
                # Model A duty surface is unchanged (same as control C)
                # Model B duty surface uses allcell normalisation when called via project()
                # For direct comparison, call duty_surface with appropriate params:
                if model_label == "Model B":
                    # Model B uses normalise_mode='allcell'
                    ds = mod.duty_surface(conn, bucket, now=s_now, reset_dt=reset_dt_fixed,
                                          normalise_mode='allcell')
                else:
                    ds = mod.duty_surface(conn, bucket, now=s_now, reset_dt=reset_dt_fixed)
                row[model_label] = ds.active_hours_remaining
                row[f"{model_label}_method"] = ds.method
            except Exception as e:
                row[model_label] = float('nan')
                row[f"{model_label}_method"] = f"ERROR: {e}"
        rows.append(row)

    # Print table
    header = f"  {'Scenario':<22}  {'Control C':>10}  {'Model A':>10}  {'Model B':>10}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for row in rows:
        c = row.get("Control C", float('nan'))
        a = row.get("Model A", float('nan'))
        b = row.get("Model B", float('nan'))
        print(f"  {row['scenario']:<22}  {c:>10.2f}h  {a:>10.2f}h  {b:>10.2f}h")
    print()

    # Method info
    for row in rows:
        print(f"  [{row['scenario']}]")
        for model_label in ["Control C", "Model A", "Model B"]:
            print(f"    {model_label}: method={row.get(f'{model_label}_method', '?')}")
    print()

    # Also run full project() for each scenario (using real DB snapshot for current_pct)
    print("─" * 78)
    print("Full project() output per scenario (uses real current_pct from DB)")
    print("─" * 78)
    print()
    print("Note: project() uses real current_pct but synthetic now.")
    print("The ratio duty/rtc tells us how much the duty surface bends the projection.")
    print()

    for scenario_label, s_now in [
        ("ACTIVE (Wed 10:00 UTC)", scenario_active_now),
        ("IDLE   (Fri 22:00 UTC)", scenario_idle_now),
    ]:
        print(f"  Scenario: {scenario_label}")
        print(f"  {'Model':<12}  {'rtc':>7}  {'duty':>7}  {'duty/rtc':>8}  {'eff_rate':>9}  notes")
        print(f"  {'─'*12}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*9}  {'─'*30}")
        for model_label, mod in [("Control C", proj_C), ("Model A", proj_A), ("Model B", proj_B)]:
            try:
                r = run_projection(mod, conn, bucket, s_now)
                ratio = r.duty_projected_pct / r.projected_pct_at_reset if r.projected_pct_at_reset > 0 else 0
                model_notes = [n for n in r.notes if '[Model' in n]
                note_str = '; '.join(model_notes) if model_notes else ''
                print(f"  {model_label:<12}  {r.projected_pct_at_reset:>6.1f}%  {r.duty_projected_pct:>6.1f}%  {ratio:>7.3f}    {r.effective_rate_pp_h:>8.4f}  {note_str[:50]}")
            except Exception as e:
                print(f"  {model_label:<12}  ERROR: {e}")
        print()

    # ---------------------------------------------------------------------------
    # Part 3: Summary analysis
    # ---------------------------------------------------------------------------
    print("=" * 78)
    print("PART 3: Summary — which model is most PRINCIPLED?")
    print("=" * 78)
    print()
    print("A PRINCIPLED duty floor should satisfy:")
    print("  (1) LOWER  when remaining time is skewed toward IDLE (weekend ahead)")
    print("  (2) HIGHER when remaining time is skewed toward ACTIVE (workday ahead)")
    print("  (3) duty < rtc always (never exceed the round-the-clock projection)")
    print()

    # Compute the deltas: IDLE - ACTIVE (negative = principled direction)
    row_active = rows[0]
    row_idle   = rows[1]

    for model_label in ["Control C", "Model A", "Model B"]:
        active_val = row_active.get(model_label, float('nan'))
        idle_val   = row_idle.get(model_label, float('nan'))
        delta = idle_val - active_val
        direction = "✓ principled (idle < active)" if delta < 0 else "✗ WRONG direction (idle >= active)"
        ratio_active = active_val / (reset_dt_fixed - scenario_active_now).total_seconds() * 3600
        ratio_idle   = idle_val   / (reset_dt_fixed - scenario_idle_now).total_seconds()   * 3600
        print(f"  {model_label}:")
        print(f"    active_remaining ACTIVE scenario: {active_val:.2f}h")
        print(f"    active_remaining IDLE   scenario: {idle_val:.2f}h")
        print(f"    IDLE - ACTIVE delta: {delta:+.2f}h  →  {direction}")
        print(f"    active/wall fraction: ACTIVE={ratio_active:.2%}  IDLE={ratio_idle:.2%}")
        print()

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
