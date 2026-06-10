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
