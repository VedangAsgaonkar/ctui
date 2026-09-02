"""The access index: one row per task, most recently opened first.

Serves `--resume --recent` only. `dream` deliberately reads no bookkeeping —
it consults the transcripts themselves.
"""

import json
from datetime import datetime


from ctui import tasks as T


def _projects(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / "projects" / f"p{i}"
        p.mkdir(parents=True)
        out.append(p)
    return out



def test_index_starts_empty(tasks_repo):
    assert T.read_access(tasks_repo) == []
    assert T.recent_tasks(tasks_repo) == []


def test_record_access_writes_a_row(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task)

    path = T.access_path(tasks_repo)
    assert path == tasks_repo / "testhost" / "access.json"
    raw = json.loads(path.read_text())
    assert raw["version"] == T.ACCESS_VERSION
    assert raw["hostname"] == "testhost"
    row = raw["entries"][0]
    assert row["task_id"] == task.task_id
    assert row["first_at"] and row["last_at"] and row["count"] == 1
    assert "session_id" not in row          # dream reads transcripts, not this


def test_repeat_access_updates_in_place(tasks_repo, project):
    """One row per task: re-opening bumps it, never appends."""
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task)
    first = T.read_access(tasks_repo)[0].first_at
    T.record_access(tasks_repo, task)

    rows = T.read_access(tasks_repo)
    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].first_at == first          # first access is preserved
    assert rows[0].last_at >= first




def test_most_recent_first(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, f"t{i}")
             for i, p in enumerate(_projects(tmp_path, 3))]
    for i, task in enumerate(tasks):
        T.record_access(tasks_repo, task)
    assert [r.task_id for r in T.read_access(tasks_repo)] == \
        [t.task_id for t in reversed(tasks)]


def test_nothing_is_capped(tasks_repo, tmp_path):
    """The whole history is kept now, not the last 50."""
    for i, p in enumerate(_projects(tmp_path, 60)):
        T.record_access(tasks_repo, T.create_task(tasks_repo, p, "t"))
    assert len(T.read_access(tasks_repo)) == 60


# ---- --resume --recent ---------------------------------------------

def test_recent_tasks_dedupes_by_task(tasks_repo, tmp_path):
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a)
    T.record_access(tasks_repo, b)
    T.record_access(tasks_repo, a)      # a again, different session

    got = T.recent_tasks(tasks_repo)
    assert [t.task_id for t in got] == [a.task_id, b.task_id]


def test_recent_tasks_respects_the_limit(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 8)]
    for i, task in enumerate(tasks):
        T.record_access(tasks_repo, task)
    assert len(T.recent_tasks(tasks_repo, limit=5)) == 5


def test_recent_tasks_skips_deleted_tasks(tasks_repo, tmp_path):
    import shutil
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a)
    T.record_access(tasks_repo, b)
    shutil.rmtree(b.dir)
    assert [t.task_id for t in T.recent_tasks(tasks_repo)] == [a.task_id]


def test_recent_tasks_spans_hosts(tasks_repo, tmp_path):
    a, b = _projects(tmp_path, 2)
    mine = T.create_task(tasks_repo, a, "mine")
    theirs = T.create_task(tasks_repo, b, "theirs", host="otherhost")
    T.record_access(tasks_repo, theirs)
    T.record_access(tasks_repo, mine)

    assert json.loads(T.access_path(tasks_repo).read_text())["hostname"] == "testhost"
    assert [(t.host, t.task_id) for t in T.recent_tasks(tasks_repo)] == [
        ("testhost", mine.task_id), ("otherhost", theirs.task_id)]
