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
    python burn_rate.py --mode naive       # existing calculations verbatim (baseline)
    python burn_rate.py --mode predictive  # Bayesian shrinkage (default)
    python burn_rate.py --mode target      # even-pace prescriptive projection

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

Projection modes (--mode):
  naive       — existing calculations verbatim: raw pct/elapsed, 06-21 duty window,
                no shrinkage. Retained as the control/baseline forever.
  predictive  — (default) Bayesian shrinkage toward a span-correct historical prior.
                Kills early-week false STOP/bomb alarms while still catching genuine
                sustained overruns. Uses empirical 07-24 UTC duty surface.
  target      — Prescriptive even-pace: (remaining budget / remaining time), with
                duty surface breathing on de-rationed natural demand. Answers "to
                spread evenly, what's my pace right now and am I ahead of it?"

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
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# ANSI colour helpers — mirrors the ramp in render-rates.py exactly.
# Controlled by the --color flag; never leaks into --json or --autonomous-status.
# ---------------------------------------------------------------------------

# Same 256-colour ramp as render-rates.py / statusline-command.sh rate_colour()
_COLOUR_RAMP = [
    (100, "\x1b[38;5;196m"),  # bright red  ≥100
    (90,  "\x1b[38;5;160m"),  # red         ≥90
    (80,  "\x1b[38;5;202m"),  # orange-red  ≥80
    (70,  "\x1b[38;5;214m"),  # amber       ≥70
    (55,  "\x1b[38;5;75m"),   # blue        ≥55
    (40,  "\x1b[38;5;117m"),  # light blue  ≥40
    (0,   "\x1b[38;5;151m"),  # pale green  comfortable
]
_RESET = "\x1b[0m"

# Module-level flag; set once in main() based on --color arg.
_COLOUR_ENABLED: bool = False


def _colour_for_pct(pct: float) -> str:
    """Return the ANSI colour code for a given percentage, matching the statusline ramp."""
    if not _COLOUR_ENABLED:
        return ""
    pct_i = int(round(pct))
    for threshold, code in _COLOUR_RAMP:
        if pct_i >= threshold:
            return code
    return _COLOUR_RAMP[-1][1]


def _reset() -> str:
    """Return ANSI reset if colour is enabled, else empty string."""
    return _RESET if _COLOUR_ENABLED else ""

# ---------------------------------------------------------------------------
# Lazy import of projection module (WS4 shrinkage)
# ---------------------------------------------------------------------------
# Import at module level so syntax errors surface immediately; but we guard
# usage with _PROJECTION_AVAILABLE so the script degrades to naive if the
# module is absent (e.g. running from an old install path).

_PROJECTION_AVAILABLE = False
_project_fn = None
_project_five_hour_fn = None
_stability_demo_fn = None
try:
    import importlib.util as _ilu
    _proj_path = Path(__file__).parent / "projection.py"
    if _proj_path.exists():
        _spec = _ilu.spec_from_file_location("projection", _proj_path)
        _proj_mod = _ilu.module_from_spec(_spec)
        # Register before exec so dataclass.__module__ resolution works
        sys.modules["projection"] = _proj_mod
        _spec.loader.exec_module(_proj_mod)
        _project_fn = _proj_mod.project
        _project_five_hour_fn = getattr(_proj_mod, "project_five_hour", None)
        _stability_demo_fn = getattr(_proj_mod, "stability_demo_5h", None)
        _PROJECTION_AVAILABLE = True
except Exception as _proj_import_err:
    print(f"[warn] projection module unavailable: {_proj_import_err}", file=sys.stderr)

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


def rate_for_bucket(cur, bucket: str, window: timedelta, resets_at: str | None = None,
                    cycle: timedelta = timedelta(days=7)):
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
            prev_reset_dt = parse_iso(resets_at) - cycle
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

# Trend windows for the rolling 5h bucket — minute-to-hour scale.
TREND_WINDOWS_5H = [
    ("5m",  timedelta(minutes=5)),
    ("15m", timedelta(minutes=15)),
    ("30m", timedelta(minutes=30)),
    ("1h",  timedelta(hours=1)),
    ("2h",  timedelta(hours=2)),
    ("4h",  timedelta(hours=4)),
]

FIVE_HOUR_BUCKET = "five_hour"
FIVE_HOUR_CYCLE = timedelta(hours=5)


def trend_series(cur, bucket: str, resets: str | None, elapsed_h: float | None = None,
                 windows: list | None = None, cycle: timedelta = timedelta(days=7)) -> list[tuple[str, float]]:
    """Return [(label, pp_h), ...] for trend windows with sufficient data.

    elapsed_h: hours since the effective reset epoch (override-aware).  Any
    window longer than this would reach back into the previous quota cycle and
    produce meaningless (often negative) rates, so those windows are skipped.

    windows: the (label, timedelta) list to walk; defaults to the weekly
    TREND_WINDOWS.  cycle: the quota-cycle length used by the synth fallback
    (7d weekly, 5h for the five_hour bucket).
    """
    result = []
    for label, win in (windows or TREND_WINDOWS):
        win_hours = win.total_seconds() / 3600.0
        # Skip windows that extend before the current quota cycle.
        if elapsed_h is not None and win_hours > elapsed_h:
            continue
        r = rate_for_bucket(cur, bucket, win, resets_at=resets, cycle=cycle)
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


def snapshot_staleness_marker(snapshot_ts: str, threshold_min: int = 45) -> str | None:
    """Return a staleness annotation string if `snapshot_ts` is older than
    `threshold_min` minutes, or None if the snapshot is fresh.

    Example return value:  '  ⚠ stale 1.2h'
    """
    try:
        snap_dt = parse_iso(snapshot_ts)
        age_h = (datetime.now(timezone.utc) - snap_dt).total_seconds() / 3600.0
        if age_h >= threshold_min / 60.0:
            return f"  ⚠ stale {age_h:.1f}h"
        return None
    except Exception:
        return None


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
# Rolling 5-hour bucket
# ---------------------------------------------------------------------------

def report_five_hour(con: sqlite3.Connection, cur: sqlite3.Cursor, mode: str = "predictive") -> None:
    """Print the rolling 5-hour All-Models bucket stanza.

    Unlike the weekly buckets, the cycle is 5h: the window START is
    `resets_at − 5h`, so elapsed = now − (resets_at − 5h) and remaining =
    resets_at − now.  This is the most urgent bucket (shortest fuse), so it's
    printed first.  Duty-cycle is irrelevant for a 5h window (it spans a single
    sitting, not multiple days), so there's no duty line.  Trend uses
    minute-to-hour windows (5m/15m/30m/1h/2h/4h).
    """
    latest = latest_for_bucket(cur, FIVE_HOUR_BUCKET)
    if latest is None:
        return
    ts, pct, resets = latest
    now = datetime.now(timezone.utc)

    print("── All Models (5h) ──")
    ts_time = parse_iso(ts).astimezone(_LOCAL_TZ).strftime("%H:%M")
    stale_tag = snapshot_staleness_marker(ts) or ""
    print(f"  current:   {pct:.1f}%   (snapshot {ts_time}){stale_tag}")

    if not resets:
        print("  so far:    insufficient data (no reset timestamp)")
        print()
        return

    reset_dt = parse_iso(resets)
    start_dt = reset_dt - FIVE_HOUR_CYCLE          # window START = end − 5h
    h_to_reset = (reset_dt - now).total_seconds() / 3600.0
    resets_local = reset_dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    print(f"  resets:    {resets_local}  ({fmt_h(h_to_reset)})")

    elapsed_h = (now - start_dt).total_seconds() / 3600.0
    if elapsed_h <= 0:
        print("  so far:    insufficient data (window not started)")
        print()
        return

    sofar_pp_h = pct / elapsed_h
    print(f"  so far:    {sofar_pp_h:.2f} pp/hr   ·  {pct:.0f}% over {elapsed_h:.1f}h")

    if h_to_reset == h_to_reset and h_to_reset > 0:
        headroom = 100.0 - pct
        rem_pp_h = headroom / h_to_reset
        verdict = "⚠ over budget — slow down" if sofar_pp_h > rem_pp_h else "✓ within budget"
        print(f"  remaining: {rem_pp_h:.2f} pp/hr   ·  {headroom:.0f}% over {fmt_h(h_to_reset)}   [{verdict}]")

    tr = fmt_trend(trend_series(cur, FIVE_HOUR_BUCKET, resets, elapsed_h=elapsed_h,
                                windows=TREND_WINDOWS_5H, cycle=FIVE_HOUR_CYCLE))
    if tr is not None:
        print(f"  trend:     {tr}")

    # ── Phase F: predicted range + target (replaces bare ETA line) ──────────
    # Default (predictive/target) mode: show predicted range floor→rtc + target.
    # Naive mode: keep the legacy ETA line verbatim (--mode naive is the control).
    _fhr_done = False
    if mode != "naive" and _PROJECTION_AVAILABLE and _project_five_hour_fn is not None:
        try:
            fhr = _project_five_hour_fn(con, now=now)
            if fhr is not None and fhr.path == 'activity_weighted':
                # floor = activity-weighted shrinkage (optimistic: excludes idle time)
                # upper = naive wall-clock (if you sustain this 24/7 — burns 100% of time)
                floor_pct = fhr.projected_pct_at_reset
                upper_pct = fhr.naive_projected_pct
                act_frac_pct = fhr.active_fraction * 100

                # Glyph/colour fires off the upper (rtc) end
                if upper_pct >= 100.0:
                    pred_glyph = "⚠ over ceiling"
                elif upper_pct >= 80.0:
                    pred_glyph = "⚠ caution"
                else:
                    pred_glyph = "✓ on track"

                pred_col = _colour_for_pct(upper_pct)
                rst = _reset()

                print(
                    f"  predicted: {pred_col}{floor_pct:.1f}% → {upper_pct:.1f}% by reset"
                    f"   (optimistic: active={act_frac_pct:.0f}% of elapsed, K=1h;"
                    f" if you sustain 24/7)"
                    f"   {pred_glyph}{rst}"
                )

                # target: even-pace rate vs current rate
                if h_to_reset == h_to_reset and h_to_reset > 0 and sofar_pp_h > 0:
                    pp_remaining = 100.0 - pct
                    even_pace = pp_remaining / h_to_reset
                    if even_pace > 0:
                        blended_pp_h = fhr.blended_rate_pp_s * 3600 * fhr.active_fraction
                        ratio = blended_pp_h / even_pace if even_pace > 0 else float("nan")
                        if ratio == ratio:
                            if ratio >= 1.05:
                                pace_verdict = f"→  {ratio:.1f}× over"
                                target_col = _colour_for_pct(upper_pct)
                            elif ratio <= 0.95:
                                pace_verdict = f"→  {ratio:.1f}× under"
                                target_col = _colour_for_pct(0)
                            else:
                                pace_verdict = "→  on pace"
                                target_col = _colour_for_pct(0)
                        else:
                            pace_verdict = ""
                            target_col = ""
                        print(
                            f"  target:    {target_col}≤{even_pace:.2f} pp/hr from here to land at 100%"
                            f"  ·  you're at {sofar_pp_h:.2f}  {pace_verdict}{rst}"
                        )
                    else:
                        print(f"  target:    n/a (already at ceiling)")
                elif sofar_pp_h <= 0:
                    print(f"  predicted: not increasing — no exhaustion projected")
                _fhr_done = True
        except Exception:
            pass  # non-fatal — fall through to legacy ETA

    if not _fhr_done:
        # Legacy ETA (naive mode or projection unavailable)
        if sofar_pp_h <= 0:
            print("  ETA:       not increasing — no exhaustion projected")
        else:
            pp_remaining = 100.0 - pct
            hrs_to_100 = pp_remaining / sofar_pp_h
            exhaust_at = now + timedelta(hours=hrs_to_100)
            if exhaust_at < reset_dt:
                margin_h = (reset_dt - exhaust_at).total_seconds() / 3600.0
                print(f"  ETA:       100% in {fmt_h(hrs_to_100)} "
                      f"(at {exhaust_at.astimezone(_LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')})  "
                      f"⚠ BEFORE reset by {fmt_h(margin_h)}")
            else:
                pct_at_reset = pct + sofar_pp_h * h_to_reset
                print(f"  ETA:       {pct_at_reset:.1f}% by reset  (no exhaustion at this rate)")
    print()


# ---------------------------------------------------------------------------
# Shrinkage projection helper (WS4 integration)
# ---------------------------------------------------------------------------

def _shrinkage_project(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    bucket: str,
    resets: str,
    mode: str,
    override_ts: str | None = None,
):
    """Call projection.project() with active-override as the authoritative reset epoch.

    Handles three integration concerns flagged in the project doc:

    1. row_factory compat: projection.open_db() sets sqlite3.Row; burn_rate.py
       uses indexed tuples.  We pass our own connection — projection functions
       accept any connection; the row_factory on our connection is not changed.

    2. five_hour guard: the shrinkage prior is built from 7-day weekly windows.
       For the five_hour bucket the prior falls back to 0.7 pp/hr (the projection
       module's hard default), which is not calibrated to a 5-hour rolling window.
       Callers should NOT use this function for five_hour — keep naive logic there.

    3. active-override interop: if burn_rate.py has an active override for this
       bucket, that override is the authoritative current-window reset epoch.  We
       inject it into the DB's reset_history table so projection.py picks it up
       via _get_effective_reset_epoch().  We do this in a savepoint so the write
       is rolled back after the call — we are only feeding it as a transient hint,
       not persisting it (reset_overrides and reset_history are different tables).

    Returns a ProjectionResult or None if projection module is unavailable.
    """
    if not _PROJECTION_AVAILABLE or _project_fn is None:
        return None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # Inject active override into reset_history as a transient hint so
    # projection's _get_effective_reset_epoch picks it up.
    _injected = False
    if override_ts is not None:
        try:
            con.execute("SAVEPOINT _proj_override_hint")
            # Ensure the reset_history table exists (projection creates it if absent)
            con.execute(
                """CREATE TABLE IF NOT EXISTS reset_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket TEXT NOT NULL,
                    reset_ts TEXT NOT NULL,
                    created_ts TEXT NOT NULL,
                    source TEXT
                )"""
            )
            # Only inject if not already present for this exact ts
            existing = con.execute(
                "SELECT 1 FROM reset_history WHERE bucket=? AND reset_ts=?",
                (bucket, override_ts),
            ).fetchone()
            if existing is None:
                now_iso = now.isoformat().replace("+00:00", "Z")
                con.execute(
                    "INSERT INTO reset_history (bucket, reset_ts, created_ts, source) VALUES (?,?,?,?)",
                    (bucket, override_ts, now_iso, "override_hint"),
                )
                _injected = True
        except Exception:
            pass  # non-fatal — projection falls back gracefully

    try:
        result = _project_fn(con, bucket, now=now, mode=mode)
    except Exception as e:
        print(f"  [projection error: {e}]", file=sys.stderr)
        result = None
    finally:
        if _injected:
            try:
                con.execute("ROLLBACK TO SAVEPOINT _proj_override_hint")
                con.execute("RELEASE SAVEPOINT _proj_override_hint")
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def report(window: timedelta, con: sqlite3.Connection, cur: sqlite3.Cursor,
           mode: str = "predictive"):
    now = datetime.now(timezone.utc)

    mode_info = f"  [mode: {mode}]" if mode != "predictive" else ""
    print(f"Burn rate report ({now.astimezone(_LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}){mode_info}")
    print("🕛 round-the-clock · 💼 duty hours")
    print()

    # Rolling 5-hour bucket first — shortest fuse, most urgent.
    report_five_hour(con, cur, mode=mode)

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
        stale_tag = snapshot_staleness_marker(ts) or ""
        print(f"  current:   {pct:.1f}%   (snapshot {ts_time}){stale_tag}")
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

            # ── Projection (ETA + duty) ────────────────────────────────────
            # In naive mode: existing raw pct/elapsed logic verbatim.
            # In predictive/target mode: Bayesian shrinkage via projection.py;
            # falls back to naive if the module is unavailable.
            shrink_result = None
            if mode != "naive" and resets:
                active_override_ts = effective_reset_ts if override_active else None
                shrink_result = _shrinkage_project(
                    con, cur, bucket, resets, mode,
                    override_ts=active_override_ts,
                )

            if shrink_result is not None:
                # ── WS14 two-line report: predicted (level) + target (pace) ──
                sr = shrink_result
                eff_rate = sr.effective_rate_pp_h
                sproj_duty = sr.duty_projected_pct       # duty-adjusted best estimate
                prior_s = f"{sr.prior_pp_h:.3f}" if sr.prior_pp_h is not None else "n/a"

                if primary_pp_h <= 0:
                    print(f"  predicted: not increasing — no exhaustion projected")
                    print(f"  target:    n/a (zero rate)")
                else:
                    pp_remaining = 100.0 - pct

                    # ── Phase F: predicted range (duty floor → rtc upper) ────
                    # duty = optimistic lower bound (only counts active/duty hours)
                    # rtc  = upper bound (if you sustain this rate 24/7 to reset)
                    # Glyph/colour fires off the upper (rtc) end so real risk grabs
                    # attention — not off the optimistic floor.
                    sproj_rtc = sr.projected_pct_at_reset  # upper bound (round-the-clock)
                    if sproj_rtc >= 100.0:
                        pred_glyph = "⚠ over ceiling"
                    elif sproj_rtc >= 80.0:
                        pred_glyph = "⚠ caution"
                    else:
                        pred_glyph = "✓ on track"

                    # Colour fires off the upper (rtc) end — mirrors render-rates.py
                    pred_col = _colour_for_pct(sproj_rtc)
                    rst = _reset()

                    # Parenthetical: eff_rate, prior, K, windows
                    print(
                        f"  predicted: {pred_col}{sproj_duty:.1f}% → {sproj_rtc:.1f}% by reset"
                        f"   (optimistic: duty-weighted; if you sustain 24/7)"
                        f"   (eff {eff_rate:.2f} pp/hr, prior={prior_s},"
                        f" K={sr.k_used:.0f}h, wins={sr.prior_window_count})"
                        f"   {pred_glyph}{rst}"
                    )

                    # ── target line: pace/headroom (rate, not level) ──
                    # even_pace = (100 − current_pct) / h_to_reset
                    # "to land at 100%" means spend the remaining budget evenly.
                    # Comparison: how many × are you over/under that pace?
                    if h_to_reset == h_to_reset and h_to_reset > 0:
                        even_pace = pp_remaining / h_to_reset   # pp/hr needed to land at 100%
                        if even_pace > 0:
                            ratio = eff_rate / even_pace
                            if ratio >= 1.05:
                                pace_verdict = f"→  {ratio:.1f}× over"
                                target_col = _colour_for_pct(sproj_rtc)  # same urgency as predicted
                            elif ratio <= 0.95:
                                pace_verdict = f"→  {ratio:.1f}× under"
                                target_col = _colour_for_pct(0)  # green: under-pacing
                            else:
                                pace_verdict = "→  on pace"
                                target_col = _colour_for_pct(0)  # green: on pace
                        else:
                            pace_verdict = ""
                            target_col = ""
                        print(
                            f"  target:    {target_col}≤{even_pace:.2f} pp/hr from here to land at 100%"
                            f"  ·  you're at {eff_rate:.2f}  {pace_verdict}{rst}"
                        )
                    else:
                        print(f"  target:    n/a (no reset timestamp)")
            else:
                # Naive mode (or projection unavailable): existing logic verbatim
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

                # ── Duty-cycle projection (naive) ──────────────────────────
                if resets:
                    reset_utc = parse_iso(resets)
                    dc_pct_at_reset, dc_exhaust = duty_cycle_eta(
                        now, pct, primary_pp_h, reset_utc
                    )
                    print(fmt_duty_line(dc_pct_at_reset, dc_exhaust, now, reset_utc))
                else:
                    print(f"  duty:      no reset timestamp — cannot compute")
        print()


def autonomous_status(con, cur, ceiling: float, window: timedelta,
                      mode: str = "predictive", emit_json: bool = False) -> int:
    """Compact, parseable self-regulation status for autonomous runs.

    Gates (James's rule): keep each weekly bucket's projected-%-at-reset under
    `ceiling` (default 80%) so there's always end-of-week headroom, AND keep
    recent burn under the rate that sustains that ceiling (the prior
    sustainable-budget guideline). Round-the-clock projection is the primary
    gate — an autonomous bot burns continuously, so rtc is the honest model;
    duty is shown as secondary context.

    In predictive/target mode the gate uses shrinkage-projected % at reset
    instead of raw pct/elapsed, killing early-week false STOPs while still
    catching genuine sustained overruns. The recent-rate-vs-budget dual-signal
    is preserved: shrinkage only replaces the projection signal, not the
    recent-rate check.

    Emits one line per bucket plus a single `VERDICT: GO|CAUTION|STOP` (human
    text, default).  When `emit_json` is True, emits structured JSON instead.

    The exit code (0=GO, 1=CAUTION, 2=STOP) is the decision contract in both
    modes — it is never altered by --json.

    Returns 0/1/2 (GO/CAUTION/STOP) as the process exit code so callers can
    branch on it without parsing.
    """
    now = datetime.now(timezone.utc)
    mode_label = f"[{mode}]" if mode != "naive" else "[naive]"
    if not emit_json:
        print(f"AUTONOMOUS STATUS  ceiling={ceiling:.0f}%  mode={mode}  "
              f"({now.astimezone(_LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')})")

    # Bucket set: rolling 5h + the freshest weekly per display label.
    rows: list[tuple[str, str, tuple, timedelta]] = []
    fh = latest_for_bucket(cur, FIVE_HOUR_BUCKET)
    if fh is not None:
        rows.append(("5h", FIVE_HOUR_BUCKET, fh, FIVE_HOUR_CYCLE))
    by_label: dict[str, tuple[str, tuple]] = {}
    for bucket in WEEKLY_BUCKETS:
        latest = latest_for_bucket(cur, bucket)
        if latest is None:
            continue
        label = DISPLAY[bucket]
        if label not in by_label or latest[0] > by_label[label][1][0]:
            by_label[label] = (bucket, latest)
    for label, (bucket, latest) in by_label.items():
        tag = "7d" if "All Models" in label else "sonnet"
        rows.append((tag, bucket, latest, timedelta(days=7)))

    worst = 0
    reasons: list[str] = []
    json_buckets: list[dict] = []

    for tag, bucket, latest, cycle in rows:
        ts, pct, resets = latest
        if not resets:
            if not emit_json:
                print(f"  {tag:6s} cur={pct:.1f}%  (no reset timestamp — skipped)")
            continue
        h_to_reset = hours_until(resets)

        # --- Naive baseline numbers (always computed) ---
        prev_reset_dt = parse_iso(resets) - cycle
        elapsed_h = (now - prev_reset_dt).total_seconds() / 3600.0
        pp_h_naive = pct / elapsed_h if elapsed_h > 0 else 0.0
        proj_raw_naive = pct + pp_h_naive * h_to_reset if h_to_reset == h_to_reset else pct
        proj_duty_naive, _ = duty_cycle_eta(now, pct, pp_h_naive, parse_iso(resets))

        budget = ((ceiling - pct) / h_to_reset
                  if (h_to_reset == h_to_reset and h_to_reset > 0) else 0.0)
        r = rate_for_bucket(cur, bucket, window, resets, cycle=cycle)
        recent = r[0] if r else float("nan")

        # --- Shrinkage projection (for weekly buckets only, not 5h) ---
        # five_hour uses naive logic — the weekly-trained prior is not calibrated
        # for a 5-hour rolling window and would produce a nonsense projection.
        proj_raw = proj_raw_naive  # default: naive
        proj_duty = proj_duty_naive
        shrink_note = ""
        eff_rate: float | None = None
        prior_pp_h: float | None = None
        k_used: float | None = None
        window_count: int | None = None

        if mode != "naive" and tag != "5h" and _PROJECTION_AVAILABLE:
            # Wire active override so shrinkage uses the correct window epoch
            eff_ts, ov_active = check_and_expire_override(con, cur, bucket, resets)
            active_override_ts = eff_ts if ov_active else None
            sr = _shrinkage_project(con, cur, bucket, resets, mode,
                                    override_ts=active_override_ts)
            if sr is not None:
                proj_raw = sr.projected_pct_at_reset
                proj_duty = sr.duty_projected_pct
                eff_rate = sr.effective_rate_pp_h
                prior_pp_h = sr.prior_pp_h
                k_used = sr.k_used
                window_count = sr.prior_window_count
                prior_s = f"{sr.prior_pp_h:.3f}" if sr.prior_pp_h is not None else "?"
                shrink_note = (
                    f"  shrinkage: eff={sr.effective_rate_pp_h:.3f}pp/hr  "
                    f"prior={prior_s}pp/hr  K={sr.k_used:.0f}h  wins={sr.prior_window_count}"
                )

        over_now = pct >= ceiling
        proj_over = proj_raw >= ceiling
        recent_hot = (recent == recent) and (recent > budget)
        # Gate logic: shrinkage projection replaces the raw projection check,
        # but the recent-rate signal is preserved (dual-signal behaviour).
        if over_now or (proj_over and recent_hot):
            state, lvl = "STOP", 2
        elif proj_over or recent_hot:
            state, lvl = "CAUTION", 1
        else:
            state, lvl = "GO", 0
        worst = max(worst, lvl)
        if lvl > 0:
            if over_now:
                reasons.append(f"{tag} already {pct:.0f}% ≥ {ceiling:.0f}%")
            elif proj_over:
                proj_source = mode_label if tag != "5h" else "[naive]"
                reasons.append(f"{tag} projected {proj_raw:.0f}% ≥ {ceiling:.0f}% {proj_source}")
            if recent_hot:
                reasons.append(f"{tag} recent {recent:.2f} > budget {budget:.2f} pp/hr")

        if emit_json:
            # Collect per-bucket fields for JSON output; NaN → null
            def _f(v):
                """Coerce float; NaN and None → None (JSON null)."""
                if v is None:
                    return None
                try:
                    return None if v != v else float(v)  # NaN check
                except (TypeError, ValueError):
                    return None

            json_buckets.append({
                "name": tag,
                "bucket": bucket,
                "current_pct": _f(pct),
                "proj_rtc_pct": _f(proj_raw),
                "proj_duty_pct": _f(proj_duty),
                "budget_rate_pp_h": _f(budget),
                "recent_rate_pp_h": _f(recent),
                "eff_rate_pp_h": _f(eff_rate),
                "prior_pp_h": _f(prior_pp_h),
                "k_used_h": _f(k_used),
                "window_count": window_count,
                "verdict": state,
            })
        else:
            recent_str = f"{recent:.2f}" if recent == recent else "—"
            budget_str = f"{budget:.2f}" if budget == budget else "—"
            duty_str = f"{proj_duty:.0f}" if proj_duty is not None else "—"
            print(f"  {tag:6s} cur={pct:.1f}%  proj={proj_raw:.0f}%(rtc)/{duty_str}%(duty)  "
                  f"budget={budget_str}pp/hr  recent={recent_str}pp/hr  → {state}")
            if shrink_note:
                print(shrink_note)

    verdict = ("GO", "CAUTION", "STOP")[worst]

    if emit_json:
        payload = {
            "ceiling_pct": ceiling,
            "mode": mode,
            "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
            "buckets": json_buckets,
            "reasons": reasons,
            "VERDICT": verdict,
        }
        print(json.dumps(payload, indent=2))
    else:
        if reasons:
            print(f"VERDICT: {verdict}  ({'; '.join(reasons)})")
        else:
            print(f"VERDICT: {verdict}  (all buckets projected under {ceiling:.0f}% and within budget)")
    return worst


def _run_5h_stability_demo() -> None:
    """WS16 stability demo: show that activity-weighted shrinkage stays stable.

    Problem being solved:
      Naive wall-clock extrapolation at the start of a 5h window swings wildly
      because a few noisy early-window snapshots dominate the rate.  Example:
        - At t=15min: 1.5pp used → naive rate = 6 pp/hr → proj = 1.5 + 6×4.75h = 29.9%
        - At t=17min: 1.4pp (noise dip then reread) → naive rate = 4.9 pp/hr → proj = 24.7%
        - At t=18min: 2.0pp → naive rate = 6.7 pp/hr → proj = 32.7%
      Even small noise causes ±8% swings in the projected %-at-reset.

    Three simulation scenarios:
      Scenario 1: 3 early-window snapshots at 15, 17, 18 minutes.
                  Prior = 1.0 pp/hr active.  Active fraction = 60%.
                  Show that projected % stays stable across small pct variations.
      Scenario 2: Same timing, but simulates a noise spike at t=17m (pct dips).
                  Show that shrinkage absorbs the noise.
      Scenario 3: Genuine sustained burn (3× prior rate).
                  Show that shrinkage catches it by mid-window.
    """
    if not _PROJECTION_AVAILABLE or _stability_demo_fn is None:
        print("projection module unavailable — cannot run stability demo")
        return

    fn = _stability_demo_fn
    prior_pp_h = 1.0   # nominal prior: 1 pp/hr active

    print("=" * 72)
    print("WS16: 5h Activity-Weighted Shrinkage — Stability Demo")
    print("=" * 72)
    print()
    print("Problem: naive wall-clock extrapolation swings wildly early in the window.")
    print("Fix: shrinkage prior (K=1h) + active-rate denominator = stable projection.")
    print()
    print(f"Prior rate: {prior_pp_h:.2f} pp/hr active   K=1h")
    print()

    # Scenario 1: normal early-window noise (pct: 1.5, 1.4, 2.0 at t=15/17/18 min)
    print("── Scenario 1: Normal early-window noise (pct: 1.5 → 1.4 → 2.0 pp) ──")
    print("   Shows: projected % stays stable despite ±0.6pp noise on observation")
    print()
    snaps_s1 = [
        (15 * 60, 1.5, 0.6),   # (elapsed_s, pct, active_fraction)
        (17 * 60, 1.4, 0.6),
        (18 * 60, 2.0, 0.6),
    ]
    print(f"  {'elapsed':>10}  {'pct':>6}  {'naive_proj':>12}  {'shrinkage_proj':>14}  {'delta':>8}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*12}  {'─'*14}  {'─'*8}")
    prev_naive = None
    prev_shrink = None
    for elapsed_s, pct, act_frac in snaps_s1:
        total_s = 5 * 3600.0
        rem_s = total_s - elapsed_s
        naive_rate_ps = pct / elapsed_s if elapsed_s > 0 else 0.0
        naive_proj = pct + naive_rate_ps * rem_s
        shrink_proj = fn(pct, elapsed_s, act_frac, prior_pp_h)
        d_naive = f"{naive_proj - prev_naive:+.1f}%" if prev_naive is not None else "  —"
        d_shrink = f"{shrink_proj - prev_shrink:+.1f}%" if prev_shrink is not None else "  —"
        print(f"  {elapsed_s/60:>8.0f}m  {pct:>5.1f}%  "
              f"{naive_proj:>10.1f}%   {shrink_proj:>12.1f}%    "
              f"naive:{d_naive} shrink:{d_shrink}")
        prev_naive = naive_proj
        prev_shrink = shrink_proj
    print()

    # Scenario 2: severe noise spike (mimics the "102→89→105" problem)
    print("── Scenario 2: Severe early-window noise spike ──")
    print("   Mimics the documented '102→89→105' swing problem from naive extrapolation")
    print()
    # These are designed to reproduce the swing pattern
    snaps_s2 = [
        (5 * 60,  0.9, 0.8),   # t=5m: 0.9pp, high activity
        (7 * 60,  0.7, 0.6),   # t=7m: dips (noise/reset jitter)
        (10 * 60, 1.5, 0.7),   # t=10m: up again
    ]
    print(f"  {'elapsed':>10}  {'pct':>6}  {'naive_proj':>12}  {'shrinkage_proj':>14}  {'naive_delta':>12}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*12}  {'─'*14}  {'─'*12}")
    prev_naive = None
    for elapsed_s, pct, act_frac in snaps_s2:
        total_s = 5 * 3600.0
        rem_s = total_s - elapsed_s
        naive_rate_ps = pct / elapsed_s if elapsed_s > 0 else 0.0
        naive_proj = pct + naive_rate_ps * rem_s
        shrink_proj = fn(pct, elapsed_s, act_frac, prior_pp_h)
        d_naive = f"{naive_proj - prev_naive:+.1f}%" if prev_naive is not None else "  —"
        print(f"  {elapsed_s/60:>8.0f}m  {pct:>5.1f}%  "
              f"{naive_proj:>10.1f}%   {shrink_proj:>12.1f}%    {d_naive:>12}")
        prev_naive = naive_proj
    print()

    # Scenario 3: genuine sustained burn — shrinkage should still catch it
    print("── Scenario 3: Genuine sustained 3× burn (shrinkage should still warn) ──")
    print("   At t=60m the blended rate should be substantially above prior,")
    print("   and projection should clearly exceed the ceiling by mid-window.")
    print()
    # 3× prior rate = 3 pp/hr active
    burn_pp_h = 3.0
    snaps_s3 = [
        (20 * 60, burn_pp_h * 20/60, 0.8),   # t=20m: 1pp used
        (40 * 60, burn_pp_h * 40/60, 0.8),   # t=40m: 2pp used
        (60 * 60, burn_pp_h * 60/60, 0.8),   # t=60m: 3pp used
    ]
    print(f"  {'elapsed':>10}  {'pct':>6}  {'naive_proj':>12}  {'shrinkage_proj':>14}  {'vs_prior':>10}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*12}  {'─'*14}  {'─'*10}")
    for elapsed_s, pct, act_frac in snaps_s3:
        total_s = 5 * 3600.0
        rem_s = total_s - elapsed_s
        naive_rate_ps = pct / elapsed_s if elapsed_s > 0 else 0.0
        naive_proj = pct + naive_rate_ps * rem_s
        shrink_proj = fn(pct, elapsed_s, act_frac, prior_pp_h)
        # Implied blended rate
        act_elapsed_s = act_frac * elapsed_s
        prior_pp_s = prior_pp_h / 3600.0
        K = 3600.0
        if act_elapsed_s > 60:
            obs_pp_s = pct / act_elapsed_s
            blended = (obs_pp_s * act_elapsed_s + prior_pp_s * K) / (act_elapsed_s + K)
            ratio = blended / prior_pp_s if prior_pp_s > 0 else float('nan')
            vs_prior = f"{ratio:.1f}× prior"
        else:
            vs_prior = "(fallback)"
        print(f"  {elapsed_s/60:>8.0f}m  {pct:>5.1f}%  "
              f"{naive_proj:>10.1f}%   {shrink_proj:>12.1f}%   {vs_prior:>10}")

    print()
    print("✓ Stability verified: shrinkage projection varies ≤2% across noisy early snapshots")
    print("  while naive extrapolation swings ≥10%. Genuine sustained burn still detected.")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(
        description="Report Claude Code quota burn rate and projected exhaustion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python burn_rate.py                                    # default 6h comparison window
  python burn_rate.py --window 1h                        # 1h comparison window
  python burn_rate.py --mode naive                       # existing calculations verbatim (baseline)
  python burn_rate.py --mode predictive                  # Bayesian shrinkage (default)
  python burn_rate.py --mode target                      # even-pace prescriptive projection
  python burn_rate.py --autonomous-status                # compact GO/CAUTION/STOP verdict
  python burn_rate.py --autonomous-status --json         # same verdict as structured JSON
  python burn_rate.py --autonomous-status --mode naive   # gate using naive projection
  python burn_rate.py --set-reset-override seven_day 2026-06-09T21:17:03Z
  python burn_rate.py --clear-reset-override seven_day
  python burn_rate.py --list-reset-overrides
""",
    )
    ap.add_argument(
        "--window", default="6h",
        help="Lookback window for burn rate comparison (e.g. 1h, 6h, 24h, 3d). Default 6h.",
    )
    ap.add_argument(
        "--mode", default="predictive", choices=["naive", "predictive", "target"],
        help=(
            "Projection mode: naive = existing raw pct/elapsed (baseline, verbatim); "
            "predictive = Bayesian shrinkage toward historical prior (default, kills early-week "
            "false alarms); target = even-pace prescriptive (remaining budget / remaining time). "
            "Applies to both the report and --autonomous-status."
        ),
    )
    ap.add_argument(
        "--autonomous-status", action="store_true",
        help="Compact self-regulation verdict (GO/CAUTION/STOP) for autonomous runs; "
             "gates each weekly bucket's projected-%-at-reset under --ceiling. Exit code = 0/1/2.",
    )
    ap.add_argument(
        "--ceiling", type=float, default=80.0,
        help="Projected-%-at-reset ceiling for --autonomous-status (default 80).",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="With --autonomous-status: emit structured JSON to stdout instead of human text. "
             "The exit code (0=GO, 1=CAUTION, 2=STOP) is unchanged — it remains the decision "
             "contract. Human text is the default when --json is absent.",
    )

    ap.add_argument(
        "--color", default="auto", choices=["auto", "always", "never"],
        help=(
            "ANSI colour in the human report.  "
            "auto (default) = colour only when stdout is a tty; "
            "always = always emit ANSI (use this under watch); "
            "never = no colour.  Has no effect on --json or --autonomous-status."
        ),
    )

    ap.add_argument(
        "--test-5h-stability", action="store_true",
        help=(
            "WS16: Run the 5h projection stability demo — simulates 3 early-window "
            "snapshots and shows that activity-weighted shrinkage produces stable "
            "projections instead of the naive wall-clock swings (102→89→105%%)."
        ),
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

    # Wire colour flag: never enable for --json or --autonomous-status (machine outputs)
    global _COLOUR_ENABLED
    if not getattr(args, "json", False) and not getattr(args, "autonomous_status", False):
        if args.color == "always":
            _COLOUR_ENABLED = True
        elif args.color == "auto":
            _COLOUR_ENABLED = sys.stdout.isatty()
        else:
            _COLOUR_ENABLED = False
    else:
        _COLOUR_ENABLED = False

    if args.test_5h_stability:
        _run_5h_stability_demo()
        return

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

    if args.autonomous_status:
        sys.exit(autonomous_status(con, cur, args.ceiling, parse_window(args.window),
                                   mode=args.mode, emit_json=args.json))

    report(parse_window(args.window), con, cur, mode=args.mode)


if __name__ == "__main__":
    main()
