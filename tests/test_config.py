"""Config loading tests."""

from __future__ import annotations

from iris.config import Config, load_dotenv, _split, _truthy


def test_split_handles_blanks_and_spacing():
    assert _split("a, b ,, c") == ["a", "b", "c"]
    assert _split("") == []
    assert _split(None) == []


def test_truthy():
    assert _truthy("true") and _truthy("1") and _truthy("YES") and _truthy("on")
    assert not _truthy("false") and not _truthy("") and not _truthy(None)


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("IRIS_MODEL=from-file\nIRIS_DISCORD_TOKEN=tok\n# comment\n", encoding="utf-8")
    monkeypatch.setenv("IRIS_MODEL", "from-real-env")
    # Hermetic: an operator-exported token (or one leaked by another test)
    # must not poison the "file fills the gap" assertion below.
    monkeypatch.delenv("IRIS_DISCORD_TOKEN", raising=False)
    load_dotenv(env_file)
    import os
    assert os.environ["IRIS_MODEL"] == "from-real-env"  # real env wins
    assert os.environ["IRIS_DISCORD_TOKEN"] == "tok"     # file fills the gap


def test_from_env_reads_values(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_DISCORD_TOKEN", "abc")
    monkeypatch.setenv("IRIS_ALLOWED_USER_IDS", "111, 222")
    monkeypatch.setenv("IRIS_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("IRIS_RESPOND_WITHOUT_MENTION", "true")
    monkeypatch.setenv("IRIS_TURN_TIMEOUT", "45")
    cfg = Config.from_env(dotenv=tmp_path / "does-not-exist.env")
    assert cfg.discord_token == "abc"
    assert cfg.allowed_user_ids == ["111", "222"]
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.respond_without_mention is True
    assert cfg.turn_timeout == 45.0


def test_from_env_defaults(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.discord_token == ""
    assert cfg.claude_bin == "claude"
    assert cfg.model is None
    assert cfg.permission_mode == "default"
    assert cfg.respond_without_mention is False
    # secure-by-default: the allowlist boundary and native-memory lock are on
    assert cfg.restrict_builtin_tools is True
    assert cfg.disable_auto_memory is True
    assert cfg.timeout_max_retries == 0  # timeouts report at once, do not block
    assert cfg.auto_resume is False  # autonomous resume is off by default
    assert cfg.auto_resume_max_per_day == 12


def test_from_env_reads_auto_resume_knobs(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_AUTO_RESUME", "true")
    monkeypatch.setenv("IRIS_AUTO_RESUME_MAX_PER_DAY", "5")
    monkeypatch.setenv("IRIS_RESUME_POLL_SECS", "7")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.auto_resume is True
    assert cfg.auto_resume_max_per_day == 5
    assert cfg.resume_poll_secs == 7.0


def test_from_env_reads_tool_boundary_flags(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_RESTRICT_BUILTIN_TOOLS", "false")
    monkeypatch.setenv("IRIS_DISABLE_AUTO_MEMORY", "no")
    monkeypatch.setenv("IRIS_TIMEOUT_RETRIES", "2")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.restrict_builtin_tools is False
    assert cfg.disable_auto_memory is False
    assert cfg.timeout_max_retries == 2


def test_flag_defaults_when_unset():
    from iris.config import _flag
    assert _flag(None, True) is True
    assert _flag(None, False) is False
    assert _flag("off", True) is False
    assert _flag("1", False) is True


def test_metrics_file_defaults_empty(monkeypatch):
    monkeypatch.delenv("IRIS_METRICS_FILE", raising=False)
    cfg = Config.from_env(dotenv="/nonexistent.env")
    assert cfg.metrics_file == ""


def test_metrics_file_from_env(monkeypatch):
    monkeypatch.setenv("IRIS_METRICS_FILE", "/tmp/iris-metrics.jsonl")
    cfg = Config.from_env(dotenv="/nonexistent.env")
    assert cfg.metrics_file == "/tmp/iris-metrics.jsonl"


def test_from_env_reads_notify_fields(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_NOTIFY_CHANNEL", "999")
    monkeypatch.setenv("IRIS_WATCH_MIN_SECONDS", "10")
    monkeypatch.setenv("IRIS_NOTIFY_PERSONA", "notify.md")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.notify_channel == "999"
    assert cfg.watch_min_seconds == 10.0
    assert cfg.notify_persona == "notify.md"


def test_notify_defaults():
    cfg = Config()
    assert cfg.notify_channel == ""
    assert cfg.watch_min_seconds == 30.0
    assert cfg.notify_persona is None


def test_from_env_reads_standing_orders_file(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_STANDING_ORDERS_FILE", "orders.md")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.standing_orders_file == "orders.md"


def test_standing_orders_file_defaults_to_none(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.standing_orders_file is None


def test_from_env_reads_memory_digest_knobs(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_MEMORY_FILE", "m.json")
    monkeypatch.setenv("IRIS_MEMORY_DIGEST_BYTES", "1200")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.memory_file == "m.json"
    assert cfg.memory_digest_bytes == 1200


def test_memory_digest_defaults(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.memory_file == "iris-memory.json"
    assert cfg.memory_digest_bytes == 2400


def test_browser_deny_tools_default_and_override(tmp_path, monkeypatch):
    import os as _os
    for key in list(_os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.browser_deny_tools == ["browser_evaluate", "browser_run_code_unsafe"]
    monkeypatch.setenv("IRIS_BROWSER_DENY_TOOLS", "browser_evaluate")
    cfg2 = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg2.browser_deny_tools == ["browser_evaluate"]
    monkeypatch.setenv("IRIS_BROWSER_DENY_TOOLS", "")  # explicit empty = deny none
    cfg3 = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg3.browser_deny_tools == []
