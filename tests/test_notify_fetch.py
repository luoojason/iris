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
