# Iris

A personal chat agent that runs on your Claude subscription, not on paid API
credits. Iris uses the official `claude` command (Claude Code) as its brain in
headless mode, so the only thing it costs is the Pro or Max plan you already
pay for.

It is a subscription-native alternative to the [Hermes
agent](https://github.com/NousResearch/hermes-agent), built the honest way: it
drives Anthropic's real client instead of extracting and spoofing your
subscription token. See [COMPLIANCE.md](COMPLIANCE.md) for exactly what that
means and where the limits are.

## Why

Most "run an agent on your Claude subscription" setups work by lifting the OAuth
token out of `~/.claude` and replaying it against the API while pretending to be
Claude Code. That is the thing Anthropic actually enforces against. Iris takes
the boring, durable route: it shells out to the `claude` binary you already
installed and signed in, the way its `-p` headless mode is meant to be used.

You give up Hermes's bespoke agent loop. You keep a real assistant: a persona,
memory across conversations, custom tools, and a Discord front end, all on
Claude Code's own extension points.

## How it works

```
Discord message ──> Iris ──> claude -p "<message>" --resume <session>
                                   --append-system-prompt-file persona.md
                                   --mcp-config tools.json --model sonnet
                              └─> reply text + new session id ──> Discord
```

- **Brain:** the official `claude` binary. No API key, no token extraction, no
  impersonation.
- **Memory across turns:** one `claude` session per conversation, resumed with
  `--resume`. Stored in a small JSON file.
- **Persona:** a system prompt file appended to Claude Code's own.
- **Tools:** MCP servers, the same mechanism Claude Code already uses. Iris ships
  an example memory tool; add your own.
- **Front end:** Discord today. The core is transport-agnostic.

## Quickstart

You need [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and
signed in to your subscription (`claude` on your PATH).

```bash
git clone https://github.com/luoojason/iris
cd iris
pip install -e ".[discord,memory]"

cp .env.example .env        # then edit it
python -m iris doctor       # checks claude is installed and signed in
python -m iris chat         # talk to it in your terminal, no Discord needed
```

To run it on Discord, create a bot at the [Discord Developer
Portal](https://discord.com/developers/applications), enable the **Message
Content** intent, put the token and **your** user id in `.env`, then:

```bash
python -m iris               # or: python -m iris discord
```

`IRIS_ALLOWED_USER_IDS` locks the bot to you. Keep it set: answering other
people from a personal subscription is against Anthropic's terms.

## Configuration

Everything is environment variables (see `.env.example`). The ones that matter:

| Variable | What it does |
| --- | --- |
| `IRIS_DISCORD_TOKEN` | Discord bot token. |
| `IRIS_ALLOWED_USER_IDS` | Comma-separated ids the bot will answer. Lock to yourself. |
| `IRIS_MODEL` | `claude-opus-4-...`, `claude-sonnet-4-...`, or blank for the default. |
| `IRIS_PERSONA_FILE` | System prompt file for the persona. |
| `IRIS_MCP_CONFIG` | MCP tool config (gives the agent tools). |
| `IRIS_PERMISSION_MODE` + `IRIS_ALLOWED_TOOLS` | Control which tools run unattended. |

## Tools via MCP

Iris keeps Hermes-style capabilities by exposing tools the way Claude Code
understands them: MCP servers launched through `--mcp-config`. The included
example is a memory tool (`iris/mcp/memory_server.py`) with `remember`, `recall`,
and `forget`. Point Claude at any MCP server (filesystem, web search, your own)
the same way.

For an unattended bot, scope tool use deliberately. Prefer an explicit
`IRIS_ALLOWED_TOOLS` list over `IRIS_PERMISSION_MODE=bypassPermissions`, since the
brain can read and edit files and run commands in the directories you grant it.

## Status

Early. The core (driver, sessions, persona, MCP tools, Discord, terminal chat)
works and is unit-tested. Roadmap: more transports (Telegram), richer memory,
and a feature-parity map against Hermes.

## Credit

Iris is inspired by [Hermes](https://github.com/NousResearch/hermes-agent) by
Nous Research (MIT licensed). It shares none of Hermes's code; it is a clean
reimplementation aimed at the subscription-via-official-client approach. Thanks
to that project for showing what a full personal agent can be.

## License

MIT. See [LICENSE](LICENSE).
