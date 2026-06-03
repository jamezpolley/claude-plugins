---
name: tg-local-agent
description: Agent brief for projects using the tg-local-client Telegram MCP. Covers tools, session startup, and migration from the dex-fabric client.
---

# tg-local-client Agent Brief

Your project has a per-project Telegram bot configured via tg-local-client. This brief covers how to use it.

**Fleet comms conventions:** read `docs/fabric-comms-conventions.md` (in this plugin's base directory) before sending any messages. It is authoritative and owned by Dex (@tchlawbot) — do not paraphrase or summarise it here.

## Your MCP tools

The MCP server name is `tg-bot-client`. Tools are prefixed `mcp__tg-bot-client__*`. The 25 tools are:

**Sending:** `send_message`, `stream_message_draft` (send-or-edit; first call sends, subsequent calls with `message_id` edit in place), `send_photo`, `send_document`

**Typing indicators:** `start_typing` (self-refreshing every 5s, auto-stops on `send_message`), `stop_typing`, `send_typing` (one-shot 5s burst — use for short operations only)

**Reading & history:** `list_recent_messages`, `mark_read`, `list_known_chats`

**Reactions & edits:** `react_to_message`, `edit_message`, `delete_message`

**Forum topics:** `create_forum_topic`, `edit_forum_topic`, `close_forum_topic`, `reopen_forum_topic`, `delete_forum_topic`

**Media:** `download_media`

**Identity:** `trust_identity`, `untrust_identity`, `lookup_identity`, `list_trusted_identities`

**Bot info:** `get_me` — returns bot's Telegram profile. Key field: `can_read_all_group_messages` (false = privacy mode on; bot only sees @mentions). Call as `mcp__tg-bot-client__get_me()`.

**Monitor:** `get_tail_command` — returns a flat `uv run … tg-local-tail …` command for live monitoring

**Meta:** `client_version`

## Session startup (required on every session start)

1. **Start the monitor** — call `get_tail_command` then pass the returned command string **directly to the `Monitor` tool** (`persistent=True`). Do NOT use `Bash(run_in_background=True)`, `tail -f`, or shell pipes — Monitor is the correct tool.

   ```python
   result = get_tail_command(triage={"role": "<your-role>", "state_file": "~/.local/share/tg-local/<slug>/waiting-on.md", "model": "claude-sonnet-4-6"})
   Monitor(command=result["command"], description="tg-bot-client inbound monitor", persistent=True)
   ```

   If a monitor from a previous session is still running (check with `ps aux | grep tg-local-tail`), kill the old process before starting a new one to avoid duplicate consumers on the same cursor file.

2. **Catch-up read:**
   ```
   list_recent_messages()
   ```
   Read what arrived while the monitor was down.

3. **Optional sanity checks** (recommended on first boot or after a plugin upgrade):
   - `get_me` — confirm bot identity and whether privacy mode is on (`can_read_all_group_messages: false` means the bot only sees @mentions)
   - `list_known_chats` — confirm which groups the bot is in and their recent activity
   - `client_version` — confirm the running code version matches expectations
   - Check your copy of the fleet comms guide: `docs/fabric-comms-conventions.md` — current version is `2026-05-31.3`. If Dex has re-shared a newer version in the coord group, re-download it.

## Key behaviours

### Acknowledge on Telegram FIRST — before any other work

**When a message arrives that requires a response, your very first action must be to acknowledge it on Telegram.** Do not start researching, running commands, or thinking — send the acknowledgement first, then work.

- **DMs:** call `stream_message_draft` immediately with `💭 thinking…`. Subsequent calls with the returned `message_id` edit it in place as you work. Finalise with `send_message`.
- **Groups:** call `send_message` with `💭 thinking…` and a `reply_to_message_id` pointing at the inbound message. Keep the returned `telegram_msg_id`. Call `edit_message` at the end of your turn with the final answer.

This is non-negotiable. A Telegram message that goes unacknowledged looks like you're offline or ignoring the sender.

### Bot-to-bot communication

Telegram supports direct bot-to-bot messaging (May 2026). Key facts:

- **In shared groups**: all member bots receive all group messages via standard getUpdates — no special setup needed. This is how fleet coordination works in practice.
- **Direct bot-to-bot DMs**: both bots must have "Bot-to-Bot Communication Mode" enabled via BotFather. Use `send_message` with the recipient's `@username` as `chat_id`.
- **Safeguards required**: if building automated bot-to-bot workflows, deduplicate messages, apply rate limits, and enforce max interaction depth to prevent infinite loops.

See https://core.telegram.org/bots/features#bot-to-bot-communication for the full spec.

### Other behaviours

- `send_message` defaults to `group_chat_ids[0]` — you normally don't pass `chat_id`
- **Reactions reach humans but NOT other bots.** Use text replies for anything another agent must see.
- `start_typing` (self-refreshing) for operations longer than ~5s; `send_typing` for short one-shot bursts.
- `close_forum_topic` / `reopen_forum_topic` only work in supergroups. In DM chats, use `delete_forum_topic` to retire a topic.
- Always pass `parse_mode="HTML"` when using HTML tags in `send_message` or `edit_message`.

## Troubleshooting — MCP not loading

**Symptom:** `mcp__tg-bot-client__*` tools are not available after session start.

**Most likely cause:** `.mcp.json` contains the literal string `${CLAUDE_PLUGIN_ROOT}` instead of the resolved absolute path. Project-scoped `.mcp.json` files do not resolve plugin system variables — only shell environment variables (like `${TG_BOT_TOKEN}`) are expanded there.

**Check:**
```bash
grep CLAUDE_PLUGIN_ROOT .mcp.json
```

**Fix:** re-run `/tg-local-client:configure`. It will detect the existing config and overwrite `.mcp.json` with the correct hardcoded path for the installed plugin version.

**After a plugin update:** the plugin cache path changes with each version. Re-run `/tg-local-client:configure` after every plugin update to refresh the path.

## Migrating from a prior tg-local-client setup

If your project previously used an older version of this plugin (MCP named `<slug>-tg`, config at `.claude/tg-local-client/`), run `/tg-local-client:configure` — it will detect the old setup and migrate automatically.

If your project previously used the dex-fabric client (`mcp__dex-tg__*`), see the tool name changes below.

### Tool name changes from dex-tg

| Old (dex-tg) | New (tg-bot-client) | Notes |
|---|---|---|
| `mcp__dex-tg__send_message` | `mcp__tg-bot-client__send_message` | Same signature |
| `mcp__dex-tg__stream_message_draft` | `mcp__tg-bot-client__stream_message_draft` | Same signature |
| `mcp__dex-tg__get_tail_command` | `mcp__tg-bot-client__get_tail_command` | Same signature |
| `mcp__dex-tg__list_recent_messages` | `mcp__tg-bot-client__list_recent_messages` | Same signature |
| `mcp__dex-tg__list_bots` | *(removed)* | Single-bot client — no `list_bots` |
| any tool with `bot=` arg | same tool, no `bot=` arg | Single-bot: `bot=` is gone |

### Structural differences from dex-tg

- **No `bot=` argument** on any tool. Remove any `bot=` kwarg from existing tool calls.
- **No `list_bots`**. Use `client_version` to confirm the running bot, or read `.claude/tg-bot-client/config.local.json` directly.
- **Channel file path**: `~/.local/share/tg-local/<slug>/channels/<slug>.jsonl`
- **MCP registration** is project-local via `.mcp.json` — not global.
- **Trust identities** may need re-running on a new client. Call `trust_identity` once per trusted human.
