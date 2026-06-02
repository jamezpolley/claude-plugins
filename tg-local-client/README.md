# tg-local-client

Per-project Telegram bot client for agent communication. Each project gets its own bot, credentials, and MCP server — no sharing between projects.

## Installation

Install **per-project** (not globally) so each project has its own isolated bot configuration:

```
/plugin marketplace add jamezpolley/claude-plugins
/plugin install tg-local-client@jamezpolley --scope project
/reload-plugins
```

Then run the configure skill to bootstrap the bot for this project:

```
/tg-local-client:configure
```

## What you get

- `configure` skill — clones the client, writes `config.local.json`, merges the bot token into `.env`, registers the MCP server in `.mcp.json`, adds permissions to `.claude/settings.json`
- `tg-local-agent` brief — loaded automatically; covers all 24 MCP tools, session startup pattern, and migration notes from the dex-fabric client

## Per-project, not global

Installing at user scope (without `--scope project`) will work but is not recommended — it makes the agent brief active in every project, and the configure skill will be available everywhere. Install per-project so the plugin is only active where you have a bot configured.
