---
name: tg-local-client:configure
description: Bootstrap a per-project tg-local-client Telegram bot. Clones the client, writes config, stores the token securely, discovers the group chat, and registers the MCP server in .mcp.json.
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
- Bot token (from BotFather — **never paste this into the chat**, see step 4)

**Do NOT ask for the group chat ID** — it will be discovered automatically from the bot's recent updates after the token is available (see step 5).

If you don't have these, collect them as follows:

**Slug:** Use AskUserQuestion — suggest 2-3 options derived from the project name (e.g. for `pod-upload-app` suggest `pod`, `upload`, `pod-upload`), plus the user can select "Other" to type their own. AskUserQuestion requires at least 2 options; derive them from the project name/directory.

**Username:** Ask as plain text.

**Token:** Do NOT ask for the token in chat — it would be stored in the session transcript. Collect it via the secure method chosen in step 4.

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

Write `.claude/tg-local-client/config.local.json` with the values collected so far. Leave `group_chat_ids` empty — it will be populated in step 5 after discovery:

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

### 4. Store the bot token securely

**IMPORTANT: never ask the user to paste the token into chat.** It would be stored in the session transcript in plaintext.

#### 4a. Check if a token is already configured

First check if `TG_BOT_TOKEN` is already set in the environment or in an existing `.env` / `mise.local.toml`:

```bash
echo "env:${#TG_BOT_TOKEN}"
grep -s TG_BOT_TOKEN .env mise.local.toml 2>/dev/null | head -3
```

If the token is already present and non-empty, skip to step 5.

#### 4b. Check for an .env.sample or .env.example

If no token is set, check whether the project has a sample env file:

```bash
ls .env.sample .env.example 2>/dev/null
```

If one exists, tell the user the simplest path:
> "The easiest way to set up your token: copy `.env.sample` to `.env`, then open `.env` in your editor and replace the placeholder with your bot token. Add `.env` to `.gitignore` if it isn't already."

Wait for them to do this, then re-check step 4a before continuing. If they prefer a more secure method (keychain, 1Password, mise), continue to 4c.

#### 4c. Detect available secret managers

Run the following to detect what's available:

```bash
echo "op:$(which op 2>/dev/null && echo yes || echo no)"
echo "mise:$(which mise 2>/dev/null && echo yes || echo no)"
echo "mise-local:$(test -f mise.local.toml && echo yes || echo no)"
echo "secret-tool:$(which secret-tool 2>/dev/null && echo yes || echo no)"
echo "security:$(which security 2>/dev/null && echo yes || echo no)"
```

#### 4d. Present options to the user

Use AskUserQuestion to ask how they want to store the token. Only offer options for tools that are available. Order by preference (most secure first). Always include `.env file` as a fallback. Include at least 2 options (required by AskUserQuestion).

**Available options** (show only if detected):

| Option | Label | Description |
|--------|-------|-------------|
| `op` available | **1Password** | Token stored in 1Password vault; never touches disk. Offers sub-choice: op run wrapper (most secure) or env var export. |
| `mise` available | **mise** | Token stored in `mise.local.toml` (gitignored by convention). mise injects it into the environment automatically. |
| `secret-tool` available | **Linux keyring** | Token stored in the system keyring via `secret-tool`. Retrieved at shell startup. |
| `security` available | **macOS Keychain** | Token stored in macOS Keychain via `security`. Retrieved at shell startup. |
| Always | **.env file** | Token written to `.env` using a silent terminal read — never echoed. Simple and portable. |

#### 4e. If user chooses **1Password**

Ask a follow-up (AskUserQuestion with 2 options):
- **op run wrapper** — .mcp.json wraps the command with `op run`; token only exists for the subprocess lifetime. More secure.
- **Environment variable** — export `TG_BOT_TOKEN` from 1Password in your shell profile using `op run`. Simpler, token lives in shell env.

For **op run wrapper**: tell the user to store the token in 1Password (if not already there) and note the vault/item/field path. Then in step 6, use this .mcp.json entry instead of the default:

```json
{
  "mcpServers": {
    "<slug>-tg": {
      "command": "op",
      "args": ["run", "--", "uv", "run", "--directory", "<abs-path>", "tg-local-mcp"],
      "env": {
        "TG_BOT_TOKEN": "op://<vault>/<item>/<field>"
      }
    }
  }
}
```

For **environment variable**: instruct the user to add to their shell profile (e.g. `~/.zshrc` or `mise.local.toml`):
```sh
export TG_BOT_TOKEN=$(op read "op://<vault>/<item>/<field>")
```

#### 4f. If user chooses **mise**

Tell the user to run this in their terminal (use `! <command>` in Claude Code to avoid the token appearing in chat):

```
Tell the user: "Run this in your terminal — the token will be written directly to mise.local.toml without appearing in chat:
! read -rs TG_BOT_TOKEN && printf '\n[env]\nTG_BOT_TOKEN = \"%s\"\n' \"$TG_BOT_TOKEN\" >> mise.local.toml && echo 'Written.'"
```

If `mise.local.toml` doesn't exist, create it first with just `[env]` and have the user append to it.
Add `mise.local.toml` to `.gitignore` if not already present.

#### 4g. If user chooses **Linux keyring** (`secret-tool`)

Tell the user to run in their terminal:
```
! read -rs TG_BOT_TOKEN && secret-tool store --label="TG bot: <slug>" service telegram-bot account <slug> <<< "$TG_BOT_TOKEN" && echo 'Stored.'
```

Then add to their shell profile to export it:
```sh
export TG_BOT_TOKEN=$(secret-tool lookup service telegram-bot account <slug>)
```

#### 4h. If user chooses **macOS Keychain** (`security`)

Tell the user to run in their terminal:
```
! read -rs TG_BOT_TOKEN && security add-generic-password -s "tg-<slug>" -a "telegram-bot" -w "$TG_BOT_TOKEN" && echo 'Stored.'
```

Then add to their shell profile to export it:
```sh
export TG_BOT_TOKEN=$(security find-generic-password -s "tg-<slug>" -a "telegram-bot" -w)
```

#### 4i. If user chooses **.env file**

Tell the user to run this in their terminal (the token is read silently and written directly to `.env`):
```
Tell the user: "Run this in your terminal:
! read -rs TG_BOT_TOKEN && printf '# tg-local-client — bot token for <slug> (@<username>)\nTG_BOT_TOKEN=%s\n' \"$TG_BOT_TOKEN\" >> .env && echo 'Written.'"
```

Add `.env` to `.gitignore` if not already present.

#### 4j. Verify the token is accessible

After the user completes their chosen method, verify the token is reachable without reading its value:

```bash
# Should print the token length (not the token itself)
echo ${#TG_BOT_TOKEN} 
```

If the length is 0, the env var isn't set — ask the user to open a fresh shell or source their profile.

For the getUpdates discovery in step 5, read the token from the environment:
```bash
TOKEN="$TG_BOT_TOKEN"
```

### 5. Discover group chat ID

Ask the user to make sure the bot has been added to their coordination group and that at least one message has been sent there (so the bot has seen the chat). Then run:

```bash
curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/getUpdates" | python3 -c "
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

**Default entry** (all storage methods except 1Password op run wrapper):

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

**1Password op run wrapper entry** (if user chose that option in step 4c):

```json
{
  "mcpServers": {
    "<slug>-tg": {
      "command": "op",
      "args": ["run", "--", "uv", "run", "--directory", "<abs-path>", "tg-local-mcp"],
      "env": {
        "TG_BOT_TOKEN": "op://<vault>/<item>/<field>"
      }
    }
  }
}
```

Replace `<abs-path>` with the absolute path to `.claude/tg-local-client/`.

### 7. Allow MCP tools in .claude/settings.json

Add `"mcp__<slug>-tg__*"` to `permissions.allow` in `.claude/settings.json`. Merge with existing permissions.

### 8. Summary

Tell the user:
- MCP server name: `<slug>-tg`
- Config: `.claude/tg-local-client/config.local.json`
- Token storage: describe the method chosen in step 4
- Next step: restart Claude Code session to load the MCP, then run `uv run tg-local-bootstrap` inside `.claude/tg-local-client/` to verify connectivity and post a hello to the group
