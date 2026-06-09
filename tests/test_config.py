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
