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


def test_failure_uses_driver_and_delivers(tmp_path):
    # usage_file under tmp_path so the recording driver does not write
    # iris-usage.json into the repo root when the suite runs from there.
    cfg = Config(discord_token="t", notify_channel="123",
                 usage_file=str(tmp_path / "usage.json"))
    sent = []

    class FakeResult:
        text = "Migration failed, sir."
        is_error = False

    class FakeDriver:
        def run(self, prompt, session_id=None, model=None, conversation_id=None):
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


def test_run_command_runs_a_real_subprocess():
    rc, duration_s, tail = watch_cmd.run_command(["sh", "-c", "printf hello; exit 3"])
    assert rc == 3
    assert "hello" in tail
    assert duration_s >= 0


def test_failure_triage_skips_the_model_when_the_credit_guard_is_parked(tmp_path):
    from iris.config import Config
    from iris.notify.watch_cmd import watch
    from iris.usage import UsageLedger

    class Turn:
        cost_usd = 9.9
        context_tokens = 0

    config = Config(usage_file=str(tmp_path / "u.json"), usage_budget_usd=10.0,
                    usage_park_at=95.0, notify_channel="chan", discord_token="tok")
    UsageLedger(config.usage_file).record("chat", Turn())
    built = []

    def factory():
        built.append(True)

        class D:
            def run(self, *a, **kw):
                raise AssertionError("the model must not be called at park level")
        return D()

    sent = []
    rc = watch(["false"], config, runner=lambda argv: (1, 2.0, "boom"),
               driver_factory=factory,
               sender=lambda channel, text, token: sent.append(text) or True)
    assert rc == 1
    assert built == []  # the triage driver was never built
    assert sent and "failed" in sent[0]  # templated line still delivered


def test_watch_folds_completion_into_the_inbox_when_asked(tmp_path):
    from iris.config import Config
    from iris.inbox import Inbox
    from iris.notify.watch_cmd import watch
    config = Config(inbox_file=str(tmp_path / "inbox.json"), notify_channel="", discord_token="")
    watch(["echo", "hi"], config, name="build-x", fold=True,
          runner=lambda argv: (0, 30.0, "done"))
    notes = Inbox(config.inbox_file).drain()
    assert notes and "build-x" in notes[0] and "finished" in notes[0].lower()


def test_watch_folds_a_failure_too(tmp_path):
    from iris.config import Config
    from iris.inbox import Inbox
    from iris.notify.watch_cmd import watch
    config = Config(inbox_file=str(tmp_path / "inbox.json"), notify_channel="", discord_token="")
    watch(["false"], config, name="build-y", fold=True,
          runner=lambda argv: (1, 5.0, "boom"))
    notes = Inbox(config.inbox_file).drain()
    assert notes and "failed" in notes[0].lower()


def test_watch_does_not_fold_by_default(tmp_path):
    from iris.config import Config
    from iris.inbox import Inbox
    from iris.notify.watch_cmd import watch
    config = Config(inbox_file=str(tmp_path / "inbox.json"))
    watch(["echo", "hi"], config, runner=lambda argv: (0, 1.0, "hi"))
    assert Inbox(config.inbox_file).drain() == []


def test_watch_enqueues_resume_when_enabled_and_asked(tmp_path):
    from iris.autoresume import ResumeQueue
    from iris.config import Config
    from iris.notify.watch_cmd import watch
    config = Config(inbox_file=str(tmp_path / "inbox.json"),
                    resume_queue_file=str(tmp_path / "resume.json"),
                    auto_resume=True, home_channel="555",
                    notify_channel="", discord_token="")
    watch(["build.sh"], config, name="build-z", fold=True, resume=True,
          runner=lambda argv: (0, 40.0, "all done"))
    items = ResumeQueue(config.resume_queue_file).drain()
    assert len(items) == 1
    assert items[0]["conversation_id"] == "discord:555"
    assert "build-z" in items[0]["prompt"]


def test_watch_does_not_enqueue_resume_when_master_flag_off(tmp_path):
    from iris.autoresume import ResumeQueue
    from iris.config import Config
    from iris.notify.watch_cmd import watch
    config = Config(inbox_file=str(tmp_path / "inbox.json"),
                    resume_queue_file=str(tmp_path / "resume.json"),
                    auto_resume=False, home_channel="555",
                    notify_channel="", discord_token="")
    watch(["build.sh"], config, name="build-z", fold=True, resume=True,
          runner=lambda argv: (0, 40.0, "done"))
    assert ResumeQueue(config.resume_queue_file).drain() == []  # master flag gates it
