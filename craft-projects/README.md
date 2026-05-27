# craft-projects

Two skills for managing project notes in a SeaDrive-synced Craft vault (crochet & knitting).

## Skills

- **new-craft-project** — Creates a new project note in `Projects/` from the standard template, gathering yarn/hook/pattern details and updating the vault's CLAUDE.md "Active Project" pointer. Triggered by things like "I'm starting a new project" or "add a project for X pattern".
- **craft-project-status** — Updates an existing project's status (`planning` / `in-progress` / `complete` / `frogged`), keeping the YAML frontmatter and the Status checklist in sync. Triggered by "I finished the shawl", "I frogged the slippers", "I cast on the hat", etc.

## Install

```
/plugin marketplace add jamezpolley/claude-plugins
/plugin install craft-projects@jamezpolley
```

## Notes

Both skills resolve the Craft vault from the current working directory (the folder containing `CLAUDE.md` and `Projects/_template.md`), falling back to asking the crafter if it can't be found. They assume that vault layout — an Obsidian-style `Projects/` folder with a `_template.md` — rather than being fully general-purpose.
