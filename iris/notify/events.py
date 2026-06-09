"""The normalized event every watcher emits and the gate and composer consume."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Event:
    source: str          # which watcher produced it, e.g. "command"
    kind: str            # what happened, e.g. "finished"
    title: str           # human label, e.g. the command or its --name
    exit_code: int
    duration_s: float
    tail: str = ""       # last lines of output, for failure triage
    urgency: str = "normal"   # "normal" | "high"
    detail: str = ""
