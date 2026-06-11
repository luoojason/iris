"""Tests for the owner-registered repo workspaces (iris/workspaces.py)."""

from __future__ import annotations

import os

import pytest

from iris.workspaces import (
    ARTIFACT_MAX_BYTES,
    ARTIFACT_MAX_FILES,
    WorkspaceStore,
    collect_artifacts,
    parse_artifact_lines,
)


def make_store(tmp_path):
    return WorkspaceStore(tmp_path / "workspaces.json")


# -- registry ----------------------------------------------------------------


def test_add_list_resolve_remove(tmp_path):
    store = make_store(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    store.add("myrepo", str(repo))
    assert store.list() == {"myrepo": str(repo.resolve())}
    assert store.resolve("myrepo") == str(repo.resolve())
    assert store.remove("myrepo") is True
    assert store.resolve("myrepo") is None
    assert store.remove("myrepo") is False


def test_add_stores_the_resolved_path(tmp_path):
    store = make_store(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store.add("ws", str(link))
    assert store.resolve("ws") == str(real.resolve())


def test_add_rejects_bad_names(tmp_path):
    store = make_store(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    for bad in ("", "Has-Caps", "with space", "-leading", "a" * 33, "../sneak", "a/b"):
        with pytest.raises(ValueError):
            store.add(bad, str(repo))


def test_add_rejects_missing_or_file_paths(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add("gone", str(tmp_path / "does-not-exist"))
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        store.add("afile", str(f))


def test_registry_persists_across_instances(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_store(tmp_path).add("ws", str(repo))
    assert make_store(tmp_path).resolve("ws") == str(repo.resolve())


def test_corrupt_registry_starts_empty(tmp_path):
    path = tmp_path / "workspaces.json"
    path.write_text("{broken", encoding="utf-8")
    assert WorkspaceStore(path).list() == {}


# -- ARTIFACT: hand-back -----------------------------------------------------


def test_parse_artifact_lines_finds_relative_names():
    report = (
        "Did the work.\n"
        "ARTIFACT: out/report.md\n"
        "Some prose.\n"
        "ARTIFACT: clips/final.mp4\n"
    )
    assert parse_artifact_lines(report) == ["out/report.md", "clips/final.mp4"]


def test_parse_artifact_lines_dedupes_and_strips():
    report = "ARTIFACT:  a.txt \nARTIFACT: a.txt\n"
    assert parse_artifact_lines(report) == ["a.txt"]


def test_collect_artifacts_returns_contained_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    f = ws / "out" / "report.md"
    f.write_text("done", encoding="utf-8")
    files, problems = collect_artifacts("ARTIFACT: out/report.md", str(ws))
    assert files == [str(f.resolve())]
    assert problems == []


def test_collect_artifacts_rejects_escapes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    (ws / "link.txt").symlink_to(outside)
    report = (
        "ARTIFACT: ../secret.txt\n"
        "ARTIFACT: /etc/passwd\n"
        "ARTIFACT: link.txt\n"
        "ARTIFACT: missing.txt\n"
    )
    files, problems = collect_artifacts(report, str(ws))
    assert files == []
    assert len(problems) == 4  # every rejection is named, never silent


def test_collect_artifacts_caps_file_count(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    lines = []
    for n in range(ARTIFACT_MAX_FILES + 2):
        f = ws / f"f{n}.txt"
        f.write_text("x", encoding="utf-8")
        lines.append(f"ARTIFACT: f{n}.txt")
    files, problems = collect_artifacts("\n".join(lines), str(ws))
    assert len(files) == ARTIFACT_MAX_FILES
    assert len(problems) == 2
    assert all("cap" in p for p in problems)


def test_collect_artifacts_caps_total_bytes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    big = ws / "big.bin"
    big.write_bytes(b"x" * (ARTIFACT_MAX_BYTES - 10))
    small = ws / "small.bin"
    small.write_bytes(b"y" * 100)
    files, problems = collect_artifacts("ARTIFACT: big.bin\nARTIFACT: small.bin", str(ws))
    assert files == [str(big.resolve())]
    assert len(problems) == 1 and "small.bin" in problems[0]


def test_collect_artifacts_without_workspace_reports_unresolvable():
    files, problems = collect_artifacts("ARTIFACT: out.txt", None)
    assert files == []
    assert len(problems) == 1 and "out.txt" in problems[0]


def test_name_validation_is_shared():
    from iris.workspaces import valid_name

    assert valid_name("myrepo")
    assert valid_name("a1-b_2")
    assert not valid_name("Nope")
    assert not valid_name("")


# -- CLI ----------------------------------------------------------------------


def test_cli_workspaces_roundtrip(tmp_path, monkeypatch, capsys):
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IRIS_WORKSPACES_FILE", str(tmp_path / "ws.json"))

    assert main(["workspaces", "add", "myrepo", str(repo)]) == 0
    assert main(["workspaces", "list"]) == 0
    out = capsys.readouterr().out
    assert "myrepo" in out and str(repo.resolve()) in out
    assert main(["workspaces", "remove", "myrepo"]) == 0
    assert main(["workspaces", "remove", "myrepo"]) == 1  # already gone


def test_cli_workspaces_add_rejects_bad_input(tmp_path, monkeypatch, capsys):
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_WORKSPACES_FILE", str(tmp_path / "ws.json"))
    assert main(["workspaces", "add", "BadName", str(tmp_path)]) == 2
    assert main(["workspaces", "add", "ok", str(tmp_path / "missing")]) == 2
    err = capsys.readouterr()
    assert "BadName" in err.out or "name" in err.out


def test_cli_workspaces_list_empty(tmp_path, monkeypatch, capsys):
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_WORKSPACES_FILE", str(tmp_path / "ws.json"))
    assert main(["workspaces", "list"]) == 0
    assert "no workspaces" in capsys.readouterr().out.lower()
