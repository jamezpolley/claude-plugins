# Memory Conventions

## Detecting which pattern a project uses

Read the project's CLAUDE.md — look for a "Project Memory" section. Follow whatever it says. If nothing is specified, default to project root `memory/` only.

## Single-location pattern

Write memory files to `memory/` in the project root. This folder should be committed to git so memories travel with the repo.

## Dual-write pattern

Some projects maintain memory in two locations so that memories are both git-tracked and auto-loaded by Claude Code at session start:

1. **Project memory** — `memory/` in the project root, committed to git
2. **Auto-memory** — `~/.claude/projects/<hash>/memory/`, auto-loaded by Claude Code

The hash is derived from the absolute project path with path separators replaced by hyphens and a leading hyphen added. For a project at `/home/user/projects/myapp`, the hash segment is `-home-user-projects-myapp`, giving a full path of `~/.claude/projects/-home-user-projects-myapp/memory/`.

When both locations exist, write each memory file to both and keep `MEMORY.md` in sync in both.

## MEMORY.md index

Every memory folder should contain a `MEMORY.md` index file. Each entry is one line under ~150 characters:

```
- [Title](filename.md) — one-line hook describing what the memory contains
```

Update the index whenever a memory file is added, changed, or removed.
