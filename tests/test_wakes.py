"""Tests for event wakes (iris/wakes.py).

A wake never calls the model: the tick evaluates cheap local conditions and
delivers a pre-written ping plus a fold-back note. See
docs/superpowers/specs/2026-06-10-event-wakes-design.md.
"""

from __future__ import annotations

import json

from iris.config import Config
from iris.inbox import Inbox
from iris.wakes import tick_wakes, validate_rules

NOW = 1780000000.0


def rule(tmp_path, **over):
    base = {
        "name": "watch-log",
        "kind": "log_pattern",
        "path": str(tmp_path / "run.log"),
        "pattern": "ERROR",
        "message": "the run hit an error",
        "cooldown_secs": 3600,
    }
    base.update(over)
    return base


def write_rules(tmp_path, rules):
    (tmp_path / "wakes.json").write_text(json.dumps(rules), encoding="utf-8")


def wake_config(tmp_path):
    return Config(
        discord_token="tok",
        home_channel="home-1",
        wakes_file=str(tmp_path / "wakes.json"),
        wakes_state=str(tmp_path / "wakes.state.json"),
        inbox_file=str(tmp_path / "inbox.json"),
    )


def tick(tmp_path, now=NOW, send=None, pings=None):
    pings = pings if pings is not None else []

    def default_send(channel, text, token):
        pings.append((channel, text))
        return True

    config = wake_config(tmp_path)
    line = tick_wakes(config, now=now, send=send or default_send)
    return line, pings, Inbox(config.inbox_file)


# -- validation -----------------------------------------------------------------


def test_validate_rules_accepts_a_good_file(tmp_path):
    rules = [
        rule(tmp_path),
        {"name": "drop-file", "kind": "file_exists", "path": "/tmp/x", "message": "landed"},
    ]
    assert validate_rules(rules) == []


def test_validate_rules_names_every_problem(tmp_path):
    rules = [
        {"name": "BadName!", "kind": "file_exists", "path": "/tmp/x", "message": "m"},
        {"name": "dup", "kind": "file_gone", "path": "/tmp/x", "message": "m"},
        {"name": "dup", "kind": "file_gone", "path": "/tmp/x", "message": "m"},
        {"name": "rel", "kind": "file_exists", "path": "relative/path", "message": "m"},
        {"name": "kindless", "kind": "smoke_signal", "path": "/tmp/x", "message": "m"},
        {"name": "mute", "kind": "file_exists", "path": "/tmp/x", "message": ""},
        {"name": "badre", "kind": "log_pattern", "path": "/tmp/x", "pattern": "(", "message": "m"},
        {"name": "negcool", "kind": "file_exists", "path": "/tmp/x", "message": "m",
         "cooldown_secs": -5},
    ]
    problems = validate_rules(rules)
    assert len(problems) == 7
    blob = "\n".join(problems)
    for key in ("BadName!", "dup", "rel", "smoke_signal", "mute", "badre", "negcool"):
        assert key in blob


def test_validate_rules_rejects_a_non_list():
    problems = validate_rules({"name": "x"})  # one problem: not a list
    assert len(problems) == 1 and "list" in problems[0]


# -- file_exists / file_gone ------------------------------------------------------


def test_file_exists_arms_then_fires_on_appearance(tmp_path):
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="it landed", pattern=None)])
    line, pings, inbox = tick(tmp_path)  # absent: arms
    assert "0 fired" in line and pings == []

    pings = []
    target.write_text("here", encoding="utf-8")
    line, pings, inbox = tick(tmp_path, pings=pings)
    assert "1 fired" in line
    assert pings == [("home-1", "wake drop: it landed")]
    assert inbox.drain("discord:home-1") == ["wake drop: it landed"]

    # still present: an edge already consumed does not re-fire
    line, pings, inbox = tick(tmp_path, now=NOW + 7200, pings=[])
    assert "0 fired" in line


def test_file_exists_does_not_fire_when_already_present_at_first_sight(tmp_path):
    target = tmp_path / "drop.txt"
    target.write_text("was always here", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="m", pattern=None)])
    line, pings, _ = tick(tmp_path)
    assert "0 fired" in line and pings == []


def test_file_gone_fires_on_disappearance(tmp_path):
    target = tmp_path / "lock"
    target.write_text("x", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path, name="unlocked", kind="file_gone",
                                path=str(target), message="lock released", pattern=None)])
    tick(tmp_path)  # arms with present=True
    target.unlink()
    line, pings, _ = tick(tmp_path, pings=[])
    assert "1 fired" in line
    assert pings[0][1] == "wake unlocked: lock released"


# -- file_changed ------------------------------------------------------------------


def test_file_changed_arms_then_fires_on_change(tmp_path):
    target = tmp_path / "export.bin"
    target.write_text("v1", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path, name="export", kind="file_changed",
                                path=str(target), message="the export moved", pattern=None)])
    line, pings, _ = tick(tmp_path)  # first observation arms without firing
    assert "0 fired" in line

    target.write_text("v2 is longer", encoding="utf-8")
    line, pings, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "1 fired" in line

    line, pings, _ = tick(tmp_path, now=NOW + 7300, pings=[])  # unchanged
    assert "0 fired" in line


# -- log_pattern --------------------------------------------------------------------


def test_log_pattern_arms_at_eof_and_fires_on_new_matches(tmp_path):
    target = tmp_path / "run.log"
    target.write_text("old ERROR that predates the rule\n", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path)])

    line, pings, _ = tick(tmp_path)  # arms at EOF; old content never fires
    assert "0 fired" in line

    with open(target, "a", encoding="utf-8") as handle:
        handle.write("all fine\n")
    line, pings, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "0 fired" in line

    with open(target, "a", encoding="utf-8") as handle:
        handle.write("ERROR: it broke\n")
    pings = []
    line, pings, inbox = tick(tmp_path, now=NOW + 120, pings=pings)
    assert "1 fired" in line
    channel, text = pings[0]
    assert "wake watch-log: the run hit an error" in text
    assert "ERROR: it broke" in text  # the matching line rides along


def test_log_pattern_survives_rotation(tmp_path):
    target = tmp_path / "run.log"
    target.write_text("a long preamble without problems\n", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path)])
    tick(tmp_path)  # arm at EOF

    target.write_text("ERROR right after rotation\n", encoding="utf-8")  # smaller file
    line, pings, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "1 fired" in line  # the rotated file's content is new content


def test_cooldown_suppresses_rapid_refires(tmp_path):
    target = tmp_path / "run.log"
    target.write_text("", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path, cooldown_secs=3600)])
    tick(tmp_path)

    with open(target, "a", encoding="utf-8") as handle:
        handle.write("ERROR one\n")
    line, _, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "1 fired" in line

    with open(target, "a", encoding="utf-8") as handle:
        handle.write("ERROR two\n")
    line, pings, _ = tick(tmp_path, now=NOW + 120, pings=[])  # within cooldown
    assert "0 fired" in line and pings == []

    with open(target, "a", encoding="utf-8") as handle:
        handle.write("ERROR three\n")
    line, _, _ = tick(tmp_path, now=NOW + 60 + 3601, pings=[])  # cooldown passed
    assert "1 fired" in line


def test_once_disarms_after_the_first_fire(tmp_path):
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="m", pattern=None, once=True)])
    tick(tmp_path)
    target.write_text("x", encoding="utf-8")
    line, _, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "1 fired" in line
    target.unlink()
    tick(tmp_path, now=NOW + 7200, pings=[])
    target.write_text("x", encoding="utf-8")  # a second appearance
    line, pings, _ = tick(tmp_path, now=NOW + 14400, pings=[])
    assert "0 fired" in line and pings == []


# -- delivery ------------------------------------------------------------------------


def test_failed_ping_retries_next_tick_but_folds_back_once(tmp_path):
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="m", pattern=None)])
    tick(tmp_path)
    target.write_text("x", encoding="utf-8")

    line, _, inbox = tick(tmp_path, now=NOW + 60, send=lambda *a: False)  # Discord down
    assert "1 fired" in line

    pings = []
    line, pings, inbox = tick(tmp_path, now=NOW + 120, pings=pings)
    assert pings == [("home-1", "wake drop: m")]  # retried
    assert inbox.drain("discord:home-1") == ["wake drop: m"]  # queued exactly once

    line, pings, _ = tick(tmp_path, now=NOW + 180, pings=[])
    assert pings == []  # delivered; nothing left to retry


def test_pending_ping_gives_up_after_max_attempts_on_a_dead_channel(tmp_path):
    # A permanently-undeliverable channel (deleted channel / removed bot) must not
    # wedge the rule: it would otherwise POST one failed REST call every tick
    # forever AND never fire on a new condition (the tick returns early while a
    # ping is owed). After a capped number of retries it gives up - the message
    # already folded into the inbox on the first fire, so it is not lost.
    from iris.wakes import MAX_PENDING_PING_ATTEMPTS

    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="m", pattern=None)])
    tick(tmp_path)  # arm
    target.write_text("x", encoding="utf-8")

    posts = []
    dead = lambda *a: posts.append(1) or False  # Discord permanently down
    tick(tmp_path, now=NOW + 60, send=dead)  # fires; ping fails -> a ping is owed
    for i in range(MAX_PENDING_PING_ATTEMPTS + 5):  # keep ticking well past the cap
        tick(tmp_path, now=NOW + 120 + i * 60, send=dead)
    # one POST on the fire + one per retry up to the cap, then it stops forever
    assert len(posts) == MAX_PENDING_PING_ATTEMPTS + 1


def test_rule_channel_overrides_home(tmp_path):
    target = tmp_path / "drop.txt"
    target.write_text("x", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_gone",
                                path=str(target), message="m", pattern=None,
                                channel_id="chan-override")])
    tick(tmp_path)
    target.unlink()
    _, pings, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert pings[0][0] == "chan-override"


# -- the tick wrapper -----------------------------------------------------------------


def test_missing_rules_file_is_silent(tmp_path):
    line, pings, _ = tick(tmp_path)
    assert line == "wakes: no rules file"
    assert pings == []


def test_malformed_rules_file_reports_and_never_raises(tmp_path):
    (tmp_path / "wakes.json").write_text("{not json", encoding="utf-8")
    line, pings, _ = tick(tmp_path)
    assert "wakes:" in line and "could not" in line
    assert pings == []


def test_invalid_rule_is_skipped_with_a_warning_line(tmp_path):
    write_rules(tmp_path, [
        rule(tmp_path, name="ok-rule", kind="file_exists", pattern=None),
        rule(tmp_path, name="broken", kind="log_pattern", pattern="("),
    ])
    line, _, _ = tick(tmp_path)
    assert "broken" in line  # the skip is visible, not silent


def test_state_of_removed_rules_is_pruned(tmp_path):
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(target), message="m", pattern=None)])
    config = wake_config(tmp_path)
    tick(tmp_path)
    state = json.loads((tmp_path / "wakes.state.json").read_text("utf-8"))
    assert "drop" in state

    write_rules(tmp_path, [])
    tick(tmp_path, now=NOW + 60)
    state = json.loads((tmp_path / "wakes.state.json").read_text("utf-8"))
    assert state == {}


def test_corrupt_state_starts_fresh(tmp_path):
    write_rules(tmp_path, [rule(tmp_path, name="drop", kind="file_exists",
                                path=str(tmp_path / "d"), message="m", pattern=None)])
    (tmp_path / "wakes.state.json").write_text("{broken", encoding="utf-8")
    line, _, _ = tick(tmp_path)
    assert "1 rules" in line  # evaluated despite the corrupt state


# -- wiring --------------------------------------------------------------------------


def test_reminders_tick_runs_wakes_beside_budget(tmp_path, monkeypatch, capsys):
    import iris.reminders as reminders_mod
    from iris.cli import reminders_tick

    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "rem.json"))
    monkeypatch.setattr(reminders_mod, "send_discord_message", lambda *a: True)
    config = wake_config(tmp_path)
    write_rules(tmp_path, [])
    assert reminders_tick(config) == 0
    out = capsys.readouterr().out
    assert "wakes:" in out
    assert "budget:" in out


def test_doctor_lines_validate_the_rules_file(tmp_path):
    from iris.wakes import doctor_lines

    config = wake_config(tmp_path)
    assert doctor_lines(config) == []  # no rules file -> no section

    write_rules(tmp_path, [rule(tmp_path)])
    assert doctor_lines(config) == ["wakes: 1 rules ok"]

    write_rules(tmp_path, [rule(tmp_path, kind="smoke_signal")])
    lines = doctor_lines(config)
    assert any("smoke_signal" in line for line in lines)


def test_wakes_config_knobs(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_WAKES_FILE", "w.json")
    monkeypatch.setenv("IRIS_WAKES_STATE", "w.state.json")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.wakes_file == "w.json"
    assert cfg.wakes_state == "w.state.json"
    cfg = Config(
        wakes_file="iris-wakes.json",
    )
    assert cfg.wakes_state == "iris-wakes.state.json"


def test_temporarily_invalid_rule_keeps_its_state(tmp_path):
    """A fat-fingered regex must not erase the rule's offset: pruning is for
    rules that no longer exist, not rules that are momentarily broken."""
    target = tmp_path / "run.log"
    target.write_text("preamble\n", encoding="utf-8")
    write_rules(tmp_path, [rule(tmp_path)])
    tick(tmp_path)  # arms at EOF

    write_rules(tmp_path, [rule(tmp_path, pattern="(")])  # owner mid-edit
    line, _, _ = tick(tmp_path, now=NOW + 60)
    assert "watch-log" in line  # skipped, visibly

    write_rules(tmp_path, [rule(tmp_path)])  # fixed
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("ERROR: while the rule was broken-then-fixed\n")
    line, _, _ = tick(tmp_path, now=NOW + 120)
    assert "1 fired" in line  # the preserved offset caught the new error


def test_one_crashing_rule_does_not_abort_the_rest(tmp_path, monkeypatch):
    import iris.wakes as wakes_mod

    real_evaluate = wakes_mod._evaluate

    def fragile(rule, entry, *args, **kwargs):
        if rule["name"] == "poisoned":
            raise OSError("filesystem oddity")
        return real_evaluate(rule, entry, *args, **kwargs)

    monkeypatch.setattr(wakes_mod, "_evaluate", fragile)
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [
        rule(tmp_path, name="poisoned", kind="file_exists",
             path="/tmp/somewhere", message="m", pattern=None),
        rule(tmp_path, name="healthy", kind="file_exists",
             path=str(target), message="it landed", pattern=None),
    ])
    line, _, _ = tick(tmp_path)
    assert "poisoned" in line  # the crash is named, not silent
    target.write_text("x", encoding="utf-8")
    line, pings, _ = tick(tmp_path, now=NOW + 60, pings=[])
    assert "1 fired" in line  # the healthy rule armed and fired normally
    assert pings[0][1] == "wake healthy: it landed"
    # and its state persisted: no re-fire while present
    line, _, _ = tick(tmp_path, now=NOW + 7300, pings=[])
    assert "0 fired" in line


def test_duplicate_rule_names_are_skipped_visibly(tmp_path):
    target = tmp_path / "drop.txt"
    write_rules(tmp_path, [
        rule(tmp_path, name="dup", kind="file_exists", path=str(target),
             message="first", pattern=None),
        rule(tmp_path, name="dup", kind="file_gone", path=str(target),
             message="second", pattern=None),
    ])
    line, _, _ = tick(tmp_path)
    assert "duplicate" in line and "dup" in line


# -- url / url_pattern kinds --------------------------------------------------


def url_rule(tmp_path, **over):
    base = {
        "name": "site",
        "kind": "url",
        "url": "https://example.com/status",
        "message": "the page changed",
        "cooldown_secs": 3600,
    }
    base.update(over)
    return base


def fetcher(pages):
    """pages: list of bodies returned on successive fetches; str raises."""
    calls = {"n": 0}

    def fetch(url, timeout):
        i = min(calls["n"], len(pages) - 1)
        calls["n"] += 1
        body = pages[i]
        if isinstance(body, Exception):
            raise body
        return body.encode("utf-8")

    return fetch


def utick(tmp_path, fetch, now=NOW, pings=None):
    pings = pings if pings is not None else []

    def send(channel, text, token):
        pings.append((channel, text))
        return True

    from iris.wakes import tick_wakes
    config = wake_config(tmp_path)
    line = tick_wakes(config, now=now, send=send, fetch=fetch)
    return line, pings, Inbox(config.inbox_file)


def test_url_arms_then_fires_on_change(tmp_path):
    write_rules(tmp_path, [url_rule(tmp_path)])
    fetch = fetcher(["v1", "v1", "v2"])
    line, pings, _ = utick(tmp_path, fetch)  # first fetch arms
    assert "0 fired" in line and pings == []
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 60, pings=[])  # unchanged
    assert "0 fired" in line
    line, pings, inbox = utick(tmp_path, fetch, now=NOW + 120, pings=[])  # changed
    assert "1 fired" in line
    assert pings == [("home-1", "wake site: the page changed")]
    assert inbox.drain("discord:home-1") == ["wake site: the page changed"]


def test_url_failed_fetch_does_not_fire_or_advance_state(tmp_path):
    write_rules(tmp_path, [url_rule(tmp_path)])
    import urllib.error
    fetch = fetcher(["v1", urllib.error.URLError("down"), "v2"])
    utick(tmp_path, fetch)  # arm on v1
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 60, pings=[])  # fetch fails
    assert "0 fired" in line and pings == []
    # the failed tick must not have advanced the digest: v2 is still a change
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 120, pings=[])
    assert "1 fired" in line


def test_url_pattern_fires_on_match_appearing(tmp_path):
    write_rules(tmp_path, [url_rule(
        tmp_path, name="instock", kind="url_pattern",
        pattern="In stock", message="it is back in stock")])
    fetch = fetcher(["Out of stock", "Out of stock", "In stock now!"])
    line, _, _ = utick(tmp_path, fetch)  # arm
    assert "0 fired" in line
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 60, pings=[])  # still out
    assert "0 fired" in line
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 120, pings=[])  # match appears
    assert "1 fired" in line
    assert "In stock now!" in pings[0][1]  # the matching line rides along


def test_url_pattern_does_not_refire_while_match_persists(tmp_path):
    write_rules(tmp_path, [url_rule(
        tmp_path, name="instock", kind="url_pattern", cooldown_secs=1,
        pattern="In stock", message="back")])
    fetch = fetcher(["nope", "In stock", "In stock"])
    utick(tmp_path, fetch)  # arm
    line, _, _ = utick(tmp_path, fetch, now=NOW + 10, pings=[])  # appears -> fire
    assert "1 fired" in line
    line, pings, _ = utick(tmp_path, fetch, now=NOW + 20, pings=[])  # persists, no refire
    assert "0 fired" in line and pings == []


def test_url_validation(tmp_path):
    good = url_rule(tmp_path)
    assert validate_rules([good]) == []
    bad_scheme = url_rule(tmp_path, name="ftp", url="ftp://x/y")
    assert validate_rules([bad_scheme])
    no_url = {"name": "nourl", "kind": "url", "message": "m"}
    assert validate_rules([no_url])
    pat_missing = {"name": "p", "kind": "url_pattern", "url": "https://x", "message": "m"}
    assert validate_rules([pat_missing])  # url_pattern needs a pattern


def test_url_doctor_reports_ok(tmp_path):
    from iris.wakes import doctor_lines
    write_rules(tmp_path, [url_rule(tmp_path)])
    assert doctor_lines(wake_config(tmp_path)) == ["wakes: 1 rules ok"]


def test_wake_http_timeout_config_knob(tmp_path, monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_WAKE_HTTP_TIMEOUT", "30")
    assert Config.from_env(dotenv=tmp_path / "none.env").wake_http_timeout == 30.0
    monkeypatch.delenv("IRIS_WAKE_HTTP_TIMEOUT")
    assert Config.from_env(dotenv=tmp_path / "none.env").wake_http_timeout == 15.0
