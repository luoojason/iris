"""MCP server: read and update the owner's wiki (an Obsidian-style vault).

Pages are referred to by vault-relative names like ``Projects/Iris`` (the
``.md`` suffix is implied). The model never sees or names filesystem paths:
every tool funnels through one ``_resolve`` that validates the name and checks
the resolved real path stays inside ``IRIS_WIKI_DIR``. No delete tool —
removing notes is an owner action in Obsidian, not an agent capability.
See docs/superpowers/specs/2026-06-09-wiki-tools-design.md.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.config import Config

mcp = FastMCP("iris-wiki")

READ_CAP = 48 * 1024
LIST_CAP = 200
NOT_CONFIGURED = "The wiki is not configured (the owner can set IRIS_WIKI_DIR)."

# Lazy config: spawned by the claude child with IRIS_* stripped, so knobs come
# from .env in the working directory; loading lazily keeps imports side-effect
# free for tests.
_CONFIG: Optional[Config] = None


def _config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


def _root() -> Optional[Path]:
    wiki_dir = _config().wiki_dir
    return Path(wiki_dir).resolve() if wiki_dir else None


class _BadName(ValueError):
    pass


def _resolve(root: Path, name: str) -> Path:
    """Page name -> real path inside the vault. The single validation funnel."""
    name = (name or "").strip()
    if not name or "\\" in name or "\0" in name or os.path.isabs(name):
        raise _BadName(f"bad page name {name!r}: use vault-relative names like Projects/Iris")
    parts = Path(name).parts
    if any(part in (".", "..") for part in parts):
        raise _BadName(f"bad page name {name!r}: no . or .. segments")
    suffix = Path(name).suffix
    if suffix == "":
        name += ".md"
    elif suffix != ".md":
        raise _BadName(f"bad page name {name!r}: pages are .md (the suffix is implied)")
    candidate = (root / name).resolve()
    if root != candidate and root not in candidate.parents:
        raise _BadName(f"bad page name {name!r}: it resolves outside the vault")
    return candidate


def _page_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root)
    return str(rel)[: -len(".md")] if str(rel).endswith(".md") else str(rel)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


@mcp.tool()
def wiki_list(prefix: str = "") -> str:
    """List wiki page names, optionally under a folder prefix like 'Projects/'."""
    root = _root()
    if root is None:
        return NOT_CONFIGURED
    names = sorted(
        _page_name(root, p)
        for p in root.rglob("*.md")
        if p.is_file() and (root in p.resolve().parents or p.resolve() == root)
    )
    if prefix:
        names = [n for n in names if n.startswith(prefix)]
    if not names:
        return "The wiki is empty." if not prefix else f"No pages under {prefix!r}."
    shown = names[:LIST_CAP]
    tail = f"\n… and {len(names) - LIST_CAP} more" if len(names) > LIST_CAP else ""
    return "\n".join(shown) + tail


@mcp.tool()
def wiki_read(name: str) -> str:
    """Read one wiki page by name (e.g. Projects/Iris)."""
    root = _root()
    if root is None:
        return NOT_CONFIGURED
    try:
        path = _resolve(root, name)
    except _BadName as exc:
        return str(exc)
    if not path.is_file():
        return f"No page named {name!r}. Use wiki_list to see what exists."
    text = path.read_text("utf-8")
    if len(text) > READ_CAP:
        return text[:READ_CAP] + "\n…[truncated]"
    return text


@mcp.tool()
def wiki_search(query: str, limit: int = 20) -> str:
    """Find wiki pages whose text contains the query (case-insensitive)."""
    root = _root()
    if root is None:
        return NOT_CONFIGURED
    needle = (query or "").strip().lower()
    if not needle:
        return "Give me something to search for."
    rows: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            continue  # a symlink pointing out of the vault is not searchable
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if needle in line.lower():
                rows.append(f"{_page_name(root, path)}: {line.strip()[:200]}")
                if len(rows) >= max(1, int(limit)):
                    return "\n".join(rows)
                break  # one row per page; wiki_read shows the rest
    return "\n".join(rows) if rows else f"No pages match {query!r}."


@mcp.tool()
def wiki_write(name: str, content: str) -> str:
    """Create or overwrite a wiki page. Prefer wiki_append for logs."""
    root = _root()
    if root is None:
        return NOT_CONFIGURED
    try:
        path = _resolve(root, name)
    except _BadName as exc:
        return str(exc)
    _atomic_write(path, content or "")
    return f"Wrote {len((content or '').encode('utf-8'))} bytes to {name}."


@mcp.tool()
def wiki_append(name: str, text: str) -> str:
    """Append a block to a wiki page (created if missing), after a blank line."""
    root = _root()
    if root is None:
        return NOT_CONFIGURED
    try:
        path = _resolve(root, name)
    except _BadName as exc:
        return str(exc)
    existing = ""
    if path.is_file():
        existing = path.read_text("utf-8")
    block = (text or "").strip("\n")
    merged = (existing.rstrip("\n") + "\n\n" + block + "\n") if existing.strip() else block + "\n"
    _atomic_write(path, merged)
    return f"Appended to {name}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
