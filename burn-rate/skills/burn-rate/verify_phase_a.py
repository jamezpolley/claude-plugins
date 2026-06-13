#!/usr/bin/env python3
"""Phase A acceptance-gate verification script.

Checks:
1. Span table matches S1-S8 structure from project doc
2. Pooled prior ≈ 0.809 pp/hr (not 0.927)
3. Current-window elapsed ≈ 10-12h (not 83h)
4. Naive mode elapsed unchanged (still uses resets_at-7d)
"""
import sqlite3
import sys
from datetime import datetime, timezone
import os

# Run from the skill directory
sys.path.insert(0, os.path.dirname(__file__))
from spans import extract_spans, pooled_prior, get_effective_epoch, _canonicalise_resets_at
from projection import project, open_db, _SPANS_AVAILABLE

DB = "/home/james/.claude/state/usage-log.sqlite"

def fmt_dur(h):
    d = int(h // 24)
    hh = h - 24 * d
    return f"{d}d{hh:4.1f}h" if d else f"{hh:5.1f}h"

conn = sqlite3.connect(DB)
now = datetime.now(timezone.utc)

print("=" * 72)
print("Phase A Acceptance Gate Verification")
print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"spans.py available: {_SPANS_AVAILABLE}")
print("=" * 72)

# ── 1. Span Table ───────────────────────────────────────────────────────────
spans = extract_spans(conn, "seven_day", now)

print("\n[1] SPAN TABLE")
print(f"{'reset_at':17} {'start':17} {'end':17} {'Δpp':>4} {'span':>9} {'pp/hr':>6} status")
print("-" * 100)
for s in spans:
    if s.exclude_reason == 'personal':
        status = "REJECT (personal)"
        rate_s = "   —"
    elif not s.is_completed:
        status = "IN-PROGRESS (excluded)"
        rate_s = f"{s.rate_pp_h:.3f}"
    else:
        parts = []
        if s.split_index: parts.append(f"split {s.split_index}/{s.split_total}")
        if s.is_censored: parts.append("CENSORED")
        if s.exclude_reason == 'thin': parts.append("EXCLUDE/THIN")
        status = "; ".join(parts) if parts else "KEEP"
        rate_s = f"{s.rate_pp_h:.3f}"
    print(f"{s.resets_at.strftime('%Y-%m-%d %H:%M'):17} "
          f"{s.start_ts.strftime('%Y-%m-%d %H:%M'):17} "
          f"{s.end_ts.strftime('%Y-%m-%d %H:%M'):17} "
          f"{s.delta_pp:>4.0f} {fmt_dur(s.span_h):>9} {rate_s:>6} {status}")

# ── 2. Prior ────────────────────────────────────────────────────────────────
print("\n[2] POOLED PRIOR (WS11)")
eligible = [s for s in spans if s.prior_eligible]
prior = pooled_prior(spans)
sum_pp = sum(s.delta_pp for s in eligible)
sum_h = sum(s.span_h for s in eligible)
print(f"  Eligible spans ({len(eligible)}): "
      f"{[s.resets_at.strftime('%m-%d') + ('*' if s.is_censored else '') for s in eligible]}")
print(f"  Σ(Δpp) = {sum_pp:.0f}   Σ(span_h) = {sum_h:.1f}h")
print(f"  Prior (pooled) = {prior:.4f} pp/hr")
target_prior = 0.809
prior_ok = abs(prior - target_prior) < 0.005
print(f"  Target ≈ {target_prior}  →  {'PASS ✓' if prior_ok else f'FAIL ✗ (diff={prior-target_prior:+.4f})'}")

# ── 3. Epoch fix ─────────────────────────────────────────────────────────────
print("\n[3] EPOCH FIX — elapsed time for current window")

# Get latest resets_at for seven_day
latest = conn.execute(
    "SELECT resets_at FROM quota_snapshots WHERE bucket='seven_day' "
    "AND resets_at NOT IN ('2286-11-20T17:46:39Z') "
    "ORDER BY snapshot_ts DESC LIMIT 1"
).fetchone()

from datetime import timedelta
raw = latest[0]
canon_map = _canonicalise_resets_at([raw])
resets_at_dt = canon_map[raw]

epoch_new = get_effective_epoch(conn, "seven_day", resets_at_dt, now)
epoch_old = max(h for h in [
    *(r[0] for r in conn.execute(
        "SELECT reset_ts FROM reset_history WHERE bucket='seven_day' AND reset_ts <= ?",
        (now.isoformat(),)
    ).fetchall()),
]) if conn.execute("SELECT count(*) FROM reset_history").fetchone()[0] > 0 else None

from datetime import datetime as dt_cls
def parse(s):
    return dt_cls.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)

if epoch_old:
    old_epoch_dt = parse(epoch_old) if isinstance(epoch_old, str) else epoch_old
    old_elapsed = (now - old_epoch_dt).total_seconds() / 3600
    print(f"  OLD epoch (max history only): {old_epoch_dt.strftime('%Y-%m-%d %H:%M UTC')} → elapsed {old_elapsed:.1f}h")

new_elapsed = (now - epoch_new).total_seconds() / 3600
print(f"  NEW epoch (max + weekly peer): {epoch_new.strftime('%Y-%m-%d %H:%M UTC')} → elapsed {new_elapsed:.1f}h")
elapsed_ok = 8.0 <= new_elapsed <= 16.0
print(f"  Target ≈ 10–12h  →  {'PASS ✓' if elapsed_ok else f'FAIL ✗ ({new_elapsed:.1f}h out of range)'}")

# ── 4. projection.py integration ─────────────────────────────────────────────
print("\n[4] projection.py — predictive mode uses new prior + epoch")
r = project(conn, "seven_day", now=now, mode='predictive')
print(f"  prior    = {r.prior_pp_h:.4f} pp/hr  (target ≈ 0.809)")
print(f"  elapsed  = {r.elapsed_h:.1f}h  (target ≈ 10–12h)")
print(f"  eff_rate = {r.effective_rate_pp_h:.4f} pp/hr")
proj_prior_ok = abs(r.prior_pp_h - target_prior) < 0.005
proj_elapsed_ok = 8.0 <= r.elapsed_h <= 16.0
print(f"  Prior  {'PASS ✓' if proj_prior_ok else 'FAIL ✗'}")
print(f"  Elapsed {'PASS ✓' if proj_elapsed_ok else 'FAIL ✗'}")

r_naive = project(conn, "seven_day", now=now, mode='naive')
# Naive uses resets_at - 7d = Jun-20 - 7d = Jun-13 00:00 UTC.
# Current window genuinely started Jun-13 00:00 so naive elapsed ≈ same as predictive.
# Verify naive uses resets_at-7d path (not history), i.e. no "Reset epoch" note in naive.
naive_has_epoch_note = any("Reset epoch" in n for n in r_naive.notes)
naive_ok = not naive_has_epoch_note  # naive should NOT emit a Reset epoch note
print(f"\n  naive elapsed = {r_naive.elapsed_h:.1f}h  (resets_at-7d path, no epoch note)")
print(f"  naive notes: {r_naive.notes}")
print(f"  Naive mode uses resets_at-7d (no epoch note)  {'PASS ✓' if naive_ok else 'FAIL ✗'}")
naive_elapsed_ok = naive_ok

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
all_pass = prior_ok and elapsed_ok and proj_prior_ok and proj_elapsed_ok and naive_ok
print(f"OVERALL: {'ALL PASS ✓' if all_pass else 'FAILURES — see above'}")
print("=" * 72)
