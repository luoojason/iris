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


def test_metrics_file_defaults_empty(monkeypatch):
    monkeypatch.delenv("IRIS_METRICS_FILE", raising=False)
    cfg = Config.from_env(dotenv="/nonexistent.env")
    assert cfg.metrics_file == ""


def test_metrics_file_from_env(monkeypatch):
    monkeypatch.setenv("IRIS_METRICS_FILE", "/tmp/iris-metrics.jsonl")
    cfg = Config.from_env(dotenv="/nonexistent.env")
    assert cfg.metrics_file == "/tmp/iris-metrics.jsonl"
