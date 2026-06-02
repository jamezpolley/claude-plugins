---
name: tg-local-client:configure
description: Bootstrap a per-project tg-local-client Telegram bot. Clones the client, writes config, merges the token into .env, updates .gitignore, and registers the MCP server in .mcp.json.
---

# tg-local-client:configure

Set up a per-project Telegram bot client for agent communication.

## Prerequisites

Before running, have ready:
- Bot slug (short identifier, e.g. `pod` or `chez`) — drives the data dir and MCP name
- Bot's Telegram @username (without the `@`)
- Chat ID of the coordination group the bot has been added to
- Bot token (from BotFather — stays gitignored, never committed)

If you don't have these, ask the user.

## Steps

### 1. Check for existing install

If `.claude/tg-local-client/` already exists, skip to step 3.

### 2. Clone tg-local-client

```bash
git clone https://github.com/jamezpolley/tg-local-client.git .claude/tg-local-client
```

Add to `.gitignore` (append, don't overwrite):
```
.claude/tg-local-client/
```

### 3. Write config.local.json

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

### 4. Merge token into .env

Read the existing `.env` if present. **Only append** — never overwrite existing values.

If `TG_BOT_TOKEN` (or whatever `token_env_var` is set to) is already present, skip.
Otherwise append:

```
# tg-local-client — bot token for <slug> (@<username>)
TG_BOT_TOKEN=<token>
```

Add `.env` to `.gitignore` if not already present. If `.env` is already gitignored (e.g. via `*.env` or a devcontainer pattern), note that and skip.

### 5. Register MCP server in .mcp.json

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

### 6. Allow MCP tools in .claude/settings.json

Add `"mcp__<slug>-tg__*"` to `permissions.allow` in `.claude/settings.json`. Merge with existing permissions.

### 7. Summary

Tell the user:
- MCP server name: `<slug>-tg`
- Config: `.claude/tg-local-client/config.local.json`
- Token: sourced from `TG_BOT_TOKEN` in `.env`
- Next step: restart Claude Code session to load the MCP, then run `tg-local-bootstrap` to verify connectivity and post a hello to the group
