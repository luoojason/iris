"""MCP server: let the agent answer "how much credit have I burned?".

One read-only tool over the usage ledger. No shell, no paths, no writes.
See docs/superpowers/specs/2026-06-08-credit-guard-design.md.
"""

from __future__ import annotations

from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.config import Config
from iris.usage import summary_text

mcp = FastMCP("iris-usage")

# Lazy config: spawned by the claude child with IRIS_* stripped, so knobs come
# from .env in the working directory; loading lazily keeps imports side-effect
# free for tests.
_CONFIG: Optional[Config] = None


def _config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


@mcp.tool()
def usage_report() -> str:
    """This month's credit draw: turns, spend estimate, budget level.

    Use it when the owner asks about usage, cost, or the monthly budget.
    """
    return summary_text(_config())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
