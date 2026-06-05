# Iris

[![ci](https://github.com/luoojason/iris/actions/workflows/ci.yml/badge.svg)](https://github.com/luoojason/iris/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A personal chat agent that runs on your Claude subscription. Iris uses the
official `claude` command (Claude Code) as its brain in headless mode, so it
runs on your existing Pro or Max plan instead of a separate pay-per-token API
bill. It is a subscription-native alternative to the [Hermes
agent](https://github.com/NousResearch/hermes-agent).

As of June 15, 2026 this is a **supported path**: Anthropic's agent credit
explicitly covers "the `claude -p` command, and third-party apps built on the
Agent SDK." It is not unlimited and not strictly free; it draws from your plan's
monthly agent credit, then API rates. Read [COMPLIANCE.md](COMPLIANCE.md) for the
cost and the one rule that matters (single-user only). It is short and honest.

## Why

Most "run an agent on your Claude subscription" projects lift the OAuth token out
of `~/.claude` and replay it against the API while pretending to be Claude Code.
That is the thing Anthropic enforces against, and it broke for several tools in
early 2026. Iris takes the durable route: it shells out to the `claude` binary
you already installed and signed in, the way its `-p` headless mode is meant to
be used. No token extraction, no impersonation.

You give up Hermes's bespoke agent loop. You keep a real assistant: a persona,
memory across conversations, custom tools, and a Discord front end, all on Claude
Code's own extension points.

## How it works

```
Discord message ──> Iris ──> claude -p "<message>" --resume <session>
                                   --append-system-prompt-file persona.md
                                   --mcp-config tools.json --model sonnet
                              └─> reply text + new session id ──> Discord
```

- **Brain:** the official `claude` binary. No API key, no token extraction.
- **Memory across turns:** one `claude` session per conversation, resumed with
  `--resume`. Stored in a small JSON file.
- **Persona:** a system prompt file appended to Claude Code's own.
- **Tools:** MCP servers, the same mechanism Claude Code already uses. Iris ships
  an example memory tool; add any others.
- **Front end:** Discord and Telegram. The core is transport-agnostic, so adding
  another is a small adapter.

It is **event-driven on purpose.** Iris only calls the model when a message
arrives, so it burns no idle inference. That is what keeps it inside your monthly
agent credit rather than draining it in the background.

## Quickstart

You need [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and
signed in to your subscription (`claude` on your PATH), and you should claim your
monthly agent credit once in your Claude account.

```bash
git clone https://github.com/luoojason/iris
cd iris
pip install -e ".[discord]"

cp .env.example .env        # then edit it
python -m iris doctor       # checks claude is installed and signed in
python -m iris tui          # full-screen terminal UI (needs: pip install -e ".[tui]")
python -m iris chat         # or a plain REPL, no extra dependency
```

The `tui` is a full-screen terminal app with a scrolling conversation, a live
thinking indicator, and proper line editing. The `chat` REPL is the no-frills
version (now with history and a thinking spinner).

To run it on Discord, create a bot at the [Discord Developer
Portal](https://discord.com/developers/applications), enable the **Message
Content** intent, put the token and **your** user id in `.env`, then:

```bash
python -m iris               # or: python -m iris discord
```

For Telegram instead, install the extra (`pip install -e ".[telegram]"`), get a
token from [@BotFather](https://t.me/BotFather), set `IRIS_TELEGRAM_TOKEN`, and
run `python -m iris telegram`.

Keep `IRIS_ALLOWED_USER_IDS` set to yourself. Answering other people from a
personal subscription is against Anthropic's terms.

### Run it as a service

To keep Iris running after you log out, there is a systemd user unit at
`examples/iris.service`. Edit the paths, then:

```bash
cp examples/iris.service ~/.config/systemd/user/iris.service
systemctl --user daemon-reload
systemctl --user enable --now iris
loginctl enable-linger "$USER"
journalctl --user -u iris -f
```

### Skills

Claude Code skills are just `SKILL.md` folders, loaded on demand by description.
Anything in `~/.claude/skills/` is available to the agent. To keep your bot's
skills in their own directory instead, point `IRIS_SKILLS_DIR` at a folder of
skill folders and Iris symlinks them into the skills path at startup. Run
`python -m iris skills` to see what the agent can use. Hermes skills use the same
format, so they carry over by copying the folders across.

## Configuration

Everything is environment variables (see `.env.example`). The ones that matter:

| Variable | What it does |
| --- | --- |
| `IRIS_DISCORD_TOKEN` | Discord bot token. |
| `IRIS_ALLOWED_USER_IDS` | Comma-separated ids the bot will answer. Lock to yourself. |
| `IRIS_MODEL` | `claude-opus-4-...`, `claude-sonnet-4-...`, `claude-haiku-4-...`, or blank. Haiku stretches the credit furthest. |
| `IRIS_MODEL_LIGHT` | Optional lighter model for trivial turns (enables routing). Blank = every turn on `IRIS_MODEL`. |
| `IRIS_PERSONA_FILE` | System prompt file for the persona. |
| `IRIS_MCP_CONFIG` | MCP tool config (gives the agent tools). |
| `IRIS_PERMISSION_MODE` + `IRIS_ALLOWED_TOOLS` | Control which tools run unattended. |

## Tools via MCP

Out of the box Iris is a plain chat bot, which runs anywhere `claude` is
installed. Tools are opt-in, exposed the way Claude Code already understands them:
MCP servers launched through `--mcp-config`.

One rule trips people up: under the `default` permission mode, any tool that is
**not** in `IRIS_ALLOWED_TOOLS` is silently skipped, and the model may even claim
it acted. So whenever you point `IRIS_MCP_CONFIG` at a tool, allowlist that tool
too.

To turn on the bundled memory tool (`remember`, `recall`, `forget`):

1. `pip install -e ".[memory]"`
2. In `examples/mcp.example.json`, set `"command"` to the python from this
   environment (`which python`); `claude` launches the tool as a subprocess.
3. Set both, together:

   ```
   IRIS_MCP_CONFIG=examples/mcp.example.json
   IRIS_ALLOWED_TOOLS=mcp__memory__remember,mcp__memory__recall,mcp__memory__forget
   ```

4. Tell the persona to use it (the example persona notes where).

Iris also ships a scoped **Discord server-actions** tool
(`iris/mcp/discord_server.py`): `create_thread`, `fetch_messages`,
`list_channels`, `search_members`. It is a narrow, audited surface (no
arbitrary "send anywhere" tool) so the agent can do Discord chores the chat
adapter can't, without raw shell. A **history search** tool
(`iris/mcp/session_search.py`) lets it recall past conversations from the
transcripts Claude Code already keeps. Point Claude at any other MCP server
(filesystem, browser, web search, your own) the same way.

### Reminders

The `reminders` tool (`iris/mcp/reminders.py`: `schedule_reminder`,
`list_reminders`, `cancel_reminder`) writes jobs to a file; a periodic
`python -m iris reminders-tick` delivers the due ones over Discord REST. No model
call happens on the clock, so this keeps the agent's zero-idle-inference shape.
Run the tick every minute, e.g. with cron:

```
* * * * * cd /path/to/iris && IRIS_DISCORD_TOKEN=... /path/to/venv/bin/python -m iris reminders-tick
```

or a systemd timer. Allowlist the `mcp__reminders__*` tools to let the agent set them. The brain can read and edit files and run commands in directories
you grant it, so scope `IRIS_ALLOWED_TOOLS` deliberately and avoid
`bypassPermissions` unless you understand the blast radius.

### Voice messages

Iris can transcribe inbound voice notes locally and for free, so you can talk to
it on Discord or Telegram. Transcription happens **in the adapter**, before the
prompt reaches the brain: a voice attachment is downloaded, run through a local
[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) model, and folded
into the prompt as text. (This is the right seam for *inbound* speech: an MCP
tool the model calls could not intercept the attachment itself.)

```bash
pip install -e ".[voice]"
# then in .env:
IRIS_VOICE=true
IRIS_VOICE_MODEL=base   # tiny | base | small | medium — larger = slower, more accurate
```

It is **off by default** on purpose. The first voice message downloads the model
(tens of MB) and runs CPU inference, which can be slow on a small host, so turn
it on only where you have verified the box can keep up inside your turn timeout.
With voice off, an audio attachment degrades to a plain file reference. The model
is loaded lazily on the first voice message, so enabling it costs nothing until
someone actually sends audio, preserving Iris's zero-idle-inference shape.

Iris can also **reply** out loud. The bundled `speak` tool
(`iris/mcp/tts_server.py`) renders text to speech with a local engine and posts
the audio to Discord. Unlike inbound transcription, *output* speech is a natural
MCP tool: the model decides when a spoken reply fits and calls it. It uses the
first available engine: `IRIS_TTS_CMD` (a template reading text on stdin, writing
to `{out}`), then [piper](https://github.com/rhasspy/piper) (set `IRIS_TTS_VOICE`
to a voice model), then macOS `say`, then `espeak-ng`. Wire the tts server into
your mcp config and allowlist `mcp__tts__speak`. If no engine is installed the
tool just reports that and the bot replies in text.

### Model routing

Set `IRIS_MODEL_LIGHT` to send clearly-trivial turns ("thanks", "lol", a short
greeting) to a cheaper, faster model while everything substantive stays on
`IRIS_MODEL`. The decision is a pure heuristic with no extra model call, and it
is deliberately one-directional: it only ever *downgrades*, and only when a
message is short, has no attachment, no code fence, and none of the words that
signal real work (explain, debug, why, plan, compare, …). When in any doubt it
keeps the strong model, because answering a hard question with a weak model is
the costly mistake. Leave `IRIS_MODEL_LIGHT` blank to run every turn on one
model. The default model can switch per turn even mid-conversation; the resumed
session is just the transcript.

### Long conversations (auto-compaction)

Each conversation rides one `claude` session, resumed turn after turn. Left
alone, a months-old Discord channel would eventually grow past the model's
context window. Claude Code auto-compacts in its interactive UI, but that
behavior is undocumented for headless `-p --resume` and there is no programmatic
`/compact`, so Iris does not rely on it: it manages its own context budget.

The trigger is **token-accurate**, not a guess. Every `claude -p` turn reports
how many prompt tokens it carried (fresh plus cache), and when that reaches
`IRIS_COMPACT_AT_TOKENS` (default 150000, leaving headroom under a 200k window)
Iris compacts. Because it reads the real context size, a single tool-heavy turn
(a big web fetch, a large file read) trips it just as a long chat would.
`IRIS_COMPACT_EVERY` (default 60 turns) is a coarse backstop for the rare case
where usage numbers are missing. Either at `0` disables that trigger.

To compact, Iris asks the current session for a summary, then carries the summary
onto a **fresh** session and continues there. The summary runs while the old
session is still inside its limit, so the summarization itself never overflows,
and it happens in a background thread **after** your reply is sent, so it never
adds latency to a message. If a turn ever does hit a context-overflow error
anyway, Iris treats it like a dead session: it starts fresh and retries, so the
bot recovers instead of wedging (that path does drop history, which is why the
token trigger is set to fire well before it). Compaction trades a little deep
history for a conversation that runs indefinitely, the same trade Claude Code's
own auto-compact makes.

## What carries over from Hermes

Because Claude Code's skills and tools use open formats, most of a Hermes-style
agent maps cleanly onto the official client:

- **Carries over well:** persona, per-conversation memory, shell and file tools,
  web search and fetch, planning, subagent delegation, browser automation (via
  the Playwright MCP), Claude Code skills (the same `SKILL.md` format), and free
  local voice both ways (speech-to-text in, text-to-speech out; see Voice).
- **Re-add as MCP servers:** custom tools, platform admin actions, and history
  search (Iris ships scoped Discord-actions, history-search, and tts servers).
- **Does not carry over:** anything that needs a paid third-party API key (image
  and video generation, paid search and voice backends), multi-model
  mixture-of-agents reasoning, and Hermes's single-turn tool-chain collapse.
  These are out of scope for a free, single-brain, single-subscription agent.

## Status

Early, and honest about what is proven. The core (the driver, sessions, the agent
loop, and the bundled memory tool) is verified end to end against the real
`claude` binary and covered by unit tests. The Discord and Telegram message loops
are wired and unit-tested in isolation, but have **not** yet been exercised
against a live bot connection. Start with `python -m iris chat` to try the brain,
then wire up a transport.

Roadmap: live-testing the wired transports end to end, and a documented
feature-survival map against Hermes.

## Related work

Iris stands on a small body of prior art for making the official Claude client
the brain of an external agent: Anthropic's own Agent-SDK-on-your-plan support,
OpenClaw's `mcp serve` channel bridge, and the ClaudeClaw supervisor pattern.
Iris's contribution is a small, standalone, honestly-documented take aimed at one
person running it on their own plan.

## Credit

Iris is inspired by [Hermes](https://github.com/NousResearch/hermes-agent) by
Nous Research (MIT licensed). It shares none of Hermes's code; it is a clean
reimplementation aimed at the subscription-via-official-client approach. The name
and branding are independent and imply no association with or endorsement by Nous
Research or Anthropic.

## License

MIT. See [LICENSE](LICENSE).
