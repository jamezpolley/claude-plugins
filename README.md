# session-sync

A Claude Code plugin that summarises what was learned in a session, confirms with the user, then persists updates to CLAUDE.md, memory files, and git.

## ⚠️ Caveat emptor

This plugin is AI slop. It was designed and written entirely by Claude, with James providing direction and feedback. James makes no representations concerning its correctness, reliability, or fitness for any particular purpose.

**Do not install this plugin unless you have:**
1. Read the skill and reference files yourself
2. Understood what they instruct Claude to do
3. Verified that it actually solves a problem you have

You have been warned.

## Installation

```
/plugin marketplace add jamezpolley/claude-session-sync
/plugin install session-sync@jamezpolley
```

This plugin depends on the `remember` plugin from `claude-plugins-official`, which should be installed automatically. If not:

```
/plugin install remember@claude-plugins-official
```

## Usage

Run `/session-sync` at the end of a session to summarise learnings, confirm with the user, update CLAUDE.md and memory files, and commit to git.

Works best when your project's CLAUDE.md describes where memory files live and which reference docs to maintain.
