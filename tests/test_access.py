"""The access index: one row per (task, session), most recent first.

Serves both `--resume --recent` (deduped by task) and dream (which sessions
could have been active on a day).
"""

import json
from datetime import date, datetime, timedelta

import pytest

from ctui import tasks as T


def _projects(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / "projects" / f"p{i}"
        p.mkdir(parents=True)
        out.append(p)
    return out


def _backdate(tasks_repo, task_id, session_id, first, last, host="testhost"):
    """Rewrite one row's timestamps, to test day coverage without waiting."""
    entries = T.read_access(tasks_repo, host)
    for entry in entries:
        if entry.task_id == task_id and entry.session_id == session_id:
            entry.first_at = datetime.combine(first, datetime.min.time()).astimezone().isoformat()
            entry.last_at = datetime.combine(last, datetime.min.time()).astimezone().isoformat()
    T.save_access(tasks_repo, entries, host)


# ---- shape ---------------------------------------------------------

def test_index_starts_empty(tasks_repo):
    assert T.read_access(tasks_repo) == []
    assert T.recent_tasks(tasks_repo) == []
    assert T.sessions_touching(tasks_repo, date(2026, 9, 2)) == []


def test_record_access_writes_a_row(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "sess-1")

    path = T.access_path(tasks_repo)
    assert path == tasks_repo / "testhost" / "access.json"
    raw = json.loads(path.read_text())
    assert raw["version"] == T.ACCESS_VERSION
    assert raw["hostname"] == "testhost"
    row = raw["entries"][0]
    assert row["task_id"] == task.task_id
    assert row["session_id"] == "sess-1"
    assert row["first_at"] and row["last_at"] and row["count"] == 1


def test_repeat_access_updates_in_place(tasks_repo, project):
    """Bounded by session count: re-opening bumps a row, never appends one."""
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "sess-1")
    first = T.read_access(tasks_repo)[0].first_at
    T.record_access(tasks_repo, task, "sess-1")

    rows = T.read_access(tasks_repo)
    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].first_at == first          # first access is preserved
    assert rows[0].last_at >= first


def test_sessions_are_tracked_separately(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "sess-1")
    T.record_access(tasks_repo, task, "sess-2")
    rows = T.read_access(tasks_repo)
    assert [r.session_id for r in rows] == ["sess-2", "sess-1"]


def test_shell_access_has_no_session(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, None)
    rows = T.read_access(tasks_repo)
    assert rows[0].session_id is None


def test_most_recent_first(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, f"t{i}")
             for i, p in enumerate(_projects(tmp_path, 3))]
    for i, task in enumerate(tasks):
        T.record_access(tasks_repo, task, f"s{i}")
    assert [r.task_id for r in T.read_access(tasks_repo)] == \
        [t.task_id for t in reversed(tasks)]


def test_nothing_is_capped(tasks_repo, tmp_path):
    """The whole history is kept now, not the last 50."""
    for i, p in enumerate(_projects(tmp_path, 60)):
        T.record_access(tasks_repo, T.create_task(tasks_repo, p, "t"), f"s{i}")
    assert len(T.read_access(tasks_repo)) == 60


# ---- --resume --recent ---------------------------------------------

def test_recent_tasks_dedupes_by_task(tasks_repo, tmp_path):
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a, "s1")
    T.record_access(tasks_repo, b, "s2")
    T.record_access(tasks_repo, a, "s3")      # a again, different session

    got = T.recent_tasks(tasks_repo)
    assert [t.task_id for t in got] == [a.task_id, b.task_id]


def test_recent_tasks_respects_the_limit(tasks_repo, tmp_path):
    tasks = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 8)]
    for i, task in enumerate(tasks):
        T.record_access(tasks_repo, task, f"s{i}")
    assert len(T.recent_tasks(tasks_repo, limit=5)) == 5


def test_recent_tasks_skips_deleted_tasks(tasks_repo, tmp_path):
    import shutil
    a, b = [T.create_task(tasks_repo, p, "t") for p in _projects(tmp_path, 2)]
    T.record_access(tasks_repo, a, "s1")
    T.record_access(tasks_repo, b, "s2")
    shutil.rmtree(b.dir)
    assert [t.task_id for t in T.recent_tasks(tasks_repo)] == [a.task_id]


def test_recent_tasks_spans_hosts(tasks_repo, tmp_path):
    a, b = _projects(tmp_path, 2)
    mine = T.create_task(tasks_repo, a, "mine")
    theirs = T.create_task(tasks_repo, b, "theirs", host="otherhost")
    T.record_access(tasks_repo, theirs, "s1")
    T.record_access(tasks_repo, mine, "s2")

    assert json.loads(T.access_path(tasks_repo).read_text())["hostname"] == "testhost"
    assert [(t.host, t.task_id) for t in T.recent_tasks(tasks_repo)] == [
        ("testhost", mine.task_id), ("otherhost", theirs.task_id)]


# ---- day coverage, for dream ---------------------------------------

def test_sessions_touching_the_access_day(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "s1")
    _backdate(tasks_repo, task.task_id, "s1", date(2026, 9, 2), date(2026, 9, 2))

    assert [e.session_id for e in T.sessions_touching(tasks_repo, date(2026, 9, 2))] == ["s1"]
    assert T.sessions_touching(tasks_repo, date(2026, 9, 5)) == []


def test_a_session_opened_the_night_before_still_counts(tasks_repo, project):
    """Only the opening is recorded, so a run past midnight must not be lost."""
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "s1")
    _backdate(tasks_repo, task.task_id, "s1", date(2026, 9, 1), date(2026, 9, 1))

    assert [e.session_id for e in T.sessions_touching(tasks_repo, date(2026, 9, 2))] == ["s1"]
    assert T.sessions_touching(tasks_repo, date(2026, 9, 3)) == []


def test_a_long_lived_session_covers_the_whole_span(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "s1")
    _backdate(tasks_repo, task.task_id, "s1", date(2026, 9, 1), date(2026, 9, 5))

    for day in range(1, 7):
        covered = T.sessions_touching(tasks_repo, date(2026, 9, day))
        assert bool(covered), f"2026-09-{day:02d} should be covered"
    assert T.sessions_touching(tasks_repo, date(2026, 8, 31)) == []
    assert T.sessions_touching(tasks_repo, date(2026, 9, 7)) == []


def test_shell_accesses_are_not_dream_candidates(tasks_repo, project):
    """A shell has no transcript to read."""
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, None)
    _backdate(tasks_repo, task.task_id, None, date(2026, 9, 2), date(2026, 9, 2))
    assert T.sessions_touching(tasks_repo, date(2026, 9, 2)) == []


def test_covers_handles_unparseable_stamps():
    entry = T.Access(host="h", task_id="T", session_id="s",
                     first_at="junk", last_at="junk")
    assert not entry.covers(date(2026, 9, 2))


# ---- robustness ----------------------------------------------------

@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 999, "entries": []}),
    json.dumps({"version": T.ACCESS_VERSION, "entries": "nonsense"}),
])
def test_unusable_index_is_ignored(tasks_repo, project, content):
    T.create_task(tasks_repo, project, "t")
    T.access_path(tasks_repo).write_text(content)
    assert T.read_access(tasks_repo) == []
    assert T.recent_tasks(tasks_repo) == []


def test_malformed_rows_are_dropped(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.access_path(tasks_repo).write_text(json.dumps({
        "version": T.ACCESS_VERSION,
        "entries": [
            {"host": "testhost"},                                  # no task_id
            {"task_id": task.task_id},                             # no host
            {"host": "testhost", "task_id": task.task_id},         # no timestamp
            "not a dict",
            {"host": "testhost", "task_id": task.task_id,
             "session_id": "s1", "last_at": "2026-09-02T10:00:00+00:00"},
        ],
    }))
    rows = T.read_access(tasks_repo)
    assert [r.session_id for r in rows] == ["s1"]
    assert rows[0].first_at == rows[0].last_at          # defaulted


def test_legacy_recent_json_is_carried_over(tasks_repo, project):
    """The pre-index file had task-level rows with a single `at`."""
    task = T.create_task(tasks_repo, project, "t")
    (tasks_repo / "testhost" / T.LEGACY_RECENT_JSON).write_text(json.dumps({
        "version": 1, "hostname": "testhost",
        "entries": [{"host": "testhost", "task_id": task.task_id,
                     "at": "2026-09-02T10:00:00+00:00"}],
    }))
    rows = T.read_access(tasks_repo)
    assert [r.task_id for r in rows] == [task.task_id]
    assert rows[0].session_id is None                   # nothing for dream
    assert [t.task_id for t in T.recent_tasks(tasks_repo)] == [task.task_id]


def test_legacy_rows_are_migrated_once_then_the_file_is_dropped(tasks_repo, project):
    """The first write folds the old history in; after that it is not re-read."""
    task = T.create_task(tasks_repo, project, "t")
    legacy = tasks_repo / "testhost" / T.LEGACY_RECENT_JSON
    legacy.write_text(json.dumps({
        "version": 1, "entries": [{"host": "testhost", "task_id": "TASK_OLD",
                                   "at": "2020-01-01T00:00:00+00:00"}]}))

    T.record_access(tasks_repo, task, "s1")
    assert [r.task_id for r in T.read_access(tasks_repo)] == [task.task_id, "TASK_OLD"]

    # Now that access.json exists, later edits to the old file are ignored.
    legacy.write_text(json.dumps({
        "version": 1, "entries": [{"host": "testhost", "task_id": "TASK_NEWER",
                                   "at": "2026-01-01T00:00:00+00:00"}]}))
    assert "TASK_NEWER" not in [r.task_id for r in T.read_access(tasks_repo)]


def test_index_is_not_mistaken_for_a_task(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    T.record_access(tasks_repo, task, "s1")
    assert len(T.load_tasks(tasks_repo)) == 1
    assert all(d.name.startswith("TASK_") for d in T.iter_task_dirs(tasks_repo))
    assert set(T.read_index(tasks_repo).entries) == {task.task_id}
