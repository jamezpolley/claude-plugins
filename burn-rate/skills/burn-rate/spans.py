#!/usr/bin/env python3
"""spans.py — canonical span extraction for the burn-rate prior.

Phase A (WS10 + WS11 + stale-epoch fix):
  - Wrinkle 0: canonicalise near-coincident resets_at labels (±120s)
  - Wrinkle 1: discard personal-account spans (reset not Sat 00:00 UTC)
  - Wrinkle 2: split at mid-week reset_history breaks
  - Wrinkle 3: censored (capped) spans end at the cap instant, not the week boundary
  - Data hygiene: running-max within each (generation, source) group to suppress
    cross-source jitter and stale late-arriving old-label readings
  - WS10 thin-span rule: exclude a span whose first reading is >THIN_LAG_H in
    and already at >THIN_PCT_THRESHOLD (the S1 pattern — left-truncation, never
    saw the climb)
  - Stale-epoch fix: _get_effective_reset_epoch uses
      epoch = max([history_boundaries ≤ now] + [resets_at − 7d])
    so the presumed weekly boundary is a peer candidate, not just a fallback

Public API:
  extract_spans(conn, bucket, now)   -> list[Span]
  get_effective_epoch(conn, bucket, resets_at_dt, now)  -> datetime
  pooled_prior(spans, weight_fn)     -> float   (WS11 duration-weighted pool)

Span.status values: 'work', 'censored', 'in_progress'
Span.exclude_reason: non-None means the span should NOT feed the prior:
  'personal'    — Wrinkle 1 personal account
  'thin'        — WS10 left-truncation
  'in_progress' — current incomplete window
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = "/home/james/.claude/state/usage-log.sqlite"

GARBAGE_RESETS = frozenset({"2286-11-20T17:46:39Z"})

# Wrinkle 0: collapse resets_at values within this many seconds of each other
RESET_MERGE_TOLERANCE_S = 120

# Wrinkle 3: a span is censored (capped) if its running-max pct reaches this
CENSORED_PCT_THRESHOLD = 98.0

# WS10: thin-span exclusion rule (interim, until more data accrues)
# Exclude a span whose first reading arrives > THIN_LAG_H hours into the span
# and is already at > THIN_PCT_THRESHOLD percent.  This is the S1 pattern:
# left-truncated — we never saw the climb, only the tail at 94%.
THIN_LAG_H = 24.0           # first reading arrived > 24h after span start
THIN_PCT_THRESHOLD = 50.0   # and was already at > 50% when it arrived


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Span:
    """One canonical quota window.

    For a split week (Wrinkle 2), each sub-span is its own Span object.
    For a censored week (Wrinkle 3), end_ts is the cap instant (not the
    weekly boundary).

    The rate field is Δpp / span_h where:
      - Δpp   = running-max pct inside this span (pct ≡ 0 at span start)
      - span_h = (end_ts − start_ts).total_seconds() / 3600   (boundary-to-boundary)

    For in-progress spans (is_completed=False), end_ts is `now` and span_h
    is the elapsed time so far.
    """
    # Identity
    resets_at: datetime          # canonical reset label for this span's generation
    start_ts: datetime           # span start (fixed boundary, never a reading ts)
    end_ts: datetime             # span end (fixed boundary, cap instant, or now)

    # Values
    delta_pp: float              # running-max pct over [start_ts, end_ts]
    span_h: float                # (end_ts - start_ts) in hours
    rate_pp_h: float             # delta_pp / span_h

    # Readings
    first_reading_ts: Optional[datetime] = None    # earliest reading ts in this span
    first_reading_pct: Optional[float]  = None     # pct at first reading
    last_reading_ts: Optional[datetime]  = None    # latest reading ts

    # Status flags
    is_censored: bool = False       # hit cap during this span (Wrinkle 3)
    is_completed: bool = True       # False = current in-progress window
    split_index: Optional[int] = None   # 1-based part index if the week was split
    split_total: Optional[int] = None   # total parts if split

    # Exclusion
    exclude_reason: Optional[str] = None   # 'personal' | 'thin' | 'in_progress' | None

    @property
    def prior_eligible(self) -> bool:
        """True if this span should feed the predictive prior."""
        return (
            self.exclude_reason is None
            and self.is_completed
            and self.span_h > 0
            and self.rate_pp_h >= 0
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_saturday_midnight_utc(dt: datetime) -> bool:
    """Work quota resets Sat 00:00 UTC.  8-minute tolerance for server wobble."""
    dt = _utc(dt)
    mins = dt.hour * 60 + dt.minute
    near_midnight = (mins <= 8) or (mins >= (24 * 60 - 8))
    return dt.weekday() == 5 and near_midnight   # Saturday in Python = 5


# ---------------------------------------------------------------------------
# Wrinkle 0: canonicalise near-coincident resets_at labels
# ---------------------------------------------------------------------------

def _canonicalise_resets_at(raw_labels: list[str]) -> dict[str, datetime]:
    """Return {raw_label: canonical_datetime} collapsing labels within 120s.

    For each cluster, the canonical value is the one nearest to Sat 00:00 UTC
    (midnight-distance in minutes).  This folds e.g. '2026-06-12T23:59:00Z'
    into '2026-06-13T00:00:00Z'.
    """
    parsed: list[tuple[str, datetime]] = []
    for ra in raw_labels:
        if ra in GARBAGE_RESETS:
            continue
        try:
            parsed.append((ra, _parse(ra)))
        except (ValueError, OverflowError):
            continue

    parsed.sort(key=lambda x: x[1])

    groups: list[list[tuple[str, datetime]]] = []
    for raw, dt in parsed:
        placed = False
        for grp in groups:
            if abs((dt - grp[0][1]).total_seconds()) <= RESET_MERGE_TOLERANCE_S:
                grp.append((raw, dt))
                placed = True
                break
        if not placed:
            groups.append([(raw, dt)])

    result: dict[str, datetime] = {}
    for grp in groups:
        # Pick the representative closest to Sat 00:00 UTC midnight
        def _midnight_dist(item: tuple[str, datetime]) -> float:
            d = item[1]
            mins = d.hour * 60 + d.minute + d.second / 60.0
            return min(mins, 24 * 60 - mins)

        canon_dt = min(grp, key=_midnight_dist)[1]
        for raw, _ in grp:
            result[raw] = canon_dt

    return result


# ---------------------------------------------------------------------------
# Reset history
# ---------------------------------------------------------------------------

def _get_reset_history(conn: sqlite3.Connection, bucket: str) -> list[datetime]:
    """All known reset boundaries from reset_history, sorted ascending."""
    try:
        rows = conn.execute(
            "SELECT reset_ts FROM reset_history WHERE bucket=? ORDER BY reset_ts ASC",
            (bucket,),
        ).fetchall()
        result = []
        for (ts,) in rows:
            try:
                result.append(_parse(ts))
            except (ValueError, OverflowError):
                pass
        return result
    except sqlite3.OperationalError:
        return []   # table doesn't exist yet


# ---------------------------------------------------------------------------
# Running-max per generation (data hygiene)
# ---------------------------------------------------------------------------

def _running_max_readings(
    readings: list[tuple[datetime, float]]
) -> list[tuple[datetime, float]]:
    """Apply running-max to suppress cross-source jitter and stale late arrivals.

    Within a generation (fixed resets_at canonical label), pct is non-decreasing
    — any drop is either jitter (±1 from statusline vs timer rounding) or a stale
    late-arriving old-generation row.  Running-max turns the time series into a
    proper monotone non-decreasing sequence, which ensures Δpp = max − 0 is the
    true total consumption rather than an artefact of source disagreement.

    Input: [(ts, pct), ...] sorted by ts ASC.
    Output: same shape, values replaced by running max.
    """
    if not readings:
        return []
    result = []
    cur_max = -1.0
    for ts, pct in readings:
        cur_max = max(cur_max, pct)
        result.append((ts, cur_max))
    return result


# ---------------------------------------------------------------------------
# Main span extraction
# ---------------------------------------------------------------------------

def _fetch_7d_stamp_for_row(conn: sqlite3.Connection, bucket: str) -> dict:
    """WS17: Return {snapshot_ts -> canonical seven_day_resets_at datetime or None}
    for all rows in `bucket` that have a non-NULL seven_day_resets_at stamp.

    Used only for non-seven_day buckets (five_hour, sonnet_weekly) where the
    bucket's own resets_at is not a Saturday anchor.  The stamp is the co-captured
    seven_day resets_at from the same /usage call, which IS the account anchor.

    Returns an empty dict if the column doesn't exist (pre-WS17 schema) or if
    the bucket is seven_day itself (not needed — seven_day uses its own resets_at).
    """
    if bucket == "seven_day":
        return {}
    try:
        col_names = {row[1] for row in conn.execute("PRAGMA table_info(quota_snapshots)")}
        if "seven_day_resets_at" not in col_names:
            return {}
        rows = conn.execute(
            "SELECT snapshot_ts, seven_day_resets_at "
            "FROM quota_snapshots "
            "WHERE bucket=? AND seven_day_resets_at IS NOT NULL",
            (bucket,),
        ).fetchall()
        result = {}
        for ts_s, stamp_s in rows:
            if stamp_s is None:
                continue
            try:
                stamp_dt = _parse(stamp_s)
            except (ValueError, OverflowError):
                continue
            result[ts_s] = stamp_dt
        return result
    except sqlite3.OperationalError:
        return {}


def extract_spans(
    conn: sqlite3.Connection,
    bucket: str,
    now: Optional[datetime] = None,
) -> list[Span]:
    """Extract all canonical spans for `bucket` per the Definitions.

    Returns all spans (work, personal, in-progress).  Caller uses
    Span.prior_eligible or Span.exclude_reason to filter.

    Steps:
    1. Fetch all non-garbage readings, group by (canonical resets_at).
    2. Discard personal-account generations (Wrinkle 1).
       - For seven_day: classify via the row's own resets_at (Sat 00:00 UTC = work).
       - For five_hour/sonnet_weekly (WS17): classify via the stamped
         seven_day_resets_at column (same account signal, co-captured at write time).
         Rows with NULL stamp (pre-WS17) are treated as unclassifiable and excluded;
         this is acceptable because the WS16 5h prior is recent-anchored.
    3. For each work generation, split at any reset_history boundary that falls
       inside the generation's week (Wrinkle 2).
    4. For each sub-span, apply running-max to its readings (data hygiene).
    5. Find cap instant for censored sub-spans (Wrinkle 3).
    6. Apply WS10 thin-span exclusion.
    7. Mark the current (in-progress) span.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # --- 1. Fetch readings ---
    rows = conn.execute(
        "SELECT snapshot_ts, pct_used, resets_at "
        "FROM quota_snapshots "
        "WHERE bucket=? "
        "ORDER BY snapshot_ts ASC",
        (bucket,),
    ).fetchall()

    # Filter garbage
    rows = [(ts, pct, ra) for ts, pct, ra in rows if ra not in GARBAGE_RESETS]
    if not rows:
        return []

    # WS17: for non-seven_day buckets, load the account stamp map
    # {snapshot_ts_str -> canonical seven_day_resets_at datetime}.
    # For seven_day itself this returns {} — not needed.
    stamp_map = _fetch_7d_stamp_for_row(conn, bucket)

    # Wrinkle 0: canonicalise all resets_at labels
    raw_labels = list({ra for _, _, ra in rows})
    canon_map = _canonicalise_resets_at(raw_labels)  # raw -> canonical datetime

    # Group readings by canonical reset label.
    # For non-seven_day buckets: also track the set of seven_day_resets_at stamps
    # seen in each generation (for Wrinkle 1 account classification).
    gen_readings: dict[datetime, list[tuple[datetime, float]]] = {}
    # gen_stamps[canon_dt] = set of canonical stamp datetimes seen (WS17, non-7d only)
    gen_stamps: dict[datetime, set[datetime]] = {}
    for ts_s, pct, ra_s in rows:
        try:
            ts = _parse(ts_s)
        except (ValueError, OverflowError):
            continue
        canon_dt = canon_map.get(ra_s)
        if canon_dt is None:
            continue
        gen_readings.setdefault(canon_dt, []).append((ts, pct))
        # WS17: collect stamps for this generation
        if stamp_map:
            stamp_dt = stamp_map.get(ts_s)
            if stamp_dt is not None:
                gen_stamps.setdefault(canon_dt, set()).add(stamp_dt)

    # Sort readings within each generation by ts
    for canon_dt in gen_readings:
        gen_readings[canon_dt].sort(key=lambda x: x[0])

    # --- Reset history boundaries (for Wrinkle 2 splits) ---
    history_breaks = _get_reset_history(conn, bucket)

    spans: list[Span] = []

    for resets_at_dt, raw_readings in sorted(gen_readings.items()):
        # --- Wrinkle 1: personal account? ---
        if bucket == "seven_day":
            # Seven-day bucket: the row's own resets_at IS the account anchor.
            is_work = _is_saturday_midnight_utc(resets_at_dt)
        else:
            # WS17 — five_hour / sonnet_weekly:
            # Use the stamped seven_day_resets_at values collected for this generation.
            # A generation is work iff at least one stamp classifies as Sat 00:00 UTC.
            # Rows with no stamp (NULL) are pre-WS17 historical rows: treat as
            # unclassifiable (exclude from prior). Dropping them is acceptable —
            # the WS16 5h prior is recent-anchored and the NULL rows are older.
            stamps = gen_stamps.get(resets_at_dt)
            if not stamps:
                # No stamp at all: pre-WS17 historical generation — skip entirely
                # (not personal, not work — just unclassifiable old data).
                continue
            # Apply Wrinkle 0 canonicalisation to stamps before checking Sat-midnight
            raw_stamp_labels = [s.strftime("%Y-%m-%dT%H:%M:%SZ") for s in stamps]
            stamp_canon_map = _canonicalise_resets_at(raw_stamp_labels)
            canon_stamp_dts = set(stamp_canon_map.values())
            is_work = any(_is_saturday_midnight_utc(s) for s in canon_stamp_dts)

        if not is_work:
            # Emit a rejected personal-account span for completeness.
            # For the seven_day bucket the span boundaries are week-aligned.
            # For non-seven_day buckets, the bucket's own resets_at is not a 7d
            # anchor so we fall back to reading extent for the span boundaries.
            pcts = [p for _, p in raw_readings]
            if bucket == "seven_day":
                span_start = resets_at_dt - timedelta(days=7)
                span_end = resets_at_dt
                span_h = 168.0
            else:
                span_start = raw_readings[0][0] if raw_readings else resets_at_dt - timedelta(hours=5)
                span_end = raw_readings[-1][0] if raw_readings else resets_at_dt
                span_h = (span_end - span_start).total_seconds() / 3600.0
            spans.append(Span(
                resets_at=resets_at_dt,
                start_ts=span_start,
                end_ts=span_end,
                delta_pp=max(pcts) if pcts else 0.0,
                span_h=max(span_h, 0.0),
                rate_pp_h=0.0,
                first_reading_ts=raw_readings[0][0] if raw_readings else None,
                first_reading_pct=raw_readings[0][1] if raw_readings else None,
                last_reading_ts=raw_readings[-1][0] if raw_readings else None,
                exclude_reason='personal',
            ))
            continue

        # Work generation: week (or window) boundaries.
        # For all buckets: use resets_at as the end boundary and resets_at - 7d
        # as the start boundary. For the seven_day bucket this is the exact 7-day
        # window. For five_hour/sonnet_weekly the resets_at is a shorter-cycle
        # reset anchor; the 7d lookback overstates the window, but in practice
        # the actual reading density (not the declared boundary) drives the rate
        # calculation — the sub-span loop collects only readings within the window,
        # so an oversized declared window just produces an in-progress span that
        # spans a lot of empty time. This is pre-existing behaviour; WS17 only
        # adds the account-classification filter, not a geometry change.
        week_start = resets_at_dt - timedelta(days=7)
        week_end = resets_at_dt

        # --- Wrinkle 2: find history breaks inside this week ---
        inside_breaks = sorted([
            b for b in history_breaks
            if week_start < b < week_end
        ])
        boundaries = [week_start] + inside_breaks + [week_end]
        n_subs = len(boundaries) - 1

        for sub_i in range(n_subs):
            sub_start = boundaries[sub_i]
            sub_end = boundaries[sub_i + 1]

            # Collect readings that belong to this sub-span:
            # ts >= sub_start and ts < sub_end (or <= for last sub-span in the generation)
            sub_raw = [
                (ts, pct) for ts, pct in raw_readings
                if sub_start <= ts < sub_end
            ]
            # Include readings at sub_end for the last sub-span (the boundary reading)
            if sub_i == n_subs - 1:
                sub_raw = [
                    (ts, pct) for ts, pct in raw_readings
                    if sub_start <= ts <= sub_end
                ]

            # Apply running-max (data hygiene: cross-source jitter + stale arrivals)
            sub_readings = _running_max_readings(sub_raw)

            # Is this sub-span the current in-progress window?
            is_current = (sub_end > now)
            effective_end = now if is_current else sub_end

            if not sub_readings:
                # No readings in this sub-span — skip (no data to build a rate from)
                continue

            # --- Wrinkle 3: censored span — find cap instant ---
            delta_pp = sub_readings[-1][1]   # running-max pct = last value after running-max
            is_censored = (delta_pp >= CENSORED_PCT_THRESHOLD) and not is_current

            if is_censored:
                # Find the first instant the pct reached 100 (or >=98 if 100 not reached)
                cap_ts = None
                for ts, pct in sub_readings:
                    if pct >= 100.0:
                        cap_ts = ts
                        break
                if cap_ts is None:
                    # Reached 98+ but not exactly 100 — use first time >=98
                    for ts, pct in sub_readings:
                        if pct >= CENSORED_PCT_THRESHOLD:
                            cap_ts = ts
                            break
                # End the span at cap_ts; delta_pp is still 100
                if cap_ts is not None:
                    effective_end = cap_ts

            span_h = (effective_end - sub_start).total_seconds() / 3600.0
            if span_h <= 0:
                continue

            rate = delta_pp / span_h if span_h > 0 else 0.0

            # WS10: thin-span exclusion
            first_ts = sub_readings[0][0]
            first_pct = sub_raw[0][1] if sub_raw else sub_readings[0][1]  # before running-max
            lag_h = (first_ts - sub_start).total_seconds() / 3600.0
            is_thin = (lag_h > THIN_LAG_H and first_pct > THIN_PCT_THRESHOLD)

            exclude = None
            if is_current:
                exclude = 'in_progress'
            elif is_thin:
                exclude = 'thin'

            spans.append(Span(
                resets_at=resets_at_dt,
                start_ts=sub_start,
                end_ts=effective_end,
                delta_pp=delta_pp,
                span_h=span_h,
                rate_pp_h=rate,
                first_reading_ts=first_ts,
                first_reading_pct=first_pct,
                last_reading_ts=sub_readings[-1][0],
                is_censored=is_censored,
                is_completed=not is_current,
                split_index=(sub_i + 1) if n_subs > 1 else None,
                split_total=(n_subs if n_subs > 1 else None),
                exclude_reason=exclude,
            ))

    # Sort by span start
    spans.sort(key=lambda s: s.start_ts)
    return spans


# ---------------------------------------------------------------------------
# Stale-epoch fix (WS10 — current-window start)
# ---------------------------------------------------------------------------

def get_effective_epoch(
    conn: sqlite3.Connection,
    bucket: str,
    resets_at_dt: datetime,
    now: Optional[datetime] = None,
) -> datetime:
    """Return the effective start of the current quota window.

    Fix for the stale-epoch bug: the presumed weekly boundary (resets_at − 7d)
    must be a peer candidate in the max(), not just a fallback.

      epoch = max([history boundaries ≤ now] + [resets_at − 7d])

    This ensures that if the most recent reset_history entry is an old mid-week
    break (e.g. Jun-09 21:30) but the actual week started later (Jun-13 00:00
    = resets_at − 7d for Jun-20), the weekly boundary wins.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    history = _get_reset_history(conn, bucket)
    recent_history = [b for b in history if b <= now]

    weekly_boundary = resets_at_dt - timedelta(days=7)

    candidates = recent_history + [weekly_boundary]
    return max(candidates)


# ---------------------------------------------------------------------------
# WS11: duration-weighted pooled prior
# ---------------------------------------------------------------------------

def pooled_prior(
    spans: list[Span],
    weight_fn: Optional[Callable[[Span], float]] = None,
) -> float:
    """Duration-weighted pooled prior from prior-eligible spans.

    Formula (WS11):
        prior = Σ(w_i · Δpp_i) / Σ(w_i · hours_i)

    The recency/decay weight w_i is applied to BOTH the numerator's Δpp and the
    denominator's hours — NOT to the rate directly.  This avoids the
    averaging-averages bug: a 26.5h sliver and a 141.5h full-week contribute in
    proportion to their actual quota evidence, not as two equal-vote rates.

    Split-invariance proof: splitting span A (Δpp=x, h=H) into two sub-spans
    (Δpp1,h1) and (Δpp2,h2) with Δpp1+Δpp2=x, h1+h2=H, same weight w:
        pooled(A only) = w·x / (w·H) = x/H
        pooled(A1+A2)  = (w·Δpp1+w·Δpp2) / (w·h1+w·h2) = x/H  ✓

    weight_fn: callable(span) -> float.  If None, uniform weight 1.0.
    Falls back to FALLBACK_PRIOR if no eligible spans.
    """
    FALLBACK_PRIOR = 0.7  # conservative fallback (pp/hr)

    eligible = [s for s in spans if s.prior_eligible]
    if not eligible:
        return FALLBACK_PRIOR

    if weight_fn is None:
        weight_fn = lambda s: 1.0

    sum_w_pp = 0.0
    sum_w_h = 0.0
    for s in eligible:
        w = weight_fn(s)
        sum_w_pp += w * s.delta_pp
        sum_w_h += w * s.span_h

    if sum_w_h <= 0:
        return FALLBACK_PRIOR

    return sum_w_pp / sum_w_h


# ---------------------------------------------------------------------------
# Verification helper
# ---------------------------------------------------------------------------

def _format_dur(h: float) -> str:
    d = int(h // 24)
    hh = h - 24 * d
    return f"{d}d{hh:4.1f}h" if d else f"{hh:5.1f}h"


def print_span_table(spans: list[Span], now: Optional[datetime] = None) -> None:
    """Print a human-readable span table matching the project-doc S1-S8 format."""
    if now is None:
        now = datetime.now(timezone.utc)
    print(f"now (UTC) = {now.strftime('%Y-%m-%d %H:%M')}")
    hdr = (
        f"{'reset_at (UTC)':17} {'span start':17} {'span end':17} "
        f"{'Δpp':>5} {'timespan':>10} {'pp/hr':>6} {'status / notes'}"
    )
    print(hdr)
    print("-" * len(hdr))
    for s in spans:
        if s.exclude_reason == 'personal':
            status = "REJECT — personal acct (reset not Sat 00:00 UTC)"
            rate_s = "   —"
        elif not s.is_completed:
            status = "IN-PROGRESS — excluded from prior"
            rate_s = f"{s.rate_pp_h:5.3f}"
        else:
            parts = []
            if s.split_index is not None:
                parts.append(f"split {s.split_index}/{s.split_total} (mid-week reset)")
            if s.is_censored:
                parts.append("CENSORED (cap instant)")
            if s.exclude_reason == 'thin':
                lag = (s.first_reading_ts - s.start_ts).total_seconds() / 3600 if s.first_reading_ts else 0
                parts.append(f"EXCLUDE/THIN: first reading {lag:.0f}h late at {s.first_reading_pct:.0f}%")
            if not parts:
                parts.append("KEEP (clean)")
            status = "; ".join(parts)
            rate_s = f"{s.rate_pp_h:5.3f}"

        print(
            f"{s.resets_at.strftime('%Y-%m-%d %H:%M'):17} "
            f"{s.start_ts.strftime('%Y-%m-%d %H:%M'):17} "
            f"{s.end_ts.strftime('%Y-%m-%d %H:%M'):17} "
            f"{s.delta_pp:>5.0f} {_format_dur(s.span_h):>10} {rate_s:>6} {status}"
        )


# ---------------------------------------------------------------------------
# Standalone verification
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc)

    print("=" * 80)
    print(f"spans.py — Phase A verification (WS10 + WS11 + epoch fix)")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 80)

    spans = extract_spans(conn, "seven_day", now)

    print("\n[Span Table]\n")
    print_span_table(spans, now)

    # Pooled prior on eligible spans
    eligible = [s for s in spans if s.prior_eligible]
    prior = pooled_prior(spans)
    print(f"\n[Pooled Prior]")
    print(f"  Eligible spans: {[s.resets_at.strftime('%m-%d') + ('*' if s.is_censored else '') for s in eligible]}")
    total_pp = sum(s.delta_pp for s in eligible)
    total_h = sum(s.span_h for s in eligible)
    print(f"  Σ(Δpp) = {total_pp:.0f}  Σ(span_h) = {total_h:.1f}h")
    print(f"  Prior = {prior:.4f} pp/hr  (target ≈ 0.809)")

    # Stale-epoch fix verification
    print(f"\n[Epoch Fix — current window start]")
    latest = conn.execute(
        "SELECT resets_at FROM quota_snapshots WHERE bucket='seven_day' "
        "AND resets_at NOT IN ('2286-11-20T17:46:39Z') "
        "ORDER BY snapshot_ts DESC LIMIT 1"
    ).fetchone()
    if latest:
        raw_labels = list({latest[0]})
        canon_map = _canonicalise_resets_at(raw_labels)
        resets_at_dt = canon_map.get(latest[0])
        if resets_at_dt:
            epoch = get_effective_epoch(conn, "seven_day", resets_at_dt, now)
            elapsed_h = (now - epoch).total_seconds() / 3600.0
            print(f"  resets_at = {resets_at_dt.strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"  Epoch = {epoch.strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"  Elapsed = {elapsed_h:.1f}h  (target ≈ 10–12h, not 83h)")
            history = _get_reset_history(conn, "seven_day")
            recent = [b for b in history if b <= now]
            weekly = resets_at_dt - timedelta(days=7)
            print(f"  History candidates ≤ now: {[b.strftime('%m-%d %H:%M') for b in recent]}")
            print(f"  Weekly boundary (resets_at-7d): {weekly.strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"  max({[b.strftime('%m-%d %H:%M') for b in recent + [weekly]]}) = {epoch.strftime('%m-%d %H:%M UTC')}")
