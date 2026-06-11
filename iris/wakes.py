"""Event wakes: owner-declared conditions the tick checks without the model.

Reminders fire at a time; wakes fire on an event — a log gains an ERROR line,
a file lands or vanishes, an export changes. The owner declares rules in
``IRIS_WAKES_FILE`` (paths are owner-authored; the model has no tool to touch
them), the existing ``reminders-tick`` cadence evaluates them with stat/read
calls only, and a fire delivers a pre-written Discord ping plus a fold-back
inbox note. **A wake never calls the model**: "waking the agent" means the
owner is pinged and the agent's next owner-initiated turn knows why.
See docs/superpowers/specs/2026-06-10-event-wakes-design.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Config
from .inbox import Inbox

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger("iris.wakes")

# File-source kinds read local paths; URL-source kinds fetch over http(s).
# The URL kinds are the merged-in "change watcher" — see
# docs/superpowers/specs/2026-06-09-url-watcher-design.md.
FILE_KINDS = ("file_exists", "file_gone", "file_changed", "log_pattern")
URL_KINDS = ("url", "url_pattern")
KINDS = FILE_KINDS + URL_KINDS
DEFAULT_COOLDOWN = 3600.0
# The most of a log's appended tail one tick will read for one rule.
MAX_TAIL_READ = 256 * 1024
# The most of a URL body one fetch will read.
MAX_URL_READ = 1024 * 1024

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def validate_rules(rules) -> list[str]:
    """Pure validation: one human-readable line per problem, [] when clean."""
    if not isinstance(rules, list):
        return ["the rules file must hold a JSON list of rule objects"]
    problems: list[str] = []
    seen: set = set()
    for index, rule in enumerate(rules):
        label = f"rule {index}"
        if not isinstance(rule, dict):
            problems.append(f"{label}: not an object")
            continue
        name = rule.get("name")
        if isinstance(name, str) and name:
            label = f"rule {name!r}"
        if not isinstance(name, str) or not _NAME.match(name or ""):
            problems.append(f"{label}: bad name {name!r} (lowercase letters, digits, - or _, max 32)")
        elif name in seen:
            problems.append(f"{label}: duplicate name")
        else:
            seen.add(name)
        kind = rule.get("kind")
        if kind not in KINDS:
            problems.append(f"{label}: unknown kind {kind!r} (use one of {', '.join(KINDS)})")
        if kind in URL_KINDS:
            url = rule.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                problems.append(f"{label}: url must be an http(s) URL, got {url!r}")
        else:
            path = rule.get("path")
            if not isinstance(path, str) or not os.path.isabs(path):
                problems.append(f"{label}: path must be absolute, got {path!r}")
        if not isinstance(rule.get("message"), str) or not rule.get("message").strip():
            problems.append(f"{label}: a non-empty message is required")
        if kind in ("log_pattern", "url_pattern"):
            pattern = rule.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                problems.append(f"{label}: {kind} needs a regex pattern")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"{label}: bad regex {pattern!r} ({exc})")
        cooldown = rule.get("cooldown_secs", DEFAULT_COOLDOWN)
        if not isinstance(cooldown, (int, float)) or cooldown <= 0:
            problems.append(f"{label}: cooldown_secs must be a positive number, got {cooldown!r}")
    return problems


def load_rules(path: Path) -> tuple[list, Optional[str]]:
    """Read the rules file. Returns (rules, problem-line-or-None)."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"could not read the rules file ({exc})"
    if not isinstance(data, list):
        return [], "could not use the rules file (it must hold a JSON list)"
    return data, None


class _StateStore:
    """The tick-owned observation state, locked across read-evaluate-write."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield self._load()
            return
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield self._load()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            # Keep the bad file around for inspection, start fresh.
            try:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            return {}

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


def http_get(url: str, timeout: float) -> bytes:
    """Fetch a URL's body (bounded). The default fetcher; tests inject a fake."""
    req = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "iris-wakes (https://github.com/luoojason/iris, 0.1)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_URL_READ)


def _evaluate(rule: dict, entry: dict, fetch=None, http_timeout: float = 15.0) -> tuple[bool, str]:
    """Evaluate one rule against its state entry. Returns (fired, detail).

    Mutates ``entry``'s observation fields so the next tick sees this tick's
    world. File kinds do stat/read only. URL kinds do one bounded HTTP GET via
    ``fetch`` (the merged change-watcher). Nothing here ever calls the model.
    """
    kind = rule["kind"]

    if kind in URL_KINDS:
        return _evaluate_url(rule, entry, fetch or http_get, http_timeout)

    path = Path(rule["path"])

    if kind in ("file_exists", "file_gone"):
        present = path.exists()
        was = entry.get("present")
        entry["present"] = present
        if was is None:
            return False, ""  # first observation arms without firing
        if kind == "file_exists":
            return (was is False and present), ""
        return (was is True and not present), ""

    if kind == "file_changed":
        try:
            stat = path.stat()
        except OSError:
            return False, ""  # nothing to observe; keep the armed state
        old = (entry.get("mtime"), entry.get("size"))
        entry["mtime"], entry["size"] = stat.st_mtime, stat.st_size
        if old == (None, None):
            return False, ""  # first observation arms without firing
        return (old != (stat.st_mtime, stat.st_size)), ""

    # log_pattern
    if not path.exists():
        entry["offset"] = 0  # when the file returns, its content is new
        return False, ""
    size = path.stat().st_size
    offset = entry.get("offset")
    if offset is None:
        entry["offset"] = size  # arm at EOF; history never fires
        return False, ""
    if size < offset:
        offset = 0  # truncated or rotated: the new file's content is new
    if size == offset:
        return False, ""
    start = max(offset, size - MAX_TAIL_READ)
    with open(path, "rb") as handle:
        handle.seek(start)
        blob = handle.read(size - start)
    entry["offset"] = size
    text = blob.decode("utf-8", "replace")
    matcher = re.compile(rule["pattern"])
    for line in text.splitlines():
        if matcher.search(line):
            return True, line.strip()[:200]
    return False, ""


def tick_wakes(config: Config, now: Optional[float] = None, send=None,
               inbox: Optional[Inbox] = None, fetch=None) -> str:
    """Evaluate every wake rule once. Called from ``iris reminders-tick``.

    Returns a one-line summary for the tick's output. Failures degrade to
    text in that line; nothing here may raise into reminder delivery.
    """
    now = time.time() if now is None else now
    rules_path = Path(config.wakes_file)
    if not rules_path.exists():
        return "wakes: no rules file"
    rules, load_problem = load_rules(rules_path)
    if load_problem:
        return f"wakes: {load_problem}"
    if send is None:
        from .reminders import send_discord_message as send
    inbox = inbox or Inbox(config.inbox_file)

    skipped: list[str] = []
    fired = 0
    store = _StateStore(config.wakes_state)
    with store.locked() as state:
        try:
            seen: set = set()
            for rule in rules:
                problems = validate_rules([rule])
                name = rule.get("name") if isinstance(rule, dict) else None
                if problems:
                    skipped.append(f"{name or 'rule'} ({problems[0]})")
                    continue
                if name in seen:
                    skipped.append(f"{name} (duplicate name; only the first rule ran)")
                    continue
                seen.add(name)
                entry = state.setdefault(name, {})

                try:
                    fired += _run_rule(rule, entry, config, send, inbox, now, fetch)
                except Exception as exc:
                    # One poisoned rule (a hostile path, a filesystem oddity)
                    # must never abort the others or skip the state save.
                    log.warning("wake rule %s crashed", name, exc_info=True)
                    skipped.append(f"{name} (evaluation crashed: {exc})")
        finally:
            # Prune by RAW rule names: a rule that is present but momentarily
            # invalid keeps its offsets and once-flags; only rules that left
            # the file lose their state. Saving in finally means a crash can
            # never replay already-recorded fires on the next tick.
            raw_names = {
                r.get("name") for r in rules
                if isinstance(r, dict) and isinstance(r.get("name"), str)
            }
            for name in list(state):
                if name not in raw_names:
                    del state[name]
            store.save(state)

    line = f"wakes: {len(rules)} rules, {fired} fired"
    if skipped:
        line += "; skipped " + ", ".join(skipped)
    return line


def _evaluate_url(rule: dict, entry: dict, fetch, http_timeout: float) -> tuple[bool, str]:
    """Fetch a URL and decide whether it fired. A failed fetch is a no-op:
    it neither fires nor advances the stored digest, so a transient outage
    cannot eat a real change."""
    try:
        body = fetch(rule["url"], http_timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("wake %s could not fetch %s: %s", rule.get("name"), rule.get("url"), exc)
        return False, ""
    digest = hashlib.sha256(body).hexdigest()

    if rule["kind"] == "url":
        old = entry.get("digest")
        entry["digest"] = digest
        if old is None:
            return False, ""  # first fetch arms without firing
        return (old != digest), ""

    # url_pattern: edge-triggered on the regex matching the body.
    text = body.decode("utf-8", "replace")
    matcher = re.compile(rule["pattern"])
    hit = matcher.search(text)
    was_matching = bool(entry.get("matching"))
    entry["matching"] = bool(hit)
    if hit and not was_matching:
        line = next((ln for ln in text.splitlines() if matcher.search(ln)), "")
        return True, line.strip()[:200]
    return False, ""


def _run_rule(rule: dict, entry: dict, config: Config, send, inbox: Inbox,
              now: float, fetch=None) -> int:
    """Evaluate one validated rule against its state entry. Returns fires (0/1)."""
    name = rule["name"]

    # Retry a ping a previous tick could not deliver, before anything new.
    pending = entry.get("pending_ping")
    if pending:
        if _send_ping(rule, config, send, pending):
            entry["pending_ping"] = None
            entry["last_fired_ts"] = now
        return 0  # no new fire while a ping is still owed

    if rule.get("once") and entry.get("fired_once"):
        return 0

    fired_now, detail = _evaluate(rule, entry, fetch, config.wake_http_timeout)
    if not fired_now:
        return 0
    last = float(entry.get("last_fired_ts") or 0.0)
    cooldown = float(rule.get("cooldown_secs", DEFAULT_COOLDOWN))
    if now - last < cooldown:
        return 0  # flapping; observation state advanced, no ping

    message = f"wake {name}: {rule['message']}"
    if detail:
        message += f"\n> {detail}"
    if rule.get("once"):
        entry["fired_once"] = True
    inbox.append(message)  # queued exactly once, ping or no ping
    if _send_ping(rule, config, send, message):
        entry["last_fired_ts"] = now
    else:
        entry["pending_ping"] = message  # the next tick retries
    return 1


def _send_ping(rule: dict, config: Config, send, message: str) -> bool:
    channel = rule.get("channel_id") or config.home_channel or config.notify_channel
    if not channel or not config.discord_token:
        # No way to ping; the inbox note still informs the next turn. Treat as
        # delivered so an unconfigured channel does not retry forever.
        return True
    return bool(send(channel, message, config.discord_token))


def doctor_lines(config: Config) -> list[str]:
    """The wakes section for `iris doctor`: [] when there is no rules file."""
    rules_path = Path(config.wakes_file)
    if not rules_path.exists():
        return []
    rules, load_problem = load_rules(rules_path)
    if load_problem:
        return [f"wakes: {load_problem}"]
    problems = validate_rules(rules)
    if not problems:
        return [f"wakes: {len(rules)} rules ok"]
    return [f"wakes: {len(problems)} problem(s) in {len(rules)} rules:"] + [
        f"  {problem}" for problem in problems
    ]
