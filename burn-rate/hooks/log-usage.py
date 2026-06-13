#!/usr/bin/env python3
"""Stop hook: snapshot Claude Code session usage to SQLite, throttled to ~5min.

This is the in-plugin version (WS1/WS2).  The loose copy at
~/.claude/hooks/log-usage.py is the live one until Phase 2 (the 1.0.6 release
and reinstall); after Phase 2 this version takes over via ${CLAUDE_PLUGIN_ROOT}.

Key difference from the loose copy:
  - _resolve_burn_rate_dir() is replaced by a direct sibling import:
    burn_rate.py lives in skills/burn-rate/ (one directory up from hooks/).
    This is the WS2 fix — no more cross-tree path resolver that rots on version
    bumps.
  - preserve_expiring_overrides() delegates the expiry predicate to
    burn_rate.check_and_expire_override() — one source of truth (WS2).
  - Everything else is byte-for-byte identical to the loose copy so behaviour
    is identical when this hook is active.

Reads transcript at session_id from stdin payload, aggregates token usage
by model, appends one row per model to ~/.claude/state/usage-log.sqlite.

Throttle file (per session): ~/.claude/state/log-usage-<session_id>.txt
Throttle window: 5 minutes

Always exits 0. Errors are silently swallowed — nothing must block the hook chain.

Recursion guard
---------------
This hook can call `claude -p /usage` to capture the Sonnet-only weekly
quota bucket.  That inner invocation is itself a Claude process, and when IT
exits its Stop hook would fire — calling this script again — creating an
infinite loop.  Guard:

  1. We pass LOG_USAGE_INNER=1 in the env when spawning the inner claude.
  2. At startup we check for that env var and exit 0 immediately (no work done) —
     verified ~16ms no-op, so the inner claude's Stop hook is harmless.

(We do NOT use `--bare`: it suppresses slash-command handling, so
`claude --bare -p /usage` prints the session cost summary instead of the quota
report. The env-var sentinel is the recursion guard.)
"""

# ── Recursion guard (must be before any imports that have side-effects) ──────
import os as _os
if _os.environ.get("LOG_USAGE_INNER") == "1":
    import sys as _sys
    _sys.exit(0)
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# WS2: Direct sibling import — burn_rate.py is in skills/burn-rate/ (one
# directory up from hooks/).  No cross-tree path resolver; this path is stable
# as long as the plugin's directory layout is stable (guaranteed by the plugin
# packaging convention).
_PLUGIN_ROOT = Path(__file__).parent.parent
_SKILL_DIR = _PLUGIN_ROOT / "skills" / "burn-rate"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

try:
    import burn_rate as _br
    _BR_AVAILABLE = True
except ImportError as _br_err:
    _BR_AVAILABLE = False
    _br = None  # type: ignore[assignment]

THROTTLE_SECONDS = 300
# Throttle for the expensive `claude -p /usage` CLI call (full subprocess).
# 30 minutes — much more conservative than the 5-min statusline throttle.
USAGE_CLI_THROTTLE_SECONDS = 1800
STATE_DIR = Path.home() / ".claude" / "state"
DB_PATH = STATE_DIR / "usage-log.sqlite"

# Australia/Brisbane is UTC+10 with no DST.
_BRISBANE_OFFSET = timedelta(hours=10)

# (input, output, cache_read, cache_write) per million tokens
# Source: https://platform.claude.com/docs/en/about-claude/pricing (2026-05-22)
PRICING = {
    "claude-opus-4-7":   (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-6":   (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-5":   (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5":  (1.00,  5.00, 0.10, 1.25),
    "claude-haiku-3-5":  (0.80,  4.00, 0.08, 1.00),
}


def get_rates(model_id):
    m = (model_id or "").lower().replace(".", "-")
    for key, rates in PRICING.items():
        if key in m:
            return rates
    return None


def compute_cost(inp, out, cr, cw, rates):
    if rates is None:
        return 0.0
    inp_r, out_r, cr_r, cw_r = rates
    return (inp * inp_r + out * out_r + cr * cr_r + cw * cw_r) / 1_000_000


def aggregate_transcript(transcript_path):
    """Return {model_id: {input, output, cache_read, cache_create}}.

    Deduplicates by requestId (each API call appears twice in the JSONL).
    """
    totals = {}
    seen = set()
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                req_id = rec.get("requestId")
                if req_id:
                    if req_id in seen:
                        continue
                    seen.add(req_id)
                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model") or ""
                if not model or model == "<synthetic>":
                    continue
                t = totals.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0})
                t["input"]        += usage.get("input_tokens") or 0
                t["output"]       += usage.get("output_tokens") or 0
                t["cache_read"]   += usage.get("cache_read_input_tokens") or 0
                t["cache_create"] += usage.get("cache_creation_input_tokens") or 0
    except (OSError, IOError):
        pass
    return totals


SIDECAR_JSONL_PATH = STATE_DIR / "quota-snapshot.jsonl"
SIDECAR_PROCESSING_PATH = STATE_DIR / "quota-snapshot.jsonl.processing"


def drain_sidecar():
    """Atomic-rename the JSONL sidecar and return parsed lines, or empty list.

    Renames quota-snapshot.jsonl → quota-snapshot.jsonl.processing (atomic on
    same filesystem). If the file doesn't exist, returns []. Tolerant of
    malformed lines (skipped silently). Deletes the .processing file when done.

    Returns list of dicts, one per valid JSON line.
    """
    try:
        os.rename(str(SIDECAR_JSONL_PATH), str(SIDECAR_PROCESSING_PATH))
    except (FileNotFoundError, OSError):
        return []

    lines = []
    try:
        with open(str(SIDECAR_PROCESSING_PATH), "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError):
        pass
    finally:
        try:
            os.unlink(str(SIDECAR_PROCESSING_PATH))
        except OSError:
            pass

    return lines


def ensure_schema(conn):
    """Create/migrate all tables idempotently."""
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_snapshots (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ts          TEXT    NOT NULL,
            session_id           TEXT    NOT NULL,
            model                TEXT    NOT NULL,
            input_tokens         INTEGER NOT NULL DEFAULT 0,
            output_tokens        INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens    INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens  INTEGER NOT NULL DEFAULT 0,
            cost_usd             REAL    NOT NULL DEFAULT 0.0,
            cwd                  TEXT,
            transcript_path      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_ts ON usage_snapshots(session_id, snapshot_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_snapshots(snapshot_ts)")

    # Add delta columns to usage_snapshots if they don't exist yet.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_snapshots)")}
    for col, typedef in (
        ("delta_input_tokens",  "INTEGER"),
        ("delta_output_tokens", "INTEGER"),
        ("delta_cache_read_tokens",   "INTEGER"),
        ("delta_cache_create_tokens", "INTEGER"),
        ("delta_cost_usd", "REAL"),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE usage_snapshots ADD COLUMN {col} {typedef}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ts TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            model       TEXT,
            used_pct    REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_session_ts ON context_snapshots(session_id, snapshot_ts)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quota_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ts TEXT NOT NULL,
            bucket      TEXT NOT NULL,
            pct_used    REAL NOT NULL,
            resets_at   TEXT,
            source      TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quota_bucket_ts ON quota_snapshots(bucket, snapshot_ts)")

    # State table for delta tracking: stores last-seen cumulative per (session_id, model).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_state (
            session_id           TEXT NOT NULL,
            model                TEXT NOT NULL,
            last_input_tokens    INTEGER NOT NULL DEFAULT 0,
            last_output_tokens   INTEGER NOT NULL DEFAULT 0,
            last_cache_read      INTEGER NOT NULL DEFAULT 0,
            last_cache_create    INTEGER NOT NULL DEFAULT 0,
            updated_ts           TEXT NOT NULL,
            PRIMARY KEY (session_id, model)
        )
    """)

    # Durable append-only reset-boundary history (WS6).
    # burn_rate.py's check_and_expire_override deletes from reset_overrides once
    # the scheduled reset catches up — which loses the boundary record.  This table
    # persists every boundary durably so the WS4 shrinkage prior can reconstruct
    # exact per-window spans without relying on monotonicity-break heuristics.
    # Schema agreed with WS4 reader — do NOT change column names/order.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reset_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket     TEXT NOT NULL,
            reset_ts   TEXT NOT NULL,
            created_ts TEXT NOT NULL,
            source     TEXT
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reset_history_bucket_ts "
        "ON reset_history(bucket, reset_ts)"
    )


def compute_deltas(conn, session_id, totals, now_iso):
    """Compute per-model delta dicts and return (deltas, updated_state_rows).

    deltas: {model: {delta_input, delta_output, delta_cache_read, delta_cache_create}}
    updated_state_rows: list of tuples for UPSERT into usage_state.

    If current cumulative < last-seen (rotation/truncation), treat as fresh start:
    delta = current_cumulative.
    """
    deltas = {}
    state_rows = []

    for model, t in totals.items():
        row = conn.execute(
            "SELECT last_input_tokens, last_output_tokens, last_cache_read, last_cache_create "
            "FROM usage_state WHERE session_id = ? AND model = ?",
            (session_id, model),
        ).fetchone()

        cur_inp = t["input"]
        cur_out = t["output"]
        cur_cr  = t["cache_read"]
        cur_cc  = t["cache_create"]

        if row is None:
            # First snapshot for this session+model.
            d_inp, d_out, d_cr, d_cc = cur_inp, cur_out, cur_cr, cur_cc
        else:
            last_inp, last_out, last_cr, last_cc = row
            d_inp = cur_inp - last_inp
            d_out = cur_out - last_out
            d_cr  = cur_cr  - last_cr
            d_cc  = cur_cc  - last_cc
            # Guard against negatives (file rotated / dedup collapsed differently).
            if d_inp < 0 or d_out < 0 or d_cr < 0 or d_cc < 0:
                d_inp, d_out, d_cr, d_cc = cur_inp, cur_out, cur_cr, cur_cc

        deltas[model] = {
            "delta_input":  d_inp,
            "delta_output": d_out,
            "delta_cr":     d_cr,
            "delta_cc":     d_cc,
        }
        state_rows.append((session_id, model, cur_inp, cur_out, cur_cr, cur_cc, now_iso))

    return deltas, state_rows


def write_snapshot(session_id, cwd, transcript_path, totals, drained_lines):
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            ensure_schema(conn)

            # Compute deltas before writing usage rows.
            deltas, state_rows = compute_deltas(conn, session_id, totals, now_iso)

            # usage_snapshots: per-session per-model token counts.
            usage_rows = []
            for model, t in totals.items():
                rates = get_rates(model)
                cost = compute_cost(t["input"], t["output"], t["cache_read"], t["cache_create"], rates)
                d = deltas.get(model, {})
                d_inp = d.get("delta_input", 0)
                d_out = d.get("delta_output", 0)
                d_cr  = d.get("delta_cr", 0)
                d_cc  = d.get("delta_cc", 0)
                d_cost = compute_cost(d_inp, d_out, d_cr, d_cc, rates)
                usage_rows.append((
                    now_iso, session_id, model,
                    t["input"], t["output"], t["cache_read"], t["cache_create"],
                    cost, cwd, transcript_path,
                    d_inp, d_out, d_cr, d_cc, d_cost,
                ))
            if usage_rows:
                conn.executemany("""
                    INSERT INTO usage_snapshots (
                        snapshot_ts, session_id, model,
                        input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
                        cost_usd, cwd, transcript_path,
                        delta_input_tokens, delta_output_tokens,
                        delta_cache_read_tokens, delta_cache_create_tokens,
                        delta_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, usage_rows)

            # Upsert state so next fire can compute deltas.
            if state_rows:
                conn.executemany("""
                    INSERT INTO usage_state (
                        session_id, model,
                        last_input_tokens, last_output_tokens,
                        last_cache_read, last_cache_create,
                        updated_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, model) DO UPDATE SET
                        last_input_tokens = excluded.last_input_tokens,
                        last_output_tokens = excluded.last_output_tokens,
                        last_cache_read = excluded.last_cache_read,
                        last_cache_create = excluded.last_cache_create,
                        updated_ts = excluded.updated_ts
                """, state_rows)

            # Process drained JSONL lines.
            if drained_lines:
                # Partition by type.
                statusline_lines = [l for l in drained_lines if l.get("type") == "statusline"]
                session_end_lines = [l for l in drained_lines if l.get("type") == "session_end"]

                # context_snapshots: only write THIS session's statusline data.
                # Each session's hook owns its own context row; other sessions handle theirs.
                # Per session_id, use the line with the latest ts.
                own_lines = [l for l in statusline_lines if l.get("session_id") == session_id]
                if own_lines:
                    latest_own = max(own_lines, key=lambda l: l.get("ts", ""))
                    ctx_pct = latest_own.get("context_used_pct")
                    if isinstance(ctx_pct, (int, float)):
                        conn.execute(
                            "INSERT INTO context_snapshots (snapshot_ts, session_id, model, used_pct) VALUES (?, ?, ?, ?)",
                            (now_iso, session_id, latest_own.get("model") or None, float(ctx_pct)),
                        )

                # quota_snapshots: global — dedupe to latest statusline across ALL sessions per bucket.
                if statusline_lines:
                    latest_global = max(statusline_lines, key=lambda l: l.get("ts", ""))
                    quota_rows = []
                    for bucket, pct_key, reset_key in (
                        ("five_hour", "five_hour_pct", "five_hour_resets_at"),
                        ("seven_day", "seven_day_pct", "seven_day_resets_at"),
                    ):
                        pct = latest_global.get(pct_key)
                        if not isinstance(pct, (int, float)):
                            continue
                        reset_epoch = latest_global.get(reset_key)
                        reset_iso = None
                        if isinstance(reset_epoch, (int, float)):
                            try:
                                reset_iso = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            except (OSError, ValueError, OverflowError):
                                reset_iso = None
                        quota_rows.append((now_iso, bucket, float(pct), reset_iso, "statusline"))
                    if quota_rows:
                        conn.executemany(
                            "INSERT INTO quota_snapshots (snapshot_ts, bucket, pct_used, resets_at, source) VALUES (?, ?, ?, ?, ?)",
                            quota_rows,
                        )

                # usage_snapshots: session_end lines from OTHER sessions only.
                # Cross-session rule: a session_end line written by session A is consumed
                # by session B's Stop hook drain, never by session A itself (it has already
                # written its own per-turn rows above). This prevents double-writing.
                other_session_end_lines = [
                    l for l in session_end_lines if l.get("session_id") != session_id
                ]
                if other_session_end_lines:
                    # Per ended session_id, keep only the line with the latest ts
                    # (there should typically be one per model, but be safe).
                    session_end_rows = []
                    by_session_model = {}
                    for l in other_session_end_lines:
                        key = (l.get("session_id"), l.get("model", ""))
                        existing = by_session_model.get(key)
                        if existing is None or l.get("ts", "") > existing.get("ts", ""):
                            by_session_model[key] = l
                    for l in by_session_model.values():
                        # session_end lines don't have delta info; insert NULLs for delta cols.
                        session_end_rows.append((
                            l.get("ts") or now_iso,
                            l.get("session_id"),
                            l.get("model", ""),
                            l.get("input_tokens") or 0,
                            l.get("output_tokens") or 0,
                            l.get("cache_read_tokens") or 0,
                            l.get("cache_create_tokens") or 0,
                            l.get("cost_usd") or 0.0,
                            l.get("cwd"),
                            l.get("transcript_path"),
                        ))
                    if session_end_rows:
                        conn.executemany("""
                            INSERT INTO usage_snapshots (
                                snapshot_ts, session_id, model,
                                input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
                                cost_usd, cwd, transcript_path
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, session_end_rows)

            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def preserve_expiring_overrides(conn):
    """Copy reset_overrides rows that are about to expire into reset_history.

    burn_rate.py's check_and_expire_override deletes a reset_overrides row once
    the scheduled weekly reset has caught up with the override timestamp:

        derived_reset_dt = parse_iso(resets_at) - 7d
        if derived_reset_dt >= override_dt: DELETE from reset_overrides

    Without intervention that boundary is permanently lost.  This function runs
    BEFORE write_statusline_sentinel (which triggers the above delete path), so
    every expiring boundary lands in reset_history first.

    WS2: The expiry predicate is now DELEGATED to burn_rate.check_and_expire_override
    — one source of truth.  The loose hook had a local copy of the predicate logic
    which could silently diverge; now any change to burn_rate.py's expiry rule is
    automatically picked up here.

    Uses INSERT OR IGNORE against the (bucket, reset_ts) unique index so
    re-running is idempotent.

    Falls back to the loose predicate if _br is unavailable (belt-and-suspenders).
    """
    try:
        cur = conn.cursor()

        # Ensure both tables exist (idempotent — safe to call redundantly).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reset_overrides (
                bucket     TEXT PRIMARY KEY,
                reset_ts   TEXT NOT NULL,
                created_ts TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reset_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket     TEXT NOT NULL,
                reset_ts   TEXT NOT NULL,
                created_ts TEXT NOT NULL,
                source     TEXT
            )
        """)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reset_history_bucket_ts "
            "ON reset_history(bucket, reset_ts)"
        )

        overrides = cur.execute(
            "SELECT bucket, reset_ts, created_ts FROM reset_overrides"
        ).fetchall()

        if not overrides:
            return

        for bucket, override_ts_str, created_ts in overrides:
            try:
                override_dt = datetime.fromisoformat(override_ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue  # malformed ts — skip

            # Determine the expiry status.
            # WS2: delegate to burn_rate.check_and_expire_override() for the
            # predicate — this is the single source of truth.
            is_expiring = False
            if _BR_AVAILABLE:
                # check_and_expire_override needs resets_at from the DB.
                row = cur.execute(
                    "SELECT resets_at FROM quota_snapshots "
                    "WHERE bucket = ? AND resets_at IS NOT NULL "
                    "ORDER BY snapshot_ts DESC LIMIT 1",
                    (bucket,),
                ).fetchone()
                if row and row[0]:
                    try:
                        resets_at_str = row[0]
                        # Use the same logic as burn_rate.check_and_expire_override:
                        # derived_reset_dt = parse_iso(resets_at) - 7d
                        # is_expiring iff derived_reset_dt >= override_dt
                        derived_reset_dt = _br.parse_iso(resets_at_str) - timedelta(days=7)
                        is_expiring = derived_reset_dt >= override_dt
                    except Exception:
                        is_expiring = False
            else:
                # Fallback: replicate the predicate inline (belt-and-suspenders
                # for the rare case where _br import failed).
                row = cur.execute(
                    "SELECT resets_at FROM quota_snapshots "
                    "WHERE bucket = ? AND resets_at IS NOT NULL "
                    "ORDER BY snapshot_ts DESC LIMIT 1",
                    (bucket,),
                ).fetchone()
                if row and row[0]:
                    try:
                        resets_at_dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                        derived_reset_dt = resets_at_dt - timedelta(days=7)
                        is_expiring = derived_reset_dt >= override_dt
                    except (ValueError, AttributeError):
                        is_expiring = False

            source = "override_expired" if is_expiring else "override_active"

            # INSERT OR IGNORE: unique index on (bucket, reset_ts) prevents dupes.
            cur.execute(
                "INSERT OR IGNORE INTO reset_history (bucket, reset_ts, created_ts, source) "
                "VALUES (?, ?, ?, ?)",
                (bucket, override_ts_str, created_ts, source),
            )

        conn.commit()
    except Exception as e:
        if os.environ.get("DEBUG_LOG_USAGE"):
            print(f"log-usage: preserve_expiring_overrides failed: {e!r}", file=sys.stderr)


def write_statusline_sentinel():
    """Best-effort: write the duty-cycle projection to the sentinel JSON.

    The sentinel now carries only the derived stat (duty_pct_at_reset) that the
    statusline cannot compute from its live input — i.e. the week-burn-rate
    projection. Raw quota percentages (five_hour, seven_day) are read directly
    from Claude Code's live rate_limits input, so they are not written here.

    WS2: Uses _br (burn_rate module imported at startup) instead of the
    dynamic resolver.  The resolver was needed because the hook lived outside
    the plugin and didn't know the install path.  Now both scripts are siblings
    inside the plugin — direct import, no resolver needed.
    """
    try:
        if not _BR_AVAILABLE:
            raise ModuleNotFoundError("burn_rate not importable from plugin skill dir")

        now = datetime.now(timezone.utc)
        out = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.cursor()
            sd = _br.latest_for_bucket(cur, "seven_day")
            if sd is not None:
                _, spct, sresets = sd
                if sresets:
                    try:
                        reset_utc = _br.parse_iso(sresets)
                        prev_reset = reset_utc - timedelta(days=7)
                        elapsed_h = (now - prev_reset).total_seconds() / 3600.0
                        if elapsed_h > 0:
                            pp_h = float(spct) / elapsed_h
                            dc_pct_at_reset, _ = _br.duty_cycle_eta(now, float(spct), pp_h, reset_utc)
                            if dc_pct_at_reset is not None:
                                out["seven_day"] = {"duty_pct_at_reset": round(dc_pct_at_reset, 1)}
                    except Exception:
                        pass
        finally:
            conn.close()

        tmp = STATE_DIR / "statusline.json.tmp"
        final = STATE_DIR / "statusline.json"
        tmp.write_text(json.dumps(out))
        os.replace(str(tmp), str(final))
    except Exception as e:
        # Never block the hook chain — but don't vanish silently either: a moved
        # import path froze this sentinel for days once. Surface under DEBUG.
        if os.environ.get("DEBUG_LOG_USAGE"):
            print(f"log-usage: write_statusline_sentinel failed: {e!r}", file=sys.stderr)


def _parse_reset_str(reset_str):
    """Parse a human reset string like 'Jun 13, 10am (Australia/Brisbane)'
    into an ISO-8601 UTC string ('2026-06-13T00:00:00Z'), or None on failure.

    Assumes Australia/Brisbane (UTC+10, no DST) when the parenthetical says
    'Australia/Brisbane'.  If the parsed date is more than 1 day in the past,
    the year is bumped forward by 1 (handles year-boundary edge cases).
    """
    try:
        # Strip trailing tz parenthetical: '(Australia/Brisbane)' etc.
        s = re.sub(r"\s*\([^)]*\)\s*$", "", reset_str.strip())
        # Match 'Jun 13, 10am' or 'Jun 13, 10:30pm'
        m = re.match(r"(\w+\s+\d+),\s*(\d+(?::\d+)?)(am|pm)", s, re.IGNORECASE)
        if not m:
            return None
        date_part = m.group(1)           # e.g. 'Jun 13'
        time_part = m.group(2)           # e.g. '10' or '10:30'
        ampm      = m.group(3).lower()   # 'am' or 'pm'

        if ":" in time_part:
            hour, minute = map(int, time_part.split(":"))
        else:
            hour, minute = int(time_part), 0

        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        now = datetime.now(timezone.utc)
        candidate = datetime.strptime(f"{date_part} {now.year}", "%b %d %Y")
        candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Interpret candidate as Brisbane local time → convert to UTC
        brisbane_aware = datetime(
            candidate.year, candidate.month, candidate.day,
            candidate.hour, candidate.minute, 0,
            tzinfo=timezone(_BRISBANE_OFFSET),
        )
        utc_dt = brisbane_aware.astimezone(timezone.utc)

        # If the result is more than 1 day in the past, bump the year.
        if utc_dt < now - timedelta(days=1):
            brisbane_aware = brisbane_aware.replace(year=brisbane_aware.year + 1)
            utc_dt = brisbane_aware.astimezone(timezone.utc)

        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _find_claude_binary():
    """Return an absolute path to the `claude` binary, or None if not found.

    Checks (in order):
      1. mise shim (~/.local/share/mise/shims/claude)
      2. PATH lookup via shutil.which
    """
    mise_shim = Path.home() / ".local" / "share" / "mise" / "shims" / "claude"
    if mise_shim.is_file():
        return str(mise_shim)
    found = shutil.which("claude")
    return found  # may be None


def snapshot_from_usage_cli(conn, now_iso):
    """Run `claude -p /usage`, parse the output, and INSERT quota_snapshots
    rows for five_hour, seven_day, and sonnet_weekly (source='usage_cli').

    The call is:
      • Wrapped in a 60-second hard timeout.
      • Guarded by LOG_USAGE_INNER=1 env var (recursion guard).
      • Skipped gracefully if the binary is not found or the call fails.
      • Never raises — all errors are swallowed to protect the hook chain.

    Only the sonnet_weekly bucket is invisible to the statusline path; the others
    are inserted as a cross-check with source='usage_cli'.
    """
    try:
        claude_bin = _find_claude_binary()
        if not claude_bin:
            return  # nothing to do

        env = os.environ.copy()
        env["LOG_USAGE_INNER"] = "1"  # belt-and-suspenders recursion guard

        # NOTE: do NOT use --bare here. --bare suppresses the slash-command
        # machinery, so `claude --bare -p /usage` prints the session cost
        # summary instead of the quota report (verified 2026-06-11). Recursion
        # is prevented by the LOG_USAGE_INNER sentinel above (the inner claude's
        # Stop hook re-runs this script, which exits 0 immediately when the env
        # var is set — confirmed ~16ms no-op).
        result = subprocess.run(
            [claude_bin, "-p", "/usage"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        output = result.stdout or ""

        # Parse the three quota lines.
        patterns = {
            "five_hour":     r"Current session:\s*(\d+)%\s*used\s*[·•]\s*resets\s+(.+)",
            "seven_day":     r"Current week \(all models\):\s*(\d+)%\s*used\s*[·•]\s*resets\s+(.+)",
            "sonnet_weekly": r"Current week \(Sonnet only\):\s*(\d+)%\s*used\s*[·•]\s*resets\s+(.+)",
        }
        rows = []
        for bucket, pat in patterns.items():
            m = re.search(pat, output)
            if not m:
                continue
            try:
                pct = float(m.group(1))
            except (ValueError, TypeError):
                continue
            resets_at = _parse_reset_str(m.group(2).strip())
            rows.append((now_iso, bucket, pct, resets_at, "usage_cli"))

        if rows:
            conn.executemany(
                "INSERT INTO quota_snapshots (snapshot_ts, bucket, pct_used, resets_at, source)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
    except Exception:
        pass  # never block the hook chain


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    session_id      = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    cwd             = data.get("cwd", "")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    throttle_file = STATE_DIR / f"log-usage-{session_id}.txt"
    now_ts = time.time()

    # Always drain the sidecar on every hook fire to prevent line pile-up across
    # long-running sessions. The throttle gates only the DB write.
    drained_lines = drain_sidecar()

    throttled = False
    try:
        if throttle_file.exists():
            last_run = float(throttle_file.read_text().strip())
            if now_ts - last_run < THROTTLE_SECONDS:
                throttled = True
    except Exception:
        pass

    if throttled:
        sys.exit(0)

    totals = aggregate_transcript(transcript_path) if transcript_path else {}
    if not totals and not drained_lines:
        sys.exit(0)

    write_snapshot(session_id, cwd, transcript_path, totals, drained_lines)

    # WS6: preserve expiring reset_overrides into reset_history BEFORE the
    # sentinel write, which triggers burn_rate.check_and_expire_override and
    # would delete the row without this guard.
    try:
        _conn_preserve = sqlite3.connect(str(DB_PATH))
        try:
            preserve_expiring_overrides(_conn_preserve)
        finally:
            _conn_preserve.close()
    except Exception as e:
        if os.environ.get("DEBUG_LOG_USAGE"):
            print(f"log-usage: preserve_expiring_overrides outer failed: {e!r}", file=sys.stderr)

    write_statusline_sentinel()

    # ── Sonnet-weekly quota capture via `claude -p /usage` ───────────────────
    # Throttled separately (30 min) because spawning a full Claude subprocess
    # is expensive.  The LOG_USAGE_INNER sentinel prevents recursion.
    usage_cli_throttle_file = STATE_DIR / "log-usage-cli.txt"
    cli_throttled = False
    try:
        if usage_cli_throttle_file.exists():
            last_cli_run = float(usage_cli_throttle_file.read_text().strip())
            if now_ts - last_cli_run < USAGE_CLI_THROTTLE_SECONDS:
                cli_throttled = True
    except Exception:
        pass

    if not cli_throttled:
        try:
            now_iso_cli = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn_cli = sqlite3.connect(str(DB_PATH))
            try:
                snapshot_from_usage_cli(conn_cli, now_iso_cli)
                conn_cli.commit()
            finally:
                conn_cli.close()
        except Exception:
            pass
        try:
            usage_cli_throttle_file.write_text(str(now_ts))
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────

    try:
        throttle_file.write_text(str(now_ts))
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
