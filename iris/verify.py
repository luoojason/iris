"""Independent verification of a finished job's result.

Before a background job reports "done", an optional second model — cheap,
tool-less, with no stake in the outcome — rules on whether the report actually
satisfies what was asked. The job worker cannot wave its own work through; a
skeptical reviewer has to agree it landed.

The verdict only ANNOTATES, it never suppresses: a job's result is always
delivered to the owner. A failed verification prepends a warning so the owner
knows to check; an unreadable verdict or a judge that errors fails open to
"couldn't verify" (no false all-clear, no blocked report). Off by default
(IRIS_JOB_VERIFY); reuses the cheap judge model. See
docs/superpowers/specs/2026-06-14-goal-loop-design.md for the sibling pattern.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger("iris.verify")

VERIFY_PROMPT = (
    "A background worker was given this task:\n\n"
    "TASK: {instructions}\n\n"
    "It reported this result:\n\n{report}\n\n"
    "You are an independent reviewer with no stake in the outcome. Did the worker "
    "actually accomplish the task as asked — not merely attempt it, claim it, or do "
    "something adjacent? Reply with ONE line beginning with exactly one of:\n"
    "PASS: <short reason> — the result satisfies the task.\n"
    "FAIL: <short reason> — it does not (incomplete, wrong, or off-target).\n"
    "Judge only what the report shows; be skeptical of vague or unsupported claims."
)


def parse_verify(text: str) -> dict:
    """A reviewer reply into ``{"ok": True|False|None, "reason": str}``.

    ``True`` only on an explicit PASS, ``False`` on an explicit FAIL; anything
    unreadable is ``None`` ("unsure") so a garbled reply is never a silent PASS.
    Scans for the first line that starts with the token (leading prose tolerated).
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        upper = line.upper()
        for token, ok in (("PASS", True), ("FAIL", False)):
            if _starts_with_word(upper, token):
                reason = line.split(":", 1)[1].strip() if ":" in line else ""
                return {"ok": ok, "reason": reason}
    return {"ok": None, "reason": "couldn't read a verdict from the reviewer"}


def _starts_with_word(text: str, token: str) -> bool:
    """True if ``text`` begins with ``token`` as a whole word, so 'PASSING' and
    'FAILED' do not read as the verdict 'PASS'/'FAIL'."""
    if not text.startswith(token):
        return False
    rest = text[len(token):]
    return not (rest and rest[0].isalpha())


def _default_judge(config) -> Callable[[str, str], dict]:
    from .driver import ClaudeDriver

    model = (getattr(config, "job_verify_model", "") or
             getattr(config, "goal_judge_model", "") or "claude-haiku-4-5")

    def judge(instructions: str, report: str) -> dict:
        driver = ClaudeDriver(
            claude_bin=config.claude_bin, model=model,
            restrict_builtin_tools=True,  # tool-less: it reviews, it does not act
            timeout=getattr(config, "turn_timeout", 300.0),
        )
        result = driver.run(VERIFY_PROMPT.format(instructions=instructions, report=report))
        if result.is_error:
            raise RuntimeError(result.error or "verification turn failed")
        return parse_verify(result.text)

    return judge


def verify_result(config, instructions: str, report: str, *,
                  judge: Optional[Callable[[str, str], dict]] = None) -> dict:
    """Rule on whether ``report`` satisfies ``instructions``. Never raises.

    Returns ``{"ok": True|False|None, "reason": str}``. A judge that errors fails
    open to ``ok=None`` so verification can never block or crash a job's report.
    """
    judge = judge or _default_judge(config)
    try:
        return judge(instructions, report)
    except Exception as exc:
        log.warning("job verification unavailable: %s", exc)
        return {"ok": None, "reason": "verification unavailable"}
