import json
from datetime import datetime

import pytest

from ctui import tasks as T


def test_create_task_makes_symlink_and_metadata(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "my task")

    assert task.dir == tasks_repo / "testhost" / task.task_id
    assert task.task_id.startswith("TASK_")
    link = task.dir / "root"
    assert link.is_symlink()
    assert link.resolve() == project.resolve()

    meta = json.loads((task.dir / "task.json").read_text())
    assert meta["name"] == "my task"
    assert meta["hostname"] == "testhost"
    assert meta["root"] == str(project.resolve())
    assert meta["sessions"] == []


def test_task_id_uses_creation_datetime(tasks_repo, project):
    when = datetime(2026, 3, 4, 5, 6, 7)
    assert T.new_task_id(when) == "TASK_20260304_050607"


def test_rapid_tasks_do_not_collide(tasks_repo, project):
    a = T.create_task(tasks_repo, project, "a")
    b = T.create_task(tasks_repo, project, "b")
    assert a.task_id != b.task_id
    assert a.dir.exists() and b.dir.exists()


def test_create_task_rejects_missing_root(tasks_repo, tmp_path):
    with pytest.raises(T.TaskError):
        T.create_task(tasks_repo, tmp_path / "nope", "x")


def test_add_session_persists(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    task.add_session("11111111-2222-3333-4444-555555555555")
    task.add_session("66666666-7777-8888-9999-000000000000")

    reloaded = T.Task.load(task.dir)
    assert [s.session_id for s in reloaded.sessions] == [
        "11111111-2222-3333-4444-555555555555",
        "66666666-7777-8888-9999-000000000000",
    ]
    assert reloaded.sessions[0].created_at


def test_find_tasks_from_cwd_walks_upwards(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    found = T.find_tasks_from_cwd(tasks_repo, project / "sub" / "deep")
    assert [t.task_id for t in found] == [task.task_id]


def test_find_tasks_from_cwd_prefers_nearest_root(tasks_repo, project):
    outer = T.create_task(tasks_repo, project, "outer")
    inner_root = project / "sub"
    inner = T.create_task(tasks_repo, inner_root, "inner")

    found = T.find_tasks_from_cwd(tasks_repo, project / "sub" / "deep")
    assert [t.task_id for t in found] == [inner.task_id]

    found = T.find_tasks_from_cwd(tasks_repo, project)
    assert [t.task_id for t in found] == [outer.task_id]


def test_find_tasks_from_cwd_returns_all_at_same_root(tasks_repo, project):
    T.create_task(tasks_repo, project, "one")
    T.create_task(tasks_repo, project, "two")
    found = T.find_tasks_from_cwd(tasks_repo, project / "sub")
    assert {t.name for t in found} == {"one", "two"}


def test_find_tasks_from_cwd_empty_when_unrelated(tasks_repo, project, tmp_path):
    T.create_task(tasks_repo, project, "t")
    assert T.find_tasks_from_cwd(tasks_repo, tmp_path) == []


def test_find_tasks_is_scoped_to_this_host(tasks_repo, project):
    T.create_task(tasks_repo, project, "elsewhere", host="otherhost")
    assert T.find_tasks_from_cwd(tasks_repo, project) == []


def test_load_tasks_spans_hosts_newest_first(tasks_repo, project):
    T.create_task(tasks_repo, project, "here")
    T.create_task(tasks_repo, project, "there", host="otherhost")
    all_tasks = T.load_tasks(tasks_repo)
    assert {t.host for t in all_tasks} == {"testhost", "otherhost"}


def test_load_tasks_skips_corrupt_metadata(tasks_repo, project):
    good = T.create_task(tasks_repo, project, "good")
    broken = tasks_repo / "testhost" / "TASK_19990101_000000"
    broken.mkdir(parents=True)
    (broken / "task.json").write_text("{not json")
    naked = tasks_repo / "testhost" / "TASK_19990102_000000"
    naked.mkdir(parents=True)

    assert [t.task_id for t in T.load_tasks(tasks_repo)] == [good.task_id]


def test_root_exists_reports_missing_root(tasks_repo, tmp_path):
    root = tmp_path / "gone"
    root.mkdir()
    task = T.create_task(tasks_repo, root, "t")
    assert task.root_exists
    root.rmdir()
    assert not T.Task.load(task.dir).root_exists


def test_created_display_falls_back_to_task_id(tasks_repo, project):
    task = T.create_task(tasks_repo, project, "t")
    task.created_at = ""
    assert task.created_display == datetime.strptime(
        task.task_id.removeprefix("TASK_"), T.TASK_STAMP_FMT
    ).strftime("%Y-%m-%d %H:%M")


def test_session_ids_are_uuids():
    import uuid
    assert uuid.UUID(T.new_session_id())


# ---- hostname resolution -------------------------------------------

def _fresh_hostname(monkeypatch, gethostname, getfqdn=None):
    """Call the detector with socket stubbed and its cache cleared."""
    monkeypatch.delenv("CTUI_HOSTNAME", raising=False)
    monkeypatch.setattr(T.socket, "gethostname", lambda: gethostname)
    monkeypatch.setattr(T.socket, "getfqdn", lambda *a: getfqdn if getfqdn else "")
    monkeypatch.setattr(T.platform, "node", lambda: "")
    T._detect_hostname.cache_clear()
    try:
        return T.hostname()
    finally:
        T._detect_hostname.cache_clear()


def test_hostname_keeps_the_full_domain(monkeypatch):
    assert _fresh_hostname(monkeypatch, "furiosa.stanford.edu") == "furiosa.stanford.edu"


def test_hostname_does_not_resolve_when_already_qualified(monkeypatch):
    """A qualified gethostname must not be overridden by a bogus getfqdn."""
    assert _fresh_hostname(
        monkeypatch, "furiosa.stanford.edu", getfqdn="localhost"
    ) == "furiosa.stanford.edu"


def test_hostname_qualifies_a_short_name_via_fqdn(monkeypatch):
    assert _fresh_hostname(
        monkeypatch, "furiosa", getfqdn="furiosa.stanford.edu"
    ) == "furiosa.stanford.edu"


def test_hostname_keeps_short_name_when_fqdn_is_useless(monkeypatch):
    assert _fresh_hostname(monkeypatch, "boxy", getfqdn="localhost.localdomain") == "boxy"
    assert _fresh_hostname(monkeypatch, "boxy", getfqdn="boxy") == "boxy"


def test_hostname_falls_back_when_nothing_is_known(monkeypatch):
    assert _fresh_hostname(monkeypatch, "") == "unknown-host"


def test_hostname_env_override_wins(monkeypatch):
    monkeypatch.setenv("CTUI_HOSTNAME", "override.example.com")
    assert T.hostname() == "override.example.com"


def test_task_directory_uses_the_full_hostname(tasks_repo, project, monkeypatch):
    monkeypatch.setenv("CTUI_HOSTNAME", "furiosa.stanford.edu")
    task = T.create_task(tasks_repo, project, "t")
    assert task.dir.parent == tasks_repo / "furiosa.stanford.edu"
    assert task.host == "furiosa.stanford.edu"
    assert T.Task.load(task.dir).host == "furiosa.stanford.edu"
    # and it is still discoverable under that name
    assert [x.task_id for x in T.find_tasks_from_cwd(tasks_repo, project)] == [task.task_id]
