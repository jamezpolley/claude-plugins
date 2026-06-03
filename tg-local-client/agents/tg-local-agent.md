---
name: tg-local-agent
description: Agent brief for projects using the tg-local-client Telegram MCP. Covers tools, session startup, and migration from the dex-fabric client.
---

# tg-local-client Agent Brief

Your project has a per-project Telegram bot configured via tg-local-client. This brief covers how to use it.

**Fleet comms conventions:** read `docs/fabric-comms-conventions.md` (in this plugin's base directory) before sending any messages. It is authoritative and owned by Dex (@tchlawbot) — do not paraphrase or summarise it here.

## Your MCP tools

Your bot slug determines the MCP server name: `<slug>-tg`. Tools are prefixed `mcp__<slug>-tg__*`. The 24 tools are:

**Sending:** `send_message`, `stream_message_draft` (send-or-edit; first call sends, subsequent calls with `message_id` edit in place), `send_photo`, `send_document`

**Typing indicators:** `start_typing` (self-refreshing every 5s, auto-stops on `send_message`), `stop_typing`, `send_typing` (one-shot 5s burst — use for short operations only)

**Reading & history:** `list_recent_messages`, `mark_read`, `list_known_chats`

**Reactions & edits:** `react_to_message`, `edit_message`, `delete_message`

**Forum topics:** `create_forum_topic`, `edit_forum_topic`, `close_forum_topic`, `reopen_forum_topic`, `delete_forum_topic`

**Media:** `download_media`

**Identity:** `trust_identity`, `untrust_identity`, `lookup_identity`, `list_trusted_identities`

**Monitor:** `get_tail_command` — returns a flat `uv run … tg-local-tail …` command for live monitoring

**Meta:** `client_version`

## Session startup (required on every session start)

1. **Pull latest tg-local-client code:**
   ```bash
   git -C .claude/tg-local-client pull
   ```
   This updates the code on disk so the next session restart picks up any new tools or fixes. The running MCP subprocess is already loaded — the update takes effect on the restart after this one.

2. **Start the monitor** — call `get_tail_command` then pass the returned command string **directly to the `Monitor` tool** (`persistent=True`). Do NOT use `Bash(run_in_background=True)`, `tail -f`, or shell pipes — Monitor is the correct tool.

   ```python
   result = get_tail_command(triage={"role": "<slug>", "state_file": "~/.local/share/tg-local/<slug>/waiting-on.md", "model": "claude-sonnet-4-6"})
   Monitor(command=result["command"], description="<slug>-tg inbound monitor", persistent=True)
   ```

   If a monitor from a previous session is still running (check with `ps aux | grep tg-local-tail`), kill the old process before starting a new one to avoid duplicate consumers on the same cursor file.

3. **Catch-up read:**
   ```
   list_recent_messages()
   ```
   Read what arrived while the monitor was down.

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

## Migrating from the dex-fabric client (mcp__dex-tg__)

If your project previously used the dex-fabric client, here are the key differences:

### Tool name changes

| Old (dex-tg) | New (slug-tg) | Notes |
|---|---|---|
| `mcp__dex-tg__send_message` | `mcp__<slug>-tg__send_message` | Same signature |
| `mcp__dex-tg__stream_message_draft` | `mcp__<slug>-tg__stream_message_draft` | Same signature |
| `mcp__dex-tg__get_tail_command` | `mcp__<slug>-tg__get_tail_command` | Same signature |
| `mcp__dex-tg__list_recent_messages` | `mcp__<slug>-tg__list_recent_messages` | Same signature |
| `mcp__dex-tg__list_bots` | *(removed)* | Single-bot client — no `list_bots` |
| any tool with `bot=` arg | same tool, no `bot=` arg | Single-bot: `bot=` is gone |

### Structural differences

- **No `bot=` argument** on any tool. The old fabric was multi-bot; this client handles exactly one bot. Remove any `bot=` kwarg from existing tool calls.
- **No `list_bots`**. If your agent called `list_bots` to discover its own identity, use `client_version` instead (returns the running bot's registered tools), or read `config.local.json` directly.
- **Channel file path changed.** Old fabric wrote to `~/.local/share/dex-tg/channels/<slug>.jsonl`. New client writes to `~/.local/share/tg-local/<slug>/channels/<slug>.jsonl`. Update any hardcoded paths.
- **MCP registration is project-local** (via `.mcp.json`), not global. The bootstrap no longer writes to `~/.claude.json`.
- **Trust identities** may need re-running. The trusted_identities DB is in `~/.local/share/tg-local/<slug>/messages.db` — it won't carry over from the old fabric automatically. Call `trust_identity` once per trusted human on the new client.

### AGENTS.md / agent brief updates

Replace any reference to:
- `mcp__dex-tg__*` → `mcp__<slug>-tg__*`
- `bot_slug` in the old fabric (e.g. `grocy410`) → same slug, new MCP prefix
- `~/.local/share/dex-tg/` data dir → `~/.local/share/tg-local/<slug>/`
- `bot=` args in tool calls → remove entirely
