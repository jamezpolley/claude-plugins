# Changelog

All notable changes to the burn-rate plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.2.0] — 2026-06-14

### Added

- **`/burn-rate-setup` skill** (`skills/burn-rate-setup/`) — wires the quota strip
  into the user's statusline, making the plugin portable to a fresh install. The
  Stop hook and `/burn-rate` report were already portable (auto-registered;
  `${CLAUDE_PLUGIN_ROOT}`); the statusline strip was not, because the statusline
  command is the user's own file that a plugin can't write. `setup-statusline.py`
  is an idempotent injector with three paths: (1) no statusLine configured →
  create one and point `settings.json` at it; (2) statusLine is a script → append
  a marker-delimited block (no-op if already wired); (3) inline command → print
  the snippet to add by hand. Dry-run by default, `--apply` writes, backs up first.
  The injected lookup is marketplace-agnostic (matches `burn-rate@<any>`), so it
  survives version bumps and re-installs.

---

## [1.1.0] — 2026-06-14

The span-based projection rebuild. Projections are now computed from explicitly
defined **spans** (account- and reset-correct windows) rather than slicing a single
mixed timeline, fixing personal-account contamination, stale-epoch elapsed, and
averaging-of-averages inflation.

### Added

- **`spans.py` (new module)** — the clean-readings/span engine. Groups readings by
  `resets_at` generation, discards personal-account spans (Wrinkle 1: work = Saturday
  00:00 UTC reset), splits weeks at mid-week resets (Wrinkle 2), canonicalises
  near-coincident reset labels (Wrinkle 0), and treats censored spans correctly
  (Wrinkle 3). Data-hygiene: running-max per generation defeats cross-source ±1
  disagreement and stale late-arriving readings.
- **Account-separation stamp (WS17)** — `quota_snapshots` gains a nullable
  `seven_day_resets_at` column. The 5h bucket has no account anchor of its own, so
  writers stamp the co-captured 7d `resets_at` onto `five_hour` / `sonnet_weekly`
  rows; `spans.py` classifies those rows work-vs-personal off the stamp. Historical
  rows stay NULL (the 5h prior is recent-anchored).
- **`--autonomous-status --json`** — structured per-bucket fields for deterministic
  consumers, alongside the human-readable text and the 0/1/2 exit-code contract.
- **`--color=auto|always|never` / `-c`** on the human report.

### Changed

- **Report restructure** — the per-bucket view is now two lines: `predicted:` shows a
  **range** `LOW–HIGH% at reset` (duty/optimistic-floor → round-the-clock upper bound,
  glyph/colour escalating off the upper end), and `target:` shows the pace needed to
  land at 100% (`≤X pp/hr from here · N× over/under`). Replaces the old ETA/duty lines.
  Internals (eff_rate, prior, K, wins, active%) move behind `--verbose`.
- **Pooled prior (WS11)** — the historical rate prior is now a pooled
  `Σ(wᵢ·Δppᵢ)/Σ(wᵢ·hoursᵢ)` rather than an average of per-span rates (which inflated
  ~14.6%; 0.927 → 0.809 pp/hr on current data).
- **Duty surface (WS12)** built on clean span readings — removes 121pp of
  personal-account contamination from the weekday×hour histogram.
- **5h projection (WS16)** — activity-weighted shrinkage over *active* hours with a
  recent-rate prior (K=1h), replacing naive wall-clock extrapolation. Early-window
  volatility ±24% → ±3.7%.
- **Report colour ramp** switched to basic 16-colour SGR so `watch -c` renders it
  (procps-ng strips 256-colour); the statusline keeps its 256-colour gradient.

### Fixed

- **Personal-account contamination** — a 62% personal-account endpoint no longer bleeds
  into the work window (spans group + discard by account).
- **Stale-epoch elapsed** — effective reset epoch is `max(reset_history ≤ now,
  resets_at − 7d)`; the presumed weekly boundary is no longer fallback-only (current
  window elapsed reads 11.6h, not 86h).

---

## [1.0.7] — 2026-06-13

### Changed

**Statusline render (`render-rates.py`) — colour & layout polish**
- Each bucket (`5h:`, `7d:`) now renders as a **single colour block** driven by the
  projected value. Previously the current-% label and the `→NN%` projection were
  coloured separately, so they mismatched (e.g. `7d:13%` green, `63%` blue).
- The reset countdown is now inside the colour block **immediately before it**: the
  5h countdown stays in the 5h block; the 7d countdown moves to the **end** of the
  stanza (after `duty:`) and takes the duty colour.
  New layout: `5h:16%→43% ⏳3h7m │ 7d:13%→63% │ duty:46.1% ⏳6d15h`.

---

## [1.0.6] — 2026-06-13

### Added

**WS4 — Bayesian shrinkage projection** (`projection.py`, new module ~1600 lines)
- Three projection modes via `--mode naive|predictive|target` (default: `predictive`):
  - `naive` — existing raw `pct/elapsed` calculations verbatim (retained as control/baseline).
  - `predictive` — Bayesian shrinkage toward a span-correct historical prior: kills early-week
    false STOP/bomb alarms while still catching genuine sustained overruns.
    `effective_rate = (observed × elapsed + prior × K) / (elapsed + K)` where K ≈ 24h.
  - `target` — even-pace prescriptive: `(remaining_budget / remaining_time)` pace line,
    with duty surface breathing on de-rationed natural demand (prevents the rationing
    feedback loop where suppressed late-week burn falsely teaches the system late-week
    is "naturally" quiet).
- Prior computation is span-correct: each completed window's rate = `final_pct / actual_span`
  (not `final_pct / 168h`). Near-100% caps are treated as right-censored lower bounds.
- Personal account excluded deterministically (work account = Saturday 00:00 UTC reset).
- Near-coincident reset labels collapsed (server-side 23:59→00:00 wobble).
- Monotonicity-break detection for intra-week resets (pct drop ≥ 30pp).
- Recency policy: equal-weight (≤5 windows), light-decay transitional (6–7), rolling 12-week
  + exponential decay half-life 5wk (≥8 windows). Tracks habit drift.
- K-widening: 2× at ≤2 windows, 1.5× at 3–4, baseline at ≥5.
- 2D weekday×hour UTC duty surface (168 cells). Activity signal = pp/hr vs idle floor (0.4 pp/hr),
  not snapshot coverage. Active window empirically derived as 07:00–24:00 UTC.
- `--autonomous-status [--ceiling N]` CLI: compact GO/CAUTION/STOP verdict + exit code 0/1/2.
  Shrinkage projection replaces raw-rate alarm while preserving the dual recent-rate signal.

**WS6 — Reset history persistence** (`log-usage.py`, DB schema)
- New append-only `reset_history` table. `preserve_expiring_overrides()` copies
  `reset_overrides` rows into `reset_history` before the sentinel write that would delete them,
  so every reset boundary survives as durable history for the WS4 shrinkage prior.
- Jun 09 21:17 UTC reset boundary re-added after auto-expiry.

**WS7 — Per-cell duty-surface shrinkage toward the all-models prior** (`projection.py`)
- Fixed the Sonnet duty projection bug: `sonnet_weekly` populated only ~22 of 168
  weekday×hour cells; empty (unsampled) cells were treated as idle (weight 0), collapsing
  active-capacity-to-reset from ~108.6 (all-models) to ~20.1 → bogus ~10.8% Sonnet duty
  projection where ~45% is correct.
- Fix: blend each bucket's per-cell weights toward the `seven_day` prior:
  `blended[cell] = (n·w_bucket + M·w_prior) / (n + M)` where M = 4 (DUTY_PRIOR_M).
  Empty cells fall back to the all-models shape; well-sampled cells graduate toward their own.
  `seven_day` self-prior = mathematical no-op (unchanged output).
- Verified: `sonnet_weekly` duty%@reset: 10.7% → 44.7%. `seven_day` unchanged.

**WS1+WS2 — log-usage.py relocated into the plugin + shared burn_rate lib**
- `hooks/log-usage.py`: in-plugin version of the loose `~/.claude/hooks/log-usage.py`.
- `hooks/hooks.json`: registers the Stop hook via `${CLAUDE_PLUGIN_ROOT}` paths —
  survives version bumps without path-rotting.
- WS2: replaced the dynamic `_resolve_burn_rate_dir()` resolver with a direct sibling import
  (`${CLAUDE_PLUGIN_ROOT}/skills/burn-rate/burn_rate.py`). The resolver was the root cause
  of the silent two-day duty-sentinel freeze (dead path + bare `except`).
- `preserve_expiring_overrides()` now delegates the expiry predicate to `burn_rate.parse_iso()`
  — one source of truth; the duplicate inline copy is removed.
- **Phase 2 note:** the loose `~/.claude/hooks/log-usage.py` and its `settings.json` wiring
  are NOT removed in this release. That removal is atomic with the Phase 2 reinstall to avoid
  a logging gap. Until the 1.0.6 reinstall, the live session runs on the 1.0.5 cache.

**render-rates.py** (new script, `skills/burn-rate/render-rates.py`)
- Standalone rate-rendering script for the statusline. Option A display:
  `5h 46%→87% ⌛2h52m` and `7d 6%→269% ⌛6d20h │ duty:168%`.
  Matches 7-colour cool→warm ramp. Not yet wired into the live statusline (WS3, Phase 3).

### Changed

- `burn_rate.py`: default projection mode changed from naive to `predictive`.
  `--mode naive` restores the old behaviour exactly.
- `_build_burn_histogram()` now returns `(hist, sample_counts)` tuple (was just `hist`).
  Callers updated.

---

## [1.0.5] — 2026-06-13 (earlier)

- Added `--autonomous-status [--ceiling N]` to `burn_rate.py`.
  Compact GO/CAUTION/STOP verdict + exit code 0/1/2 for autonomous-loop self-regulation.
- Statusline colour ramp: 7 cool→warm gradations; amber not before 70%, ≥100 brightest.
- Statusline duty staleness: sentinel >1h old → dim grey + trailing `~`.
- Repaired `log-usage.py` dead import (cross-tree path to `burn-rate-custom/` removed when
  the custom skill became the plugin); dynamic resolver added as interim fix.

## [1.0.4] and earlier

Initial plugin releases. Core `burn_rate.py` report: current %, pp/hr, projected exhaustion,
trend series, duty-cycle projection.
