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

**CRITICAL — this is the #1 rule for this entire step: the bot token must NEVER appear in the Claude Code chat or session transcript.** Transcripts are stored in plaintext at `~/.claude/projects/`. A token in chat is a token on disk, potentially in backups, logs, and future AI context windows.

This means: do not ask for the token, do not have the user paste it, do not echo it back, do not include it in any message. If you are ever unsure whether a step would expose the token, choose the option that doesn't.

**`!` commands are NOT safe for token-containing commands.** When a user runs `! <command>` in Claude Code, the command string appears in the chat transcript. Never suggest `! curl https://api.telegram.org/bot<TOKEN>/...` or any command with the token inline — the token ends up in the transcript. If a curl/API call involving the token is needed for debugging, tell the user to run it in a separate terminal outside of Claude Code.

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

```bash
echo "op:$(which op 2>/dev/null && echo yes || echo no)"
echo "op-env-supported:$(op environment read --help >/dev/null 2>&1 && echo yes || echo no)"
echo "op-service-account:$([ -n \"$OP_SERVICE_ACCOUNT_TOKEN\" ] && echo yes || echo no)"
echo "mise:$(which mise 2>/dev/null && echo yes || echo no)"
echo "secret-tool:$(which secret-tool 2>/dev/null && echo yes || echo no)"
echo "security:$(which security 2>/dev/null && echo yes || echo no)"
```

#### 4d. Present options and guide the user

Use AskUserQuestion to ask how they want to store the token. Only offer options for tools that are detected. Always include `.env file` as a fallback. At least 2 options required.

**Available options** (show only if detected):

| Tool | Condition | Label | Description |
|------|-----------|-------|-------------|
| `op` + `op environment` supported + `OP_SERVICE_ACCOUNT_TOKEN` set | all three | **1Password Environment** | Token lives in a 1Password Environment; injected via `op run --environment`. Requires op CLI beta + service account. No env block needed in .mcp.json. |
| `op` available | `op` only | **1Password vault** | Token stored in a vault item; injected via `op run`. Works with desktop app or service account. |
| `mise` | `mise` found | **mise** | Stored in `mise.local.toml` or `.mise.local.toml` (gitignored); injected automatically. |
| `secret-tool` | found | **Linux keyring** | Stored in system keyring. |
| `security` | found | **macOS Keychain** | Stored in macOS Keychain. |
| always | — | **.env file** | Written via silent terminal read; never echoed to chat. |

Once the user picks, look up how to use that tool (`<tool> --help`, `man <tool>`, or context7) and guide them through:
1. Storing the token (out-of-band — the user runs the command in their terminal, not via chat)
2. Exposing it as `TG_BOT_TOKEN` in the environment

**Key constraint**: the token must never appear in the Claude Code chat. Use `read -rs` or equivalent silent-input patterns. Remind the user they can run terminal commands with `! <command>` in Claude Code to keep output local.

**1Password Environment special case** (requires op CLI beta + `OP_SERVICE_ACCOUNT_TOKEN`): no `env` block in .mcp.json — instead wrap the command with `op run --environment <environmentID>`. See step 6. Docs: https://www.1password.dev/environments/read-environment-variables#cli

**1Password vault special case**: offer a sub-choice between the `op run` wrapper (token injected at subprocess launch, never in shell env — requires a different .mcp.json entry, see step 6) vs exporting via `op read` in their shell profile.

#### 4e. Verify the token is accessible

After setup, verify without revealing the value:

```bash
echo "token length: ${#TG_BOT_TOKEN}"
```

If 0, the env var isn't set — ask the user to open a fresh shell or source their profile.

### 5. Discover group chat ID

#### 5a. Verify the token with getMe

Before querying for updates, call `getMe` to confirm the token is valid and check privacy mode:

```bash
curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe"
```

Check the response:
- `username` should match the configured bot username — if not, the token is for the wrong bot
- `can_read_all_group_messages: false` means privacy mode is on — the bot only sees messages that @mention it directly. Tell the user to send `@<botusername> hello` in the group rather than any plain message.

Do not suggest running any command with the token embedded as a literal value in the chat — `${TG_BOT_TOKEN}` is safe because it resolves in the shell.

#### 5b. Query for updates

Ask the user to make sure the bot has been added to their group and a message has been sent (or `@<botusername> hello` if privacy mode is on). Then run:

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
- If **no groups** are found, check:
  - **Webhook set?** Run `curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/getWebhookInfo"` — if a webhook URL is present, another client is consuming updates. Suggest rotating the token via BotFather to cut off existing connections.
  - **Another long-polling client?** Same symptom — rotating the token is the fix.
  - **Forum supergroup?** (`has_topics_enabled: true` in group info) — messages in topic threads may not appear in getUpdates. Fall back to asking the user to find the chat ID from the Telegram Web URL (`web.telegram.org` → open group → copy ID from URL).

After updating `config.local.json` with the discovered chat ID(s), continue to step 6.

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

**1Password Environment entry** (requires op CLI beta + `OP_SERVICE_ACCOUNT_TOKEN` in the environment — set it in `mise.local.toml` or `.mise.local.toml` and mise will inject it automatically):

```json
{
  "mcpServers": {
    "<slug>-tg": {
      "command": "op",
      "args": ["run", "--environment", "<environmentID>", "--", "uv", "run", "--directory", "<abs-path>", "tg-local-mcp"],
      "env": {
        "OP_SERVICE_ACCOUNT_TOKEN": "${OP_SERVICE_ACCOUNT_TOKEN}"
      }
    }
  }
}
```

`op run --environment` reads the 1Password Environment and injects its variables into the subprocess directly — no separate `op environment read` step needed.

**1Password vault op run wrapper entry**:

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

> **Scope warning:** The MCP server must be registered at **project scope only** (via `.mcp.json`). If the user previously ran `claude mcp add` without `--scope project`, it may also be registered at user scope. Check and remove any duplicate:
> ```bash
> claude mcp list
> ```
> If `<slug>-tg` appears in both project and user scope, remove the user-scoped one:
> ```bash
> claude mcp remove <slug>-tg -s user
> ```

### 8. Add SessionStart hook to .claude/settings.json

Add a `SessionStart` hook that automatically pulls the latest tg-local-client code and reminds the agent to start the Telegram monitor. Merge with any existing hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "git -C .claude/tg-local-client pull --quiet 2>&1 | grep -v 'Already up to date' || true; echo '[tg-local-client] Start your Telegram monitor now: call get_tail_command then pass the result directly to Monitor(persistent=True).'"
          }
        ]
      }
    ]
  }
}
```

If `SessionStart` hooks already exist, append this entry to the existing array — do not replace it.

### 9. Summary

Tell the user:
- MCP server name: `<slug>-tg`
- Config: `.claude/tg-local-client/config.local.json`
- Token storage: describe the method chosen in step 4
- Next step: restart Claude Code session to load the MCP, then run `uv run tg-local-bootstrap` inside `.claude/tg-local-client/` to verify connectivity and post a hello to the group
