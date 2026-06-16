"""Canonical test doubles shared across the suite.

The one sanctioned exception to the project's no-conftest rule. A ``ClaudeDriver``
signature change used to mean editing a hand-rolled fake in every test file; the
common ones live here so it is a single edit. Genuinely domain-specific fakes
(cost-bearing results, single-shot drivers, probes) still live in their own test
file next to the test that needs them.

Importable directly (``from fakes import FakeDriver``): pytest's default import
mode puts the tests directory on ``sys.path``.
"""

from __future__ import annotations

from iris.driver import ClaudeResult
from iris.sessions import SessionStore


class FakeDriver:
    """One-shot driver that returns queued results and records every call.

    ``calls`` holds ``(prompt, session_id)`` tuples; ``model_calls`` holds the
    model argument per call (so routing assertions are easy). ``model`` is the
    driver's default model, read by the router.
    """

    def __init__(self, results, model=None):
        self.results = list(results)
        self.calls: list[tuple[str, str | None]] = []
        self.model_calls: list[str | None] = []
        self.model = model

    def run(self, prompt, session_id=None, model=None, conversation_id=None) -> ClaudeResult:
        self.calls.append((prompt, session_id))
        self.model_calls.append(model)
        return self.results.pop(0)


class FakeStreamTurn:
    """A streaming turn that yields one queued result and reports finished."""

    def __init__(self, result: ClaudeResult):
        self._result = result
        self.open = False
        self.strays: list[ClaudeResult] = []

    def wait_primary(self, timeout=None):
        return self._result

    def wait_finished(self, timeout=None):
        return True

    def inject(self, text):
        return False


class FakeStreamDriver:
    """Hands back a FakeStreamTurn per ``start()``, recording the prompts."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def start(self, prompt, session_id=None, model=None) -> FakeStreamTurn:
        self.prompts.append(prompt)
        return FakeStreamTurn(self.results.pop(0))


def tmp_store(tmp_path, name: str = "s.json") -> SessionStore:
    """A SessionStore backed by a file under a pytest tmp_path."""
    return SessionStore(tmp_path / name)
