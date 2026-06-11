#!/usr/bin/env python3
"""Render the burn-rate skyline SVG and emit a markdown data-URI image.

Reads from ~/.claude/state/usage-log.sqlite, builds the All Models bucket's
projection-vs-actual chart, writes the SVG to /tmp/burn-skyline.svg, and
prints the `![alt](data:image/svg+xml;base64,...)` markdown line to stdout
so the caller can paste it into a chat reply.

Design choices (per James's iteration on 2026-05-24):
- y-axis fixed 0-150%, no expansion. Predictions above 150% are clipped.
- Projection rendered as polygons grouped by 5% color buckets:
  consecutive same-bucket points collapse into a single polygon with
  flat baseline and jagged top edge.
- Colors: green (#16a34a) at 50%, amber (#fbbf24) at 100%, red (#ef4444)
  at 150%, linearly interpolated. Above 150% = solid red. Below 50% = green.
- Actual % used drawn as a sky-blue line on top.
- Dashed ideal-even-burn line from (0,0) to (100,100). No inline label.
- Legend: 50% (left) [gradient bar] 150% (right, flush). "100%" label above
  midpoint of the gradient with a small white tick mark.
- 100% Y-axis gridline drawn solid white with bold label; other gridlines faint.
- No inline endpoint labels on the data points — position alone tells the story.
"""

import sqlite3
import datetime
import base64
from pathlib import Path

DB = Path.home() / ".claude" / "state" / "usage-log.sqlite"
RESET = datetime.datetime.fromisoformat("2026-05-30T00:00:00+00:00")
PRIOR_RESET = RESET - datetime.timedelta(days=7)
WEEK_H = 168.0


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_points():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT snapshot_ts, pct_used FROM quota_snapshots "
        "WHERE bucket='seven_day' AND snapshot_ts >= ? AND snapshot_ts <= ? "
        "ORDER BY snapshot_ts ASC",
        (PRIOR_RESET.isoformat().replace("+00:00", "Z"),
         RESET.isoformat().replace("+00:00", "Z"))
    ).fetchall()
    out = []
    for ts, pct in rows:
        t = parse_iso(ts)
        eh = (t - PRIOR_RESET).total_seconds() / 3600.0
        if eh < 0.5:
            continue
        pw = eh / WEEK_H * 100
        pred = pct * WEEK_H / eh
        out.append((pw, pred, pct))
    return out


W, H = 820, 460
PADL, PADR, PADT, PADB = 60, 30, 80, 50
PW, PH = W - PADL - PADR, H - PADT - PADB
YMAX = 150


def X(pw):
    return PADL + pw / 100 * PW


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


def polygon_for(group, next_group_first_pw=None):
    """Build polygon for `group`. If next_group_first_pw is given, extend
    the right edge to that x at the height of the last point in this group,
    so adjacent polygons touch (no gap)."""
    gid, gcol, gpts = group
    coords = [(X(gpts[0][0]), BASELINE)]
    for pw, pred in gpts:
        coords.append((X(pw), Y(pred)))
    if next_group_first_pw is not None:
        last_y = Y(gpts[-1][1])
        coords.append((X(next_group_first_pw), last_y))
        coords.append((X(next_group_first_pw), BASELINE))
    else:
        coords.append((X(gpts[-1][0]), BASELINE))
    s = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    # opacity=1 + matching stroke covers anti-aliasing seams between adjacent
    # polygons that share a vertical edge. Without this, the dark background
    # bleeds through at polygon boundaries as a thin seam.
    return f'<polygon points="{s}" fill="{gcol}" stroke="{gcol}" stroke-width="0.75" stroke-linejoin="miter"/>'


def build_svg(pts):
    groups = []
    cur_id, cur_col, cur_pts = None, None, []
    for pw, pred, pct in pts:
        bid, bcol = bucket(pred)
        if bid != cur_id:
            if cur_pts:
                groups.append((cur_id, cur_col, cur_pts))
            cur_id, cur_col, cur_pts = bid, bcol, []
        cur_pts.append((pw, pred))
    if cur_pts:
        groups.append((cur_id, cur_col, cur_pts))

    svg = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,system-ui,sans-serif">')
    svg.append(f'<rect width="{W}" height="{H}" fill="#111"/>')
    svg.append('<text x="20" y="20" fill="#888" font-size="11" letter-spacing="0.5">ALL MODELS — projection skyline &amp; actuals (reset Sat 10:00 Bne)</text>')

    # Legend
    LEG_Y_LABEL = 36
    LEG_Y_TICK_TOP = 42
    LEG_Y_BAR_TOP = 50
    LEG_Y_BAR_BOT = 66
    LEG_Y_SIDE = 62
    # Half-width legend: 150% flush right; 50% label shifted right so bar is ~half its original width.
    LEG_X_150_END = W - PADR
    LEG_X_BAR_R = LEG_X_150_END - 30
    LEG_X_BAR_L = LEG_X_BAR_R - 317  # half of previous 634px width
    LEG_X_50_END = LEG_X_BAR_L - 6
    LEG_X_BAR_MID = (LEG_X_BAR_L + LEG_X_BAR_R) / 2

    svg.append(f'<text x="{LEG_X_50_END}" y="{LEG_Y_SIDE}" fill="#eee" font-size="11" text-anchor="end">50%</text>')
    svg.append(f'<text x="{LEG_X_150_END}" y="{LEG_Y_SIDE}" fill="#eee" font-size="11" text-anchor="end">150%</text>')
    svg.append(f'<rect x="{LEG_X_BAR_L:.1f}" y="{LEG_Y_BAR_TOP}" width="{LEG_X_BAR_R-LEG_X_BAR_L:.1f}" height="{LEG_Y_BAR_BOT-LEG_Y_BAR_TOP}" fill="url(#lg)"/>')
    svg.append(f'<text x="{LEG_X_BAR_MID:.1f}" y="{LEG_Y_LABEL}" fill="#fff" font-size="11" font-weight="600" text-anchor="middle">100%</text>')
    svg.append(f'<line x1="{LEG_X_BAR_MID:.1f}" x2="{LEG_X_BAR_MID:.1f}" y1="{LEG_Y_TICK_TOP}" y2="{LEG_Y_BAR_TOP-1}" stroke="#fff" stroke-width="2"/>')

    # Skyline polygons (extend each to the next group's first x for no-gap touching)
    for i, g in enumerate(groups):
        next_first_pw = groups[i + 1][2][0][0] if i + 1 < len(groups) else None
        svg.append(polygon_for(g, next_first_pw))

    # Y gridlines + labels
    for yv in [0, 25, 50, 75, 100, 125, 150]:
        yp = Y(yv)
        if yv == 100:
            svg.append(f'<line x1="{PADL}" x2="{W-PADR}" y1="{yp:.1f}" y2="{yp:.1f}" stroke="#fff" stroke-width="2"/>')
            svg.append(f'<text x="{PADL-8}" y="{yp+4:.1f}" fill="#fff" font-size="10" text-anchor="end" font-weight="700">100%</text>')
        else:
            svg.append(f'<line x1="{PADL}" x2="{W-PADR}" y1="{yp:.1f}" y2="{yp:.1f}" stroke="#fff" stroke-width="1" stroke-dasharray="2,3" opacity="0.25"/>')
            svg.append(f'<text x="{PADL-8}" y="{yp+4:.1f}" fill="#aaa" font-size="10" text-anchor="end">{yv}%</text>')

    # Day labels
    for i, day in enumerate(["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        pw = i / 7 * 100
        xp = X(pw)
        svg.append(f'<line x1="{xp:.1f}" x2="{xp:.1f}" y1="{PADT}" y2="{H-PADB}" stroke="#fff" stroke-width="1" opacity="0.15"/>')
        svg.append(f'<text x="{xp:.1f}" y="{H-PADB+14}" fill="#aaa" font-size="10" text-anchor="middle">{day}</text>')

    # Ideal trajectory (no label)
    svg.append(f'<line x1="{X(0):.1f}" y1="{Y(0):.1f}" x2="{X(100):.1f}" y2="{Y(100):.1f}" stroke="#fff" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.6"/>')

    # "now" marker
    if pts:
        nx = X(pts[-1][0])
        svg.append(f'<line x1="{nx:.1f}" x2="{nx:.1f}" y1="{PADT}" y2="{H-PADB}" stroke="#fff" stroke-width="1.5" stroke-dasharray="3,2"/>')
        svg.append(f'<text x="{nx:.1f}" y="{PADT-6}" fill="#fff" font-size="10" text-anchor="middle" font-weight="600">now</text>')

    # Actual % used line
    if pts:
        seg = [(X(p[0]), Y(p[2])) for p in pts]
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        svg.append(f'<path d="{d}" fill="none" stroke="#38bdf8" stroke-width="2.5"/>')
        last = pts[-1]
        ax, ay = X(last[0]), Y(last[2])
        svg.append(f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="5" fill="#38bdf8" stroke="#111" stroke-width="2"/>')
        if last[1] <= YMAX:
            px, py = X(last[0]), Y(last[1])
            svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{color_at(last[1])}" stroke="#111" stroke-width="2"/>')

    svg.append('<defs><linearGradient id="lg" x1="0" x2="1"><stop offset="0%" stop-color="#16a34a"/><stop offset="50%" stop-color="#fbbf24"/><stop offset="100%" stop-color="#ef4444"/></linearGradient></defs>')
    svg.append('</svg>')
    return "\n".join(svg)


def main():
    pts = load_points()
    svg = build_svg(pts)
    out_path = Path("/tmp/burn-skyline.svg")
    out_path.write_text(svg)
    b64 = base64.b64encode(svg.encode()).decode()
    print(f"![burn](data:image/svg+xml;base64,{b64})")


if __name__ == "__main__":
    main()
