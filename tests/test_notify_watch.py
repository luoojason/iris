"""Tests for the command-wrapper watcher (subprocess + Discord faked)."""

from __future__ import annotations

from iris.config import Config
from iris.notify import watch_cmd


def fake_runner(result):
    # result is (exit_code, duration_s, tail)
    return lambda argv: result


def collect_sender(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def test_exit_code_passthrough_and_quick_success_is_silent():
    cfg = Config(discord_token="t", notify_channel="123", watch_min_seconds=30)
    sent = []
    rc = watch_cmd.watch(["true"], cfg, runner=fake_runner((0, 2.0, "")),
                         sender=collect_sender(sent))
    assert rc == 0
    assert sent == []  # quick success dropped, nothing delivered


def test_long_success_notifies_with_template():
    cfg = Config(discord_token="t", notify_channel="123", watch_min_seconds=30)
    sent = []
    rc = watch_cmd.watch(["build.sh"], cfg, name="build",
                         runner=fake_runner((0, 45.0, "")),
                         sender=collect_sender(sent))
    assert rc == 0
    assert sent == [("123", "done: build passed in 45s", "t")]


def test_failure_uses_driver_and_delivers():
    cfg = Config(discord_token="t", notify_channel="123")
    sent = []

    class FakeResult:
        text = "Migration failed, sir."
        is_error = False

    class FakeDriver:
        def run(self, prompt, session_id=None, model=None):
            return FakeResult()

    rc = watch_cmd.watch(["deploy.sh"], cfg, name="deploy",
                         runner=fake_runner((1, 5.0, "ERR: boom")),
                         driver_factory=lambda: FakeDriver(),
                         sender=collect_sender(sent))
    assert rc == 1
    assert sent == [("123", "Migration failed, sir.", "t")]


def test_falls_back_to_print_when_no_delivery(capsys):
    cfg = Config()  # no token or channel
    rc = watch_cmd.watch(["job.sh"], cfg, name="job",
                         runner=fake_runner((1, 5.0, "boom")),
                         driver_factory=lambda: None)
    assert rc == 1
    assert "failed: job exited 1 after 5s" in capsys.readouterr().out
