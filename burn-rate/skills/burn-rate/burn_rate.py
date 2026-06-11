#!/usr/bin/env python3
"""Report burn rate + projected exhaustion for weekly Claude Code quotas.

Reads from ~/.claude/state/usage-log.sqlite (populated by the statusline hook
that captures quota_snapshots).

Weekly buckets are `all_models_weekly` and `sonnet_weekly` (per James's note:
only TWO weekly buckets exist; no Opus bucket). The `seven_day` bucket name
appears in newer rows — treated as an alias for `all_models_weekly`.

Usage:
    python burn_rate.py              # since-reset rate (primary) + 6h recent-rate comparison
    python burn_rate.py --window 1h  # recent comparison window = last hour
    python burn_rate.py --window 24h # recent comparison window = last day

    # Set a manual reset-epoch override (e.g. when a bucket reset early):
    python burn_rate.py --set-reset-override seven_day 2026-06-09T21:17:03Z

    # Clear a manual override:
    python burn_rate.py --clear-reset-override seven_day

    # List active overrides:
    python burn_rate.py --list-reset-overrides

The primary projection always uses the rate since the last reset (the only
honest denominator for a weekly bucket). The recent-window rate is shown
underneath as a comparison so you can see whether you're trending hotter or
cooler than the week average.

Manual reset-epoch overrides
-----------------------------
Sometimes a bucket resets early (e.g. the All Models bucket drops from 70% to
0% mid-week while `resets_at` still shows the original date). In that case the
derived "since last reset" epoch — `resets_at − 7 days` — is wrong, producing
an inflated elapsed window and understated burn rate.

Use --set-reset-override to record the true reset timestamp. On every run the
script checks each override against the derived reset:

    derived_reset = parse_iso(resets_at) - 7 days

If override_ts > derived_reset  → use override_ts as the epoch (override wins).
If derived_reset >= override_ts → the scheduled reset has caught up/passed the
    override; the override is stale, so it is DELETED automatically and the
    derived value is used. This is the auto-expiry mechanism — you never need
    to clean up manually.

Overrides are stored in a `reset_overrides` table in the same SQLite DB:
    reset_overrides(bucket TEXT PRIMARY KEY, reset_ts TEXT, created_ts TEXT)
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"

# --- Duty-cycle active window (local time, 24h clock) ---
ACTIVE_START_HOUR = 6   # 06:00 local
ACTIVE_END_HOUR   = 21  # 21:00 local (exclusive — burn stops at 21:00)
ACTIVE_HOURS_PER_DAY = ACTIVE_END_HOUR - ACTIVE_START_HOUR  # 15

# Local timezone for active-window arithmetic (reads machine tz)
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo

# Map raw bucket names → display label. seven_day is the new name for the
# all-models weekly cap (statusline source moved to it mid-2026).
DISPLAY = {
    "all_models_weekly": "All Models (weekly)",
    "seven_day":         "All Models (weekly)",
    "sonnet_weekly":     "Sonnet (weekly)",
}
WEEKLY_BUCKETS = list(DISPLAY.keys())


def parse_iso(s: str) -> datetime:
    # Accept "...Z" or "...+10:00"
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def parse_window(s: str) -> timedelta:
    s = s.strip().lower()
    if s.endswith("h"):
        return timedelta(hours=float(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=float(s[:-1]))
    if s.endswith("d"):
        return timedelta(days=float(s[:-1]))
    raise ValueError(f"window must end in h/m/d, got {s!r}")


# ---------------------------------------------------------------------------
# Reset-override table management
# ---------------------------------------------------------------------------

def ensure_override_table(cur: sqlite3.Cursor) -> None:
    """Create reset_overrides table if it doesn't exist yet."""
    cur.execute(
        """CREATE TABLE IF NOT EXISTS reset_overrides (
            bucket     TEXT PRIMARY KEY,
            reset_ts   TEXT NOT NULL,
            created_ts TEXT NOT NULL
        )"""
    )


def get_override(cur: sqlite3.Cursor, bucket: str) -> str | None:
    """Return the override reset_ts for `bucket`, or None."""
    ensure_override_table(cur)
    row = cur.execute(
        "SELECT reset_ts FROM reset_overrides WHERE bucket = ?", (bucket,)
    ).fetchone()
    return row[0] if row else None


def set_override(con: sqlite3.Connection, cur: sqlite3.Cursor, bucket: str, reset_ts: str) -> None:
    """Store (or replace) a manual reset-epoch override."""
    ensure_override_table(cur)
    # Validate the timestamp parses
    parse_iso(reset_ts)
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cur.execute(
        "INSERT OR REPLACE INTO reset_overrides (bucket, reset_ts, created_ts) VALUES (?, ?, ?)",
        (bucket, reset_ts, now_iso),
    )
    con.commit()


def clear_override(con: sqlite3.Connection, cur: sqlite3.Cursor, bucket: str) -> bool:
    """Delete a manual override. Returns True if one existed."""
    ensure_override_table(cur)
    cur.execute("DELETE FROM reset_overrides WHERE bucket = ?", (bucket,))
    deleted = cur.rowcount > 0
    if deleted:
        con.commit()
    return deleted


def list_overrides(cur: sqlite3.Cursor) -> list[tuple[str, str, str]]:
    """Return [(bucket, reset_ts, created_ts), ...] for all active overrides."""
    ensure_override_table(cur)
    return cur.execute(
        "SELECT bucket, reset_ts, created_ts FROM reset_overrides ORDER BY bucket"
    ).fetchall()


def check_and_expire_override(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    bucket: str,
    resets_at: str,
) -> tuple[str | None, bool]:
    """Check override for `bucket` against derived reset; expire if stale.

    Returns (effective_reset_ts, override_was_used):
      - effective_reset_ts: the ISO ts to use as the "since last reset" epoch
        (None if override doesn't exist and we should use derived value).
      - override_was_used: True if the override is still valid and was applied.

    Auto-expiry rule:
      derived_reset = parse_iso(resets_at) - 7 days
      If derived_reset >= override_ts → stale, delete and use derived.
      If override_ts  >  derived_reset → override wins.
    """
    override_ts_str = get_override(cur, bucket)
    if override_ts_str is None:
        return None, False

    override_dt = parse_iso(override_ts_str)
    derived_reset_dt = parse_iso(resets_at) - timedelta(days=7)

    if derived_reset_dt >= override_dt:
        # Scheduled reset has caught up — override is stale, delete it.
        clear_override(con, cur, bucket)
        return None, False

    # Override is still valid (override_dt > derived_reset_dt).
    return override_ts_str, True


# ---------------------------------------------------------------------------
# Core query helpers
# ---------------------------------------------------------------------------

def latest_for_bucket(cur, bucket: str):
    """Return (ts, pct, resets_at) for the newest row of `bucket`, or None."""
    row = cur.execute(
        "SELECT snapshot_ts, pct_used, resets_at FROM quota_snapshots "
        "WHERE bucket = ? ORDER BY snapshot_ts DESC LIMIT 1",
        (bucket,),
    ).fetchone()
    return row


def rate_for_bucket(cur, bucket: str, window: timedelta, resets_at: str | None = None):
    """Linear regression-ish: use first vs last snapshot in the window.
    Returns (pp_per_hour, first_pct, last_pct, span_hours, synthesized) or None.

    When `resets_at` is given, restrict to rows matching the same reset
    generation — this drops stale pre-reset snapshots whose timestamp is after
    the wall-clock reset boundary but whose `resets_at` field still points at
    the now-past reset (server-side hadn't refreshed yet).

    Fallback: if fewer than 2 in-window rows match the current reset, but the
    bucket is weekly and we have at least one real snapshot, synthesize a
    (prev_reset_ts, 0.0) datum at the prior reset boundary (resets_at − 7d).
    This gives a rate from the start of the current quota window.
    `synthesized` is True when this fallback fired."""
    now = datetime.now(timezone.utc)
    since = (now - window).isoformat().replace("+00:00", "Z")
    if resets_at is not None:
        rows = cur.execute(
            "SELECT snapshot_ts, pct_used FROM quota_snapshots "
            "WHERE bucket = ? AND snapshot_ts >= ? AND resets_at = ? "
            "ORDER BY snapshot_ts ASC",
            (bucket, since, resets_at),
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT snapshot_ts, pct_used FROM quota_snapshots "
            "WHERE bucket = ? AND snapshot_ts >= ? "
            "ORDER BY snapshot_ts ASC",
            (bucket, since),
        ).fetchall()

    synthesized = False
    if len(rows) < 2 and resets_at is not None:
        # Pull the most recent matching-reset row regardless of window, so a
        # single fresh snapshot still yields a rate when paired with the
        # synthesized start-of-window 0%.
        anchor = cur.execute(
            "SELECT snapshot_ts, pct_used FROM quota_snapshots "
            "WHERE bucket = ? AND resets_at = ? ORDER BY snapshot_ts DESC LIMIT 1",
            (bucket, resets_at),
        ).fetchone()
        if anchor is not None:
            prev_reset_dt = parse_iso(resets_at) - timedelta(days=7)
            prev_reset_iso = prev_reset_dt.isoformat().replace("+00:00", "Z")
            rows = [(prev_reset_iso, 0.0), anchor]
            synthesized = True

    if len(rows) < 2:
        return None
    t0 = parse_iso(rows[0][0])
    t1 = parse_iso(rows[-1][0])
    span_h = (t1 - t0).total_seconds() / 3600.0
    if span_h <= 0:
        return None
    delta = rows[-1][1] - rows[0][1]
    return delta / span_h, rows[0][1], rows[-1][1], span_h, synthesized


def hours_until(reset_iso: str) -> float:
    try:
        return (parse_iso(reset_iso) - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return float("nan")


def fmt_h(h: float) -> str:
    if h != h:  # nan
        return "—"
    if h < 0:
        return f"{-h:.1f}h ago"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def active_hours_between(start: datetime, end: datetime) -> float:
    """Count only active-window hours between two UTC datetimes.

    Walks forward day-by-day in local time so DST transitions are handled
    correctly.  Each calendar day contributes the overlap of
    [ACTIVE_START_HOUR, ACTIVE_END_HOUR) with the requested [start, end) span.
    """
    if end <= start:
        return 0.0

    # Work in local time throughout
    local_start = start.astimezone(_LOCAL_TZ)
    local_end   = end.astimezone(_LOCAL_TZ)

    total = 0.0
    # Iterate day by day
    day = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < local_end:
        next_day = day + timedelta(days=1)
        # Active window for this calendar day
        win_s = day.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
        win_e = day.replace(hour=ACTIVE_END_HOUR,   minute=0, second=0, microsecond=0)
        # Clamp to our actual range
        seg_s = max(win_s, local_start)
        seg_e = min(win_e, local_end)
        if seg_e > seg_s:
            total += (seg_e - seg_s).total_seconds() / 3600.0
        day = next_day
    return total


def duty_cycle_eta(
    now_utc: datetime,
    pct: float,
    pp_per_hour: float,
    reset_utc: datetime,
) -> tuple[float | None, datetime | None]:
    """Compute duty-cycle projections.

    Returns (pct_at_reset, exhaust_wall_time).
    - pct_at_reset is always computed (projected % at reset).
    - exhaust_wall_time is the wall-clock UTC time the bucket hits 100%,
      walking through active windows only; None if it doesn't before reset.

    The rate pp_per_hour is applied ONLY during active hours.
    """
    active_to_reset = active_hours_between(now_utc, reset_utc)
    pct_at_reset = pct + pp_per_hour * active_to_reset

    # Find exhaustion wall-clock time: walk forward in active-window chunks
    if pp_per_hour <= 0:
        return pct_at_reset, None

    pp_remaining = 100.0 - pct
    active_hours_needed = pp_remaining / pp_per_hour

    # Walk forward minute-by-minute is expensive; instead walk in chunks:
    # at each moment, find how many active hours remain today, consume as many
    # as we need, then jump to next active window start.
    cursor = now_utc.astimezone(_LOCAL_TZ)
    remaining = active_hours_needed

    for _ in range(21):  # max 21 days (3 weeks) — safety valve
        # Are we currently inside an active window?
        h = cursor.hour + cursor.minute / 60.0 + cursor.second / 3600.0
        if ACTIVE_START_HOUR <= h < ACTIVE_END_HOUR:
            # Hours left in today's active window from cursor
            today_end = cursor.replace(
                hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0
            )
            hours_in_window = (today_end - cursor).total_seconds() / 3600.0
            if remaining <= hours_in_window:
                exhaust_local = cursor + timedelta(hours=remaining)
                exhaust_utc = exhaust_local.astimezone(timezone.utc)
                if exhaust_utc < reset_utc:
                    return pct_at_reset, exhaust_utc
                else:
                    return pct_at_reset, None
            remaining -= hours_in_window
            cursor = today_end  # now at 21:00 — fall through to advance to tomorrow
        # Advance to next day's ACTIVE_START_HOUR
        # If cursor is past today's active window, move to tomorrow's start
        next_active = cursor.replace(
            hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0
        )
        if cursor >= next_active:
            next_active = next_active + timedelta(days=1)
        cursor = next_active
        if cursor.astimezone(timezone.utc) >= reset_utc:
            return pct_at_reset, None

    return pct_at_reset, None  # should not reach


TREND_WINDOWS = [
    ("1h",  timedelta(hours=1)),
    ("2h",  timedelta(hours=2)),
    ("6h",  timedelta(hours=6)),
    ("1d",  timedelta(days=1)),
    ("2d",  timedelta(days=2)),
    ("5d",  timedelta(days=5)),
]


def trend_series(cur, bucket: str, resets: str | None, elapsed_h: float | None = None) -> list[tuple[str, float]]:
    """Return [(label, pp_h), ...] for trend windows with sufficient data.

    elapsed_h: hours since the effective reset epoch (override-aware).  Any
    window longer than this would reach back into the previous quota cycle and
    produce meaningless (often negative) rates, so those windows are skipped.
    """
    result = []
    for label, win in TREND_WINDOWS:
        win_hours = win.total_seconds() / 3600.0
        # Skip windows that extend before the current quota cycle.
        if elapsed_h is not None and win_hours > elapsed_h:
            continue
        r = rate_for_bucket(cur, bucket, win, resets_at=resets)
        if r is None:
            continue
        pp_h, _first, _last, span_h, synthesized = r
        if synthesized:
            continue
        if span_h < win_hours * 0.8:
            continue
        result.append((label, pp_h))
    return result


def fmt_trend(series: list[tuple[str, float]]) -> str | None:
    """Format trend series as load-average style string, or None if empty."""
    if not series:
        return None
    nums = " · ".join(f"{pp:.2f}" for _, pp in series)
    labels = [lbl for lbl, _ in series]
    # Compress legend: if all labels end in "h", strip h and join with /
    if all(lbl.endswith("h") for lbl in labels):
        legend = "/".join(lbl[:-1] for lbl in labels) + "h"
    else:
        legend = "/".join(labels)
    return f"{nums}   pp/hr  ({legend})"


def fmt_duty_line(
    pct_at_reset: float | None,
    exhaust_utc: datetime | None,
    now_utc: datetime,
    reset_utc: datetime | None,
) -> str:
    """Format the duty: output line."""
    window_label = f"active {ACTIVE_START_HOUR:02d}:00–{ACTIVE_END_HOUR:02d}:00"

    if pct_at_reset is None:
        return f"  duty:      insufficient data  [{window_label}]"

    if exhaust_utc is not None:
        days_away = (exhaust_utc - now_utc).total_seconds() / 86400.0
        local_str = exhaust_utc.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
        return (
            f"  duty:      {pct_at_reset:.1f}% by reset  ·  "
            f"100% in ~{days_away:.1f}d (at {local_str})  [{window_label}]"
        )
    else:
        return (
            f"  duty:      {pct_at_reset:.1f}% by reset  "
            f"(no exhaustion at this rate)  [{window_label}]"
        )


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def report(window: timedelta, con: sqlite3.Connection, cur: sqlite3.Cursor):
    now = datetime.now(timezone.utc)

    print(f"Burn rate report ({now.astimezone(_LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')})")
    print("🕛 round-the-clock · 💼 duty hours")
    print()

    # Collapse seven_day + all_models_weekly into one logical bucket;
    # pick the bucket with the newest snapshot per label.
    by_label: dict[str, tuple[str, tuple]] = {}
    for bucket in WEEKLY_BUCKETS:
        latest = latest_for_bucket(cur, bucket)
        if latest is None:
            continue
        label = DISPLAY[bucket]
        if label not in by_label or latest[0] > by_label[label][1][0]:
            by_label[label] = (bucket, latest)

    for label, (bucket, latest) in by_label.items():
        ts, pct, resets = latest
        h_to_reset = hours_until(resets) if resets else float("nan")

        # --- Determine effective "since last reset" epoch ---
        # Default: derived from resets_at - 7 days.
        # Override: if a manual override exists AND it is newer than derived,
        #           use it instead (and auto-expire it once derived catches up).
        override_active = False
        effective_reset_ts = None
        if resets:
            effective_reset_ts, override_active = check_and_expire_override(
                con, cur, bucket, resets
            )
            if override_active and effective_reset_ts is not None:
                prev_reset_dt = parse_iso(effective_reset_ts)
            else:
                prev_reset_dt = parse_iso(resets) - timedelta(days=7)
        else:
            prev_reset_dt = None

        # Primary rate: since last reset (raw average across total hours
        # elapsed in this quota window).
        primary = None
        if resets and prev_reset_dt is not None:
            elapsed_h = (now - prev_reset_dt).total_seconds() / 3600.0
            if elapsed_h > 0:
                primary_pp_h = pct / elapsed_h
                primary = (primary_pp_h, elapsed_h)

        print(f"── {label} ──")
        ts_time = parse_iso(ts).astimezone(_LOCAL_TZ).strftime("%H:%M")
        print(f"  current:   {pct:.1f}%   (snapshot {ts_time})")
        if resets:
            resets_local = parse_iso(resets).astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
            override_tag = ""
            if override_active and effective_reset_ts is not None:
                override_tag = f"   [override epoch {effective_reset_ts}]"
            print(f"  resets:    {resets_local}  ({fmt_h(h_to_reset)}){override_tag}")

        if primary is None:
            print(f"  so far:    insufficient data (no reset timestamp)")
        else:
            primary_pp_h, elapsed_h = primary

            # so far: two bases — 🕛 24h and 💼 duty
            active_elapsed = active_hours_between(prev_reset_dt, now)
            sofar_duty = pct / active_elapsed if active_elapsed > 0 else float("nan")
            print(f"  so far:    🕛 {primary_pp_h:.2f}   💼 {sofar_duty:.2f}   pp/hr"
                  f"   ·  {pct:.0f}% over {elapsed_h:.1f}h")

            # remaining: headroom at two bases
            if h_to_reset == h_to_reset and h_to_reset > 0:
                headroom = 100.0 - pct
                rem_24h = headroom / h_to_reset
                reset_utc_dt = parse_iso(resets)
                active_remaining = active_hours_between(now, reset_utc_dt)
                rem_duty = headroom / active_remaining if active_remaining > 0 else float("nan")

                over_24h = primary_pp_h > rem_24h
                over_duty = (sofar_duty == sofar_duty) and (rem_duty == rem_duty) and (sofar_duty > rem_duty)
                if over_duty:
                    verdict = "⚠ over even on 💼 — slow down"
                elif over_24h:
                    verdict = "⚠ over on 🕛, ok on 💼"
                else:
                    verdict = "✓ within budget"

                rem_duty_str = f"{rem_duty:.2f}" if rem_duty == rem_duty else "—"
                print(f"  remaining: 🕛 {rem_24h:.2f}   💼 {rem_duty_str}   pp/hr"
                      f"   ·  {headroom:.0f}% over {fmt_h(h_to_reset)}   [{verdict}]")

            # trend: load-average style
            tr = fmt_trend(trend_series(cur, bucket, resets, elapsed_h=elapsed_h))
            if tr is not None:
                print(f"  trend:     {tr}")

            if primary_pp_h <= 0:
                print(f"  ETA:       not increasing — no exhaustion projected")
            else:
                pp_remaining = 100.0 - pct
                hrs_to_100 = pp_remaining / primary_pp_h
                exhaust_at = now + timedelta(hours=hrs_to_100)
                if resets and exhaust_at < parse_iso(resets):
                    margin_h = (parse_iso(resets) - exhaust_at).total_seconds() / 3600.0
                    print(f"  ETA:       100% in {fmt_h(hrs_to_100)} "
                          f"(at {exhaust_at.astimezone(_LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')})  "
                          f"⚠ BEFORE reset by {fmt_h(margin_h)}")
                else:
                    pct_at_reset = pct + primary_pp_h * h_to_reset if h_to_reset == h_to_reset else None
                    if pct_at_reset is not None:
                        print(f"  ETA:       {pct_at_reset:.1f}% by reset  (no exhaustion at this rate)")
                    else:
                        print(f"  ETA:       100% in {fmt_h(hrs_to_100)} (no reset known)")

            # ── Duty-cycle projection ──────────────────────────────────────
            # Same pp/hr rate, but burn only accrues during active hours
            # (ACTIVE_START_HOUR–ACTIVE_END_HOUR local time).
            if resets:
                reset_utc = parse_iso(resets)
                dc_pct_at_reset, dc_exhaust = duty_cycle_eta(
                    now, pct, primary_pp_h, reset_utc
                )
                print(fmt_duty_line(dc_pct_at_reset, dc_exhaust, now, reset_utc))
            else:
                print(f"  duty:      no reset timestamp — cannot compute")
        print()


def main():
    ap = argparse.ArgumentParser(
        description="Report Claude Code quota burn rate and projected exhaustion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python burn_rate.py                                    # default 6h comparison window
  python burn_rate.py --window 1h                        # 1h comparison window
  python burn_rate.py --set-reset-override seven_day 2026-06-09T21:17:03Z
  python burn_rate.py --clear-reset-override seven_day
  python burn_rate.py --list-reset-overrides
""",
    )
    ap.add_argument(
        "--window", default="6h",
        help="Lookback window for burn rate comparison (e.g. 1h, 6h, 24h, 3d). Default 6h.",
    )

    override_group = ap.add_mutually_exclusive_group()
    override_group.add_argument(
        "--set-reset-override", nargs=2, metavar=("BUCKET", "RESET_TS"),
        help="Set a manual reset-epoch override for BUCKET (ISO 8601 timestamp, e.g. 2026-06-09T21:17:03Z).",
    )
    override_group.add_argument(
        "--clear-reset-override", metavar="BUCKET",
        help="Clear the manual reset-epoch override for BUCKET.",
    )
    override_group.add_argument(
        "--list-reset-overrides", action="store_true",
        help="List all active manual reset-epoch overrides.",
    )

    args = ap.parse_args()

    if not DB.exists():
        print(f"No usage DB at {DB}")
        return

    con = sqlite3.connect(DB)
    cur = con.cursor()

    if args.set_reset_override:
        bucket, reset_ts = args.set_reset_override
        try:
            parse_iso(reset_ts)
        except ValueError as e:
            print(f"Invalid timestamp {reset_ts!r}: {e}")
            return
        set_override(con, cur, bucket, reset_ts)
        print(f"Override set: bucket={bucket!r}  reset_ts={reset_ts}")
        print("Auto-expiry: this override will be cleared automatically once the next")
        print("  scheduled reset (resets_at − 7 days) reaches or passes this timestamp.")
        return

    if args.clear_reset_override:
        bucket = args.clear_reset_override
        deleted = clear_override(con, cur, bucket)
        if deleted:
            print(f"Override cleared for bucket {bucket!r}.")
        else:
            print(f"No override found for bucket {bucket!r}.")
        return

    if args.list_reset_overrides:
        ensure_override_table(cur)
        rows = list_overrides(cur)
        if not rows:
            print("No active reset-epoch overrides.")
        else:
            print("Active reset-epoch overrides:")
            for bucket, reset_ts, created_ts in rows:
                print(f"  {bucket:20s}  reset_ts={reset_ts}  created={created_ts}")
        return

    report(parse_window(args.window), con, cur)


if __name__ == "__main__":
    main()
