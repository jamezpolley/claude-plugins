# dex-fabric-grocy

Persistent Grocy inventory bot for the Dex fabric. Provides the `grocy-bot` agent type — a Telegram-gated teammate that arms its own monitor, loops on inbound from James, and applies Grocy inventory/shopping updates.

## What it does

- Watches the `grocy410` (@Grocy410_bot) Telegram channel
- Applies stock consumption, receipt processing, and shopping list updates via Grocy MCP
- Replies with confirmations in the same Telegram thread

## Agents

| Agent | Description |
|---|---|
| `grocy-bot` | Persistent Telegram-loop Grocy responder |

## Prerequisites

- `mcp__grocy__*` tools configured (Grocy MCP)
- `mcp__dex-tg__*` tools configured (dex-tg MCP with grocy410 bot access)
- grocy410 bot token registered in dex-tg

## Usage

Spawn at session start:

```
Agent(subagent_type="grocy-bot", run_in_background=true)
```

No brief needed — the agent self-configures from its definition.

## Bundled references

- `references/fabric-comms-conventions.md` — versioned Telegram comms conventions (owned by Dex/@tchlawbot; update by writing a new version to this path)

## Install

```
/plugin marketplace add jamezpolley/dex-fabric-grocy
```
