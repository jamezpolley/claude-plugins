# craft-projects

Two skills for managing project notes in James's Craft vault (crochet & knitting).

## Skills

- **new-craft-project** — Creates a new project note in `Projects/` from the standard template, gathering yarn/hook/pattern details and updating the vault's CLAUDE.md "Active Project" pointer. Triggered by things like "I'm starting a new project" or "add a project for X pattern".
- **craft-project-status** — Updates an existing project's status (`planning` / `in-progress` / `complete` / `frogged`), keeping the YAML frontmatter and the Status checklist in sync. Triggered by "I finished the shawl", "I frogged the slippers", "I cast on the hat", etc.

## Install

```
/plugin marketplace add jamezpolley/claude-plugins
/plugin install craft-projects@jamezpolley
```

## Notes

Both skills target the Craft vault at `C:\Users\jamez\.cache\seadrive\James\My Libraries\Craft`. They are personal to that vault layout rather than general-purpose.
