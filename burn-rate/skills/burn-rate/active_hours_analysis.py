#!/usr/bin/env python3
"""Compute active hours so far this week from quota_snapshots gaps.

An "active" minute is one where snapshots are being written. Gaps between
consecutive snapshots greater than IDLE_THRESHOLD_MIN are treated as idle
(I wasn't running). Sum the remaining intervals to get active hours.

Then derive:
- rate per active hour
- max affordable active hours before exhaustion
- expected active hours remaining (at current active fraction)
- whether that crosses the budget
"""

import sqlite3
import datetime
from pathlib import Path

DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"
RESET = datetime.datetime.fromisoformat("2026-05-30T00:00:00+00:00")
PRIOR_RESET = RESET - datetime.timedelta(days=7)
WEEK_H = 168.0
IDLE_THRESHOLD_MIN = 20  # tweak this


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT snapshot_ts, pct_used FROM quota_snapshots "
        "WHERE bucket='seven_day' AND snapshot_ts >= ? AND snapshot_ts <= ? "
        "ORDER BY snapshot_ts ASC",
        (PRIOR_RESET.isoformat().replace("+00:00", "Z"),
         RESET.isoformat().replace("+00:00", "Z"))
    ).fetchall()

    if not rows:
        print("no snapshots")
        return

    times = [parse_iso(ts) for ts, _ in rows]
    pcts = [pct for _, pct in rows]

    last_pct = pcts[-1]
    last_t = times[-1]
    elapsed_h = (last_t - PRIOR_RESET).total_seconds() / 3600.0
    wall_remaining_h = (RESET - last_t).total_seconds() / 3600.0

    # Compute active duration: sum of all gaps <= threshold, plus a small
    # "credit" for the first/last snapshot (we don't know what happened
    # before the first or after the last — just sum the connecting intervals).
    threshold_s = IDLE_THRESHOLD_MIN * 60
    active_s = 0.0
    idle_gaps = []
    for i in range(len(times) - 1):
        gap_s = (times[i + 1] - times[i]).total_seconds()
        if gap_s <= threshold_s:
            active_s += gap_s
        else:
            idle_gaps.append((times[i], times[i + 1], gap_s / 3600))

    active_h = active_s / 3600.0
    active_fraction = active_h / elapsed_h if elapsed_h > 0 else 0
    idle_h = elapsed_h - active_h

    print(f"IDLE_THRESHOLD = {IDLE_THRESHOLD_MIN} min")
    print(f"Wall elapsed (since reset):  {elapsed_h:6.2f} h")
    print(f"  active hours:              {active_h:6.2f} h  ({active_fraction*100:.1f}% of elapsed)")
    print(f"  idle hours:                {idle_h:6.2f} h  ({(1-active_fraction)*100:.1f}% of elapsed)")
    print(f"Wall remaining (to reset):   {wall_remaining_h:6.2f} h")
    print()
    print(f"Current % used:              {last_pct:.1f}%")
    print(f"Rate per WALL hour:          {last_pct/elapsed_h:.3f} pp/h")
    print(f"Rate per ACTIVE hour:        {last_pct/active_h:.3f} pp/active-h")
    print()

    # Projections
    print("=== PROJECTIONS ===")
    # 1. Week-pace (current default)
    proj_wall = last_pct * WEEK_H / elapsed_h
    print(f"Week-pace projection (pct × week/elapsed): {proj_wall:.1f}% by reset")

    # 2. Active-rate with same active fraction (mathematically identical)
    expected_active_remaining_h = wall_remaining_h * active_fraction
    proj_active = last_pct + (last_pct / active_h) * expected_active_remaining_h
    print(f"Active-rate projection (same active fraction): {proj_active:.1f}% by reset")
    print(f"  (expected active hours remaining: {expected_active_remaining_h:.1f} h)")

    print()
    print("=== AFFORDABILITY ===")
    pp_left = 100 - last_pct
    max_active_h = pp_left / (last_pct / active_h)
    print(f"PP budget remaining:                     {pp_left:.1f}pp")
    print(f"Max affordable active hours:             {max_active_h:.1f} h at current active-rate")
    print(f"Expected active hours remaining:         {expected_active_remaining_h:.1f} h")
    print(f"Over/under:                              {expected_active_remaining_h - max_active_h:+.1f} h")

    # Levers
    print()
    print("=== LEVERS ===")
    # If I cut active rate by 20%:
    new_rate = (last_pct / active_h) * 0.8
    new_proj = last_pct + new_rate * expected_active_remaining_h
    print(f"If rate per active hour drops 20% (to {new_rate:.2f} pp/h): proj = {new_proj:.1f}%")
    # If I cut active hours by 20%:
    new_active_remaining = expected_active_remaining_h * 0.8
    new_proj2 = last_pct + (last_pct / active_h) * new_active_remaining
    print(f"If active hours drop 20% (to {new_active_remaining:.1f}h remaining): proj = {new_proj2:.1f}%")

    print()
    print(f"=== IDLE GAPS DETECTED ({len(idle_gaps)} gaps > {IDLE_THRESHOLD_MIN}min) ===")
    for start, end, hours in idle_gaps[:20]:
        start_local = start.astimezone()
        end_local = end.astimezone()
        print(f"  {start_local.strftime('%a %H:%M')} → {end_local.strftime('%a %H:%M')}  ({hours:.2f}h idle)")
    if len(idle_gaps) > 20:
        print(f"  ... and {len(idle_gaps) - 20} more")


if __name__ == "__main__":
    main()
