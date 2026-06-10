"""MCP server: a read-only window into the owner's wiki vault.

The owner keeps an Obsidian vault (a folder of markdown, mirrored to the
bot's box as a git clone) as the source of truth for project status. These
tools let the agent answer "where does X stand" from the vault instead of
reciting stale memory. READ-ONLY by design: the server never writes, and
every tool returns a friendly string, never raises. Config key ``wiki``;
allowlist ``mcp__wiki__search_wiki``, ``mcp__wiki__read_wiki_page``, and
``mcp__wiki__recent_wiki_changes``. The env block needs ``IRIS_WIKI_DIR``
(servers do not inherit the bot's ``IRIS_*`` vars).

The vault may sit next to secrets, so every page access is fenced: requested
paths are resolved and must land inside ``IRIS_WIKI_DIR`` (no ``..`` hops, no
absolute paths, no symlinks that point out), and only ``.md`` files are
served. Test seams: ``WIKI_DIR`` (monkeypatched to a tmp_path vault) and
``_now`` (the module's one clock).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterator, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "The wiki tool needs the MCP SDK. Install it with:\n"
        "    pip install mcp\n"
        "or install Iris with the memory extra: pip install 'iris-agent[memory]'"
    ) from exc

WIKI_DIR = Path(os.environ.get("IRIS_WIKI_DIR", ""))

mcp = FastMCP("iris-wiki")

# Search skips pages larger than this; a wiki page that big is not prose.
MAX_FILE_BYTES = 2 * 1024 * 1024
SNIPPET_CHARS = 160


def _now() -> float:
    """The module's single clock; tests monkeypatch this."""
    return time.time()


def _fmt_age(seconds: float) -> str:
    """Render an age in the jobs-listing style, plus a day unit for old pages."""
    seconds = max(0, int(seconds))
    if seconds >= 86_400:
        return f"{seconds // 86_400}d"
    if seconds >= 3_600:
        return f"{seconds // 3_600}h"
    return f"{seconds // 60}m"


def _vault() -> tuple[Optional[Path], str]:
    """The resolved vault root, or (None, friendly setup message)."""
    raw = str(WIKI_DIR)
    if raw in ("", "."):  # Path("") renders as "."
        return None, ("No wiki directory is configured; set IRIS_WIKI_DIR in "
                      "the wiki server's env block to the vault checkout.")
    root = WIKI_DIR.expanduser()
    if not root.is_dir():
        return None, (f"Wiki directory {raw!r} does not exist; check "
                      "IRIS_WIKI_DIR in the wiki server's env block.")
    return root.resolve(), ""


def _pages(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (relative path, file) for every real .md page inside the vault.

    Hidden folders (.git, .obsidian, .trash) are skipped, and so is anything
    whose resolved location falls outside the vault, so a symlink planted in
    the vault can never pull neighboring secrets into a listing or search.
    """
    for page in sorted(root.rglob("*.md")):
        try:
            if not page.is_file():
                continue
            rel = page.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if not page.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        yield rel.as_posix(), page


def _hit(rel: str, line: str) -> str:
    """One search-result line: 'path: best matching line'."""
    line = line.strip()
    if len(line) > SNIPPET_CHARS:
        line = line[:SNIPPET_CHARS] + "..."
    return f"{rel}: {line}" if line else rel


@mcp.tool()
def search_wiki(query: str, max_results: int = 8) -> str:
    """Search the owner's wiki vault for pages matching a query.

    The wiki is the owner's source of truth for project status: answer
    "where does X stand" questions from it instead of from memory. Matching
    is case-insensitive; pages whose path matches rank above content
    matches, and each hit shows its best matching line.

    Args:
        query: Words to look for in page paths and content; a page matches
            when every word appears.
        max_results: Maximum pages to list (clamped between 1 and 25).
    """
    try:
        root, msg = _vault()
        if root is None:
            return msg
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return "Search needs a query."
        limit = max(1, min(25, int(max_results)))
        name_hits: list[str] = []
        content_hits: list[str] = []
        for rel, page in _pages(root):
            try:
                if page.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = page.read_text("utf-8", errors="replace")
            except OSError:
                continue
            in_name = all(t in rel.lower() for t in terms)
            in_text = all(t in text.lower() for t in terms)
            if not in_name and not in_text:
                continue
            best_line, best_score = "", 0
            for line in text.splitlines():
                score = sum(1 for t in terms if t in line.lower())
                if score > best_score:
                    best_line, best_score = line, score
            (name_hits if in_name else content_hits).append(_hit(rel, best_line))
        hits = name_hits + content_hits
        if not hits:
            return f"No wiki pages match {query!r}."
        return "\n".join(hits[:limit])
    except Exception as exc:  # the tool feeds the model: friendly, never raises
        return f"wiki search failed: {exc}"


@mcp.tool()
def read_wiki_page(path: str, max_chars: int = 8000) -> str:
    """Read one wiki page by its vault-relative path.

    Use search_wiki or recent_wiki_changes to find the path first. Only
    markdown pages inside the vault can be read; the tool refuses anything
    that resolves outside it.

    Args:
        path: Page path relative to the vault root, e.g. 'Projects/Iris.md'.
        max_chars: Truncate the page past this many characters
            (clamped between 200 and 50000).
    """
    try:
        root, msg = _vault()
        if root is None:
            return msg
        rel = (path or "").strip()
        if not rel:
            return "read_wiki_page needs a path, e.g. 'Projects/Iris.md'."
        if Path(rel).is_absolute():
            return ("Absolute paths are not allowed; pass a path relative "
                    "to the vault root, e.g. 'Projects/Iris.md'.")
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return f"{rel!r} resolves outside the wiki; nothing read."
        if target.suffix.lower() != ".md":
            return "Only .md wiki pages can be read."
        if not target.is_file():
            return f"No wiki page at {rel!r}; try search_wiki to find the path."
        text = target.read_text("utf-8", errors="replace")
        limit = max(200, min(50_000, int(max_chars)))
        if len(text) > limit:
            return text[:limit] + f"\n... (truncated; {len(text)} chars total)"
        return text
    except Exception as exc:  # the tool feeds the model: friendly, never raises
        return f"wiki read failed: {exc}"


@mcp.tool()
def recent_wiki_changes(limit: int = 10) -> str:
    """List the most recently modified wiki pages, newest first, with ages.

    A cheap way to see what the owner has been working on lately, or to spot
    the page that just changed. Based on file modification times.

    Args:
        limit: Maximum pages to list (clamped between 1 and 50).
    """
    try:
        root, msg = _vault()
        if root is None:
            return msg
        count = max(1, min(50, int(limit)))
        stamped: list[tuple[float, str]] = []
        for rel, page in _pages(root):
            try:
                stamped.append((page.stat().st_mtime, rel))
            except OSError:
                continue
        if not stamped:
            return "The wiki has no pages yet."
        stamped.sort(key=lambda item: (-item[0], item[1]))
        now = _now()
        return "\n".join(f"{rel} - {_fmt_age(now - mtime)} ago"
                         for mtime, rel in stamped[:count])
    except Exception as exc:  # the tool feeds the model: friendly, never raises
        return f"wiki changes failed: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
