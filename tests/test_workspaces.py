"""WorkspaceStore: the owner-side binding of workspace names to checkouts.

The security boundary from the design spec lives here: the model only ever
speaks a workspace NAME, and this store is the single place names resolve to
filesystem paths. Only the owner writes it (local CLI), so a hostile prompt
cannot point a job at an arbitrary directory.
"""

from __future__ import annotations

import pytest

from iris.workspaces import WorkspaceStore


def make_store(tmp_path):
    return WorkspaceStore(tmp_path / "workspaces.json")


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    return repo


# -- add / get ---------------------------------------------------------------


def test_add_and_get_round_trip(tmp_path):
    store = make_store(tmp_path)
    repo = make_repo(tmp_path)
    store.add("geosql", str(repo))
    entry = store.get("geosql")
    assert entry is not None
    assert entry["path"] == str(repo.resolve())
    assert isinstance(entry["added_at"], float)


def test_add_resolves_relative_paths_to_absolute(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    store.add("repo", "repo")  # relative: must be stored resolved + absolute
    assert store.get("repo")["path"] == str((tmp_path / "repo").resolve())


def test_add_rebinding_a_name_updates_its_path(tmp_path):
    store = make_store(tmp_path)
    first = make_repo(tmp_path, "first")
    second = make_repo(tmp_path, "second")
    store.add("repo", str(first))
    store.add("repo", str(second))
    assert store.get("repo")["path"] == str(second.resolve())
    assert len(store.all()) == 1


def test_get_unknown_name_is_none(tmp_path):
    assert make_store(tmp_path).get("nope") is None


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "geosql", "my-repo-2", "0", "a" * 32])
def test_valid_names_accepted(tmp_path, name):
    store = make_store(tmp_path)
    repo = make_repo(tmp_path)
    store.add(name, str(repo))
    assert store.get(name)["path"] == str(repo.resolve())


@pytest.mark.parametrize("name", [
    "", "Geosql", "my_repo", "a.b", "a/b", "a b", "a" * 33, "..", "café",
    "repo\n", " geosql",
])
def test_invalid_names_rejected_with_a_friendly_message(tmp_path, name):
    store = make_store(tmp_path)
    repo = make_repo(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        store.add(name, str(repo))
    assert "a-z" in str(excinfo.value)  # tells the owner what a name may be
    assert store.all() == {}  # nothing was written


def test_nonexistent_path_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        store.add("repo", str(tmp_path / "missing"))
    assert "directory" in str(excinfo.value)
    assert store.all() == {}


def test_file_path_rejected(tmp_path):
    store = make_store(tmp_path)
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("hi")
    with pytest.raises(ValueError) as excinfo:
        store.add("repo", str(not_a_dir))
    assert "directory" in str(excinfo.value)
    assert store.all() == {}


# -- remove / all --------------------------------------------------------------


def test_remove_returns_true_then_false(tmp_path):
    store = make_store(tmp_path)
    store.add("repo", str(make_repo(tmp_path)))
    assert store.remove("repo") is True
    assert store.get("repo") is None
    assert store.remove("repo") is False


def test_all_is_sorted_by_name(tmp_path):
    store = make_store(tmp_path)
    repo = make_repo(tmp_path)
    for name in ("zeta", "alpha", "mid"):
        store.add(name, str(repo))
    assert list(store.all()) == ["alpha", "mid", "zeta"]


# -- persistence ----------------------------------------------------------------


def test_persists_across_reinstantiation(tmp_path):
    repo = make_repo(tmp_path)
    make_store(tmp_path).add("geosql", str(repo))
    reopened = make_store(tmp_path)
    assert reopened.get("geosql")["path"] == str(repo.resolve())


def test_corrupt_file_loads_empty_and_recovers(tmp_path):
    path = tmp_path / "workspaces.json"
    path.write_text("{not json", encoding="utf-8")
    store = WorkspaceStore(path)
    assert store.all() == {}
    store.add("repo", str(make_repo(tmp_path)))  # still writable afterwards
    assert list(store.all()) == ["repo"]


def test_non_dict_json_loads_empty(tmp_path):
    path = tmp_path / "workspaces.json"
    path.write_text('["a", "list"]', encoding="utf-8")
    assert WorkspaceStore(path).all() == {}
