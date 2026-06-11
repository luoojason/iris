# Wiki MCP tools

Date: 2026-06-09
Status: approved

## Problem

The owner keeps an Obsidian vault. The agent should be able to read it for
context and update it (project pages, logs) when asked — without a shell,
without the model ever naming filesystem paths.

## Design

One FastMCP server, `iris/mcp/wiki.py` (server name `iris-wiki`), rooted at
`IRIS_WIKI_DIR`. Unset means every tool answers "the wiki is not configured".

Pages are referred to by **vault-relative page names** like `Projects/Iris`
(the `.md` suffix is implied and enforced). Name validation is one shared
`_resolve(name)`:

- reject absolute paths, empty names, `.` / `..` segments, backslashes, and
  null bytes;
- append `.md` if missing; reject any other suffix;
- resolve and check containment: the resolved real path must stay inside the
  resolved `IRIS_WIKI_DIR` (symlinked pages cannot escape the vault).

Tools (allowlist as `mcp__wiki__*`):

- `wiki_list(prefix="")` — page names under an optional folder prefix,
  sorted, capped at 200 with a "… and N more" tail.
- `wiki_read(name)` — full page text, capped at 48 KB with a truncation
  marker.
- `wiki_search(query, limit=20)` — case-insensitive substring search over
  page text; returns `name: matching line` rows.
- `wiki_write(name, content)` — create or overwrite a page (atomic
  tempfile + replace; parent folders created). Returns bytes written.
- `wiki_append(name, text)` — append a block to an existing page (creates it
  if missing), separated by a blank line. This is the cheap, safe default
  for logs.

No delete tool. Deleting notes is an owner action in Obsidian, not an agent
capability.

## Invariants

- Every tool call funnels through `_resolve`; there is no second path-joining
  code path to drift.
- Output never contains an absolute path — names only.
- Writes are atomic, append uses a read-modify-write of the whole page (vault
  pages are small; simplicity wins over a partial-append protocol).
