"""Scoring and ranking for durable notes: the brain of the memory tool.

The memory MCP server stores notes in a flat JSON file. Recall used to be a raw
substring match, which loses on two fronts: it cannot rank (a one-word match and
a perfect match come back in the same arbitrary order), and it cannot weigh a
note the user marked important over passing chatter.

This module is the pure, side-effect-free core that fixes both. It is split out
from the server (which does the I/O) so the ranking logic is easy to test and
reason about, the same way ``router.choose_model`` is a pure function the agent
core leans on. Nothing here reads a file or calls a model.

A note's rank combines four signals, in deliberate order of trust:

* **pinned** — a human floor. A pinned note is always near the top. No automatic
  signal can demote it and none can promote an unpinned note past it.
* **relevance** — term overlap between the query and the note's text and tags.
  Objective: it is just counting shared words.
* **importance** — a human 1-5 weight set when the note is saved.
* **recency** — newer notes edge out older ones. Objective: it is just the clock.
* **usefulness** — a bounded nudge from ``use_count``, which is incremented
  *only* by an explicit ``mark_useful`` call the model makes when a recalled note
  actually informed its reply. It breaks ties; it can never dominate the human
  and objective signals above it. This is the one learned signal, and it is kept
  deliberately weak so a note cannot bootstrap its own rank: matching a query
  does not raise it, only being judged useful afterward does.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

# Default importance for a note saved without one. Mid-scale, so an unrated note
# sits below something explicitly flagged important and above something flagged
# trivial.
DEFAULT_IMPORTANCE = 3
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Very common words carry no retrieval signal; dropping them keeps "what is the
# plan" from matching every note that contains "the".
_STOPWORDS = frozenset(
    "a an the is are was were be been being of to in on at for and or but if "
    "it its this that these those i you he she we they me my your our do does "
    "did with as by from about what when where who how".split()
)


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words, stopwords removed."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOPWORDS]


def _parse_ts(value: Optional[str]) -> Optional[float]:
    """Best-effort epoch seconds from a stored timestamp string."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        try:
            # 'Z' is only accepted by fromisoformat from Python 3.11; normalize
            # it so stored ISO timestamps parse on the 3.10 floor too.
            iso = value[:-1] + "+00:00" if isinstance(value, str) and value.endswith("Z") else value
            return datetime.fromisoformat(iso).timestamp()
        except (ValueError, TypeError):
            return None


def normalize(entry: dict) -> dict:
    """Fill a stored note's optional fields with defaults.

    Tolerates legacy notes saved before importance/pinned/usefulness existed, so
    a memory file from an older version keeps working untouched.
    """
    tags = entry.get("tags")
    if not isinstance(tags, list):
        tags = [t.strip() for t in str(tags or "").split(",") if t.strip()]
    importance = entry.get("importance", DEFAULT_IMPORTANCE)
    try:
        importance = max(1, min(5, int(importance)))
    except (TypeError, ValueError):
        importance = DEFAULT_IMPORTANCE
    try:
        use_count = max(0, int(entry.get("use_count", 0)))
    except (TypeError, ValueError):
        use_count = 0
    return {
        "id": entry.get("id"),
        "text": str(entry.get("text", "")),
        "tags": [str(t) for t in tags],
        "created_at": entry.get("created_at"),
        "importance": importance,
        "pinned": bool(entry.get("pinned", False)),
        "use_count": use_count,
        "last_used": entry.get("last_used"),
    }


def _recency_bonus(created_at: Optional[str], now_ts: float) -> float:
    """A small bump for newer notes that fades to zero over ~90 days."""
    created = _parse_ts(created_at)
    if created is None:
        return 0.0
    age_days = max(0.0, (now_ts - created) / 86400.0)
    return max(0.0, 3.0 * (1.0 - age_days / 90.0))


def relevance(entry: dict, query_tokens: list[str]) -> int:
    """How many distinct query terms appear in the note's text or tags."""
    if not query_tokens:
        return 0
    haystack = set(_tokens(entry.get("text", "")))
    for tag in entry.get("tags", []):
        haystack.update(_tokens(tag))
    return sum(1 for term in set(query_tokens) if term in haystack)


def score(entry: dict, query_tokens: list[str], now_ts: float) -> Optional[float]:
    """Rank score for one note against a query. ``None`` means 'drop it'.

    A note is dropped only when there is a query, it shares no term with it, and
    it is not pinned. With no query (a browse), nothing is dropped and ranking
    falls back to pinned > importance > recency > usefulness.
    """
    note = normalize(entry)
    hits = relevance(note, query_tokens)
    if query_tokens and hits == 0 and not note["pinned"]:
        return None

    s = 0.0
    if note["pinned"]:
        s += 1000.0  # human floor: always above any unpinned note
    s += hits * 10.0  # objective relevance, the dominant signal when querying
    s += note["importance"] * 2.0  # human weight
    s += _recency_bonus(note["created_at"], now_ts)  # objective freshness
    s += min(note["use_count"], 12) * 0.5  # learned tie-breaker, capped low
    return s


def pinned_digest(entries: list[dict], now_ts: float, max_bytes: int = 2400) -> str:
    """Render the pinned notes into a compact block for the system prompt.

    This is the always-loaded memory tier: pinned notes are the human floor of
    the ranking, so they are the ones worth paying for on every single turn.
    Everything else stays behind the recall tool. Whole notes only — one that
    would overflow the byte budget is skipped so a smaller one can still fit.
    Returns "" when nothing is pinned or the budget is zero.
    """
    if max_bytes <= 0:
        return ""
    pinned = [e for e in entries if isinstance(e, dict) and normalize(e)["pinned"]]
    if not pinned:
        return ""
    header = "Pinned memory (durable facts; rely on these without re-asking):"
    lines = [header]
    used = len(header.encode("utf-8"))
    for entry in rank(pinned, None, now_ts, limit=len(pinned)):
        text = " ".join(normalize(entry)["text"].split())
        if not text:
            continue
        line = f"- {text}"
        cost = len(line.encode("utf-8")) + 1  # the joining newline
        if used + cost > max_bytes:
            continue
        lines.append(line)
        used += cost
    return "\n".join(lines) if len(lines) > 1 else ""


def rank(entries: list[dict], query: Optional[str], now_ts: float, limit: int = 20) -> list[dict]:
    """Return the highest-scoring notes for a query, best first.

    Pure: it reads nothing and mutates nothing. The server calls this and does
    the formatting and any writes itself.
    """
    query_tokens = _tokens(query or "")
    scored: list[tuple[float, dict]] = []
    for entry in entries:
        s = score(entry, query_tokens, now_ts)
        if s is None:
            continue
        scored.append((s, entry))
    # Sort by score, then newest id as a stable tiebreaker so order is deterministic.
    scored.sort(key=lambda pair: (pair[0], pair[1].get("id") or 0), reverse=True)
    return [entry for _, entry in scored[: max(0, limit)]]
