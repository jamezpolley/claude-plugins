---
name: new-craft-project
description: Creates a new craft project note in the crafter's Craft vault. Use this whenever the crafter wants to start tracking a new crochet or knitting project — triggered by things like "I'm starting a new project", "add a project for X pattern", "create a note for Y", "I want to track this shawl/hat/bag/slipper pattern", or "set up a project for [designer/pattern name]". Fills in an Obsidian-compatible markdown file with YAML frontmatter (yarn, hook, status, etc.) from the project template. Always use this skill rather than creating a file manually.
---

# new-craft-project

Creates a new project note in `Projects/` inside the crafter's Craft vault, populated from the standard template.

## Locating the vault

The Craft vault is a SeaDrive-synced folder whose absolute path differs per machine and per user. Resolve the **vault root** before doing anything else, in this order:

1. If the current working directory is inside the Craft vault (it contains a `CLAUDE.md` describing the "Craft" repo and a `Projects/` folder), use that — walk up from the cwd to find the folder containing `Projects/_template.md`.
2. Otherwise ask the crafter for the vault path rather than guessing.

All paths below are relative to that resolved **vault root**:

- **Projects folder:** `Projects/`
- **Template:** `Projects/_template.md`
- **CLAUDE.md:** `CLAUDE.md`

## What to gather

Before creating the file, collect these details. If the user has already provided them in their message, use those values and don't ask again. For anything missing, ask in a single grouped question rather than one at a time.

| Field | Required? | Notes |
|-------|-----------|-------|
| Project name | Yes | Human-readable, e.g. "Cosy Winter Hat" |
| Pattern name | Yes | As printed on pattern |
| Designer | Yes | |
| Craft type | Yes | crochet / knitting / other |
| Yarn | Yes | Brand + colorway if known |
| Weight | Yes | lace / fingering / DK / worsted / bulky / etc. |
| Hook or needle size | Yes | e.g. 5mm, 4.5mm, US 7 |
| Year (of pattern) | No | Leave blank if unknown |
| Pattern file path | No | Relative path under Patterns/, if known |
| Yarnl entry URL | No | Leave blank if not yet added |

## Creating the file

1. **Generate a filename** — kebab-case from the project name, lowercase, spaces to hyphens, strip punctuation. E.g. "Cosy Winter Hat" becomes `cosy-winter-hat.md`.

2. **Set today's date** as `started:` in ISO format (YYYY-MM-DD).

3. **Choose the right tool field** — for crochet use `hook:`, for knitting use `needles:`. Use the same label in the Materials section body (`**Hook:**` or `**Needles:**`).

4. **Build the frontmatter** — set `status: planning` by default (unless the user says they've already started, in which case use `in-progress`). Leave optional fields blank rather than omitting them.

5. **Write the file** to `Projects/<filename>.md` using this structure:

```markdown
---
tags: [project, <craft-type>]
status: planning
started: <YYYY-MM-DD>
completed:
pattern: <pattern name>
designer: <designer>
year: <year or blank>
craft: <crochet/knitting/other>
yarn: <yarn>
weight: <weight>
hook: <size>
---

# <Project Name>

**Designer:** <designer> (<year if known>)
**Pattern file:** `Patterns/<filename or blank>`
**Yarnl entry:** <URL or blank>

---

## Materials

**Yarn:** <yarn>
**Hook:** <size>

---

## Gauge

**Target:**
**Actual:**

---

## Status

- [ ] Gauge swatch
- [ ] Cast on / begin
- [ ] Complete

---

## Notes

```

For knitting projects, use `needles:` instead of `hook:` in the frontmatter key and `**Needles:**` in the Materials section.

6. **Update CLAUDE.md** — find the `## Active Project` section. If it points at a different project, move that to a `## Previous Projects` list (create one if needed). Update the pointer to the new file.

## Confirm with the user

After writing the file, tell the user the filename, that CLAUDE.md was updated, and a one-line summary. Don't show the full file contents unless asked.
