# Contributing

Iris is small on purpose. Contributions that keep it small, honest, and easy to
read are very welcome.

## Setup

```bash
pip install -e ".[dev]"   # core + pytest
python -m pytest -q
```

The core (`driver`, `sessions`, `config`) has no third-party dependencies and is
fully unit-tested without a real `claude`. Tests inject a fake runner. Please add
or update tests with any change to that core.

## Ground rules

- Keep the core dependency-free. New runtime dependencies belong behind an
  optional extra (like `discord` and `memory` in `pyproject.toml`).
- Drive the official `claude` binary. Do not add anything that reads, copies, or
  replays a subscription OAuth token, or that disguises requests as another
  client. That is the line this project exists to stay on the right side of.
- Keep `COMPLIANCE.md` accurate. If Anthropic's terms or billing change, update
  it rather than leaving a stale claim.
- Match the surrounding style: plain, direct, no decorative noise.

## Good first contributions

- A new transport adapter (Telegram, Slack) alongside the Discord one.
- Small MCP tool servers under `iris/mcp/` (web search, files, voice).
- A skills loader and docs for bringing Claude Code skills to the agent.
