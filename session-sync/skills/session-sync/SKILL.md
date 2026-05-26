---
name: session-sync
description: This skill should be used when the user says "sync session", "save what we learned", "update project docs", "end of session", or wants to persist learnings from the current conversation to CLAUDE.md, memory files, and git.
---

# Session Knowledge Sync

Capture and persist session learnings: plan → approve → update docs and memory → commit.

## Step 1 — Enter plan mode and draft

Call `EnterPlanMode` (skip if plan mode is already active — the user may have pre-entered). Build the sync plan in the plan file using these sections:

- **Confirmed or corrected facts** — things now known with more certainty, or previous records that were wrong
- **New patterns or rules** — behaviours, formulas, constraints discovered this session
- **Data recorded** — tables written to, rows inserted, files changed during the session (already happened; recording for the audit trail)
- **Open questions** — things needing more data or follow-up
- **Proposed doc changes** — for each file you intend to edit, the specific addition/correction (paraphrase or diff). Name the file path explicitly.

One sentence per item in the summary sections; specific file paths plus the actual edit content for the proposed changes.

Call `ExitPlanMode` to request approval. Do not proceed to Step 2 until the user approves. If the user corrects or edits the plan, the approved plan file (which may reflect their edits) is now the spec for the subsequent steps.

## Step 2 — Update docs

### CLAUDE.md

Edit surgically — add or correct only what changed this session. Do not rewrite stable sections. Target:
- Domain rules, formulas, thresholds
- Terminology corrections or additions
- New database tables or schema changes
- Non-obvious constraints or gotchas

### Reference files

Check CLAUDE.md for what reference files the project maintains and when to update them. Only update files that are relevant to this session's learnings.

## Step 3 — Update memory

Check CLAUDE.md for the project's memory conventions. If no conventions are specified, default to a `memory/` folder in the project root.

For projects using the dual-write pattern (project `memory/` folder + auto-memory path), write to both and keep `MEMORY.md` in sync in both locations.

See [references/memory-conventions.md](references/memory-conventions.md) for the dual-write pattern detail and how to derive the auto-memory path.

Memory file format:
```
---
name: <name>
description: <one-line relevance hint for future sessions>
type: user | feedback | project | reference
---

<content>
For feedback/project types: lead with the rule/fact, then **Why:** and **How to apply:** lines.
```

Update the `MEMORY.md` index whenever a file is added or changed.

## Step 4 — Commit

```bash
git add CLAUDE.md memory/ <any reference files changed>
git commit -m "Session sync: <one-line summary>"
```

If the project has a pre-commit hook that handles additional artefacts (DB dumps, etc.), stage broadly and let the hook handle it.

## What NOT to do

- Do not write data rows during sync — those happen during the session
- Do not rewrite sections that haven't changed
- Do not create new reference files that CLAUDE.md doesn't already describe
