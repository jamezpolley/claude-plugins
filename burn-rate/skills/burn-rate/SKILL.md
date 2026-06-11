---
name: burn-rate
description: Report recent Claude Code quota burn rate and projected exhaustion of weekly buckets, using data from ~/.claude/state/usage-log.sqlite
---

# Burn Rate

Reports current % used + burn rate (pp/hr) + projected exhaustion for the weekly Claude Code quotas (All Models, Sonnet). Reads from the statusline-populated SQLite at `~/.claude/state/usage-log.sqlite`.

## Process

### Step 1: Run the report

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py
```

Custom recent-comparison window:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py --window 1h    # "right now"
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py --window 24h   # last day
```

#### Manual reset-epoch overrides

Use these when a bucket resets early (drops to 0% before `resets_at`). Without
an override, the "since last reset" epoch is derived as `resets_at − 7 days`,
which can be wrong by days if the real reset happened mid-week.

```bash
# Set override — use the EARLIEST timestamp where the drop was still visible:
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py \
    --set-reset-override seven_day 2026-06-09T21:17:03Z

# Clear manually (normally not needed — auto-expiry handles it):
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py --clear-reset-override seven_day

# Inspect active overrides:
python3 ${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py --list-reset-overrides
```

**Auto-expiry (critical):** On every run the script compares the override
timestamp against the derived reset (`resets_at − 7 days`). If the derived
reset has reached or passed the override, the override is stale — it is
**deleted automatically** and the derived epoch is used. You never need to
clean up manually; the override just disappears once the next scheduled reset
catches up.

**Storage:** Overrides live in a `reset_overrides(bucket, reset_ts, created_ts)`
table in `~/.claude/state/usage-log.sqlite` — the same DB as `quota_snapshots`.

### *** STEP 2: RAW OUTPUT FIRST — NON-NEGOTIABLE ***

**The FIRST thing shown to James MUST be the raw stdout from `burn_rate.py`, VERBATIM, in a fenced code block.** No preamble, no "here's the output", no interpretation before it. Just:

````
```
<exact stdout — not paraphrased, not trimmed, not reordered>
```
````

This is a hard rule derived from [[feedback_burn_rate_raw_first]]. James wants to read the numbers himself. Burying or paraphrasing them first violates this contract.

Only AFTER the raw block may you add interpretation or commentary.

### Step 3: Interpret (after raw block)

- **`pp/hr`** = percentage points per hour. `+1.2 pp/hr` means the bucket is filling by 1.2% per hour.
- **`rate:` line** is always since the last reset — i.e. current pct ÷ hours elapsed in the current quota window. That's the honest denominator for a weekly bucket and what all projections (`ETA`, `% by reset`) are computed against.
- **`recent:` line** is a comparison signal: rate over the last `--window` (default 6h), labelled `↑ hotter` / `↓ cooler` / `≈ flat` vs the week average. **Don't mistake `recent` for `rate`** — `rate` is the source of all projections; `recent` is just the comparison.
- **`⚠ BEFORE reset`** = at the since-reset rate the bucket will hit 100% before the next reset.
- **`X% by reset`** = projected % used at reset if the since-reset rate holds.
- **`duty:` line** = duty-cycle projection: same pp/hr rate, but burn only accrues during active hours (default 06:00–21:00 local, 15 h/day). This models the realistic scenario where you stop working at night. Both raw and duty projections are always shown. Early in the week the duty-cycle prediction is usually more useful; later in the week the raw projection becomes equally grounded. Active-window hours are configurable via `ACTIVE_START_HOUR`/`ACTIVE_END_HOUR` constants at the top of `burn_rate.py`.

### Step 4: Quota-bucket reminder

Per [[reference_claude_code_quota_buckets]]: only TWO weekly buckets exist — All Models + Sonnet. No Opus bucket. Opus depletes All Models only; Sonnet depletes both. "Delegating to Sonnet to save Opus quota" is incoherent — it's cheaper per token but draws from the same All Models pool.

## Recording a /usage paste

When James pastes `/usage` output (typically Sonnet bucket, since it's not in the statusline JSON), record it manually:

```python
INSERT INTO quota_snapshots (snapshot_ts, bucket, pct_used, resets_at, source)
VALUES (<now-iso>, 'sonnet_weekly', <pct>, <resets_at>, 'user_paste')
```

**CRITICAL — `resets_at` alignment:** Sonnet weekly resets at the **same instant** as the All Models weekly (`seven_day`) bucket. **Do not compute it from the human-readable "resets Xd" text** — that's rounded ("5d", "2d") and will be off by hours. Pull the canonical timestamp from the most recent `seven_day` row:

```sql
SELECT resets_at FROM quota_snapshots WHERE bucket='seven_day' ORDER BY id DESC LIMIT 1;
```

Use that exact `resets_at` for the Sonnet row. Same rule for any other weekly bucket added later. If no recent `seven_day` row exists, ask James — don't guess from "Xd".

## Notes

- This is a custom skill, protected from Dex updates.
- Data freshness depends on the statusline hook writing snapshots — if the latest row is stale, the statusline isn't running.
- Edit `burn_rate.py` to tweak output format, add per-day burn averages, or surface other buckets (`five_hour`, `session_ctx`).
