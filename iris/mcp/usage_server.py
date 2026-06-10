"""MCP server: a read-only spend summary over the metrics JSONL.

READ-ONLY by design: one tool, ``usage_summary``, rendering the same text as
``iris usage`` through the shared budget functions. Pure file arithmetic, no
model calls, and the tool returns a friendly string, never raises. Config key
``usage``; allowlist ``mcp__usage__usage_summary``. The env block needs
``IRIS_METRICS_FILE`` and optionally ``IRIS_MONTHLY_CREDIT`` (servers do not
inherit the bot's ``IRIS_*`` vars). Test seams: ``METRICS_FILE`` /
``MONTHLY_CREDIT`` (monkeypatched) and ``_now`` (the module's one clock).
"""

from __future__ import annotations

import os
import time

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "The usage tool needs the MCP SDK. Install it with:\n"
        "    pip install mcp\n"
        "or install Iris with the memory extra: pip install 'iris-agent[memory]'"
    ) from exc

from iris import budget

METRICS_FILE = os.environ.get("IRIS_METRICS_FILE", "")


def _env_credit() -> float:
    try:
        return float(os.environ.get("IRIS_MONTHLY_CREDIT") or 0)
    except ValueError:
        return 0.0


MONTHLY_CREDIT = _env_credit()

mcp = FastMCP("iris-usage")

_PERIODS = ("day", "week", "month")


def _now() -> float:
    """The module's single clock; tests monkeypatch this."""
    return time.time()


@mcp.tool()
def usage_summary(period: str = "month") -> str:
    """Spend summary from the per-turn metrics file: totals, cost by model and
    transport (job spend separated), error rate, context p95, and the top
    conversations by cost. Pure file arithmetic, no model call.

    Args:
        period: day, week, or month (calendar boundaries, local time).
            The month view also shows credit percent used and a linear
            month-end projection when a monthly credit is configured.
    """
    try:
        if not METRICS_FILE:
            return ("No metrics file is configured; set IRIS_METRICS_FILE in "
                    "the usage server's env block.")
        wanted = (period or "month").strip().lower()
        if wanted not in _PERIODS:
            return f"Unknown period {period!r}; valid: {', '.join(_PERIODS)}."
        now = _now()
        records = budget.read_metrics(METRICS_FILE, budget.window(now, wanted))
        summary = budget.summarize(records)
        credit = MONTHLY_CREDIT if wanted == "month" else 0.0
        proj = budget.projection(records, now) if credit > 0 else None
        return budget.format_summary(summary, credit=credit, projection=proj)
    except Exception as exc:  # the tool feeds the model: friendly, never raises
        return f"usage summary failed: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
