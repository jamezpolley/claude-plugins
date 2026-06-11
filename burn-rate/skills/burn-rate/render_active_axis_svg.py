#!/usr/bin/env python3
"""James's variant: drop idle time from the x-axis.

The x-axis here represents *active hours only* — idle gaps (>20 min between
snapshots) are removed entirely. The visualization compresses time so you
can see the trajectory through your *working* time, with sleep/breaks/idle
periods squeezed out.

Total expected active hours = active_fraction × 168 (assuming the same
active fraction continues for the rest of the week).
"""

import sqlite3
import datetime
import base64
from pathlib import Path

DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"
RESET = datetime.datetime.fromisoformat("2026-05-30T00:00:00+00:00")
PRIOR_RESET = RESET - datetime.timedelta(days=7)
WEEK_H = 168.0
IDLE_THRESHOLD_S = 20 * 60


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_snapshots():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT snapshot_ts, pct_used FROM quota_snapshots "
        "WHERE bucket='seven_day' AND snapshot_ts >= ? AND snapshot_ts <= ? "
        "ORDER BY snapshot_ts ASC",
        (PRIOR_RESET.isoformat().replace("+00:00", "Z"),
         RESET.isoformat().replace("+00:00", "Z"))
    ).fetchall()
    return [(parse_iso(ts), pct) for ts, pct in rows]


def compute_active_timeline(snapshots):
    """For each snapshot, compute its active_time (sum of all non-idle gaps
    up to and including it). Returns list of (active_h, wall_t, pct)."""
    out = []
    active_s = 0.0
    prev_t = None
    for t, pct in snapshots:
        if prev_t is not None:
            gap_s = (t - prev_t).total_seconds()
            if gap_s <= IDLE_THRESHOLD_S:
                active_s += gap_s
        out.append((active_s / 3600.0, t, pct))
        prev_t = t
    return out


W, H = 820, 460
PADL, PADR, PADT, PADB = 60, 30, 80, 60
PW, PH = W - PADL - PADR, H - PADT - PADB
YMAX = 150


def Y(v):
    return PADT + (YMAX - min(v, YMAX)) / YMAX * PH


BASELINE = Y(0)


def lerp(a, b, t):
    return a + (b - a) * t


def hex_to_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


G = hex_to_rgb("#16a34a")
A = hex_to_rgb("#fbbf24")
R = hex_to_rgb("#ef4444")


def color_at(p):
    p = max(50, min(150, p))
    if p <= 100:
        t = (p - 50) / 50
        c = tuple(lerp(G[i], A[i], t) for i in range(3))
    else:
        t = (p - 100) / 50
        c = tuple(lerp(A[i], R[i], t) for i in range(3))
    return rgb_to_hex(*c)


def bucket(p):
    if p > YMAX:
        return "HIGH", "#ef4444"
    if p < 50:
        return "LOW", color_at(50)
    b = int(round((p - 50) / 5)) * 5 + 50
    b = max(50, min(150, b))
    return f"B{b}", color_at(b)


def build_svg(snapshots, timeline):
    # snapshots: list of (wall_t, pct)
    # timeline: list of (active_h, wall_t, pct), same length, aligned
    if not snapshots:
        return ""

    last_active_h, last_wall_t, last_pct = timeline[-1]
    elapsed_wall_h = (last_wall_t - PRIOR_RESET).total_seconds() / 3600.0
    wall_remaining_h = (RESET - last_wall_t).total_seconds() / 3600.0
    active_fraction = last_active_h / elapsed_wall_h if elapsed_wall_h > 0 else 0
    expected_total_active_h = active_fraction * WEEK_H
    xmax_active = expected_total_active_h if expected_total_active_h > 0 else last_active_h * 2

    def X(active_h):
        return PADL + (active_h / xmax_active) * PW

    # Build (x, pred, pct) tuples for points; pred = pct × week / elapsed_wall
    # (we use wall elapsed so the Y values are identical to the wall-axis chart)
    pts = []
    for active_h, wall_t, pct in timeline:
        eh_wall = (wall_t - PRIOR_RESET).total_seconds() / 3600.0
        if eh_wall < 0.5:
            continue
        pred = pct * WEEK_H / eh_wall
        pts.append((active_h, pred, pct))

    # Group points by bucket (same coloring as the wall-axis renderer)
    groups = []
    cur_id, cur_col, cur_pts = None, None, []
    for active_h, pred, pct in pts:
        bid, bcol = bucket(pred)
        if bid != cur_id:
            if cur_pts:
                groups.append((cur_id, cur_col, cur_pts))
            cur_id, cur_col, cur_pts = bid, bcol, []
        cur_pts.append((active_h, pred))
    if cur_pts:
        groups.append((cur_id, cur_col, cur_pts))

    def polygon_for(group, next_group_first_x=None):
        gid, gcol, gpts = group
        coords = [(X(gpts[0][0]), BASELINE)]
        for ax, pred in gpts:
            coords.append((X(ax), Y(pred)))
        if next_group_first_x is not None:
            last_y = Y(gpts[-1][1])
            coords.append((X(next_group_first_x), last_y))
            coords.append((X(next_group_first_x), BASELINE))
        else:
            coords.append((X(gpts[-1][0]), BASELINE))
        s = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        return f'<polygon points="{s}" fill="{gcol}" stroke="{gcol}" stroke-width="0.75" stroke-linejoin="miter"/>'

    svg = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,system-ui,sans-serif">')
    svg.append(f'<rect width="{W}" height="{H}" fill="#111"/>')
    svg.append('<text x="20" y="20" fill="#888" font-size="11" letter-spacing="0.5">ALL MODELS — projection vs ACTIVE hours only (idle gaps removed from x-axis)</text>')

    # Legend (top-right)
    LEG_Y_LABEL = 36
    LEG_Y_TICK_TOP = 42
    LEG_Y_BAR_TOP = 50
    LEG_Y_BAR_BOT = 66
    LEG_Y_SIDE = 62
    LEG_X_150_END = W - PADR
    LEG_X_BAR_R = LEG_X_150_END - 30
    LEG_X_BAR_L = LEG_X_BAR_R - 317
    LEG_X_50_END = LEG_X_BAR_L - 6
    LEG_X_BAR_MID = (LEG_X_BAR_L + LEG_X_BAR_R) / 2

    svg.append(f'<text x="{LEG_X_50_END}" y="{LEG_Y_SIDE}" fill="#eee" font-size="11" text-anchor="end">50%</text>')
    svg.append(f'<text x="{LEG_X_150_END}" y="{LEG_Y_SIDE}" fill="#eee" font-size="11" text-anchor="end">150%</text>')
    svg.append(f'<rect x="{LEG_X_BAR_L:.1f}" y="{LEG_Y_BAR_TOP}" width="{LEG_X_BAR_R-LEG_X_BAR_L:.1f}" height="{LEG_Y_BAR_BOT-LEG_Y_BAR_TOP}" fill="url(#lg)"/>')
    svg.append(f'<text x="{LEG_X_BAR_MID:.1f}" y="{LEG_Y_LABEL}" fill="#fff" font-size="11" font-weight="600" text-anchor="middle">100%</text>')
    svg.append(f'<line x1="{LEG_X_BAR_MID:.1f}" x2="{LEG_X_BAR_MID:.1f}" y1="{LEG_Y_TICK_TOP}" y2="{LEG_Y_BAR_TOP-1}" stroke="#fff" stroke-width="2"/>')

    # Skyline polygons
    for i, g in enumerate(groups):
        next_first_x = groups[i + 1][2][0][0] if i + 1 < len(groups) else None
        svg.append(polygon_for(g, next_first_x))

    # Y gridlines + labels
    for yv in [0, 25, 50, 75, 100, 125, 150]:
        yp = Y(yv)
        if yv == 100:
            svg.append(f'<line x1="{PADL}" x2="{W-PADR}" y1="{yp:.1f}" y2="{yp:.1f}" stroke="#fff" stroke-width="2"/>')
            svg.append(f'<text x="{PADL-8}" y="{yp+4:.1f}" fill="#fff" font-size="10" text-anchor="end" font-weight="700">100%</text>')
        else:
            svg.append(f'<line x1="{PADL}" x2="{W-PADR}" y1="{yp:.1f}" y2="{yp:.1f}" stroke="#fff" stroke-width="1" stroke-dasharray="2,3" opacity="0.25"/>')
            svg.append(f'<text x="{PADL-8}" y="{yp+4:.1f}" fill="#aaa" font-size="10" text-anchor="end">{yv}%</text>')

    # X gridlines + labels — every 10 active hours
    step = 10
    hr = 0
    while hr <= xmax_active + 0.5:
        xp = X(hr)
        svg.append(f'<line x1="{xp:.1f}" x2="{xp:.1f}" y1="{PADT}" y2="{H-PADB}" stroke="#fff" stroke-width="1" opacity="0.15"/>')
        svg.append(f'<text x="{xp:.1f}" y="{H-PADB+14}" fill="#aaa" font-size="10" text-anchor="middle">{hr}h</text>')
        hr += step
    svg.append(f'<text x="{(PADL+W-PADR)/2:.1f}" y="{H-PADB+30}" fill="#888" font-size="11" text-anchor="middle">active hours since last reset (expected total: {xmax_active:.1f}h)</text>')

    # Ideal even-burn line: from (0,0) → (xmax_active, 100%) in active-time space
    svg.append(f'<line x1="{X(0):.1f}" y1="{Y(0):.1f}" x2="{X(xmax_active):.1f}" y2="{Y(100):.1f}" stroke="#fff" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.6"/>')

    # "now" line at current active_h
    nx = X(last_active_h)
    svg.append(f'<line x1="{nx:.1f}" x2="{nx:.1f}" y1="{PADT}" y2="{H-PADB}" stroke="#fff" stroke-width="1.5" stroke-dasharray="3,2"/>')
    svg.append(f'<text x="{nx:.1f}" y="{PADT-6}" fill="#fff" font-size="10" text-anchor="middle" font-weight="600">now ({last_active_h:.1f}h active)</text>')

    # Actual % used line — sky blue, on top
    seg = [(X(p[0]), Y(p[2])) for p in pts]
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
    svg.append(f'<path d="{d}" fill="none" stroke="#38bdf8" stroke-width="2.5"/>')
    last = pts[-1]
    ax, ay = X(last[0]), Y(last[2])
    svg.append(f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="5" fill="#38bdf8" stroke="#111" stroke-width="2"/>')
    if last[1] <= YMAX:
        px, py = X(last[0]), Y(last[1])
        svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{color_at(last[1])}" stroke="#111" stroke-width="2"/>')

    # Footer with stats
    stats = f"active fraction so far: {active_fraction*100:.1f}% · rate per active hour: {last_pct/last_active_h:.2f} pp/h · wall remaining: {wall_remaining_h:.0f}h"
    svg.append(f'<text x="20" y="{H-8}" fill="#888" font-size="10">{stats}</text>')

    svg.append('<defs><linearGradient id="lg" x1="0" x2="1"><stop offset="0%" stop-color="#16a34a"/><stop offset="50%" stop-color="#fbbf24"/><stop offset="100%" stop-color="#ef4444"/></linearGradient></defs>')
    svg.append('</svg>')
    return "\n".join(svg)


def main():
    snaps = load_snapshots()
    if not snaps:
        print("No snapshots found.")
        return
    timeline = compute_active_timeline(snaps)
    svg = build_svg(snaps, timeline)
    out = Path("/tmp/burn-active-axis.svg")
    out.write_text(svg)
    b64 = base64.b64encode(svg.encode()).decode()
    print(f"![burn-active-axis](data:image/svg+xml;base64,{b64})")


if __name__ == "__main__":
    main()
