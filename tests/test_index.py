"""The root -> task index is a cache; task.json stays the source of truth."""

import json
import os
import shutil

import pytest

from ctui import tasks as T


@pytest.fixture
def counted(monkeypatch):
    """Count task.json reads, so 'does not open every task' is actually asserted."""
    calls = {"n": 0}
    original = T.Task.load

    def _load(task_dir):
        calls["n"] += 1
        return original(task_dir)

    monkeypatch.setattr(T.Task, "load", staticmethod(_load))
    return calls


def _settle(tasks_repo, host="testhost"):
    """Move the index's mtime safely ahead of every task.json.

    This is the steady state in real use: the index was written when a task was
    created, and lookups happen later. Straight after creation the newest
    task.json shares a timestamp tick with the index and is deliberately
    re-read (see get_index's racy-entry handling), which would otherwise make
    read counts depend on timing.
    """
    T.get_index(tasks_repo, host)
    path = T.index_path(tasks_repo, host)
    ahead = path.stat().st_mtime_ns + 2_000_000_000
    os.utime(path, ns=(ahead, ahead))


def _make_racy(tasks_repo, task, host="testhost"):
    """Force the racy condition an edit can genuinely hide in.

    Two things must hold at once: task.json's mtime still equals the value the
    index recorded for it (so the equality check sees nothing), and it is not
    strictly older than the index file (so it fell in the same tick).
    """
    recorded = T.read_index(tasks_repo, host).entries[task.task_id]
    os.utime(task.dir / "task.json", ns=(recorded, recorded))
    os.utime(T.index_path(tasks_repo, host), ns=(recorded, recorded))


def _projects(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / "projects" / f"p{i}"
        p.mkdir(parents=True)
        out.append(p)
    return out


# ---- shape ---------------------------------------------------------

def test_create_task_writes_the_index(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    path = T.index_path(tasks_repo)
    assert path == tasks_repo / "testhost" / "index.json"

    raw = json.loads(path.read_text())
    assert raw["version"] == T.INDEX_VERSION
    assert raw["hostname"] == "testhost"
    assert raw["roots"] == {str(project.resolve()): [task.task_id]}
    assert list(raw["entries"]) == [task.task_id]


def test_index_is_per_host(tasks_repo, tmp_path):
    a, b = _projects(tmp_path, 2)
    here = T.create_task(tasks_repo, a, "here")
    there = T.create_task(tasks_repo, b, "there", host="otherhost")

    mine = T.read_index(tasks_repo, "testhost")
    theirs = T.read_index(tasks_repo, "otherhost")
    assert set(mine.entries) == {here.task_id}
    assert set(theirs.entries) == {there.task_id}
    assert T.index_path(tasks_repo, "otherhost").exists()


def test_several_tasks_share_a_root_entry(tasks_repo, project):
    one = T.create_task(tasks_repo, project, "one")
    two = T.create_task(tasks_repo, project, "two")
    roots = T.read_index(tasks_repo).roots
    assert sorted(roots[str(project.resolve())]) == sorted([one.task_id, two.task_id])


def test_index_file_is_not_mistaken_for_a_task(tasks_repo, project):
    T.create_task(tasks_repo, project, "t")
    assert T.index_path(tasks_repo).exists()
    assert len(T.load_tasks(tasks_repo)) == 1
    assert all(d.name.startswith("TASK_") for d in T.iter_task_dirs(tasks_repo))


# ---- the point: lookups stop reading everything --------------------

def test_miss_reads_no_task_json(tasks_repo, tmp_path, counted):
    for p in _projects(tmp_path, 12):
        T.create_task(tasks_repo, p, "t")
    fresh = tmp_path / "unrelated"
    fresh.mkdir()

    _settle(tasks_repo)

    counted["n"] = 0
    assert T.tasks_for_root(tasks_repo, fresh) == []
    assert counted["n"] == 0


def test_hit_reads_only_the_matching_task(tasks_repo, tmp_path, counted):
    projects = _projects(tmp_path, 12)
    wanted = T.create_task(tasks_repo, projects[7], "wanted")
    for p in projects[:7] + projects[8:]:
        T.create_task(tasks_repo, p, "other")
    _settle(tasks_repo)

    counted["n"] = 0
    found = T.tasks_for_root(tasks_repo, projects[7])
    assert [t.task_id for t in found] == [wanted.task_id]
    assert counted["n"] == 1


def test_ancestor_walk_reads_only_the_nearest_match(tasks_repo, tmp_path, counted):
    projects = _projects(tmp_path, 10)
    nearest = T.create_task(tasks_repo, projects[3], "nearest")
    for p in projects[:3] + projects[4:]:
        T.create_task(tasks_repo, p, "other")
    deep = projects[3] / "a" / "b" / "c"
    deep.mkdir(parents=True)
    _settle(tasks_repo)

    counted["n"] = 0
    found = T.find_tasks_from_cwd(tasks_repo, deep)
    assert [t.task_id for t in found] == [nearest.task_id]
    assert counted["n"] == 1


def test_creating_a_task_does_not_rebuild_the_whole_index(tasks_repo, tmp_path, counted):
    for p in _projects(tmp_path, 20):
        T.create_task(tasks_repo, p, "t")
    later = tmp_path / "later"
    later.mkdir()

    counted["n"] = 0
    T.create_task(tasks_repo, later, "later")
    assert counted["n"] == 0          # incremental, not a full re-read


# ---- self-healing --------------------------------------------------

def test_missing_index_is_built_on_lookup(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.index_path(tasks_repo).unlink()

    found = T.tasks_for_root(tasks_repo, project)
    assert [t.task_id for t in found] == [task.task_id]
    assert T.index_path(tasks_repo).exists()


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 999, "roots": {}}),
    json.dumps({"version": T.INDEX_VERSION, "roots": "nonsense"}),
    json.dumps({"version": T.INDEX_VERSION, "roots": {}}),  # no entries key
])
def test_unusable_index_is_rebuilt(tasks_repo, project, content):
    task = T.create_task(tasks_repo, project, "t")
    T.index_path(tasks_repo).write_text(content)

    found = T.tasks_for_root(tasks_repo, project)
    assert [t.task_id for t in found] == [task.task_id]
    assert json.loads(T.index_path(tasks_repo).read_text())["version"] == T.INDEX_VERSION


def test_task_arriving_externally_is_picked_up(tasks_repo, tmp_path):
    """A task pulled in by `ctui --sync` was never seen by this host's index."""
    a, b = _projects(tmp_path, 2)
    T.create_task(tasks_repo, a, "known")

    # Simulate `git pull` landing a task directory the index has never seen:
    # written straight to disk, with no call into ctui to keep the index current.
    landed_id = "TASK_20250101_120000"
    landed = tasks_repo / "testhost" / landed_id
    landed.mkdir()
    (landed / "root").symlink_to(b, target_is_directory=True)
    (landed / "task.json").write_text(json.dumps({
        "name": "arrived", "id": landed_id, "hostname": "testhost",
        "root": str(b.resolve()), "created_at": "2025-01-01T12:00:00+00:00",
        "sessions": [],
    }))
    assert landed_id not in T.read_index(tasks_repo).entries

    found = T.tasks_for_root(tasks_repo, b)
    assert [t.task_id for t in found] == [landed_id]
    assert landed_id in T.read_index(tasks_repo).entries


def test_removed_task_is_dropped(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    shutil.rmtree(task.dir)

    assert T.tasks_for_root(tasks_repo, project) == []
    assert T.read_index(tasks_repo).entries == {}


def _repoint(task, new_root):
    meta = json.loads((task.dir / "task.json").read_text())
    meta["root"] = str(new_root.resolve())
    (task.dir / "task.json").write_text(json.dumps(meta))


def test_task_json_wins_when_the_index_disagrees(tasks_repo, tmp_path):
    """Hand-editing a root in task.json must not be overridden by the cache."""
    a, b = _projects(tmp_path, 2)
    task = T.create_task(tasks_repo, a, "t")
    _repoint(task, b)

    # The index still points the old root at this task; task.json disagrees.
    assert T.tasks_for_root(tasks_repo, a) == []
    assert [t.task_id for t in T.tasks_for_root(tasks_repo, b)] == [task.task_id]


def test_edited_root_is_found_without_touching_the_old_path_first(tasks_repo, tmp_path):
    """Querying only the *new* root must work.

    A directory-count staleness check cannot see this edit (the count is
    unchanged) and nothing about the new root looks stale, so the answer would
    silently be wrong.
    """
    a, b = _projects(tmp_path, 2)
    task = T.create_task(tasks_repo, a, "t")
    _repoint(task, b)

    assert [t.task_id for t in T.tasks_for_root(tasks_repo, b)] == [task.task_id]


def test_edit_hidden_by_an_identical_mtime_is_still_found(tasks_repo, tmp_path):
    """The racy case, forced: an edit that leaves mtime untouched.

    Kernel timestamps advance on a coarse tick, so this happens for real when an
    external write lands in the same tick as ctui's own. mtime comparison alone
    cannot see it; re-reading entries not strictly older than the index can.
    """
    a, b = _projects(tmp_path, 2)
    task = T.create_task(tasks_repo, a, "t")
    _repoint(task, b)
    _make_racy(tasks_repo, task)

    stamp = (task.dir / "task.json").stat().st_mtime_ns
    assert stamp == T.read_index(tasks_repo).entries[task.task_id]   # invisible
    assert [t.task_id for t in T.tasks_for_root(tasks_repo, b)] == [task.task_id]
    assert T.tasks_for_root(tasks_repo, a) == []


def test_racy_refresh_settles_after_one_lookup(tasks_repo, tmp_path, counted):
    """The re-read is one-time: the rewrite moves the index past the task."""
    a, _ = _projects(tmp_path, 2)
    task = T.create_task(tasks_repo, a, "t")
    _make_racy(tasks_repo, task)

    T.tasks_for_root(tasks_repo, a)
    counted["n"] = 0
    assert [t.task_id for t in T.tasks_for_root(tasks_repo, a)] == [task.task_id]
    assert counted["n"] == 1          # the match itself, no refresh re-read


def test_edited_task_json_refreshes_the_stored_mtime(tasks_repo, tmp_path, counted):
    a, b = _projects(tmp_path, 2)
    task = T.create_task(tasks_repo, a, "t")
    _repoint(task, b)
    T.tasks_for_root(tasks_repo, b)          # triggers the rebuild
    _settle(tasks_repo)

    counted["n"] = 0
    T.tasks_for_root(tasks_repo, b)          # must now be a cache hit
    assert counted["n"] == 1                 # only the matching task, no rebuild


def test_unreadable_task_does_not_cause_repeated_rebuilds(tasks_repo, project, counted):
    T.create_task(tasks_repo, project, "good")
    broken = tasks_repo / "testhost" / "TASK_19990101_000000"
    broken.mkdir(parents=True)
    (broken / "task.json").write_text("{oops")

    fresh = project.parent / "fresh"
    fresh.mkdir()
    T.tasks_for_root(tasks_repo, fresh)        # first call rebuilds
    _settle(tasks_repo)
    counted["n"] = 0
    T.tasks_for_root(tasks_repo, fresh)        # second must not
    assert counted["n"] == 0


def test_index_keys_collapse_symlinked_roots(tasks_repo, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    task = T.create_task(tasks_repo, link, "t")
    assert list(T.read_index(tasks_repo).roots) == [str(real)]
    # findable by either path
    assert [t.task_id for t in T.tasks_for_root(tasks_repo, real)] == [task.task_id]
    assert [t.task_id for t in T.tasks_for_root(tasks_repo, link)] == [task.task_id]


def test_build_index_recovers_a_repo_with_no_index(tasks_repo, tmp_path):
    projects = _projects(tmp_path, 3)
    ids = [T.create_task(tasks_repo, p, "t").task_id for p in projects]
    T.index_path(tasks_repo).unlink()

    index = T.build_index(tasks_repo)
    assert set(index.entries) == set(ids)
    for p, task_id in zip(projects, ids):
        assert index.task_ids_for(p) == [task_id]
