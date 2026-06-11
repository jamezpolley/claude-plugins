# burn-rate

Claude Code quota burn-rate reporting. The `/burn-rate` skill runs `burn_rate.py`,
which reads the statusline-populated SQLite at `~/.claude/state/usage-log.sqlite`
and reports, per weekly bucket (All Models, Sonnet):

- current % used
- burn rate in percentage-points/hour (recent window vs since-reset average)
- projected exhaustion time vs the scheduled reset

The skill's contract is **raw output first**: the full unfiltered stdout of
`burn_rate.py` is shown verbatim before any commentary.

## Requirements (machine-specific)

This plugin assumes the host machine populates `~/.claude/state/usage-log.sqlite`
(`quota_snapshots` table) — on James's setup that's done by the statusline script
plus a Stop hook running `claude -p /usage`. Without that pipeline the report has
no data. It is published in this marketplace for James's own machines, not as a
general-purpose tool.

## Extras

- `--set-reset-override / --clear-reset-override / --list-reset-overrides` —
  correct the since-reset epoch when a bucket resets early (auto-expires).
- `active_hours_analysis.py`, `render_active_axis_svg.py`, `render_skyline_svg.py` —
  usage-pattern analysis and SVG renders over the same database.

## History

Migrated 2026-06-11 from `dex/.claude/skills/burn-rate-custom/` (renamed
`burn-rate`, task-20260609-011), consolidating parallel copies that had
accumulated in the dex repo and its fork.
