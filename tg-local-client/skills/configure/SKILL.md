---
name: tg-local-client:configure
description: Bootstrap a per-project tg-local-client Telegram bot. Clones the client, writes config, merges the token into .env, updates .gitignore, and registers the MCP server in .mcp.json.
---

# tg-local-client:configure

Set up a per-project Telegram bot client for agent communication.

> **Installing this plugin for the first time?**
> ```
> /plugin marketplace add jamezpolley/claude-plugins
> /plugin install tg-local-client@jamezpolley --scope project
> /reload-plugins
> ```
> Then re-run `/tg-local-client:configure`.

## Step 0 — Gather information from the user

Tell the user you need four things, explaining each one clearly:

1. **Bot slug** — a short lowercase identifier you choose for this bot, e.g. `pod` for a pod-upload-app bot or `chez` for a chezmoi bot. It determines the MCP server name (`<slug>-tg`) and the local data directory. Pick something short that identifies this project.

2. **Bot's Telegram @username** — the username of the Telegram bot assigned to this project (without the leading `@`). If the user doesn't know it, they can find it in Telegram by messaging @BotFather with `/mybots`.

3. **Coordination group chat ID** — the numeric Telegram chat ID of the group this bot has been added to (negative number, e.g. `-1003730692254`). If the user doesn't know it, they can find it by adding @userinfobot to the group.

4. **Bot token** — the secret token from BotFather for this bot. It looks like `123456789:AAH...`. It will be stored in `.env` (gitignored) and never committed.

Ask for all four in a single message. Don't proceed until you have them all.

## Step 1 — Check for existing install

If `.claude/tg-local-client/` already exists, skip to step 3.

## Step 2 — Clone tg-local-client

```bash
git clone https://github.com/jamezpolley/tg-local-client.git .claude/tg-local-client
```

Add to `.gitignore` (append, don't overwrite):
```
.claude/tg-local-client/
```

## Step 3 — Write config.local.json

Write `.claude/tg-local-client/config.local.json` with the values provided:

```json
{
  "bot_slug": "<slug>",
  "bot_username": "<username>",
  "mcp_name": "",
  "group_chat_ids": [<chat_id>],
  "token_env_var": "TG_BOT_TOKEN"
}
```

`mcp_name` left empty defaults to `<slug>-tg`. Set it explicitly only if the slug-derived name would conflict.

## Step 4 — Merge token into .env

Read the existing `.env` if present. **Only append** — never overwrite existing values.

If `TG_BOT_TOKEN` (or whatever `token_env_var` is set to) is already present, skip.
Otherwise append:

```
# tg-local-client — bot token for <slug> (@<username>)
TG_BOT_TOKEN=<token>
```

Add `.env` to `.gitignore` if not already present. If `.env` is already gitignored (e.g. via `*.env` or a devcontainer pattern), note that and skip.

## Step 5 — Register MCP server in .mcp.json

Create or update `.mcp.json` in the project root. Merge — preserve any existing entries.

The entry to add (replace `<abs-path>` with the absolute path to `.claude/tg-local-client/` and `TG_BOT_TOKEN` with the configured `token_env_var`):

```json
{
  "mcpServers": {
    "<slug>-tg": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "tg-local-mcp"],
      "env": {
        "TG_BOT_TOKEN": "${TG_BOT_TOKEN}"
      }
    }
  }
}
```

## Step 6 — Allow MCP tools in .claude/settings.json

Add `"mcp__<slug>-tg__*"` to `permissions.allow` in `.claude/settings.json`. Merge with existing permissions.

## Step 7 — Summary

Tell the user:
- MCP server name: `<slug>-tg`
- Config: `.claude/tg-local-client/config.local.json`
- Token: sourced from `TG_BOT_TOKEN` in `.env`
- Next step: restart Claude Code session to load the MCP, then run `uv run tg-local-bootstrap` inside `.claude/tg-local-client/` to verify connectivity and post a hello to the group
