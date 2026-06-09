# Iris change-watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a general poll-and-diff change-watcher: register watches from the CLI, and a cron-driven `iris watch-tick` notices when a fetched value changes and pings you on Discord through the existing notify spine.

**Architecture:** New units under `iris/notify/` beside the spine. `watches.py` is a file-backed store of watch definitions and their last-seen values. `fetch.py` gets a value (HTTP GET or shell command) and extracts the part you care about (text, json field, regex, status). `watch_tick.py` is the engine plus CLI handlers: for each due watch it fetches, diffs against the stored value, and on a real change emits a `watch` Event through the existing gate, compose, and deliver. The gate and compose get one small branch each for watch events. No model call in fetch or diff; a change renders a free template, preserving zero-idle-inference.

**Tech Stack:** Python 3.10+, stdlib `urllib`/`subprocess`/`json`/`re`/`tempfile`, the existing `iris/notify/` spine (`events`, `gate`, `compose`, `deliver`), `pytest` with injected fakes (no real network, subprocess, or Discord in tests).

Spec: `docs/superpowers/specs/2026-06-09-iris-change-watcher-design.md`

---

## File structure

- Create `iris/notify/watches.py` - `new_watch` factory and the file-backed `WatchStore`.
- Create `iris/notify/fetch.py` - `fetch` (HTTP GET / shell command, injectable) and `extract` (text/json/regex/status), both total (errors become a sentinel value).
- Modify `iris/notify/gate.py` - notify on a `watch` source event.
- Modify `iris/notify/compose.py` - a watch-change template branch plus a `_short` truncator.
- Create `iris/notify/watch_tick.py` - `tick` engine, `make_watch_from_flags`, and the `watch-add`/`watch-list`/`watch-rm`/`watch-tick` CLI handlers.
- Modify `iris/cli.py` - wire the four subcommands.
- Modify `.env.example` - document `IRIS_WATCHES_FILE` and a cron line.
- Modify `.gitignore` - ignore `iris-watches.json`.
- Create `tests/test_notify_watches.py`, `tests/test_notify_fetch.py`, `tests/test_notify_watch_tick.py`.
- Modify `tests/test_notify_gate.py`, `tests/test_notify_compose.py`, `tests/test_cli.py`.

---

### Task 1: Watch store

**Files:**
- Create: `iris/notify/watches.py`
- Test: `tests/test_notify_watches.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_watches.py`:

```python
"""Tests for the change-watch store."""

from __future__ import annotations

from iris.notify.watches import WatchStore, new_watch


def test_new_watch_defaults():
    w = new_watch("n", cmd="echo hi")
    assert w["cmd"] == "echo hi" and w["url"] is None
    assert w["extract"] == {"kind": "text", "arg": ""}
    assert w["last_value"] is None
    assert w["every_seconds"] == 0.0


def test_add_get_list_remove(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("blog", url="http://x"))
    assert s.get("blog")["url"] == "http://x"
    assert [w["name"] for w in s.list()] == ["blog"]
    assert s.remove("blog") is True
    assert s.get("blog") is None
    assert s.remove("blog") is False


def test_persists_and_records(tmp_path):
    p = tmp_path / "w.json"
    s = WatchStore(p)
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.2.3", 100.0, changed=True)
    reloaded = WatchStore(p)
    w = reloaded.get("v")
    assert w["last_value"] == "1.2.3"
    assert w["last_checked"] == 100.0
    assert w["last_changed"] == 100.0


def test_due_respects_every_seconds(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("fast", url="http://x", every_seconds=0))
    s.add(new_watch("hourly", url="http://y", every_seconds=3600))
    s.record("hourly", "v", 1000.0, changed=False)
    due = [w["name"] for w in s.due(now=1500.0)]
    assert "fast" in due
    assert "hourly" not in due
    assert "hourly" in [w["name"] for w in s.due(now=5000.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_watches.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.watches'`

- [ ] **Step 3: Create the store**

Create `iris/notify/watches.py`:

```python
"""File-backed store of change-watches and their last-seen values.

Mirrors SessionStore / ReminderStore: a small JSON file plus an in-process lock,
written atomically (temp file, fsync, rename). Keyed by watch name.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional


def new_watch(name, *, url=None, cmd=None, extract_kind="text", extract_arg="", every_seconds=0.0):
    """Build a watch definition with empty last-seen state."""
    return {
        "name": name,
        "url": url,
        "cmd": cmd,
        "extract": {"kind": extract_kind, "arg": extract_arg},
        "every_seconds": float(every_seconds),
        "last_value": None,
        "last_checked": 0.0,
        "last_changed": 0.0,
    }


class WatchStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, watch: dict) -> None:
        with self._lock:
            self._data[watch["name"]] = watch
            self._flush()

    def remove(self, name: str) -> bool:
        with self._lock:
            existed = name in self._data
            self._data.pop(name, None)
            if existed:
                self._flush()
            return existed

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(name)

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._data.values())

    def record(self, name: str, value: str, checked_ts: float, changed: bool) -> None:
        with self._lock:
            watch = self._data.get(name)
            if not watch:
                return
            watch["last_value"] = value
            watch["last_checked"] = checked_ts
            if changed:
                watch["last_changed"] = checked_ts
            self._flush()

    def due(self, now: float) -> list[dict]:
        with self._lock:
            return [
                w for w in self._data.values()
                if w.get("last_checked", 0.0) + w.get("every_seconds", 0.0) <= now
            ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_watches.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/watches.py tests/test_notify_watches.py
git commit -m "Add the change-watch store"
```

---

### Task 2: Fetch and extract

**Files:**
- Create: `iris/notify/fetch.py`
- Test: `tests/test_notify_fetch.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_fetch.py`:

```python
"""Tests for fetching and extracting a watch value (network/subprocess faked)."""

from __future__ import annotations

from iris.notify.fetch import extract, fetch
from iris.notify.watches import new_watch


def test_http_returns_body():
    w = new_watch("b", url="http://x")
    assert fetch(w, http_get=lambda url: (200, "hello body")) == "hello body"


def test_http_status_extract_returns_code():
    w = new_watch("s", url="http://x", extract_kind="status")
    raw = fetch(w, http_get=lambda url: (503, "err page"))
    assert raw == "503"
    assert extract(raw, w) == "503"


def test_command_uses_runner():
    w = new_watch("c", cmd="git ls-remote")
    assert fetch(w, runner=lambda cmd: "abc123\tHEAD") == "abc123\tHEAD"


def test_fetch_error_is_sentinel():
    def boom(url):
        raise RuntimeError("timed out")

    assert fetch(new_watch("e", url="http://x"), http_get=boom).startswith("<error:")


def test_extract_text_strips():
    assert extract("  hi \n", new_watch("t", url="http://x")) == "hi"


def test_extract_json_path():
    w = new_watch("j", url="http://x", extract_kind="json", extract_arg="data.version")
    assert extract('{"data": {"version": "4.2"}}', w) == "4.2"


def test_extract_regex_capture():
    w = new_watch("r", url="http://x", extract_kind="regex", extract_arg=r"v([0-9.]+)")
    assert extract("release v1.8.0 now", w) == "1.8.0"


def test_extract_regex_no_match_is_empty():
    w = new_watch("r", url="http://x", extract_kind="regex", extract_arg=r"zzz")
    assert extract("nothing here", w) == ""


def test_extract_keeps_fetch_error_stable():
    w = new_watch("j", url="http://x", extract_kind="json", extract_arg="a")
    assert extract("<error: timed out>", w) == "<error: timed out>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.fetch'`

- [ ] **Step 3: Create fetch and extract**

Create `iris/notify/fetch.py`:

```python
"""Fetch a watch's current value (HTTP GET or shell command) and extract it.

Both functions are total: any network, command, parse, or timeout failure is
turned into a sentinel string starting with "<error:" rather than raised, so a
failure (a site going down, a broken command) registers as a value change and
surfaces, instead of crashing the tick or being silently swallowed.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request


def fetch(watch, *, http_get=None, runner=None) -> str:
    """Return the raw value for a watch. Injectable backends for tests."""
    try:
        if watch.get("url"):
            return _http(watch, http_get)
        if watch.get("cmd"):
            return _command(watch, runner)
        return "<error: watch has neither url nor cmd>"
    except Exception as exc:
        return f"<error: {exc}>"


def _http(watch, http_get) -> str:
    get = http_get or _default_http_get
    status, body = get(watch["url"])
    if watch["extract"]["kind"] == "status":
        return str(status)
    return body


def _default_http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iris-watch/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return getattr(resp, "status", 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx still has a status code, which is what a --status watch wants.
        return exc.code, exc.read().decode("utf-8", "replace")


def _command(watch, runner) -> str:
    if runner is not None:
        return runner(watch["cmd"])
    # shell=True is intentional: the whole point of the --cmd backend is to run a
    # shell command the OWNER wrote (e.g. "git ls-remote ... | head"). Watches are
    # added only via the owner's own CLI on their own single-user box, so there is
    # no untrusted-input path here. If a future MCP tool ever lets the agent create
    # watches from chat, this becomes an injection surface and must be revisited.
    proc = subprocess.run(watch["cmd"], shell=True, capture_output=True, text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)


def extract(raw: str, watch) -> str:
    """Pull the watched value out of the raw fetch result."""
    if raw.startswith("<error:"):
        return raw  # keep a fetch error stable across ticks so it pings once
    kind = watch["extract"]["kind"]
    arg = watch["extract"]["arg"]
    try:
        if kind in ("text", "status"):
            return raw.strip()
        if kind == "json":
            obj = json.loads(raw)
            for part in [p for p in arg.split(".") if p]:
                obj = obj[int(part)] if isinstance(obj, list) else obj[part]
            return str(obj)
        if kind == "regex":
            match = re.search(arg, raw)
            return "" if not match else (match.group(1) if match.groups() else match.group(0))
        return raw.strip()
    except Exception as exc:
        return f"<error: {exc}>"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_fetch.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/fetch.py tests/test_notify_fetch.py
git commit -m "Add watch fetch and extract"
```

---

### Task 3: Spine branches for watch events

**Files:**
- Modify: `iris/notify/gate.py`
- Modify: `iris/notify/compose.py`
- Test: `tests/test_notify_gate.py`, `tests/test_notify_compose.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_notify_gate.py`:

```python
def test_watch_event_notifies():
    e = Event(source="watch", kind="changed", title="blog", exit_code=0, duration_s=0.0)
    assert decide(e, 30) == "notify"


def test_watch_event_quiet_drops():
    e = Event(source="watch", kind="changed", title="blog", exit_code=0, duration_s=0.0)
    assert decide(e, 30, quiet=True) == "drop"
```

Add to `tests/test_notify_compose.py`:

```python
def test_watch_change_template():
    e = Event(source="watch", kind="changed", title="api-version",
              exit_code=0, duration_s=0.0, tail="4.1", detail="4.2")
    assert compose.render(e, None) == "changed: api-version is now 4.2 (was 4.1)"


def test_watch_change_truncates_long_values():
    e = Event(source="watch", kind="changed", title="page",
              exit_code=0, duration_s=0.0, tail="old", detail="x" * 300)
    out = compose.render(e, None)
    assert out.startswith("changed: page is now ")
    assert "..." in out
    assert len(out) < 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notify_gate.py tests/test_notify_compose.py -v`
Expected: FAIL (`test_watch_event_notifies` returns "drop"; `test_watch_change_template` produces the command template, not the watch one)

- [ ] **Step 3: Add the watch branches**

In `iris/notify/gate.py`, change `decide` to notify on a watch event. The function becomes exactly:

```python
def decide(event: Event, min_seconds: float, force: bool = False, quiet: bool = False) -> str:
    """Return "notify" or "drop" for this event."""
    if quiet:
        return "drop"
    if event.source == "watch":
        return "notify"  # a watch event only exists when its value actually changed
    if force:
        return "notify"
    if event.exit_code != 0:
        return "notify"
    if event.duration_s >= min_seconds:
        return "notify"
    return "drop"
```

In `iris/notify/compose.py`, add a `_short` helper after `_fmt`:

```python
def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
```

And change `_template` to handle watch events first:

```python
def _template(event: Event) -> str:
    if event.source == "watch":
        return f"changed: {event.title} is now {_short(event.detail)} (was {_short(event.tail)})"
    if event.exit_code == 0:
        return f"done: {event.title} passed in {_fmt(event.duration_s)}"
    return f"failed: {event.title} exited {event.exit_code} after {_fmt(event.duration_s)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notify_gate.py tests/test_notify_compose.py -v`
Expected: PASS (all, including the new ones)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/gate.py iris/notify/compose.py tests/test_notify_gate.py tests/test_notify_compose.py
git commit -m "Notify on watch-change events and render their template"
```

---

### Task 4: The tick engine

**Files:**
- Create: `iris/notify/watch_tick.py`
- Test: `tests/test_notify_watch_tick.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_watch_tick.py`:

```python
"""Tests for the change-watch tick engine (fetch and delivery faked)."""

from __future__ import annotations

from iris.config import Config
from iris.notify import watch_tick
from iris.notify.watches import WatchStore, new_watch


def cfg():
    return Config(discord_token="t", notify_channel="123", watch_min_seconds=30)


def collect(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def test_first_sighting_is_silent_baseline(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "1.0"), sender=collect(sent))
    assert (checked, changed) == (1, 0)
    assert sent == []
    assert s.get("v")["last_value"] == "1.0"


def test_unchanged_is_silent(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "1.0"), sender=collect(sent))
    assert (checked, changed) == (1, 0)
    assert sent == []


def test_change_notifies(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "2.0"), sender=collect(sent))
    assert (checked, changed) == (1, 1)
    assert sent == [("123", "changed: v is now 2.0 (was 1.0)", "t")]
    assert s.get("v")["last_value"] == "2.0"


def test_every_seconds_throttles(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x", every_seconds=3600))
    s.record("v", "1.0", 1000.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=1500.0,
                                       http_get=lambda url: (200, "2.0"), sender=collect(sent))
    assert (checked, changed) == (0, 0)
    assert sent == []


def test_change_falls_back_to_print_when_no_delivery(tmp_path, capsys):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    checked, changed = watch_tick.tick(s, Config(), now=10.0,
                                       http_get=lambda url: (200, "2.0"))
    assert (checked, changed) == (1, 1)
    assert "changed: v is now 2.0 (was 1.0)" in capsys.readouterr().out


def test_make_watch_from_flags_status():
    w = watch_tick.make_watch_from_flags("s", url="http://x", status=True)
    assert w["extract"] == {"kind": "status", "arg": ""}


def test_make_watch_from_flags_json():
    w = watch_tick.make_watch_from_flags("j", url="http://x", json_key="a.b")
    assert w["extract"] == {"kind": "json", "arg": "a.b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_watch_tick.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.watch_tick'`

- [ ] **Step 3: Create the engine and CLI handlers**

Create `iris/notify/watch_tick.py`:

```python
"""Run due change-watches once: fetch, diff, and notify on a real change.

No model call here: fetch and diff are free, and a change renders a template via
compose. Run by cron or a systemd timer, like reminders-tick, so there is no idle
process and no idle inference.
"""

from __future__ import annotations

import os
import time

from . import compose, deliver, gate
from .events import Event
from .fetch import extract, fetch
from .watches import WatchStore, new_watch


def tick(store, config, *, now, http_get=None, runner=None, sender=None):
    """Check every due watch once. Returns (checked_count, changed_count)."""
    checked = 0
    changed = 0
    for watch in store.due(now):
        checked += 1
        value = extract(fetch(watch, http_get=http_get, runner=runner), watch)
        if watch["last_value"] is None:
            store.record(watch["name"], value, now, changed=False)  # baseline, silent
            continue
        if value == watch["last_value"]:
            store.record(watch["name"], value, now, changed=False)
            continue
        event = Event(
            source="watch", kind="changed", title=watch["name"],
            exit_code=0, duration_s=0.0,
            tail=watch["last_value"], detail=value, urgency="normal",
        )
        if gate.decide(event, config.watch_min_seconds) == "notify":
            text = compose.render(event, None)
            if not deliver.send(text, token=config.discord_token,
                                channel=config.notify_channel, sender=sender):
                print(text)
        store.record(watch["name"], value, now, changed=True)
        changed += 1
    return checked, changed


def make_watch_from_flags(name, *, url=None, cmd=None, json_key=None, match=None, status=False, every=0.0):
    """Build a watch dict from the CLI flags (at most one extractor wins)."""
    if status:
        kind, arg = "status", ""
    elif json_key is not None:
        kind, arg = "json", json_key
    elif match is not None:
        kind, arg = "regex", match
    else:
        kind, arg = "text", ""
    return new_watch(name, url=url, cmd=cmd, extract_kind=kind, extract_arg=arg, every_seconds=every)


def _store() -> WatchStore:
    return WatchStore(os.environ.get("IRIS_WATCHES_FILE", "iris-watches.json"))


def cli_add(args) -> int:
    watch = make_watch_from_flags(
        args.name, url=args.url, cmd=args.cmd,
        json_key=args.json, match=args.match, status=args.status, every=args.every,
    )
    _store().add(watch)
    print(f"watching '{args.name}'")
    return 0


def cli_list(args) -> int:
    watches = _store().list()
    if not watches:
        print("no watches")
        return 0
    for w in watches:
        print(f"  {w['name']}: {w['url'] or w['cmd']} (last={w['last_value']})")
    return 0


def cli_rm(args) -> int:
    removed = _store().remove(args.name)
    print(f"removed '{args.name}'" if removed else f"no watch named '{args.name}'")
    return 0 if removed else 1


def cli_tick(config) -> int:
    checked, changed = tick(_store(), config, now=time.time())
    print(f"watch-tick: {checked} checked, {changed} changed")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_watch_tick.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/watch_tick.py tests/test_notify_watch_tick.py
git commit -m "Add the change-watch tick engine and CLI handlers"
```

---

### Task 5: CLI wiring and docs

**Files:**
- Modify: `iris/cli.py`
- Modify: `.env.example`, `.gitignore`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_watch_add_list_rm_roundtrip(tmp_path, monkeypatch):
    from iris.cli import main
    monkeypatch.setenv("IRIS_WATCHES_FILE", str(tmp_path / "w.json"))
    assert main(["watch-add", "--name", "blog", "--url", "http://x"]) == 0
    assert main(["watch-list"]) == 0
    assert main(["watch-rm", "blog"]) == 0
    assert main(["watch-rm", "blog"]) == 1  # already gone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py::test_watch_add_list_rm_roundtrip -v`
Expected: FAIL (argparse exits 2 on the unknown `watch-add` subcommand)

- [ ] **Step 3: Wire the subcommands**

In `iris/cli.py`, in `main`, add these subparsers next to the others (after the `watch` parser block):

```python
    add_p = sub.add_parser("watch-add", help="register a change-watch")
    add_p.add_argument("--name", required=True)
    src_group = add_p.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--url")
    src_group.add_argument("--cmd")
    ext_group = add_p.add_mutually_exclusive_group()
    ext_group.add_argument("--json", dest="json")
    ext_group.add_argument("--match")
    ext_group.add_argument("--status", action="store_true")
    add_p.add_argument("--every", type=float, default=0.0)
    sub.add_parser("watch-list", help="list change-watches")
    rm_p = sub.add_parser("watch-rm", help="remove a change-watch")
    rm_p.add_argument("name")
    sub.add_parser("watch-tick", help="check due change-watches (run from cron/timer)")
```

In `iris/cli.py`, in `main`, add these dispatch branches before the final `# discord` fallback block:

```python
    if command == "watch-add":
        from .notify.watch_tick import cli_add
        return cli_add(args)
    if command == "watch-list":
        from .notify.watch_tick import cli_list
        return cli_list(args)
    if command == "watch-rm":
        from .notify.watch_tick import cli_rm
        return cli_rm(args)
    if command == "watch-tick":
        from .notify.watch_tick import cli_tick
        return cli_tick(config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Document and ignore the state file**

In `.gitignore`, under the sessions/state block (next to `iris-reminders.json`), add:

```
iris-watches.json
```

In `.env.example`, in the `--- Proactive notifications (iris watch) ---` section (after the `IRIS_NOTIFY_PERSONA` line), add:

```bash
# Change-watcher state file (iris watch-add / watch-tick). Local state, gitignored.
IRIS_WATCHES_FILE=iris-watches.json
# Run the tick from cron or a timer, e.g. every 5 minutes:
#   */5 * * * * cd /path/to/iris && IRIS_DISCORD_TOKEN=... IRIS_NOTIFY_CHANNEL=... /path/to/venv/bin/python -m iris watch-tick
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all prior tests plus the new watch tests)

- [ ] **Step 7: Commit**

```bash
git add iris/cli.py .env.example .gitignore tests/test_cli.py
git commit -m "Wire the watch-add/list/rm/tick subcommands and document them"
```

---

## Manual verification (after the plan)

Run by hand once, no Discord needed (prints locally when delivery is unset):

```bash
python -m iris watch-add --name self --cmd "date +%S"   # seconds, changes every run
python -m iris watch-tick     # first run: silent baseline
python -m iris watch-tick     # likely prints: changed: self is now NN (was MM)
python -m iris watch-list
python -m iris watch-rm self
```
