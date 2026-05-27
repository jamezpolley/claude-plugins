# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code plugin marketplace owned by `jamezpolley`. It publishes plugins as a named marketplace so they can be installed via `/plugin marketplace add jamezpolley/<name>`.

## Repository structure

Each plugin lives in its own subdirectory and is self-contained:

```
<plugin-name>/
  .claude-plugin/plugin.json   # Plugin metadata (name, version, description, dependencies)
  skills/<skill-name>/
    SKILL.md                   # The skill instruction file loaded into Claude's context
    references/                # Optional reference files cited by SKILL.md
  README.md                    # Human-facing docs
```

The top-level `.claude-plugin/marketplace.json` registers this repo as the `jamezpolley` marketplace and lists all plugins with their source paths.

## Plugin schema

**`marketplace.json`** — top-level registry:
- `name` — marketplace identifier used in `/plugin marketplace add <name>`
- `allowCrossMarketplaceDependenciesOn` — array of other marketplaces whose plugins can be declared as dependencies
- `plugins[]` — each entry has `name`, `source` (relative path to plugin dir), `description`, `keywords`

**`plugin.json`** — per-plugin metadata:
- `name`, `version`, `description`, `author`, `license`, `keywords`
- `dependencies[]` — optional; each entry: `{ "name": "<plugin>", "marketplace": "<marketplace>", "version": "<semver range>" }`

**`SKILL.md`** — instruction file with YAML frontmatter (`name`, `description`) followed by markdown. The `description` field controls when Claude auto-invokes the skill. The body is the actual instructions Claude follows.

## Adding a new plugin

1. Create `<plugin-name>/.claude-plugin/plugin.json` with metadata
2. Create `<plugin-name>/skills/<skill-name>/SKILL.md` with frontmatter + instructions
3. Add a `plugins[]` entry to `.claude-plugin/marketplace.json`
4. Add a `README.md` if the plugin is user-facing

## Current plugins

- **session-sync** — end-of-session knowledge sync (summarise → confirm → update CLAUDE.md + memory → git commit). Depends on `remember@claude-plugins-official >=0.5.0`.
- **get-current-time** — gets current local time via `user_time_v0`, `bash date`, Desktop Commander, or Windows-MCP, in that priority order.
- **craft-projects** — manage Craft vault project notes; bundles two skills: `new-craft-project` (create a note from the template) and `craft-project-status` (update status + checklist).
