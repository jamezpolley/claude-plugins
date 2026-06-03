---
name: tg-local-client:configure
description: Bootstrap a per-project Telegram bot for agent communication. Detects and migrates existing setups, or runs a fresh install. Writes config, stores the token securely, discovers the group chat, registers the MCP server in .mcp.json, and adds a SessionStart monitor reminder.
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

## Steps

### 1. Detect install type

Check for existing config files to determine the install state:

```bash
cat .claude/tg-bot-client/config.local.json 2>/dev/null && echo "---NEW---" || true
cat .claude/tg-local-client/config.local.json 2>/dev/null && echo "---OLD---" || true
```

- **`tg-bot-client` config found** → **re-run/recovery mode**: config already written from a prior configure run. Use the existing `bot_slug`, `bot_username`, and `group_chat_ids`. Skip steps 2, 3, and 5. Proceed to step 4 (verify token) and step 6 (fix `.mcp.json` if needed).
- **`tg-local-client` config found** → **migration mode**: read values from it. Skip steps 2 and 5.
- **Neither found** → **fresh install**: proceed through all steps.

Also check `.mcp.json` directly — read it with the Read tool and note any `mcpServers` keys that are not `tg-bot-client`. Those are stale and will be removed in step 6. Also check whether the `tg-bot-client` entry (if present) contains the literal string `CLAUDE_PLUGIN_ROOT` — if so, it is broken and must be rewritten in step 6.

### 2. Collect prerequisites (fresh install only — skip if migration)

**Slug:** Use AskUserQuestion — suggest 2-3 options derived from the project name (e.g. for `pod-upload-app` suggest `pod`, `upload`, `pod-upload`), plus "Other" for custom input. AskUserQuestion requires at least 2 options.

**Username:** Ask as plain text (the bot's Telegram @username, without the `@`).

**Token:** Do NOT ask in chat — collect via the secure method in step 4.

If the user doesn't have a bot yet, point them to https://core.telegram.org/bots#botfather.

### 3. Write .claude/tg-bot-client/config.local.json

```bash
mkdir -p .claude/tg-bot-client
```

Write `.claude/tg-bot-client/config.local.json`. For migration, use values read in step 1. For fresh install, leave `group_chat_ids` empty (populated in step 5):

```json
{
  "bot_slug": "<slug>",
  "bot_username": "<username>",
  "group_chat_ids": [],
  "token_env_var": "TG_BOT_TOKEN"
}
```

Add to `.gitignore` (append, don't overwrite):
```
.claude/tg-bot-client/
```

### 4. Store the bot token securely

**CRITICAL — the #1 rule: the bot token must NEVER appear in the Claude Code chat or session transcript.** Transcripts are stored in plaintext at `~/.claude/projects/`. A token in chat is a token on disk, potentially in backups, logs, and future AI context windows.

Do not ask for the token, do not have the user paste it, do not echo it back, do not include it in any message.

**`!` commands are NOT safe for token-containing commands.** When a user runs `! <command>` in Claude Code, the command string appears in the transcript. Never suggest `! curl https://api.telegram.org/bot<TOKEN>/...` or any command with the token inline. If a curl/API call involving the token is needed for debugging, tell the user to run it in a separate terminal outside of Claude Code.

#### 4a. Check if a token is already configured

```bash
echo "token length: ${#TG_BOT_TOKEN}"
```

If the length is non-zero, the token is available in the environment — skip to step 5. Do not run grep or any command that could print file contents containing the token.

#### 4b. Check for an .env.sample or .env.example

```bash
ls .env.sample .env.example 2>/dev/null
```

If one exists, offer the simplest path:
> "The easiest way: copy `.env.sample` to `.env`, open it in your editor, and replace the placeholder with your bot token. Add `.env` to `.gitignore` if it isn't already."

Wait for them to do this, then re-check step 4a. If they prefer a more secure method, continue to 4c.

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

Use AskUserQuestion. Only offer options for tools that are detected. Always include `.env file` as a fallback.

| Tool | Condition | Label | Description |
|------|-----------|-------|-------------|
| `op` + `op environment` supported + `OP_SERVICE_ACCOUNT_TOKEN` set | all three | **1Password Environment** | Injected via `op run --environment`. Requires op CLI beta + service account. |
| `op` available | op only | **1Password vault** | Injected via `op run`. Works with desktop app or service account. |
| `mise` found | — | **mise** | Stored in `mise.local.toml` or `.mise.local.toml` (gitignored). |
| `secret-tool` found | — | **Linux keyring** | Stored in system keyring. |
| `security` found | — | **macOS Keychain** | Stored in macOS Keychain. |
| always | — | **.env file** | Written via silent terminal read; never echoed to chat. |

Guide the user through storing the token and exposing it as `TG_BOT_TOKEN`. The token must never appear in chat — use `read -rs` or equivalent silent-input patterns.

**1Password Environment special case**: wrap the MCP command with `op run --environment <environmentID>` (see step 6). Docs: https://www.1password.dev/environments/read-environment-variables#cli

**1Password vault special case**: offer a sub-choice between `op run` wrapper vs exporting via `op read` in their shell profile.

#### 4e. Verify the token is accessible

```bash
echo "token length: ${#TG_BOT_TOKEN}"
```

If 0, the env var isn't set — ask the user to open a fresh shell or source their profile.

### 5. Discover group chat ID (fresh install only — skip if migration)

#### 5a. Verify the token with getMe

```bash
curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe"
```

- `username` should match the configured bot username
- `can_read_all_group_messages: false` means privacy mode is on — the bot only sees @mentions. Tell the user to send `@<botusername> hello` in the group.

#### 5b. Query for updates

Ask the user to add the bot to their group and send a message (or `@<botusername> hello` if privacy mode is on). Then run:

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

- **One group**: use its ID, update `group_chat_ids` in `.claude/tg-bot-client/config.local.json`.
- **Multiple groups**: use AskUserQuestion to let the user pick. Then update `group_chat_ids`.
- **No groups**: check for webhook (`getWebhookInfo`) or another long-polling client consuming updates — rotating the token via BotFather fixes both. For forum supergroups (`has_topics_enabled: true`), fall back to asking the user to find the chat ID from the Telegram Web URL.

### 6. Update .mcp.json

Create or update `.mcp.json` in the project root. Remove any stale entries identified in step 1. Add the `tg-bot-client` entry.

**Note for the agent reading this skill — how paths work in `.mcp.json`:**

> Project-scoped `.mcp.json` files only expand **shell environment variables** (e.g. `${TG_BOT_TOKEN}`, `${HOME}`). The plugin-system variables `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PROJECT_DIR` are **not** shell env vars — they are only resolved inside plugin-provided configs, not in project `.mcp.json`.
>
> This means:
> - **`${TG_BOT_TOKEN}`** — write exactly as shown; Claude Code expands it from the shell environment at launch.
> - **`--directory` path** — the skill renderer has already expanded `CLAUDE_PLUGIN_ROOT` to the correct absolute path. The path shown in the templates below **is what you must write** into `.mcp.json`. Do not substitute the variable form DOLLAR{CLAUDE_PLUGIN_ROOT} — that will not be resolved in a project-scoped `.mcp.json` and the MCP will fail to start.
> - **`TG_CONFIG_DIR`** — similarly pre-expanded. Run `realpath .` in the project root to confirm the project path if needed, then write it as an absolute path.
>
> **After a plugin update** the plugin cache path changes (the version number in the path changes). You must re-run `/tg-local-client:configure` after each plugin update to refresh the hardcoded path in `.mcp.json`.
>
> **Recovery — if your MCP is not loading:** check whether `.mcp.json` contains the literal string `CLAUDE_PLUGIN_ROOT`. If it does, a previous configure run wrote the variable name instead of the resolved path. Re-run this skill to fix it.
> ```bash
> grep CLAUDE_PLUGIN_ROOT .mcp.json
> ```

Before writing, get the project directory:

```bash
realpath .
```

**Default entry** (all token storage methods except 1Password op run):

```
{
  "mcpServers": {
    "tg-bot-client": {
      "command": "uv",
      "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}/mcp-src", "tg-local-mcp"],
      "env": {
        "TG_BOT_TOKEN": "${TG_BOT_TOKEN}",
        "TG_CONFIG_DIR": "${CLAUDE_PROJECT_DIR}/.claude/tg-bot-client"
      }
    }
  }
}
```

**1Password Environment entry**:

```
{
  "mcpServers": {
    "tg-bot-client": {
      "command": "op",
      "args": ["run", "--environment", "<environmentID>", "--", "uv", "run", "--directory", "${CLAUDE_PLUGIN_ROOT}/mcp-src", "tg-local-mcp"],
      "env": {
        "OP_SERVICE_ACCOUNT_TOKEN": "${OP_SERVICE_ACCOUNT_TOKEN}",
        "TG_CONFIG_DIR": "${CLAUDE_PROJECT_DIR}/.claude/tg-bot-client"
      }
    }
  }
}
```

**1Password vault op run wrapper entry**:

```
{
  "mcpServers": {
    "tg-bot-client": {
      "command": "op",
      "args": ["run", "--", "uv", "run", "--directory", "${CLAUDE_PLUGIN_ROOT}/mcp-src", "tg-local-mcp"],
      "env": {
        "TG_BOT_TOKEN": "op://<vault>/<item>/<field>",
        "TG_CONFIG_DIR": "${CLAUDE_PROJECT_DIR}/.claude/tg-bot-client"
      }
    }
  }
}
```

> **Scope warning:** The plugin must be installed at **project scope** (`--scope project`). Check for and remove any user-scoped duplicate:
> ```bash
> claude mcp list
> ```
> If `tg-bot-client` appears in both scopes:
> ```bash
> claude mcp remove tg-bot-client -s user
> ```

### 7. Update .claude/settings.json — permissions and SessionStart hook

#### 7a. Permissions

Add `"mcp__tg-bot-client__*"` to `permissions.allow`. Remove any stale `"mcp__*-tg__*"` entries (e.g. `"mcp__chez-tg__*"`, `"mcp__poddy-tg__*"`).

#### 7b. SessionStart hook

Add a `SessionStart` hook that reminds the agent to start the Telegram monitor. If a hook already exists from a prior install (look for the `tg-local-client` marker in the command string), replace it with the updated version. Otherwise append to the existing `SessionStart` array.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '[tg-bot-client] Start your Telegram monitor now: call get_tail_command then pass the result directly to Monitor(persistent=True).'"
          }
        ]
      }
    ]
  }
}
```

### 8. Migration cleanup (migration mode only)

If migrating from a prior setup:

1. **Delete the old clone:**
   ```bash
   rm -rf .claude/tg-local-client/
   ```

2. **Update .gitignore:** replace `.claude/tg-local-client/` with `.claude/tg-bot-client/` if the old entry is present.

3. Tell the user what was removed.

### 9. Summary

Tell the user:
- MCP server name: `tg-bot-client`
- Config: `.claude/tg-bot-client/config.local.json`
- Token storage: describe the method chosen in step 4
- Whether this was a fresh install or migration (and what was cleaned up)
- Next step: restart Claude Code session to load the MCP
