---
name: craft-project-status
description: Updates the status of an existing craft project in the crafter's Craft vault. Use whenever the crafter mentions completing a stage, finishing a project, frogging something, or moving a project forward — triggered by phrases like "I finished the shawl", "mark X as complete", "I frogged the slipper project", "I've started the hat", "update the status of Y", "I cast on the cardigan", "I've done the swatch for Z". Updates the YAML frontmatter status field and the Status checklist in the project file. Always use this skill rather than editing the file manually.
---

# craft-project-status

Updates the status of an existing project file in `Projects/`.

## Locating the vault

Resolve the **vault root** before editing: if the current working directory is inside the Craft vault (it contains the Craft `CLAUDE.md` and a `Projects/` folder), walk up to the folder containing `Projects/_template.md` and use that. Otherwise ask the crafter for the vault path. All work happens in the `Projects/` folder relative to that root.

## Valid statuses

| Status | Meaning |
|--------|---------|
| `planning` | Not yet started; thinking about it |
| `in-progress` | Actively working on it |
| `complete` | Finished object |
| `frogged` | Abandoned / unravelled |

## Step 1: Identify the project

Find the matching file in `Projects/` by fuzzy-matching the filename or the `pattern:` frontmatter field. If there is ambiguity, list the matches and ask. If no project was named and multiple exist, list them and ask.

## Step 2: Determine the new status

Infer from what the user said — "I finished" means `complete`, "I frogged" means `frogged`, "I've started" / "I cast on" means `in-progress`. Ask if unclear.

Note any specific stage completion (swatch done, cast on, etc.) to update the checklist accurately.

## Step 3: Update the file

### Frontmatter

- Set `status:` to the new value
- Update `tags:` — remove the old status tag (e.g. `active`) and add the new one
- If `complete`: set `completed:` to today's date (YYYY-MM-DD) if blank
- If `frogged`: add `frogged:` field with today's date if it doesn't exist
- If returning to `in-progress` or `planning`: clear `completed:` (set to blank)

### Status checklist

The template has exactly three items:
```
- [ ] Gauge swatch
- [ ] Cast on / begin
- [ ] Complete
```

Check or uncheck existing items only — never add new checklist items.

| New status | What to do |
|------------|------------|
| `planning` | Uncheck all three |
| `in-progress` | Check only items the user said are done; leave the rest unchecked |
| `complete` | Check all three |
| `frogged` | Leave checklist as-is; add a note in Notes: `Frogged YYYY-MM-DD — <reason>.` |

If the user mentions completing a specific stage, check that item regardless of overall status.

## Step 4: Confirm

Two lines: which file changed, and old status → new status plus any checklist items ticked.
