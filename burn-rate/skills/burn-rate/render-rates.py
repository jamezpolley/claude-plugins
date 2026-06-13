#!/usr/bin/env python3
"""Statusline rate renderer — Option A display with shrinkage projection.

Reads the same JSON that statusline-command.sh receives on stdin (from Claude
Code's statusLine command), extracts rate_limits, calls projection.py for
shrinkage-projected values, and emits ANSI-coloured rate segments for the
statusline.

Output format (Option A):
  5h: │ 🟢 5h:46%→87% ⌛2h52m
  7d: │ 🔴 7d:6%→55% ⌛6d20h │ duty:168%

The →NN% projection segment uses the SAME glyph + colour escalation as the
current-% segment (both driven by the projected value), matching the existing
statusline ramp: pale-green / light-blue / blue / amber(≥70) / orange-red /
red / bright-red(≥100).

Invocation:
  echo "$statusline_json" | python3 render-rates.py
  echo "$statusline_json" | python3 render-rates.py --mode naive

Falls back gracefully when:
  - projection.py is absent: renders current% only (no →proj)
  - DB is absent / no data: skips that bucket
  - JSON parse error: emits nothing (silent, never crash the statusline)

Exit: always 0 — this script must not error-exit (statusline failure is silent).
"""

from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Colour / glyph ramp (matches statusline-command.sh exactly)
# ---------------------------------------------------------------------------

# ANSI 256-colour codes matching statusline-command.sh rate_colour()
_RAMP = [
    (100, "\x1b[38;5;196m"),  # bright red — will exhaust (≥100)
    (90,  "\x1b[38;5;160m"),  # red
    (80,  "\x1b[38;5;202m"),  # orange-red
    (70,  "\x1b[38;5;214m"),  # amber — first warm tone (≥70)
    (55,  "\x1b[38;5;75m"),   # blue
    (40,  "\x1b[38;5;117m"),  # light blue
    (0,   "\x1b[38;5;151m"),  # pale green — comfortable
]
_RESET = "\x1b[0m"
_SEP = "\x1b[38;5;245m│\x1b[0m"  # dim segment separator
_DIM_GREY = "\x1b[38;5;240m"     # stale sentinel colour

# Nerd Font glyphs — Nerd Font PUA (matching statusline-command.sh)
_GLYPH_BOMB = ""   # nf-fa-bomb  (≥100)
_GLYPH_WARN = ""   # nf-fa-warning (≥90)
_GLYPH_NONE = ""   # nf-fa-check-circle (<90)


def _colour(pct: float) -> str:
    pct_i = int(round(pct))
    for threshold, code in _RAMP:
        if pct_i >= threshold:
            return code
    return _RAMP[-1][1]


def _glyph(pct: float) -> str:
    pct_i = int(round(pct))
    if pct_i >= 100:
        return _GLYPH_BOMB
    if pct_i >= 90:
        return _GLYPH_WARN
    return _GLYPH_NONE


def _fmt_countdown(secs: float) -> str:
    """Format seconds remaining as ⌛2h52m or ⌛6d20h."""
    if secs <= 0:
        return ""
    s = int(secs)
    d = s // 86400
    h = (s % 86400) // 3600
    m = (s % 3600) // 60
    if d > 0:
        return f" ⏳{d}d{h}h"
    if h > 0:
        return f" ⏳{h}h{m}m"
    return f" ⏳{m}m"


# ---------------------------------------------------------------------------
# Projection module loader
# ---------------------------------------------------------------------------

_project_fn = None
_PROJECTION_AVAILABLE = False

try:
    import importlib.util as _ilu

    _proj_path = Path(__file__).parent / "projection.py"
    if _proj_path.exists():
        _spec = _ilu.spec_from_file_location("projection", _proj_path)
        _proj_mod = _ilu.module_from_spec(_spec)
        sys.modules["projection"] = _proj_mod
        _spec.loader.exec_module(_proj_mod)
        _project_fn = _proj_mod.project
        _PROJECTION_AVAILABLE = True
except Exception:
    pass  # degrade gracefully — render without →proj

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

_DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"


def _open_db():
    """Open the usage DB; return connection or None."""
    if not _DB.exists():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(str(_DB))
        # Do NOT set row_factory — burn_rate uses indexed tuples on this conn;
        # projection.py accepts any connection (it uses conn.execute().fetchall()
        # with column-indexed access internally, and the Row factory is only
        # set by open_db() which we don't call here).
        return conn
    except Exception:
        return None


def _proj(conn, bucket: str, mode: str):
    """Call project() safely; return ProjectionResult or None."""
    if not _PROJECTION_AVAILABLE or _project_fn is None or conn is None:
        return None
    try:
        return _project_fn(conn, bucket, now=datetime.now(timezone.utc), mode=mode)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Segment renderers
# ---------------------------------------------------------------------------

def _render_five_hour(used: float, resets_at_epoch: int, mode: str, conn) -> str:
    """Render the 5h segment.

    Option A: │ glyph 5h:46%→87% ⌛2h52m

    5h uses naive projection only (weekly shrinkage prior is not calibrated for
    a 5-hour rolling window — it would produce nonsense).  The naive projection
    is simply current% × (period_total / elapsed) — same as the existing
    statusline calculation.
    """
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    period_total = 18000  # 5h in seconds
    remaining = resets_at_epoch - now_epoch
    elapsed = period_total - remaining

    # Naive projection (always for 5h)
    if elapsed > 0 and used > 0:
        proj = used * period_total / elapsed
    else:
        proj = 0.0

    # Use current% for colour/glyph on the bucket label; projected for the →proj
    cur_col = _colour(used)
    cur_glyph = _glyph(used)
    proj_col = _colour(proj)
    proj_glyph = _glyph(proj)

    countdown = _fmt_countdown(max(remaining, 0))

    # Format: │ glyph 5h:46%→87% ⌛2h52m
    # glyph and colour on current%, proj colour on →proj%
    proj_int = int(round(proj))
    cur_int = int(round(used))
    return (
        f" {_SEP} {cur_col}{cur_glyph} 5h:{cur_int}%"
        f"→{proj_col}{proj_int}%{_RESET}{countdown}"
    )


def _render_seven_day(used: float, resets_at_epoch: int, duty_pct: float | None,
                      duty_stale: bool, mode: str, conn) -> str:
    """Render the 7d segment.

    Option A (7d has both inline projection AND duty segment):
      │ glyph 7d:6%→55% ⌛6d20h │ duty:168%

    Inline projection uses shrinkage (predictive/target) or naive.
    Duty segment: from sentinel (duty_pct) — same stale-guard behaviour as
    the existing statusline, but now we also have shrinkage duty from
    projection.projected_pct_at_reset + duty_projected_pct.
    """
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    period_total = 604800  # 7d in seconds
    remaining = resets_at_epoch - now_epoch
    elapsed = period_total - remaining

    # Naive projection (fallback if shrinkage unavailable)
    if elapsed > 0 and used > 0:
        naive_proj = used * period_total / elapsed
    else:
        naive_proj = 0.0

    # Shrinkage projection
    sr = _proj(conn, "seven_day", mode) if mode != "naive" else None
    # Also try the legacy bucket name if seven_day returns nothing
    if sr is None and mode != "naive":
        sr = _proj(conn, "all_models_weekly", mode)

    if sr is not None:
        inline_proj = sr.projected_pct_at_reset
        # Use shrinkage duty projection if available — more accurate than sentinel
        shrink_duty = sr.duty_projected_pct
    else:
        inline_proj = naive_proj
        shrink_duty = None

    cur_col = _colour(used)
    cur_glyph = _glyph(used)
    proj_col = _colour(inline_proj)
    proj_glyph = _glyph(inline_proj)

    countdown = _fmt_countdown(max(remaining, 0))
    cur_int = int(round(used))
    proj_int = int(round(inline_proj))

    # Main segment
    seg = (
        f" {_SEP} {cur_col}{cur_glyph} 7d:{cur_int}%"
        f"→{proj_col}{proj_int}%{_RESET}{countdown}"
    )

    # Duty segment — prefer shrinkage duty, fall back to sentinel
    # Both use the same stale-guard: if sentinel is stale, dim grey + trailing ~
    effective_duty = shrink_duty if shrink_duty is not None else duty_pct
    if effective_duty is not None:
        duty_int = int(round(effective_duty))
        stale = duty_stale and shrink_duty is None  # only apply stale if using sentinel
        if stale:
            duty_col = _DIM_GREY
            seg += f" {_SEP} {duty_col}duty:{effective_duty:.1f}%~{_RESET}"
        else:
            duty_col = _colour(duty_int)
            duty_glyph = _glyph(duty_int)
            if duty_int >= 90:
                seg += (
                    f" {_SEP} {duty_col}{duty_glyph} duty:{effective_duty:.1f}% "
                    f"{duty_glyph}{_RESET}"
                )
            else:
                seg += f" {_SEP} {duty_col}duty:{effective_duty:.1f}%{_RESET}"

    return seg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Parse --mode argument (default predictive)
    mode = "predictive"
    args = sys.argv[1:]
    if "--mode" in args:
        idx = args.index("--mode")
        if idx + 1 < len(args):
            mode = args[idx + 1]
    if mode not in ("naive", "predictive", "target"):
        mode = "predictive"

    # Read stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        return  # silent — never crash the statusline

    # Extract rate_limits
    rl = data.get("rate_limits", {})
    if not rl:
        return

    fh_pct = None
    fh_reset = None
    sd_pct = None
    sd_reset = None

    fh = rl.get("five_hour", {})
    if fh:
        fh_pct = fh.get("used_percentage")
        fh_reset = fh.get("resets_at")

    sd = rl.get("seven_day", {})
    if sd:
        sd_pct = sd.get("used_percentage")
        sd_reset = sd.get("resets_at")

    # Read sentinel for duty% (legacy; may be supplemented by shrinkage)
    sentinel_path = Path.home() / ".claude" / "state" / "statusline.json"
    duty_pct = None
    duty_stale = False
    if sentinel_path.is_file():
        try:
            s = json.loads(sentinel_path.read_text())
            duty_pct = s.get("seven_day", {}).get("duty_pct_at_reset")
            sentinel_ts = s.get("ts")
            if sentinel_ts:
                from datetime import datetime, timezone
                try:
                    st = datetime.fromisoformat(sentinel_ts.replace("Z", "+00:00"))
                    age_s = (datetime.now(timezone.utc) - st).total_seconds()
                    if age_s > 3600:
                        duty_stale = True
                except Exception:
                    pass
        except Exception:
            pass

    # Open DB for shrinkage projection
    conn = _open_db()

    output = ""

    if fh_pct is not None and fh_reset is not None:
        try:
            fh_pct_f = float(fh_pct)
            fh_reset_i = int(fh_reset)
            output += _render_five_hour(fh_pct_f, fh_reset_i, mode, conn)
        except Exception:
            pass

    if sd_pct is not None and sd_reset is not None:
        try:
            sd_pct_f = float(sd_pct)
            sd_reset_i = int(sd_reset)
            output += _render_seven_day(
                sd_pct_f, sd_reset_i, duty_pct, duty_stale, mode, conn
            )
        except Exception:
            pass

    if output:
        sys.stdout.write(output)
        sys.stdout.flush()

    if conn:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # silent — never crash the statusline
    sys.exit(0)
