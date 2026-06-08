"""Tests for splitting replies to per-platform message limits."""

from __future__ import annotations

from iris.textutil import FENCE, chunk_text


def _fence_count(s: str) -> int:
    return sum(1 for line in s.splitlines() if line.strip().startswith(FENCE))


def test_short_text_is_one_piece():
    assert chunk_text("hello", 100) == ["hello"]


def test_empty_text_is_no_pieces():
    assert chunk_text("", 100) == []
    assert chunk_text(None, 100) == []


def test_plain_text_splits_under_limit_without_loss():
    plain = "\n".join(f"line {i}" for i in range(200))
    pieces = chunk_text(plain, 200)
    assert len(pieces) > 1
    assert all(len(p) <= 200 for p in pieces)
    assert "".join(pieces).replace("\n", "") == plain.replace("\n", "")


def test_long_line_is_hard_split():
    pieces = chunk_text("x" * 500, 100)
    assert all(len(p) <= 100 for p in pieces)
    assert "".join(pieces) == "x" * 500


def test_code_fence_stays_balanced_across_pieces():
    code = "```python\n" + "\n".join(f"    x{i} = {i}" for i in range(120)) + "\n" + FENCE
    pieces = chunk_text(code, 300)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece) <= 300
        assert _fence_count(piece) % 2 == 0  # no dangling fence in any message


def test_split_fence_carries_the_language():
    code = "```python\n" + "\n".join(f"    x{i} = {i}" for i in range(120)) + "\n" + FENCE
    pieces = chunk_text(code, 300)
    # every continuation piece reopens the block with its language
    assert all(p.lstrip().startswith("```python") for p in pieces)


def test_split_fence_preserves_all_code_lines():
    code = "```python\n" + "\n".join(f"    x{i} = {i}" for i in range(120)) + "\n" + FENCE
    joined = "".join(chunk_text(code, 300))
    code_lines = [line for line in joined.splitlines() if line.strip().startswith("x")]
    assert len(code_lines) == 120
