"""The recently-accessed log behind `ctui --resume --recent`."""

import json

import pytest

from ctui import tasks as T


def _projects(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / "projects" / f"p{i}"
        p.mkdir(parents=True)
        out.append(p)
    return out


def test_log_starts_empty(tasks_repo):
    assert T.read_recent(tasks_repo) == []
    assert T.recent_tasks(tasks_repo) == []


def test_record_access_writes_the_log(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task)

    path = T.recent_path(tasks_repo)
    assert path == tasks_repo / "testhost" / "recent.json"
    raw = json.loads(path.read_text())
    assert raw["version"] == T.RECENT_VERSION
    assert raw["hostname"] == "testhost"
    assert raw["entries"][0]["task_id"] == task.task_id
    assert raw["entries"][0]["host"] == "testhost"
    assert raw["entries"][0]["at"]


def test_most_recent_first(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, f"t{i}")
             for i, p in enumerate(_projects(tmp_path, 3))]
    for task in tasks:
        T.record_access(tasks_repo, task)

    got = T.recent_tasks(tasks_repo)
    assert [t.task_id for t in got] == [t.task_id for t in reversed(tasks)]


def test_re_accessing_moves_to_front_without_duplicating(tasks_repo, tmp_path):
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a)
    T.record_access(tasks_repo, b)
    T.record_access(tasks_repo, a)

    got = T.recent_tasks(tasks_repo)
    assert [t.task_id for t in got] == [a.task_id, b.task_id]
    assert len(T.read_recent(tasks_repo)) == 2


def test_limit_is_respected(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 8)]
    for task in tasks:
        T.record_access(tasks_repo, task)

    assert len(T.recent_tasks(tasks_repo, limit=5)) == 5
    assert [t.task_id for t in T.recent_tasks(tasks_repo, limit=5)] == \
        [t.task_id for t in reversed(tasks)][:5]


def test_log_is_capped(tasks_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "RECENT_CAP", 3)
    for p in _projects(tmp_path, 5):
        T.record_access(tasks_repo, T.create_task(tasks_repo, p, "t"))
    assert len(T.read_recent(tasks_repo)) == 3


def test_deleted_task_is_skipped_not_reported(tasks_repo, tmp_path):
    import shutil
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a)
    T.record_access(tasks_repo, b)
    shutil.rmtree(b.dir)

    assert [t.task_id for t in T.recent_tasks(tasks_repo)] == [a.task_id]


def test_log_spans_hosts(tasks_repo, tmp_path):
    """A task resumed from another host is logged under its own host."""
    a, b = _projects(tmp_path, 2)
    mine = T.create_task(tasks_repo, a, "mine")
    theirs = T.create_task(tasks_repo, b, "theirs", host="otherhost")
    T.record_access(tasks_repo, theirs)
    T.record_access(tasks_repo, mine)

    # written by this host ...
    assert json.loads(T.recent_path(tasks_repo).read_text())["hostname"] == "testhost"
    assert not T.recent_path(tasks_repo, "otherhost").exists()
    # ... but resolves the other host's task correctly
    got = T.recent_tasks(tasks_repo)
    assert [(t.host, t.task_id) for t in got] == [
        ("testhost", mine.task_id), ("otherhost", theirs.task_id)
    ]


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 999, "entries": []}),
    json.dumps({"version": T.RECENT_VERSION, "entries": "nonsense"}),
])
def test_unusable_log_is_ignored(tasks_repo, project, content):
    T.create_task(tasks_repo, project, "t")
    T.recent_path(tasks_repo).write_text(content)
    assert T.read_recent(tasks_repo) == []
    assert T.recent_tasks(tasks_repo) == []


def test_malformed_entries_are_dropped(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.recent_path(tasks_repo).write_text(json.dumps({
        "version": T.RECENT_VERSION,
        "entries": [
            {"host": "testhost"},                      # no task_id
            "not a dict",
            {"task_id": task.task_id},                 # no host
            {"host": "testhost", "task_id": task.task_id},
        ],
    }))
    assert [e["task_id"] for e in T.read_recent(tasks_repo)] == [task.task_id]


def test_recent_log_is_not_mistaken_for_a_task(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task)
    assert len(T.load_tasks(tasks_repo)) == 1
    assert all(d.name.startswith("TASK_") for d in T.iter_task_dirs(tasks_repo))
    # and it does not disturb the root index
    assert set(T.read_index(tasks_repo).entries) == {task.task_id}
