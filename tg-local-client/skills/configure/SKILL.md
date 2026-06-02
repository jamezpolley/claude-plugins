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

## Prerequisites

Before running, have ready:
- Bot slug (short identifier, e.g. `pod` or `chez`) — drives the data dir and MCP name
- Bot's Telegram @username (without the `@`)
- Bot token (from BotFather — stays gitignored, never committed)

**Do NOT ask for the group chat ID** — it will be discovered automatically from the bot's recent updates after the token is available (see step 5).

If you don't have these, collect them as follows:

**Slug:** Use AskUserQuestion — suggest 2-3 options derived from the project name (e.g. for `pod-upload-app` suggest `pod`, `upload`, `pod-upload`), plus the user can select "Other" to type their own. AskUserQuestion requires at least 2 options; derive them from the project name/directory.

**Username and token:** Ask for these as plain text in a single follow-up message — do NOT use AskUserQuestion for these (no meaningful options to suggest, and the tool requires ≥2 options per question).

If the user doesn't have a bot yet, point them to https://core.telegram.org/bots#botfather — the official guide covers creating a bot and obtaining the token in a few steps.

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

Write `.claude/tg-local-client/config.local.json` with the values provided. Leave `group_chat_ids` empty for now — it will be populated in step 5 after discovery:

```json
{
  "bot_slug": "<slug>",
  "bot_username": "<username>",
  "mcp_name": "",
  "group_chat_ids": [],
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

### 5. Discover group chat ID

Ask the user to make sure the bot has been added to their coordination group and that at least one message has been sent there (so the bot has seen the chat). Then run:

```bash
TOKEN=$(grep TG_BOT_TOKEN .env | cut -d= -f2)
curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c "
import sys, json
data = json.load(sys.stdin)
chats = {}
for update in data.get('result', []):
    for key in ['message', 'channel_post', 'my_chat_member', 'chat_member']:
        if key in update:
            chat = update[key].get('chat', {})
            if chat and chat.get('type') in ('group', 'supergroup', 'channel'):
                chats[chat['id']] = chat
for cid, chat in sorted(chats.items()):
    print(f'{cid}: {chat.get(\"title\", \"?\")} ({chat[\"type\"]})')
"
```

- If **one group** is found: use its ID, tell the user, and update `group_chat_ids` in `config.local.json`.
- If **multiple groups** are found: use AskUserQuestion to present them (label = title, description = chat ID + type) so the user can pick. Then update `group_chat_ids`.
- If **no groups** are found: tell the user the bot hasn't seen any group messages yet. Ask them to send a message in the group (or add the bot if they haven't), then re-run this step.

After updating `config.local.json` with the discovered chat ID, continue to step 6.

### 6. Register MCP server in .mcp.json


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

### 7. Allow MCP tools in .claude/settings.json

Add `"mcp__<slug>-tg__*"` to `permissions.allow` in `.claude/settings.json`. Merge with existing permissions.

### 8. Summary

Tell the user:
- MCP server name: `<slug>-tg`
- Config: `.claude/tg-local-client/config.local.json`
- Token: sourced from `TG_BOT_TOKEN` in `.env`
- Next step: restart Claude Code session to load the MCP, then run `uv run tg-local-bootstrap` inside `.claude/tg-local-client/` to verify connectivity and post a hello to the group
