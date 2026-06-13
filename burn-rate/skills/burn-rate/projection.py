#!/usr/bin/env python3
"""Bayesian shrinkage projection for Claude Code quota burn rate.

This module (WS4) delivers span-correct, censoring-aware, account-separated
prior computation and three projection modes:
  - naive:       existing calc verbatim (baseline / control)
  - predictive:  shrinkage prior + empirical duty surface (descriptive)
  - target:      even-pace line on de-rationed natural demand (prescriptive)

Public API (for integration step to import):
  compute_prior(conn, bucket)         -> float  (pp/hr, span-correct prior)
  effective_rate(observed, elapsed_h, prior, K=24.0) -> float
  duty_surface(conn, bucket)          -> DutySurface (remaining active capacity)
  project(conn, bucket, now, mode)    -> ProjectionResult

Design notes / choices made under ambiguity:
  - "Work account" detection: a generation is work iff its normalised resets_at
    falls on Saturday 00:00 UTC (day-of-week == 6, hour == 0). The Jun 10 21:00
    generation (Wed 21:00) is personal account, excluded.
  - "Near-coincident reset labels": collapse resets_at values within 2 minutes
    of each other to the canonical Sat 00:00 value (2026-06-12T23:59 and
    2026-06-13T00:00 → 2026-06-13T00:00; 2026-06-19T23:59 and 2026-06-20T00:00
    → 2026-06-20T00:00). Tolerance chosen to catch server-side 1-minute wobble.
  - Garbage row 2286-11-20: filtered out early.
  - Monotonicity-break detection: a drop of >=30 pp within a single resets_at
    generation signals an intra-week reset (e.g. Jun 09 21:17 within the
    2026-06-13 generation). Threshold 30 chosen to exclude noise (1-2 pp drops
    from rounding/snapshot lag). This splits one resets_at generation into
    multiple real quota windows.
  - Censoring: a window is right-censored if its peak_pct >= 98.0 (near-100%)
    OR it ended in a mid-week reset (not a Sat 00:00 reset). In censored windows
    the true rate is >= final_pct / actual_span. We do NOT treat them as
    right-censored in the full survival-analysis sense; instead we simply
    include them with their observed rate (which is a lower bound). This is
    conservative — it understates the prior slightly — but the shrinkage K
    damps early-week spikes even with an understated prior, so the censoring
    treatment is "safe" in the primary use case.
  - Duty surface (2D, IMPLEMENTED): per-(weekday,hour) burn-share weights on a
    UTC week starting Sat 00:00 UTC. hour-of-week (0..167) is the linear
    in-window coordinate. "Active capacity remaining to reset" = weighted sum
    over the cells ahead. Activity is derived from per-hour pp/hr against an
    idle floor (~0.4 pp/hr), NOT snapshot coverage. Reset/account-switch jumps
    (Δ>5pp in <1h) are excluded. Falls back to the flat 07-24 UTC window when
    the surface is too sparse (< MIN_CELLS_FOR_2D populated cells). AEST shown
    for human display only.
  - Predictive vs target surfaces (endogeneity split, IMPLEMENTED):
      predictive -> ACTUAL demand surface (incl. rationing): "where will I land?"
      target     -> DE-RATIONED natural surface: late-week suppressed/censored
                    cells excluded, then imputed from their unconstrained mirror
                    (Fri-night ≈ Mon-night). Stops the rationing feedback loop
                    (feeding the suppressed late-week hole into the target would
                    license front-loading — see the Objective in the project doc).
  - Recency policy (IMPLEMENTED): equal-weight all windows while thin (<=5);
    rolling window (~12wk) + exponential decay (half-life ~5wk) once mature
    (>=8); light-decay transition in between. Tracks habit drift instead of
    anchoring to stale behaviour.
  - K-widening (IMPLEMENTED): K is a function of clean-window count — 2× at <=2
    windows, 1.5× at 3-4, baseline at >=5. Thin history anchors harder to prior.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Phase A (WS10/WS11/epoch-fix): import clean span extraction and pooled prior.
# spans.py lives alongside this file; import lazily to avoid circular deps.
try:
    from spans import (
        extract_spans as _spans_extract,
        get_effective_epoch as _spans_get_epoch,
        pooled_prior as _spans_pooled_prior,
        Span as _Span,
    )
    _SPANS_AVAILABLE = True
except ImportError:
    _SPANS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"

# Garbage resets_at that must be excluded everywhere
GARBAGE_RESETS = {"2286-11-20T17:46:39Z"}

# Known personal-account generation: resets_at 2026-06-10T21:00:00Z
# Deterministic rule: personal account resets_at are NOT Saturday 00:00 UTC.
# We identify work account via Saturday 00:00 UTC filter.

# Near-coincident collapse tolerance: resets_at values within this many seconds
# of each other are the same boundary.
RESET_MERGE_TOLERANCE_S = 120  # 2 minutes

# Drop threshold (pp) for intra-week reset detection via monotonicity break
MONOTONICITY_BREAK_THRESHOLD = 30.0

# Duty window — empirical, UTC. Dead zone is 01:00-07:00 UTC.
# Active: 07:00-24:00 UTC = 17 active hours/day.
ACTIVE_START_HOUR_UTC = 7
ACTIVE_END_HOUR_UTC = 24  # exclusive; 24 = wrap to next day 00:00

# Bayesian shrinkage pseudo-hours of prior weight
DEFAULT_K = 24.0

# K-widening schedule (function of clean work-window count).  Thin history =>
# wider damping (trust the prior longer, since the few windows we have are
# noisy).  See _k_for_window_count() for the schedule.
#   <= 2 windows : K * 2.0   (very thin — anchor hard to the prior)
#   3-4 windows  : K * 1.5   (still thin)
#   5-7 windows  : K * 1.0   (baseline)
#   >= 8 windows : K * 1.0   (recency policy kicks in instead; prior is trustworthy)
# NOTE: the multipliers below are first-cut judgement on ~5 weeks of data.
# Revisit once >=8 clean windows exist and the prior's week-to-week variance
# can be measured directly (then K could be derived from observed variance
# rather than a hand-set schedule).
K_WIDEN_VERY_THIN = 2.0   # <=2 windows
K_WIDEN_THIN = 1.5        # 3-4 windows
K_WIDEN_BASELINE = 1.0    # >=5 windows

# "Near-100%" threshold for censoring annotation
CENSORED_PCT_THRESHOLD = 98.0

# Rationing threshold: hours after which pct exceeded this are considered
# potentially rationed for target mode de-rationing.
RATIONING_PCT_THRESHOLD = 80.0

# --- 2D duty surface (weekday×hour, UTC, week starting Sat 00:00 UTC) ---
# Idle floor: per-hour burn below this rate is treated as "not active" (idle
# session writing 0%-ish snapshots).  The rate, not snapshot coverage, is the
# activity signal.  NOTE: 0.3-0.5 pp/hr range per the analysis; 0.4 is the
# midpoint.  Tune as more weeks accrue.
IDLE_FLOOR_PP_H = 0.4

# WS7: Per-cell duty-surface shrinkage toward the all-models prior.
#
# Problem: a thin bucket (e.g. sonnet_weekly) only populates a handful of the
# 168 weekday×hour cells.  Empty cells are treated as weight 0 (idle), which
# collapses "active capacity to reset" far below the true value.  Result: the
# Sonnet duty-projection reads ~10.8% where ~45% is correct.
#
# Fix: the all-models (seven_day) surface is the PRIOR for every other bucket's
# duty surface.  Blend per-cell, weighted by that cell's own sample count:
#
#   blended_cell = (n_samples · bucket_cell + M · allmodels_cell)
#                  / (n_samples + M)
#
# where:
#   n_samples  = number of qualifying active intervals in this bucket's cell
#   bucket_cell = normalised weight from the bucket's own histogram
#   allmodels_cell = normalised weight from the all-models (seven_day) histogram
#   M          = pseudo-count of prior weight (duty-surface analogue of rate K)
#
# When n_samples == 0 (empty cell): blend = allmodels_cell → no collapse.
# When n_samples >> M (well-sampled cell): blend → bucket_cell.
# seven_day itself uses its own surface as prior → the blend is a self-blend
# (prior == self) and the formula simplifies to bucket_cell → exact no-op.
#
# M = 4 (active samples) is the starting point — "four representative
# burns establish a cell".  Tune down toward 1-2 when Sonnet data is denser,
# up toward 8-10 if the all-models prior is very noisy.
DUTY_PRIOR_M = 4

# Histogram hygiene: exclude reset / account-switch jumps — a positive delta
# greater than this many pp in under JUMP_MAX_GAP_H hours is not continuous
# burn (it's a reset artefact or account switch).
JUMP_DELTA_PP = 5.0
JUMP_MAX_GAP_H = 1.0

# Hour-of-week coordinate: week starts Sat 00:00 UTC.  168 cells (7 days × 24h).
HOURS_PER_WEEK = 168
# Python weekday(): Mon=0 .. Sat=5, Sun=6.  We want Sat=0 as the week start.
# hour_of_week = ((weekday - 5) % 7) * 24 + hour
_SAT_WEEKDAY = 5

# AEST offset for human-readable display only (DB is UTC; never bin in local).
AEST_OFFSET_H = 10

# Recency policy thresholds (see _select_windows_by_recency).
#   thin   : <= 5 clean windows  -> use ALL completed windows
#   mature : >= 8 clean windows  -> rolling window + exponential decay
RECENCY_THIN_MAX = 5
RECENCY_MATURE_MIN = 8
RECENCY_ROLLING_WEEKS = 12        # rolling window cap once mature
RECENCY_DECAY_HALFLIFE_WEEKS = 5  # exponential-decay half-life (4-6wk range)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QuotaWindow:
    """A single real quota window (may span less than 7 days due to mid-week resets)."""
    bucket: str
    start_ts: datetime      # start of this window (reset boundary)
    end_ts: datetime        # end of this window (next reset boundary or "current")
    start_pct: float        # pct at start (nominally 0.0)
    end_pct: float          # pct at end of window
    actual_span_h: float    # actual hours this window ran
    rate_pp_h: float        # end_pct / actual_span_h
    is_work_account: bool   # True if this is a work-account window (Sat reset)
    is_censored: bool       # True if window hit ~100% early (right-censored)
    is_completed: bool      # False if this is the current (in-progress) window
    source: str             # 'reset_history', 'monotonicity_break', 'resets_at'


@dataclass
class DutySurface:
    """Empirical active-capacity model.

    The headline field is `active_hours_remaining` — the duty-weighted active
    capacity between now and the reset, in "equivalent active hours".  For the
    2D surface this is a *weighted* sum over the hour-of-week cells ahead (each
    cell weighted by its burn share, normalised so the mean active cell ≈ 1.0),
    NOT a raw count of wall-clock hours.  The projection multiplies the
    effective rate by this number, so weighting it correctly bends the
    projection toward the real weekly rhythm.
    """
    active_start_utc: int   # 07 (flat-window mode only; informational for 2D)
    active_end_utc: int     # 24 (flat-window mode only; informational for 2D)
    active_hours_per_day: float
    active_hours_remaining: float   # duty-weighted active capacity to reset
    method: str             # 'empirical_window_07_24_utc' | '2d_surface_actual' | '2d_surface_natural'
    # 2D-surface extras (None for the flat-window fallback):
    cell_weights: Optional[dict] = None   # {hour_of_week: weight} 0..167
    surface_kind: Optional[str] = None     # 'actual' | 'natural' | None


@dataclass
class ProjectionResult:
    """Result of project()."""
    mode: str
    bucket: str
    current_pct: float
    elapsed_h: float
    h_to_reset: float
    observed_rate_pp_h: float       # raw observed rate (pct/elapsed)
    effective_rate_pp_h: float      # after shrinkage (predictive/target) or raw (naive)
    prior_pp_h: Optional[float]     # computed prior (None for naive mode)
    projected_pct_at_reset: float   # main output
    duty_projected_pct: float       # using duty surface
    k_used: float
    prior_window_count: int         # how many windows fed the prior
    notes: list[str]                # human-readable design choices applied


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _ensure_reset_history(conn: sqlite3.Connection) -> None:
    """Create reset_history if absent — matches the WS6 canonical schema."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reset_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket     TEXT NOT NULL,
            reset_ts   TEXT NOT NULL,
            created_ts TEXT NOT NULL,
            source     TEXT
        )"""
    )
    conn.commit()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Reset boundary detection
# ---------------------------------------------------------------------------

def _normalise_resets_at(resets_at: str) -> Optional[str]:
    """Return None for garbage rows; otherwise return canonical form."""
    if resets_at in GARBAGE_RESETS:
        return None
    return resets_at


def _is_work_account_reset(resets_at_dt: datetime) -> bool:
    """A generation is work iff its resets_at lands on Saturday 00:00 UTC.

    We use an 8-minute tolerance to handle server-side 23:59→00:00 wobble
    (Jun 12 23:59 is the same canonical boundary as Jun 13 00:00).
    """
    dt = _to_utc(resets_at_dt)
    # Check if it's Saturday (weekday 5 in Python = Saturday)
    # and the time is within 8 minutes of 00:00
    minutes_from_midnight = dt.hour * 60 + dt.minute
    is_sat = dt.weekday() == 5  # Monday=0, Saturday=5
    near_midnight = minutes_from_midnight <= 8 or minutes_from_midnight >= (24 * 60 - 8)
    return is_sat and near_midnight


def _merge_resets_at_labels(resets_at_list: list[str]) -> dict[str, str]:
    """Collapse near-coincident resets_at labels to a canonical value.

    Returns a mapping: raw_resets_at -> canonical_resets_at.

    Groups any two resets_at values within RESET_MERGE_TOLERANCE_S seconds;
    the canonical value for each group is the one that looks most like
    Saturday 00:00 UTC (i.e. the one closest to midnight Saturday).
    """
    parsed = []
    for ra in resets_at_list:
        try:
            parsed.append((ra, _to_utc(_parse_iso(ra))))
        except Exception:
            pass
    # Sort by time
    parsed.sort(key=lambda x: x[1])

    canonical_map: dict[str, str] = {}
    groups: list[list[tuple[str, datetime]]] = []

    # Group by proximity
    for raw, dt in parsed:
        placed = False
        for grp in groups:
            rep_dt = grp[0][1]
            if abs((dt - rep_dt).total_seconds()) <= RESET_MERGE_TOLERANCE_S:
                grp.append((raw, dt))
                placed = True
                break
        if not placed:
            groups.append([(raw, dt)])

    for grp in groups:
        # Pick canonical: prefer the one closest to Sat 00:00 UTC
        def canon_score(item: tuple[str, datetime]) -> float:
            dt = item[1]
            # How many minutes from midnight?
            mins = dt.hour * 60 + dt.minute + dt.second / 60.0
            midnight_dist = min(mins, 24 * 60 - mins)
            return midnight_dist

        canonical = min(grp, key=canon_score)
        for raw, _ in grp:
            canonical_map[raw] = canonical[0]

    return canonical_map


def _get_reset_history_boundaries(conn: sqlite3.Connection, bucket: str) -> list[datetime]:
    """Return all known reset boundaries from reset_history table, sorted ascending."""
    _ensure_reset_history(conn)
    rows = conn.execute(
        "SELECT reset_ts FROM reset_history WHERE bucket = ? ORDER BY reset_ts ASC",
        (bucket,)
    ).fetchall()
    result = []
    for (ts,) in rows:
        try:
            result.append(_to_utc(_parse_iso(ts)))
        except Exception:
            pass
    return result


def _detect_monotonicity_breaks(snapshots: list[tuple[str, float]]) -> list[tuple[datetime, datetime]]:
    """Find intra-generation reset points via pct drops >= threshold.

    snapshots: [(ts_iso, pct_used), ...] sorted by ts ASC for one resets_at generation.
    Returns list of (end_of_prior_window, start_of_next_window) pairs.
    The prior window ends at prev_ts (last high pct before the drop).
    The next window starts at curr_ts (first low pct after the drop = 0 or low value).
    """
    breaks = []
    for i in range(1, len(snapshots)):
        prev_ts, prev_pct = snapshots[i - 1]
        curr_ts, curr_pct = snapshots[i]
        if prev_pct - curr_pct >= MONOTONICITY_BREAK_THRESHOLD:
            try:
                end_of_prev = _to_utc(_parse_iso(prev_ts))
                start_of_next = _to_utc(_parse_iso(curr_ts))
                breaks.append((end_of_prev, start_of_next))
            except Exception:
                pass
    return breaks


# ---------------------------------------------------------------------------
# Window extraction
# ---------------------------------------------------------------------------

def _fetch_all_snapshots(conn: sqlite3.Connection, bucket: str) -> list[tuple[str, float, str]]:
    """Return all non-garbage snapshots for bucket, sorted by ts ASC."""
    rows = conn.execute(
        "SELECT snapshot_ts, pct_used, resets_at FROM quota_snapshots "
        "WHERE bucket = ? ORDER BY snapshot_ts ASC",
        (bucket,)
    ).fetchall()
    return [
        (ts, pct, ra)
        for ts, pct, ra in rows
        if ra not in GARBAGE_RESETS
    ]


def _extract_windows(conn: sqlite3.Connection, bucket: str, now: datetime) -> list[QuotaWindow]:
    """Extract all real quota windows from the snapshot history.

    Strategy:
    1. Prefer reset_history table for boundary records (WS6).
    2. Fall back to monotonicity-break detection (pct drop >= threshold).
    3. Also use resets_at label changes as a coarser boundary signal.

    Key design: a monotonicity break gives us TWO timestamps —
      (end_of_window_A, start_of_window_B)
    so snapshots in A end at ts_high and snapshots in B start at ts_low.
    This is different from a clean Sat 00:00 boundary where all snapshots
    are neatly on one side.

    Returns a list of QuotaWindow objects (completed and in-progress).
    """
    all_snaps = _fetch_all_snapshots(conn, bucket)
    if not all_snaps:
        return []

    # --- Normalise resets_at labels ---
    resets_at_values = list({ra for _, _, ra in all_snaps})
    canonical_map = _merge_resets_at_labels(resets_at_values)

    canonical_dts: set[datetime] = set()
    for ra in resets_at_values:
        canon_ra = canonical_map.get(ra, ra)
        try:
            dt = _to_utc(_parse_iso(canon_ra))
            canonical_dts.add(dt)
        except Exception:
            pass

    # --- Collect boundary INTERVALS: (end_exclusive, start_inclusive) pairs ---
    # An interval here means: "window A ends at or before end_exclusive;
    # window B starts at or after start_inclusive."
    # For a clean Sat 00:00 reset: end_exclusive == start_inclusive == reset_dt
    # For a monotonicity break: end_exclusive = prev_ts, start_inclusive = curr_ts

    # From reset_history (WS6) — these are clean boundaries
    history_boundaries = _get_reset_history_boundaries(conn, bucket)
    clean_boundaries: list[tuple[datetime, datetime]] = []
    for b in history_boundaries:
        clean_boundaries.append((b, b))

    # From resets_at label changes: derive window starts as resets_at - 7d
    for ra_dt in canonical_dts:
        if _is_work_account_reset(ra_dt):
            start_of_gen = ra_dt - timedelta(days=7)
            clean_boundaries.append((start_of_gen, start_of_gen))

    # From monotonicity breaks within each canonical generation
    gen_snaps: dict[str, list[tuple[str, float]]] = {}
    for ts, pct, ra in all_snaps:
        canon_ra = canonical_map.get(ra, ra)
        gen_snaps.setdefault(canon_ra, []).append((ts, pct))

    mono_boundaries: list[tuple[datetime, datetime]] = []
    for canon_ra, snaps in gen_snaps.items():
        snaps.sort(key=lambda x: x[0])
        for end_dt, start_dt in _detect_monotonicity_breaks(snaps):
            mono_boundaries.append((end_dt, start_dt))

    # Merge mono_boundaries with clean_boundaries: if a clean boundary is within
    # a few minutes of a mono break, prefer the clean one (it has a better-known ts)
    all_boundary_intervals: list[tuple[datetime, datetime]] = list(clean_boundaries)
    for mono_end, mono_start in mono_boundaries:
        # Only add if no clean boundary is nearby (within 4h)
        close_clean = any(
            abs((ce - mono_end).total_seconds()) < 4 * 3600
            for ce, _ in clean_boundaries
        )
        if not close_clean:
            all_boundary_intervals.append((mono_end, mono_start))

    # Sort by the "end" (left side) timestamp
    all_boundary_intervals.sort(key=lambda x: x[0])

    # Deduplicate: if two entries are within RESET_MERGE_TOLERANCE_S, keep one
    deduped: list[tuple[datetime, datetime]] = []
    for end_dt, start_dt in all_boundary_intervals:
        if deduped and abs((end_dt - deduped[-1][0]).total_seconds()) <= RESET_MERGE_TOLERANCE_S:
            continue
        deduped.append((end_dt, start_dt))
    all_boundary_intervals = deduped

    # --- Build explicit window slices: [(snap_start_ts, snap_end_ts, is_current), ...] ---
    # Between consecutive boundary intervals, we have a window of snapshots.
    # snap_start_ts = boundary[i].start_inclusive
    # snap_end_ts = boundary[i+1].end_exclusive  (or now for the last window)

    first_snap_dt = _to_utc(_parse_iso(all_snaps[0][0]))
    # Filter out boundaries before our data
    all_boundary_intervals = [
        (e, s) for e, s in all_boundary_intervals
        if e >= first_snap_dt - timedelta(hours=1)
    ]

    # Add a terminal "now" boundary
    all_boundary_intervals.append((now, now))

    # Sort again after additions
    all_boundary_intervals.sort(key=lambda x: x[0])

    # Build snapshot index keyed by (ts_str, pct, ra)
    snaps_list = [(ts, pct, ra) for ts, pct, ra in all_snaps]

    windows: list[QuotaWindow] = []

    for i in range(len(all_boundary_intervals) - 1):
        # Window runs from start_inclusive[i] to end_exclusive[i+1]
        _, win_snap_start = all_boundary_intervals[i]   # snapshots in this window start here
        win_snap_end, _ = all_boundary_intervals[i + 1] # snapshots in this window end here
        is_current = (win_snap_end == now)

        # Collect snapshots strictly within this window
        win_snaps = [
            (ts, pct) for ts, pct, ra in snaps_list
            if win_snap_start <= _to_utc(_parse_iso(ts)) <= win_snap_end
        ]
        if not win_snaps:
            continue

        first_pct = win_snaps[0][1]
        # Start at 0 if first snapshot is low (fresh reset), else use the first observed value
        start_pct = 0.0 if first_pct <= 5.0 else 0.0  # always 0 — window starts fresh
        end_pct = win_snaps[-1][1]

        # Determine work account: check the resets_at labels of snapshots in this window
        snap_resets_ats = list({ra for ts, pct, ra in snaps_list
                                if win_snap_start <= _to_utc(_parse_iso(ts)) <= win_snap_end})
        is_work = any(
            _is_work_account_reset(_to_utc(_parse_iso(canonical_map.get(ra, ra))))
            for ra in snap_resets_ats
            if ra not in GARBAGE_RESETS
        )

        # Span and rate: use actual data span (first to last snapshot in window)
        if len(win_snaps) >= 2:
            t0 = _to_utc(_parse_iso(win_snaps[0][0]))
            t1 = _to_utc(_parse_iso(win_snaps[-1][0]))
            data_span_h = (t1 - t0).total_seconds() / 3600.0
            if data_span_h < 0.1:
                continue
            rate = (end_pct - start_pct) / data_span_h
        else:
            # Single snapshot: use window span
            wall_span_h = (win_snap_end - win_snap_start).total_seconds() / 3600.0
            data_span_h = max(wall_span_h, 0.1)
            rate = (end_pct - start_pct) / data_span_h

        if rate < 0:
            # Should not happen (reset detection eliminated drops), but guard anyway
            continue

        # Censoring
        peak_pct = max(pct for _, pct in win_snaps)
        is_censored = peak_pct >= CENSORED_PCT_THRESHOLD

        # Source annotation
        if any(abs((win_snap_start - b).total_seconds()) < 60 for b in history_boundaries):
            source = 'reset_history'
        elif any(
            abs((win_snap_start - (ra - timedelta(days=7))).total_seconds()) < 3600
            for ra in canonical_dts if _is_work_account_reset(ra)
        ):
            source = 'resets_at_derived'
        else:
            source = 'monotonicity_break'

        windows.append(QuotaWindow(
            bucket=bucket,
            start_ts=win_snap_start,
            end_ts=win_snap_end,
            start_pct=start_pct,
            end_pct=end_pct,
            actual_span_h=data_span_h,
            rate_pp_h=rate,
            is_work_account=is_work,
            is_censored=is_censored,
            is_completed=not is_current,
            source=source,
        ))

    return windows


# ---------------------------------------------------------------------------
# Prior computation
# ---------------------------------------------------------------------------

def _k_for_window_count(K: float, n_windows: int) -> tuple[float, str]:
    """Return (effective_K, note) widening K when clean history is thin.

    Schedule (see K_WIDEN_* constants):
      <= 2 windows -> K * 2.0   (very thin)
      3-4 windows  -> K * 1.5   (thin)
      >= 5 windows -> K * 1.0   (baseline; recency policy handles maturity)
    """
    if n_windows <= 2:
        return K * K_WIDEN_VERY_THIN, (
            f"Very thin history ({n_windows} windows): K widened "
            f"{K_WIDEN_VERY_THIN:g}× to {K * K_WIDEN_VERY_THIN:.0f}h"
        )
    if n_windows <= 4:
        return K * K_WIDEN_THIN, (
            f"Thin history ({n_windows} windows): K widened "
            f"{K_WIDEN_THIN:g}× to {K * K_WIDEN_THIN:.0f}h"
        )
    return K * K_WIDEN_BASELINE, (
        f"Mature history ({n_windows} windows): K at baseline {K:.0f}h"
    )


def _select_windows_by_recency(
    windows: list[QuotaWindow],
    now: datetime,
) -> tuple[list[tuple[QuotaWindow, float]], str]:
    """Apply the recency policy and return [(window, weight), ...] + a note.

    Policy (from the project doc "Prior maintenance / recompute cadence"):
      - THIN (<= RECENCY_THIN_MAX clean windows): use ALL completed windows,
        equal weight. The few windows we have are all the signal there is.
      - MATURE (>= RECENCY_MATURE_MIN clean windows): switch to a rolling
        window (cap at RECENCY_ROLLING_WEEKS most-recent) AND apply exponential
        decay (half-life RECENCY_DECAY_HALFLIFE_WEEKS) so the prior tracks habit
        drift instead of anchoring to stale behaviour.
      - IN-BETWEEN (6-7 windows): transitional — use all but begin light decay,
        so the switch at 8 isn't a cliff.

    Weight is by recency of each window's end_ts relative to `now`.
    Returns windows sorted newest-first with their weights.

    NOTE: thresholds (5 / 8) and the 12-week / 5-week-halflife knobs are the
    project-doc starting points. With only ~5 windows today this resolves to
    THIN; the MATURE branch is exercised by the synthetic test in __main__.
    Revisit window length + half-life as data deepens (doc says "revisit").
    """
    if not windows:
        return [], "no windows"

    # Sort newest-first by window end
    ordered = sorted(windows, key=lambda w: w.end_ts, reverse=True)
    n = len(ordered)

    def _decay_weight(w: QuotaWindow, halflife_weeks: float) -> float:
        age_weeks = (now - w.end_ts).total_seconds() / (7 * 86400.0)
        if age_weeks < 0:
            age_weeks = 0.0
        return 0.5 ** (age_weeks / halflife_weeks)

    if n <= RECENCY_THIN_MAX:
        # THIN: all windows, equal weight.
        weighted = [(w, 1.0) for w in ordered]
        note = f"Recency: THIN ({n} windows ≤ {RECENCY_THIN_MAX}) — all windows, equal weight"
        return weighted, note

    if n >= RECENCY_MATURE_MIN:
        # MATURE: rolling window cap + exponential decay.
        rolling = ordered[:RECENCY_ROLLING_WEEKS]
        weighted = [(w, _decay_weight(w, RECENCY_DECAY_HALFLIFE_WEEKS)) for w in rolling]
        note = (
            f"Recency: MATURE ({n} windows ≥ {RECENCY_MATURE_MIN}) — rolling "
            f"{len(rolling)}/{RECENCY_ROLLING_WEEKS}wk + exp-decay "
            f"(half-life {RECENCY_DECAY_HALFLIFE_WEEKS}wk)"
        )
        return weighted, note

    # TRANSITIONAL (6-7): all windows, light decay so 8 isn't a cliff.
    # Use a longer half-life than mature (gentler) to ease the transition.
    transitional_halflife = RECENCY_DECAY_HALFLIFE_WEEKS * 2.0
    weighted = [(w, _decay_weight(w, transitional_halflife)) for w in ordered]
    note = (
        f"Recency: TRANSITIONAL ({n} windows, {RECENCY_THIN_MAX}<n<"
        f"{RECENCY_MATURE_MIN}) — all windows, light decay "
        f"(half-life {transitional_halflife:.0f}wk)"
    )
    return weighted, note


def compute_prior(
    conn: sqlite3.Connection,
    bucket: str,
    now: Optional[datetime] = None,
) -> tuple[float, list[QuotaWindow]]:
    """Compute the span-correct, recency-weighted historical prior for `bucket`.

    Phase A (WS10/WS11): delegates to spans.py when available for the correct
    pooled-prior calculation.  Falls back to the legacy _extract_windows path
    when spans.py is not importable (e.g., during a transition period).

    Returns (prior_pp_per_hour, list_of_windows_used).

    The returned list is typed as list[QuotaWindow] for API compatibility; when
    the spans path is used, the objects are actually spans._Span instances but
    they expose compatible attributes (start_ts, end_ts, end_pct, rate_pp_h,
    is_censored, is_completed).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if _SPANS_AVAILABLE:
        # Phase A path: use canonical span extraction + WS11 pooled prior
        spans = _spans_extract(conn, bucket, now)
        eligible = [s for s in spans if s.prior_eligible]

        if not eligible:
            return 0.7, []

        # Recency weight function (mirrors legacy recency policy).
        # THIN (<=5 spans): equal weight; MATURE (>=8): rolling+decay.
        # For now, delegate to the same _select_windows_by_recency logic but
        # operating on spans.  We adapt by building QuotaWindow proxies so we
        # can reuse the existing recency machinery without duplicating it.
        proxy_windows = [
            QuotaWindow(
                bucket=bucket,
                start_ts=s.start_ts,
                end_ts=s.end_ts,
                start_pct=0.0,
                end_pct=s.delta_pp,
                actual_span_h=s.span_h,
                rate_pp_h=s.rate_pp_h,
                is_work_account=True,   # eligible already filters personal
                is_censored=s.is_censored,
                is_completed=True,
                source='spans_py',
            )
            for s in eligible
        ]
        weighted_pairs, _ = _select_windows_by_recency(proxy_windows, now)

        # WS11: pooled prior — Σ(w·Δpp) / Σ(w·hours) NOT mean-of-rates
        sum_w_pp = sum(wt * w.end_pct for w, wt in weighted_pairs)
        sum_w_h = sum(wt * w.actual_span_h for w, wt in weighted_pairs)
        prior = sum_w_pp / sum_w_h if sum_w_h > 0 else 0.7

        return prior, [w for w, _ in weighted_pairs]

    # Legacy fallback path (spans.py not available)
    windows = _extract_windows(conn, bucket, now)
    work_completed = [
        w for w in windows
        if w.is_work_account and w.is_completed and w.rate_pp_h >= 0
    ]
    if not work_completed:
        all_completed = [w for w in windows if w.is_completed and w.rate_pp_h >= 0]
        if not all_completed:
            return 0.7, []
        weighted, _ = _select_windows_by_recency(all_completed, now)
        prior = _weighted_mean(weighted)
        return prior, [w for w, _ in weighted]

    weighted, _ = _select_windows_by_recency(work_completed, now)
    prior = _weighted_mean(weighted)
    return prior, [w for w, _ in weighted]


def _weighted_mean(weighted: list[tuple[QuotaWindow, float]]) -> float:
    """Legacy recency-weighted mean of window rates (averaging-averages).

    Retained as a fallback for the legacy _extract_windows path.
    The WS11 fix (pooled prior) is in compute_prior's spans.py branch.
    """
    if not weighted:
        return 0.7
    total_w = sum(wt for _, wt in weighted)
    if total_w <= 0:
        rates = [w.rate_pp_h for w, _ in weighted]
        return sum(rates) / len(rates)
    return sum(w.rate_pp_h * wt for w, wt in weighted) / total_w


# ---------------------------------------------------------------------------
# Effective rate (shrinkage)
# ---------------------------------------------------------------------------

def effective_rate(
    observed_pp_h: float,
    elapsed_h: float,
    prior_pp_h: float,
    K: float = DEFAULT_K,
) -> float:
    """Bayesian shrinkage toward the prior.

    effective_rate = (observed * elapsed + prior * K) / (elapsed + K)

    At elapsed = 0:  returns prior.
    At elapsed = K:  weights observed and prior equally.
    At elapsed >> K: approaches observed.
    """
    return (observed_pp_h * elapsed_h + prior_pp_h * K) / (elapsed_h + K)


# ---------------------------------------------------------------------------
# Duty surface
# ---------------------------------------------------------------------------

def _active_hours_remaining_empirical(now: datetime, reset_dt: datetime) -> float:
    """Count empirical active hours from now to reset.

    Uses the 07:00-24:00 UTC window (true dead zone is 01:00-07:00 UTC).
    Walks forward day-by-day in UTC (no local tz needed — active window is UTC).
    """
    if reset_dt <= now:
        return 0.0

    total_active = 0.0
    cursor = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    reset_utc = reset_dt.replace(tzinfo=timezone.utc) if reset_dt.tzinfo is None else reset_dt.astimezone(timezone.utc)

    while cursor < reset_utc:
        next_day = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Active window for this UTC day: 07:00-24:00
        day_start = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        win_s = day_start.replace(hour=ACTIVE_START_HOUR_UTC)
        win_e = day_start.replace(hour=0) + timedelta(days=1)  # midnight next day = 24:00

        # Clamp to [cursor, reset_utc]
        seg_s = max(win_s, cursor)
        seg_e = min(win_e, reset_utc)

        if seg_e > seg_s:
            total_active += (seg_e - seg_s).total_seconds() / 3600.0

        cursor = next_day

    return total_active


def _hour_of_week(dt: datetime) -> int:
    """Map a UTC datetime to its hour-of-week, week starting Sat 00:00 UTC.

    Returns 0..167. Sat 00:00 UTC -> 0; Sat 01:00 -> 1; ... Fri 23:00 -> 167.
    """
    dt = _to_utc(dt)
    day_index = (dt.weekday() - _SAT_WEEKDAY) % 7   # Sat=0, Sun=1, ... Fri=6
    return day_index * 24 + dt.hour


def _aest_label(hour_of_week: int) -> str:
    """Human-readable AEST label for an hour-of-week cell (display only)."""
    # hour_of_week is in UTC week (Sat 00:00 UTC = 0). Convert to AEST for label.
    utc_dow = hour_of_week // 24          # 0=Sat .. 6=Fri (UTC)
    utc_hour = hour_of_week % 24
    aest_hour_abs = utc_hour + AEST_OFFSET_H
    aest_hour = aest_hour_abs % 24
    day_roll = aest_hour_abs // 24
    dow_names = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]
    aest_dow = dow_names[(utc_dow + day_roll) % 7]
    return f"{aest_dow} {aest_hour:02d}:00 AEST"


def _build_burn_histogram(
    conn: sqlite3.Connection,
    bucket: str,
    windows: list[QuotaWindow],
    de_ration: bool,
) -> tuple[dict[int, float], dict[int, int]]:
    """Build a per-hour-of-week burn histogram (pp accumulated per cell).

    Activity signal = per-hour pp/hr, NOT snapshot coverage (idle-but-open
    sessions write 0%-ish snapshots, which would overstate active hours).

    Hygiene:
      - Exclude reset/account-switch jumps: positive Δ > JUMP_DELTA_PP in
        < JUMP_MAX_GAP_H hours (reset boundary / account switch, not burn).
      - Exclude negative deltas (resets).
      - Only count deltas whose implied rate clears the idle floor; sub-floor
        intervals are treated as idle (contribute 0 to the active histogram).

    De-rationing (target surface):
      - When de_ration=True, exclude intervals that occurred while the bucket
        was already >= RATIONING_PCT_THRESHOLD (rationing likely in force), and
        intervals inside CENSORED windows (the cap was hit — late-week demand is
        suppressed). This stops the rationed late-week hole from teaching the
        target profile that late-week is "naturally" quiet (the feedback loop
        the Objective warns about). Cells with no natural data are imputed from
        their day-of-week-symmetric counterpart (Fri-night ≈ Mon-night) — see
        _impute_natural_cells().

    Returns:
      (hist, sample_counts) where:
        hist         = {hour_of_week (0..167): total_active_pp}
        sample_counts = {hour_of_week (0..167): n_qualifying_intervals}

    Cells with no qualifying burn are absent from both dicts (treated as 0 by
    the caller).  sample_counts is used by the WS7 per-cell prior blend.

    NOTE: When spans.py is available, prefer _build_burn_histogram_from_spans()
    which uses cleaned readings (Wrinkles 0-3 + running-max hygiene) to avoid
    personal-account contamination.  This legacy function is retained as a
    fallback for the prior-bucket path and for backwards compatibility.
    """
    # Restrict to work-account windows for the duty profile (personal account
    # is a different quota scale and rhythm).
    work_windows = [w for w in windows if w.is_work_account]
    if not work_windows:
        work_windows = windows  # degrade: use what we have

    # Build a set of (start,end) spans we care about, plus censored flags.
    span_index: list[tuple[datetime, datetime, bool]] = [
        (w.start_ts, w.end_ts, w.is_censored) for w in work_windows
    ]

    all_snaps = _fetch_all_snapshots(conn, bucket)
    # Index snapshots by parse once
    parsed = [(_to_utc(_parse_iso(ts)), pct, ra) for ts, pct, ra in all_snaps]

    hist: dict[int, float] = {}
    sample_counts: dict[int, int] = {}

    for win_start, win_end, win_censored in span_index:
        # Snapshots strictly within this window, sorted
        win_snaps = sorted(
            [(t, pct) for t, pct, ra in parsed if win_start <= t <= win_end],
            key=lambda x: x[0],
        )
        for i in range(1, len(win_snaps)):
            t_prev, pct_prev = win_snaps[i - 1]
            t_curr, pct_curr = win_snaps[i]
            gap_h = (t_curr - t_prev).total_seconds() / 3600.0
            if gap_h <= 0:
                continue
            delta = pct_curr - pct_prev
            if delta <= 0:
                continue  # flat or reset — not burn
            # Hygiene: reset/account-switch jump
            if delta > JUMP_DELTA_PP and gap_h < JUMP_MAX_GAP_H:
                continue
            rate = delta / gap_h
            # Idle floor: sub-floor intervals are idle, not active burn
            if rate < IDLE_FLOOR_PP_H:
                continue

            # De-rationing exclusions for the target (natural) surface
            if de_ration:
                # Skip intervals where the bucket was already rationing-likely
                if pct_prev >= RATIONING_PCT_THRESHOLD:
                    continue
                # Skip censored windows entirely (cap hit => suppressed demand)
                if win_censored:
                    continue

            # Attribute the burn to the hour-of-week of the interval midpoint.
            # NOTE: a long interval spanning multiple cells is attributed to its
            # midpoint cell. With ~20-min snapshot cadence this is fine; if
            # cadence drops we'd want to split across cells. Revisit if gaps grow.
            mid = t_prev + (t_curr - t_prev) / 2
            how = _hour_of_week(mid)
            hist[how] = hist.get(how, 0.0) + delta
            sample_counts[how] = sample_counts.get(how, 0) + 1

    return hist, sample_counts


def _build_burn_histogram_from_spans(
    conn: sqlite3.Connection,
    bucket: str,
    spans: list,   # list[spans.Span] — typed as list for import flexibility
    de_ration: bool,
) -> tuple[dict[int, float], dict[int, int]]:
    """WS12: Build the burn histogram from cleaned span readings.

    Same logic as _build_burn_histogram but feeds on the cleaned readings
    produced by spans.py (Wrinkles 0–3 + running-max data hygiene) rather than
    raw _fetch_all_snapshots().

    Key correctness improvements over the legacy function:
    1. Personal-account readings (S5 / Wrinkle 1) are excluded — they were being
       pulled in by the timestamp-range filter since S5 timestamps overlap S4's
       calendar window.  Now we filter by resets_at so only readings labelled with
       a work-account reset are considered.
    2. Running-max-per-generation is applied within each span's reading window,
       eliminating the cross-source ±1 jitter and stale late-arriving label gotchas
       documented in "Data hygiene gotchas — raw readings".
    3. Censoring (Wrinkle 3) is correctly applied: each span knows its own
       end_ts (cap instant for censored spans) rather than relying on the window's
       peak_pct heuristic.

    Parameters match _build_burn_histogram so the caller (duty_surface) can
    swap them transparently.  Returns the same (hist, sample_counts) shape.
    """
    from spans import Span as _SpanType  # local import to avoid circular

    # Work-account spans only; discard personal, in-progress, and thin spans
    # for the shape (we want the WHEN profile to reflect real work rhythm).
    # For the histogram, in-progress spans ARE included — they show current-week
    # activity and contribute to the WHEN shape even if excluded from the prior.
    work_spans = [s for s in spans if s.exclude_reason not in ('personal', 'thin')]
    if not work_spans:
        return {}, {}

    # Fetch all non-garbage readings once, keyed by (canonical resets_at, ts).
    # We only include readings whose resets_at maps to a work-account generation.
    # This is the key fix: filtering by resets_at (not just timestamp) ensures
    # personal-account readings (which carry a different resets_at label) are
    # excluded even when their timestamps overlap a work-account time window.

    # Build set of canonical resets_at values for work spans
    # (use .strftime to match against DB string format)
    work_resets_ats_dt = {s.resets_at for s in work_spans}

    # Also build the canonical_map from raw labels to datetime (for matching)
    from spans import _canonicalise_resets_at as _canon_fn, _parse as _spans_parse

    raw_rows = conn.execute(
        "SELECT snapshot_ts, pct_used, resets_at FROM quota_snapshots "
        "WHERE bucket=? ORDER BY snapshot_ts ASC",
        (bucket,),
    ).fetchall()

    # Filter garbage and build canonical map for all raw resets_at in DB
    from spans import GARBAGE_RESETS as _SPANS_GARBAGE
    raw_rows = [(ts, pct, ra) for ts, pct, ra in raw_rows if ra not in _SPANS_GARBAGE]
    if not raw_rows:
        return {}, {}

    raw_labels = list({ra for _, _, ra in raw_rows})
    canon_map = _canon_fn(raw_labels)  # raw_label -> canonical datetime

    # Group readings by canonical resets_at datetime
    gen_readings: dict[datetime, list[tuple[datetime, float]]] = {}
    for ts_s, pct, ra_s in raw_rows:
        canon_dt = canon_map.get(ra_s)
        if canon_dt is None:
            continue
        # Only include readings from work-account generations (Wrinkle 1 filter)
        if canon_dt not in work_resets_ats_dt:
            continue
        try:
            ts_dt = _spans_parse(ts_s)
        except (ValueError, OverflowError):
            continue
        gen_readings.setdefault(canon_dt, []).append((ts_dt, pct))

    for canon_dt in gen_readings:
        gen_readings[canon_dt].sort(key=lambda x: x[0])

    # Build a lookup: span -> (sub_start, sub_end, is_censored)
    # Each span has its own sub-start/end (respects Wrinkle-2 splits and
    # Wrinkle-3 cap instants); we process readings within each span's boundary.

    hist: dict[int, float] = {}
    sample_counts: dict[int, int] = {}

    for span in work_spans:
        # Readings from this span's generation only
        raw_readings = gen_readings.get(span.resets_at, [])
        if not raw_readings:
            continue

        # Collect readings within this span's time window
        sub_raw = [
            (ts, pct) for ts, pct in raw_readings
            if span.start_ts <= ts <= span.end_ts
        ]
        if not sub_raw:
            continue

        # Apply running-max (data hygiene: cross-source jitter + stale arrivals)
        from spans import _running_max_readings
        sub_clean = _running_max_readings(sub_raw)

        for i in range(1, len(sub_clean)):
            t_prev, pct_prev = sub_clean[i - 1]
            t_curr, pct_curr = sub_clean[i]
            gap_h = (t_curr - t_prev).total_seconds() / 3600.0
            if gap_h <= 0:
                continue
            delta = pct_curr - pct_prev
            if delta <= 0:
                continue  # flat or reset — not burn (running-max means all deltas ≥ 0)
            # Hygiene: reset/account-switch jump
            if delta > JUMP_DELTA_PP and gap_h < JUMP_MAX_GAP_H:
                continue
            rate = delta / gap_h
            # Idle floor
            if rate < IDLE_FLOOR_PP_H:
                continue

            # De-rationing exclusions for the target (natural) surface
            if de_ration:
                if pct_prev >= RATIONING_PCT_THRESHOLD:
                    continue
                # Use span.is_censored (correctly identifies cap from Wrinkle 3)
                if span.is_censored:
                    continue

            mid = t_prev + (t_curr - t_prev) / 2
            how = _hour_of_week(mid)
            hist[how] = hist.get(how, 0.0) + delta
            sample_counts[how] = sample_counts.get(how, 0) + 1

    return hist, sample_counts


def _impute_natural_cells(hist: dict[int, float]) -> dict[int, float]:
    """Impute missing natural-demand cells from their symmetric counterpart.

    The de-rationed (natural) histogram has holes where late-week demand was
    suppressed. The Objective says Fri-night demand ≈ Mon-night demand: impute
    a missing cell from the same hour-of-day on a "mirror" weekday so the
    even-pace target line still breathes with the real daily rhythm rather than
    flat-lining the suppressed cells to zero.

    Mirror map (week starts Sat=0): we mirror late-week days onto early-week
    days of the same hour-of-day:
        Fri(6) <- Mon(2),  Thu(5) <- Tue(3),  Wed(4) <- Wed(4 self, no change)
    For any still-empty cell, fall back to the mean of populated cells at the
    same hour-of-day across all days.

    NOTE: the mirror pairing is a first approximation of "unconstrained
    equivalent". With more weeks we could instead build the natural surface
    only from weeks where the cap was never hit (no imputation needed). Kept
    as a documented heuristic until that data exists.
    """
    if not hist:
        return hist
    out = dict(hist)

    # day index 0=Sat..6=Fri; mirror late-week onto early-week same hour-of-day
    mirror_day = {6: 2, 5: 3}  # Fri<-Mon, Thu<-Tue

    # Precompute hour-of-day -> list of (day, value) for fallback mean
    by_hod: dict[int, list[float]] = {}
    for how, val in hist.items():
        hod = how % 24
        by_hod.setdefault(hod, []).append(val)

    for day in range(7):
        for hod in range(24):
            how = day * 24 + hod
            if how in out:
                continue
            imputed = None
            # Try the day-mirror
            if day in mirror_day:
                src_how = mirror_day[day] * 24 + hod
                if src_how in hist:
                    imputed = hist[src_how]
            # Fallback: mean of same hour-of-day across populated days
            if imputed is None and hod in by_hod and by_hod[hod]:
                imputed = sum(by_hod[hod]) / len(by_hod[hod])
            if imputed is not None:
                out[how] = imputed
    return out


def _histogram_to_weights(hist: dict[int, float]) -> dict[int, float]:
    """Normalise a burn histogram into per-cell duty weights.

    Weights are scaled so the MEAN of populated (active) cells == 1.0. Then
    "active capacity remaining" = sum of weights over the cells between now and
    reset, which is in units of "equivalent average-active-hours". Empty cells
    contribute 0 (genuinely idle hours, e.g. the 01-07 UTC dead zone).

    This keeps the scale comparable to the flat-window count (a flat 17h day
    would sum to ~17), so duty_projected_pct stays in a sane range and
    burn_rate.py's consumption of the number is unaffected.
    """
    if not hist:
        return {}
    active_vals = [v for v in hist.values() if v > 0]
    if not active_vals:
        return {}
    mean_active = sum(active_vals) / len(active_vals)
    if mean_active <= 0:
        return {}
    return {how: v / mean_active for how, v in hist.items()}


def _active_capacity_remaining_2d(
    weights: dict[int, float],
    now: datetime,
    reset_dt: datetime,
) -> float:
    """Weighted active capacity between now and reset over the 2D surface.

    Walks hour-by-hour from now to reset in UTC, summing each cell's weight
    (fractional first/last hour handled). hour-of-week is the linear in-window
    coordinate (week starts Sat 00:00 UTC).
    """
    now = _to_utc(now)
    reset_dt = _to_utc(reset_dt)
    if reset_dt <= now:
        return 0.0

    total = 0.0
    cursor = now
    while cursor < reset_dt:
        # End of the current UTC hour
        next_hour = (cursor + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        seg_end = min(next_hour, reset_dt)
        frac = (seg_end - cursor).total_seconds() / 3600.0
        how = _hour_of_week(cursor)
        total += weights.get(how, 0.0) * frac
        cursor = seg_end
    return total


def _blend_duty_weights(
    bucket_weights: dict[int, float],
    bucket_counts: dict[int, int],
    prior_weights: dict[int, float],
    M: float = DUTY_PRIOR_M,
) -> dict[int, float]:
    """WS7: Blend bucket's duty weights toward the all-models prior per-cell.

    Formula (see DUTY_PRIOR_M docstring above):
        blended[cell] = (n · w_bucket + M · w_prior) / (n + M)

    where n = bucket's sample count for this cell.

    Scaling note: both bucket_weights and prior_weights are already
    mean-normalised to ~1.0 by _histogram_to_weights(), so they share the
    same scale.  The blend is a weighted-average on that shared scale, keeping
    the output on the same ~1.0 mean scale.  This preserves the invariant that
    _active_capacity_remaining_2d() returns a count comparable to the
    flat-window count (≈ active hours, just weighted).

    Empty bucket cells (n=0) fall back entirely to the prior (no collapse).
    Well-sampled cells (n >> M) graduate toward their own observed shape.

    When prior_weights is the bucket's own weights (seven_day is its own prior),
    this is a self-blend: (n · w + M · w) / (n + M) = w — exact no-op.
    """
    if not prior_weights:
        # No prior available — return bucket weights unchanged (safe fallback).
        return bucket_weights

    all_cells = set(bucket_weights.keys()) | set(prior_weights.keys())
    blended: dict[int, float] = {}
    for cell in all_cells:
        n = bucket_counts.get(cell, 0)
        w_bucket = bucket_weights.get(cell, 0.0)
        w_prior = prior_weights.get(cell, 0.0)
        blended[cell] = (n * w_bucket + M * w_prior) / (n + M)

    # Drop zero-weight cells (cells where both bucket and prior had 0 weight).
    return {c: v for c, v in blended.items() if v > 0.0}


def duty_surface(
    conn: sqlite3.Connection,
    bucket: str,
    now: Optional[datetime] = None,
    reset_dt: Optional[datetime] = None,
    surface_kind: str = 'actual',
    prior_bucket: Optional[str] = None,
) -> DutySurface:
    """Return the empirical 2D weekday×hour duty surface (UTC, week from Sat 00:00).

    surface_kind:
      'actual'  - actual demand incl. rationing (for PREDICTIVE projection).
      'natural' - de-rationed demand (for TARGET projection): late-week
                  suppressed/censored cells excluded and imputed from their
                  unconstrained mirror (Fri-night ≈ Mon-night). Prevents the
                  rationing feedback loop the Objective warns about.

    prior_bucket (WS7):
      When set to a different bucket name (typically 'seven_day'), the prior
      bucket's normalised duty weights are used as a Bayesian prior for each
      cell, blended in by sample count (see _blend_duty_weights and DUTY_PRIOR_M).
      This prevents empty (unsampled) cells from collapsing to 0, which caused
      the Sonnet duty projection to read ~10.8% when ~45% was correct.

      When prior_bucket == bucket (or None), the blend is a no-op (self-prior).
      project() passes prior_bucket='seven_day' for all non-seven_day buckets.

    Falls back to the flat 07-24 UTC window when there is not enough histogram
    data to build a surface (e.g. a brand-new bucket), but only AFTER the
    prior-blend is applied — the blend typically rescues thin buckets from this
    fallback by filling empty cells with the all-models prior shape.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if reset_dt is None:
        row = conn.execute(
            "SELECT resets_at FROM quota_snapshots WHERE bucket=? AND resets_at NOT IN ('2286-11-20T17:46:39Z') "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            (bucket,)
        ).fetchone()
        if row and row[0]:
            reset_dt = _to_utc(_parse_iso(row[0]))
        else:
            reset_dt = now + timedelta(days=7)

    de_ration = (surface_kind == 'natural')

    # WS12: Use span-cleaned readings when spans.py is available.
    # This eliminates personal-account contamination (S5 readings bleed into
    # S4's calendar window) and applies running-max data hygiene.
    # Falls back to the legacy _extract_windows path when spans.py is absent.
    if _SPANS_AVAILABLE:
        spans = _spans_extract(conn, bucket, now)
        hist, sample_counts = _build_burn_histogram_from_spans(
            conn, bucket, spans, de_ration=de_ration
        )
    else:
        windows = _extract_windows(conn, bucket, now)
        hist, sample_counts = _build_burn_histogram(conn, bucket, windows, de_ration=de_ration)

    if de_ration:
        hist = _impute_natural_cells(hist)

    weights = _histogram_to_weights(hist)
    active_per_day = ACTIVE_END_HOUR_UTC - ACTIVE_START_HOUR_UTC  # 17 (informational)

    # WS7: Per-cell prior blend.
    # Build the prior weights from prior_bucket if it differs from this bucket.
    # seven_day is self-prior → blend is a mathematical no-op (see docstring).
    # NOTE: we blend BEFORE the MIN_CELLS_FOR_2D check so the filled-in prior
    # cells count toward that threshold and spare thin buckets from falling back
    # to the flat window.
    effective_prior_bucket = prior_bucket if prior_bucket else bucket
    if effective_prior_bucket != bucket:
        # Fetch and normalise the prior bucket's histogram.
        # WS12: use span-cleaned path for the prior bucket too.
        if _SPANS_AVAILABLE:
            prior_spans = _spans_extract(conn, effective_prior_bucket, now)
            prior_hist, _prior_counts = _build_burn_histogram_from_spans(
                conn, effective_prior_bucket, prior_spans, de_ration=de_ration
            )
        else:
            prior_windows = _extract_windows(conn, effective_prior_bucket, now)
            prior_hist, _prior_counts = _build_burn_histogram(
                conn, effective_prior_bucket, prior_windows, de_ration=de_ration
            )
        if de_ration:
            prior_hist = _impute_natural_cells(prior_hist)
        prior_weights = _histogram_to_weights(prior_hist)
        # Blend: cells missing from bucket fall back to the prior's shape.
        weights = _blend_duty_weights(weights, sample_counts, prior_weights)

    # NOTE: require a minimum number of populated cells before trusting the 2D
    # surface. With ~3-5 weeks the per-cell magnitudes are noisy; below this
    # floor we fall back to the flat window (more honest than a sparse surface).
    # Raise this floor as more weeks accrue (the surface gets denser).
    # With WS7 prior-blend, thin buckets will typically clear this threshold
    # (prior fills empty cells); the fallback is still a last resort for
    # completely data-free buckets (e.g. a brand-new install).
    MIN_CELLS_FOR_2D = 12
    if len([v for v in weights.values() if v > 0]) < MIN_CELLS_FOR_2D:
        active_remaining = _active_hours_remaining_empirical(now, reset_dt)
        return DutySurface(
            active_start_utc=ACTIVE_START_HOUR_UTC,
            active_end_utc=ACTIVE_END_HOUR_UTC,
            active_hours_per_day=active_per_day,
            active_hours_remaining=active_remaining,
            method='empirical_window_07_24_utc',
            cell_weights=None,
            surface_kind=None,
        )

    active_remaining = _active_capacity_remaining_2d(weights, now, reset_dt)
    return DutySurface(
        active_start_utc=ACTIVE_START_HOUR_UTC,
        active_end_utc=ACTIVE_END_HOUR_UTC,
        active_hours_per_day=active_per_day,
        active_hours_remaining=active_remaining,
        method=('2d_surface_natural' if de_ration else '2d_surface_actual'),
        cell_weights=weights,
        surface_kind=surface_kind,
    )


# ---------------------------------------------------------------------------
# Effective reset epoch (mirroring burn_rate.py's override logic)
# ---------------------------------------------------------------------------

def _get_effective_reset_epoch(conn: sqlite3.Connection, bucket: str,
                               resets_at_dt: datetime,
                               now: Optional[datetime] = None) -> datetime:
    """Return the effective start of the current quota window.

    Phase A stale-epoch fix (WS10):
      epoch = max([history boundaries ≤ now] + [resets_at − 7d])

    The presumed weekly boundary (resets_at − 7d) is a PEER candidate in the
    max(), not just a fallback.  The old code returned max(history ≤ now) and
    only used resets_at−7d if history was empty — so the Jun-09 21:30 history
    entry was reported as the epoch even though the current week started
    Jun-13 00:00 (resets_at_dt − 7d = Jun-20 − 7d = Jun-13).

    Delegates to spans.get_effective_epoch() when spans.py is available.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if _SPANS_AVAILABLE:
        return _spans_get_epoch(conn, bucket, resets_at_dt, now)

    # Fallback (spans.py not importable): replicate the fix inline
    _ensure_reset_history(conn)
    history = _get_reset_history_boundaries(conn, bucket)
    recent = [b for b in history if b <= now]
    weekly_boundary = resets_at_dt - timedelta(days=7)
    candidates = recent + [weekly_boundary]
    return max(candidates)


def _get_latest_snapshot(conn: sqlite3.Connection, bucket: str) -> Optional[tuple[str, float, str]]:
    """Return (ts, pct, resets_at) for latest snapshot, or None."""
    row = conn.execute(
        "SELECT snapshot_ts, pct_used, resets_at FROM quota_snapshots "
        "WHERE bucket=? AND resets_at NOT IN ('2286-11-20T17:46:39Z') "
        "ORDER BY snapshot_ts DESC LIMIT 1",
        (bucket,)
    ).fetchone()
    return row if row else None


# ---------------------------------------------------------------------------
# Target-mode de-rationed demand estimation
# ---------------------------------------------------------------------------

def _compute_natural_demand_pp_h(windows: list[QuotaWindow]) -> Optional[float]:
    """Estimate natural (de-rationed) demand from unconstrained weeks.

    "Natural" = weeks that did NOT hit 100% early. If most weeks are censored,
    we still use them but annotate.

    De-rationing approach for target mode:
    - Only use windows that are work account and NOT censored (cap not hit).
    - If no uncensored windows, use all work windows as a fallback.
    - This prevents the late-week suppressed-demand artefact from feeding
      the target profile.
    """
    work_completed = [w for w in windows if w.is_work_account and w.is_completed]
    uncensored = [w for w in work_completed if not w.is_censored]

    if uncensored:
        rates = [w.rate_pp_h for w in uncensored]
        return sum(rates) / len(rates)
    elif work_completed:
        # All censored — use them with a note (caller receives this info)
        rates = [w.rate_pp_h for w in work_completed]
        return sum(rates) / len(rates)
    return None


# ---------------------------------------------------------------------------
# Main projection API
# ---------------------------------------------------------------------------

def project(
    conn: sqlite3.Connection,
    bucket: str,
    now: Optional[datetime] = None,
    mode: str = 'predictive',
    K: float = DEFAULT_K,
) -> ProjectionResult:
    """Compute quota projection in one of three modes.

    mode:
      'naive'      - existing calc verbatim (raw pct/elapsed, 06-21 duty, no shrinkage)
      'predictive' - shrinkage prior + 07-24 UTC duty surface (descriptive)
      'target'     - even-pace on de-rationed demand (prescriptive)

    Returns a ProjectionResult with all relevant fields.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    notes: list[str] = []

    # --- Get latest snapshot ---
    latest = _get_latest_snapshot(conn, bucket)
    if latest is None:
        return ProjectionResult(
            mode=mode, bucket=bucket,
            current_pct=0.0, elapsed_h=0.0, h_to_reset=0.0,
            observed_rate_pp_h=0.0, effective_rate_pp_h=0.0,
            prior_pp_h=None, projected_pct_at_reset=0.0,
            duty_projected_pct=0.0, k_used=K,
            prior_window_count=0,
            notes=["No data for this bucket"],
        )

    ts_str, current_pct, resets_at_raw = latest

    if not resets_at_raw or resets_at_raw in GARBAGE_RESETS:
        return ProjectionResult(
            mode=mode, bucket=bucket,
            current_pct=current_pct, elapsed_h=0.0, h_to_reset=0.0,
            observed_rate_pp_h=0.0, effective_rate_pp_h=0.0,
            prior_pp_h=None, projected_pct_at_reset=current_pct,
            duty_projected_pct=current_pct, k_used=K,
            prior_window_count=0,
            notes=["No valid resets_at — cannot project"],
        )

    # --- Normalise resets_at ---
    canonical_map = _merge_resets_at_labels([resets_at_raw])
    resets_at_canon = canonical_map.get(resets_at_raw, resets_at_raw)
    reset_dt = _to_utc(_parse_iso(resets_at_canon))
    h_to_reset = (reset_dt - now).total_seconds() / 3600.0

    # --- Elapsed: how long have we been in this window? ---
    if mode == 'naive':
        # Existing burn_rate.py logic: derived epoch = resets_at - 7 days
        epoch_dt = reset_dt - timedelta(days=7)
    else:
        # Phase A: use epoch fix (history-boundary + weekly-boundary peer max)
        epoch_dt = _get_effective_reset_epoch(conn, bucket, reset_dt, now)
        notes.append(f"Reset epoch: {epoch_dt.isoformat()}")

    elapsed_h = (now - epoch_dt).total_seconds() / 3600.0
    if elapsed_h <= 0:
        elapsed_h = 0.001  # avoid div-by-zero

    observed_rate = current_pct / elapsed_h

    # --- Prior computation (not used in naive mode) ---
    prior_pp_h: Optional[float] = None
    prior_windows: list[QuotaWindow] = []
    prior_window_count = 0

    if mode != 'naive':
        prior_pp_h, prior_windows = compute_prior(conn, bucket, now=now)
        prior_window_count = len(prior_windows)

        # Recency policy note (recompute to surface which branch fired)
        _, recency_note = _select_windows_by_recency(prior_windows, now)
        notes.append(recency_note)

        # Widen K as a function of clean-window count (documented schedule).
        effective_K, k_note = _k_for_window_count(K, prior_window_count)
        notes.append(k_note)
    else:
        effective_K = K

    # --- Effective rate ---
    if mode == 'naive':
        eff_rate = observed_rate
    elif mode == 'predictive':
        eff_rate = effective_rate(observed_rate, elapsed_h, prior_pp_h, effective_K)
        notes.append(
            f"Shrinkage: obs={observed_rate:.3f} * {elapsed_h:.1f}h + prior={prior_pp_h:.3f} * K={effective_K:.0f}h"
        )
    elif mode == 'target':
        # For target: use de-rationed natural demand as prior context
        all_windows = _extract_windows(conn, bucket, now)
        natural_demand = _compute_natural_demand_pp_h(all_windows)
        if natural_demand is not None:
            prior_pp_h = natural_demand
            notes.append(f"Target natural demand: {natural_demand:.3f} pp/hr (de-rationed)")
        eff_rate = effective_rate(observed_rate, elapsed_h, prior_pp_h or observed_rate, effective_K)

        # Target: the "right" rate to be on even pace
        if h_to_reset > 0:
            budget_rate = (100.0 - current_pct) / h_to_reset
        else:
            budget_rate = 0.0

        notes.append(f"Even-pace budget rate: {budget_rate:.3f} pp/hr "
                     f"({100-current_pct:.0f}% over {h_to_reset:.1f}h)")
        ahead = "AHEAD" if eff_rate <= budget_rate else "BEHIND"
        notes.append(f"You are {ahead} of even pace ({eff_rate:.3f} effective vs {budget_rate:.3f} needed)")
    else:
        raise ValueError(f"Unknown mode {mode!r}, expected 'naive'|'predictive'|'target'")

    # --- Projections ---
    if mode == 'naive':
        # Round-the-clock projection
        projected_rtc = current_pct + eff_rate * h_to_reset

        # Duty cycle: 06-21 local (matching existing burn_rate.py)
        # For naive we replicate the existing logic — active 15h/day
        active_h_per_day_naive = 15.0  # 06-21 local
        total_wall_h_to_reset = h_to_reset
        # Approximation: active fraction
        active_fraction = active_h_per_day_naive / 24.0
        projected_duty = current_pct + eff_rate * (total_wall_h_to_reset * active_fraction)
        notes.append("Naive mode: raw pct/elapsed, 15h/day duty (06-21 local), no shrinkage")
    else:
        # Round-the-clock projection
        projected_rtc = current_pct + eff_rate * h_to_reset

        # Duty surface: PREDICTIVE uses the ACTUAL demand surface (incl.
        # rationing); TARGET uses the DE-RATIONED natural surface so the
        # even-pace line breathes on natural, not suppressed, demand.
        # WS7: pass prior_bucket='seven_day' for all non-seven_day buckets so
        # empty cells fall back to the all-models shape (no collapse to idle).
        surface_kind = 'natural' if mode == 'target' else 'actual'
        ws7_prior = 'seven_day' if bucket != 'seven_day' else None
        ds = duty_surface(conn, bucket, now=now, reset_dt=reset_dt,
                          surface_kind=surface_kind, prior_bucket=ws7_prior)
        projected_duty = current_pct + eff_rate * ds.active_hours_remaining
        notes.append(
            f"Duty surface [{ds.method}]: {ds.active_hours_remaining:.1f} "
            f"weighted active capacity to reset"
        )

    return ProjectionResult(
        mode=mode,
        bucket=bucket,
        current_pct=current_pct,
        elapsed_h=elapsed_h,
        h_to_reset=h_to_reset,
        observed_rate_pp_h=observed_rate,
        effective_rate_pp_h=eff_rate,
        prior_pp_h=prior_pp_h,
        projected_pct_at_reset=projected_rtc,
        duty_projected_pct=projected_duty,
        k_used=effective_K,
        prior_window_count=prior_window_count,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Convenience: open the default DB
# ---------------------------------------------------------------------------

def open_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Recency-policy synthetic test
# ---------------------------------------------------------------------------

def _make_synthetic_window(end_dt: datetime, rate: float) -> QuotaWindow:
    """Build a synthetic completed work window ending at end_dt with given rate."""
    span = 168.0
    return QuotaWindow(
        bucket="seven_day",
        start_ts=end_dt - timedelta(hours=span),
        end_ts=end_dt,
        start_pct=0.0,
        end_pct=rate * span,
        actual_span_h=span,
        rate_pp_h=rate,
        is_work_account=True,
        is_censored=False,
        is_completed=True,
        source="synthetic",
    )


def _run_recency_synthetic_test() -> bool:
    """Exercise the recency policy's THIN, TRANSITIONAL and MATURE branches.

    Builds synthetic window sets and asserts:
      - <=5 windows  -> THIN branch, equal weights (all == 1.0).
      - 6-7 windows  -> TRANSITIONAL branch, light decay.
      - >=8 windows  -> MATURE branch: rolling cap + exponential decay so recent
                        windows outweigh stale ones, and the prior tracks a
                        habit-drift (rising) trend rather than the flat mean.

    Returns True if all assertions pass (also prints a summary).
    """
    now = datetime(2026, 6, 13, 0, tzinfo=timezone.utc)
    ok = True

    # THIN: 3 windows, equal weight
    thin = [_make_synthetic_window(now - timedelta(weeks=i), 0.5) for i in range(3)]
    weighted, note = _select_windows_by_recency(thin, now)
    weights = [w for _, w in weighted]
    thin_ok = all(abs(w - 1.0) < 1e-9 for w in weights) and len(weighted) == 3
    print(f"  THIN (3 windows):        {note}")
    print(f"    weights={[round(w,3) for w in weights]}  equal_weight={'PASS' if thin_ok else 'FAIL'}")
    ok = ok and thin_ok

    # TRANSITIONAL: 7 windows, light decay (weights should differ, newest highest)
    trans = [_make_synthetic_window(now - timedelta(weeks=i), 0.5) for i in range(7)]
    weighted_t, note_t = _select_windows_by_recency(trans, now)
    weights_t = [w for _, w in weighted_t]
    trans_ok = (len(weighted_t) == 7
                and weights_t[0] >= weights_t[-1]
                and weights_t[0] > weights_t[-1])  # strictly decaying
    print(f"  TRANSITIONAL (7 windows): {note_t}")
    print(f"    weights(newest→oldest)={[round(w,3) for w in weights_t]}  "
          f"decaying={'PASS' if trans_ok else 'FAIL'}")
    ok = ok and trans_ok

    # MATURE: 10 windows with a RISING habit-drift trend (older=low, recent=high).
    # The decayed prior should land ABOVE the flat mean (recency favours the
    # recent higher-burn windows).
    mature_rates = [0.3, 0.3, 0.4, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]  # oldest→newest
    mature = []
    for idx, rate in enumerate(mature_rates):
        weeks_ago = (len(mature_rates) - 1 - idx)  # newest has weeks_ago=0
        mature.append(_make_synthetic_window(now - timedelta(weeks=weeks_ago), rate))
    weighted_m, note_m = _select_windows_by_recency(mature, now)
    decayed_prior = _weighted_mean(weighted_m)
    flat_mean = sum(mature_rates) / len(mature_rates)
    rolling_cap_ok = len(weighted_m) <= RECENCY_ROLLING_WEEKS
    drift_ok = decayed_prior > flat_mean  # recency tracks the rising trend
    print(f"  MATURE (10 windows):      {note_m}")
    print(f"    flat_mean={flat_mean:.4f}  decayed_prior={decayed_prior:.4f}  "
          f"(decayed>flat, tracks drift)={'PASS' if drift_ok else 'FAIL'}")
    print(f"    rolling_cap≤{RECENCY_ROLLING_WEEKS}: kept {len(weighted_m)} windows  "
          f"{'PASS' if rolling_cap_ok else 'FAIL'}")
    ok = ok and drift_ok and rolling_cap_ok

    # MATURE rolling cap proof: 15 windows -> capped at RECENCY_ROLLING_WEEKS
    many = [_make_synthetic_window(now - timedelta(weeks=i), 0.5) for i in range(15)]
    weighted_many, _ = _select_windows_by_recency(many, now)
    cap_ok = len(weighted_many) == RECENCY_ROLLING_WEEKS
    print(f"  MATURE (15 windows):      rolling cap kept {len(weighted_many)} "
          f"(expected {RECENCY_ROLLING_WEEKS})  {'PASS' if cap_ok else 'FAIL'}")
    ok = ok and cap_ok

    # K-widening schedule proof
    print("  K-widening schedule:")
    for n, expect_mult in [(1, K_WIDEN_VERY_THIN), (2, K_WIDEN_VERY_THIN),
                           (3, K_WIDEN_THIN), (4, K_WIDEN_THIN),
                           (5, K_WIDEN_BASELINE), (8, K_WIDEN_BASELINE)]:
        k_eff, _ = _k_for_window_count(DEFAULT_K, n)
        exp = DEFAULT_K * expect_mult
        k_ok = abs(k_eff - exp) < 1e-9
        ok = ok and k_ok
        print(f"    n={n:>2}: K={k_eff:>5.0f}h (expect {exp:.0f})  {'PASS' if k_ok else 'FAIL'}")

    print(f"\n  RECENCY SYNTHETIC TEST: {'ALL PASS ✓' if ok else 'FAILURES ✗'}")
    return ok


# ---------------------------------------------------------------------------
# Standalone test / report
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        sys.exit(1)

    conn = open_db(db_path)
    now = datetime.now(timezone.utc)

    print("=" * 72)
    print(f"WS4 Bayesian Projection Report")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 72)

    buckets_to_test = ["seven_day"]
    # Check if five_hour is present and recent
    fh_latest = _get_latest_snapshot(conn, "five_hour")
    if fh_latest:
        fh_age_h = (now - _to_utc(_parse_iso(fh_latest[0]))).total_seconds() / 3600.0
        if fh_age_h < 6:
            buckets_to_test.append("five_hour")

    for bucket in buckets_to_test:
        print(f"\n{'─'*72}")
        print(f"BUCKET: {bucket}")
        print(f"{'─'*72}")

        # --- Prior breakdown ---
        print("\n[Prior Computation]")
        prior_pp_h, prior_windows = compute_prior(conn, bucket)
        print(f"  Prior: {prior_pp_h:.4f} pp/hr  ({len(prior_windows)} work windows)")

        if prior_windows:
            print(f"\n  {'Window':^55}  {'Span':>7}  {'End%':>5}  {'Rate':>8}  {'Censored':>9}  {'Source'}")
            print(f"  {'─'*55}  {'─'*7}  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*20}")
            for w in prior_windows:
                start_s = w.start_ts.strftime("%m-%d %H:%M")
                end_s   = w.end_ts.strftime("%m-%d %H:%M")
                label = f"{start_s} → {end_s}"
                censor = "CENSORED" if w.is_censored else ""
                print(
                    f"  {label:<55}  {w.actual_span_h:>6.1f}h  "
                    f"{w.end_pct:>4.0f}%  {w.rate_pp_h:>7.4f}  {censor:>9}  {w.source}"
                )

        # --- All detected windows (including non-work) ---
        print("\n[All Detected Windows (incl. non-work)]")
        all_windows = _extract_windows(conn, bucket, now)
        if all_windows:
            print(f"  {'Window':^55}  {'Span':>7}  {'End%':>5}  {'Rate':>8}  {'Work':>5}  {'Done':>5}  {'Cens':>5}")
            print(f"  {'─'*55}  {'─'*7}  {'─'*5}  {'─'*8}  {'─'*5}  {'─'*5}  {'─'*5}")
            for w in all_windows:
                start_s = w.start_ts.strftime("%m-%d %H:%M")
                end_s   = w.end_ts.strftime("%m-%d %H:%M")
                label   = f"{start_s} → {end_s}"
                work_s = 'Y' if w.is_work_account else 'N'
                done_s = 'Y' if w.is_completed else 'N'
                cens_s = 'Y' if w.is_censored else 'N'
                print(
                    f"  {label:<55}  {w.actual_span_h:>6.1f}h  "
                    f"{w.end_pct:>4.0f}%  {w.rate_pp_h:>7.4f}  "
                    f"{work_s:>5}  {done_s:>5}  {cens_s:>5}"
                )
        else:
            print("  (no windows detected)")

        # --- Three-mode comparison ---
        print("\n[Three-Mode Comparison]")
        header = f"  {'Mode':<12}  {'Obs(pp/h)':>9}  {'Eff(pp/h)':>9}  {'Prior':>7}  {'K':>5}  {'RTC%@reset':>10}  {'Duty%@reset':>11}  {'Elapsed':>7}  {'H-to-reset':>10}"
        print(header)
        print(f"  {'─'*12}  {'─'*9}  {'─'*9}  {'─'*7}  {'─'*5}  {'─'*10}  {'─'*11}  {'─'*7}  {'─'*10}")

        for mode in ("naive", "predictive", "target"):
            try:
                r = project(conn, bucket, now=now, mode=mode)
                prior_s = f"{r.prior_pp_h:.4f}" if r.prior_pp_h is not None else "   n/a"
                print(
                    f"  {mode:<12}  {r.observed_rate_pp_h:>9.4f}  {r.effective_rate_pp_h:>9.4f}  "
                    f"{prior_s:>7}  {r.k_used:>5.0f}  "
                    f"{r.projected_pct_at_reset:>9.1f}%  {r.duty_projected_pct:>10.1f}%  "
                    f"{r.elapsed_h:>6.1f}h  {r.h_to_reset:>9.1f}h"
                )
            except Exception as e:
                print(f"  {mode:<12}  ERROR: {e}")

        # --- Notes from predictive mode ---
        print("\n[Predictive Mode Notes]")
        r_pred = project(conn, bucket, now=now, mode='predictive')
        for note in r_pred.notes:
            print(f"  • {note}")

        # --- Notes from target mode ---
        print("\n[Target Mode Notes]")
        r_tgt = project(conn, bucket, now=now, mode='target')
        for note in r_tgt.notes:
            print(f"  • {note}")

        # --- 2D duty surface (actual vs natural) ---
        if bucket == "seven_day":
            for kind in ("actual", "natural"):
                ds = duty_surface(conn, bucket, now=now, surface_kind=kind)
                print(f"\n[2D Duty Surface — {kind}]  method={ds.method}  "
                      f"capacity_remaining={ds.active_hours_remaining:.1f}")
                if ds.cell_weights:
                    # Compact weekday×hour grid: rows = day (Sat..Fri), cols = UTC hour 0..23
                    dow_names = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]
                    print("       " + "".join(f"{h:>4}" for h in range(0, 24, 1)))
                    for day in range(7):
                        cells = []
                        for hod in range(24):
                            how = day * 24 + hod
                            w = ds.cell_weights.get(how, 0.0)
                            cells.append(f"{w:>4.1f}" if w > 0 else "   ·")
                        print(f"  {dow_names[day]}  " + "".join(cells))
                    # Highlight hot zones
                    top = sorted(ds.cell_weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
                    print("  Hot cells (UTC / AEST):")
                    for how, w in top:
                        dn = dow_names[how // 24]
                        print(f"    {dn} {how % 24:02d}:00 UTC  ({_aest_label(how)})  weight={w:.2f}")
                else:
                    print("  (insufficient data — fell back to flat 07-24 window)")

        # --- Early-week shrinkage demo ---
        print("\n[Shrinkage Behaviour: early-week burst simulation]")
        prior_pp_h_val, _ = compute_prior(conn, bucket)
        print(f"  Prior: {prior_pp_h_val:.4f} pp/hr   K={DEFAULT_K:.0f}h")
        print(f"  {'Elapsed':>8}  {'Observed':>10}  {'Effective':>10}  {'Ratio':>8}")
        print(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}")
        burst_rate = 3.0  # simulated 3× burst
        for elapsed in [1, 3, 6, 12, 24, 48, 72]:
            eff = effective_rate(burst_rate, elapsed, prior_pp_h_val, DEFAULT_K)
            ratio = eff / burst_rate if burst_rate > 0 else float('nan')
            print(f"  {elapsed:>7}h  {burst_rate:>9.4f}  {eff:>9.4f}  {ratio:>7.1%}")

    # --- Predictive vs target duty divergence (endogeneity split) ---
    print("\n" + "=" * 72)
    print("Predictive vs Target duty capacity (endogeneity split)")
    print("Target should de-ration: natural surface excludes/imputes the")
    print("suppressed late-week cells, so its capacity differs from actual.")
    print("─" * 72)
    ds_actual = duty_surface(conn, "seven_day", now=now, surface_kind='actual')
    ds_natural = duty_surface(conn, "seven_day", now=now, surface_kind='natural')
    print(f"  actual  (predictive): capacity={ds_actual.active_hours_remaining:.2f}  "
          f"method={ds_actual.method}  "
          f"populated_cells={len([v for v in (ds_actual.cell_weights or {}).values() if v>0])}")
    print(f"  natural (target):     capacity={ds_natural.active_hours_remaining:.2f}  "
          f"method={ds_natural.method}  "
          f"populated_cells={len([v for v in (ds_natural.cell_weights or {}).values() if v>0])}")
    if ds_actual.cell_weights and ds_natural.cell_weights:
        n_act = len([v for v in ds_actual.cell_weights.values() if v > 0])
        n_nat = len([v for v in ds_natural.cell_weights.values() if v > 0])
        print(f"  → natural surface has {n_nat} cells vs actual {n_act} "
              f"(imputed from mirror to fill suppressed late-week holes)")

    # --- Recency policy synthetic test (drives the >=8-window MATURE path) ---
    print("\n" + "=" * 72)
    print("Recency policy synthetic test (THIN vs MATURE branches)")
    print("─" * 72)
    _run_recency_synthetic_test()

    print("\n" + "=" * 72)
    print("Sanity check: Jun 06-13 window split")
    print("Expected: two windows, ~0.75 and ~1.02 pp/hr, NOT one gentle ~0.45")
    print("─" * 72)
    # Pull the windows for that period from our detected list
    target_start = datetime(2026, 6, 6, 0, tzinfo=timezone.utc)
    target_end   = datetime(2026, 6, 13, 1, tzinfo=timezone.utc)
    conn2 = open_db(db_path)
    test_now = datetime(2026, 6, 13, 6, tzinfo=timezone.utc)
    all_wins = _extract_windows(conn2, "seven_day", test_now)
    period_wins = [
        w for w in all_wins
        if w.start_ts >= target_start - timedelta(hours=1)
        and w.end_ts <= target_end + timedelta(hours=1)
    ]
    if period_wins:
        for w in period_wins:
            print(
                f"  {w.start_ts.strftime('%m-%d %H:%M')} → {w.end_ts.strftime('%m-%d %H:%M')}"
                f"  span={w.actual_span_h:.1f}h  end%={w.end_pct:.0f}%  rate={w.rate_pp_h:.4f} pp/hr"
                f"  work={w.is_work_account}"
            )
    else:
        print("  (no windows found in range — check detection logic)")
    print("=" * 72)
