import json

import pytest

from ctui import commands, launcher, view
from ctui.cli import main
from ctui.tasks import Session, Task, create_task

PARENT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def task(config, project):
    return create_task(config.tasks_repo, project, "demo task")


@pytest.fixture
def seeded(task):
    task.add_session(PARENT)
    return task


def invocations(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


# ---- the session model ----------------------------------------------

def test_forked_from_round_trips(seeded, config):
    seeded.add_session("child", forked_from=PARENT)
    reloaded = Task.load(seeded.dir)
    assert reloaded.sessions[-1].forked_from == PARENT
    assert reloaded.sessions[0].forked_from is None


def test_forked_from_is_omitted_when_absent(task):
    task.add_session("plain")
    raw = json.loads((task.dir / "task.json").read_text())
    assert "forked_from" not in raw["sessions"][0]


def test_forked_from_is_written_when_present(seeded):
    seeded.add_session("child", forked_from=PARENT)
    raw = json.loads((seeded.dir / "task.json").read_text())
    assert raw["sessions"][-1]["forked_from"] == PARENT


def test_session_from_dict_tolerates_blank_parent():
    assert Session.from_dict({"session_id": "a", "created_at": "",
                              "forked_from": ""}).forked_from is None


def test_find_session(seeded):
    assert seeded.find_session(PARENT).session_id == PARENT
    assert seeded.find_session("nope") is None


def test_register_session_records_the_parent(seeded):
    child = launcher.register_session(seeded, forked_from=PARENT)
    assert seeded.find_session(child).forked_from == PARENT


def test_register_session_defaults_to_no_parent(task):
    child = launcher.register_session(task)
    assert task.find_session(child).forked_from is None


# ---- the launcher ----------------------------------------------------

def test_fork_passes_resume_fork_and_new_session_id(seeded, fake_claude):
    session_id, code = launcher.fork_session(seeded, PARENT, replace=False)
    assert code == 0
    argv = invocations(fake_claude)[0]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == PARENT
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == session_id
    assert session_id != PARENT


def test_fork_registers_before_launching(seeded, fake_claude):
    session_id, _ = launcher.fork_session(seeded, PARENT, replace=False)
    reloaded = Task.load(seeded.dir)
    assert reloaded.find_session(session_id).forked_from == PARENT


def test_fork_keeps_the_artifact_prompt_and_add_dir(seeded, fake_claude):
    launcher.fork_session(seeded, PARENT, replace=False)
    argv = invocations(fake_claude)[0]["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(seeded.dir)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert str(seeded.dir) in prompt


def test_fork_runs_in_the_task_root(seeded, fake_claude):
    launcher.fork_session(seeded, PARENT, replace=False)
    assert invocations(fake_claude)[0]["cwd"] == str(seeded.root)


def test_fork_passes_extra_args_through(seeded, fake_claude):
    launcher.fork_session(seeded, PARENT, extra=["--model", "opus"], replace=False)
    argv = invocations(fake_claude)[0]["argv"]
    assert argv[-2:] == ["--model", "opus"]


def test_fork_honours_a_preminted_session_id(seeded, fake_claude):
    minted = launcher.register_session(seeded, forked_from=PARENT)
    session_id, _ = launcher.fork_session(seeded, PARENT, replace=False,
                                          session_id=minted)
    assert session_id == minted
    assert len(Task.load(seeded.dir).sessions) == 2


def test_fork_refuses_a_relative_task_dir(seeded, fake_claude):
    seeded.dir = seeded.dir.relative_to(seeded.dir.anchor)
    with pytest.raises(launcher.LauncherError, match="absolute"):
        launcher.fork_session(seeded, PARENT, replace=False)


def test_fork_does_not_register_when_validation_fails(seeded, fake_claude):
    real_dir = seeded.dir
    before = len(Task.load(real_dir).sessions)
    seeded.dir = seeded.dir.relative_to(seeded.dir.anchor)
    with pytest.raises(launcher.LauncherError):
        launcher.fork_session(seeded, PARENT, replace=False)
    assert len(Task.load(real_dir).sessions) == before


# ---- the command -----------------------------------------------------

def test_cmd_fork_branches_the_chosen_session(seeded, config, project,
                                              fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(commands.ui, "ask_select",
                        _answers([seeded, PARENT]))
    assert commands.cmd_fork(replace=False) == 0
    argv = invocations(fake_claude)[0]["argv"]
    assert argv[argv.index("--resume") + 1] == PARENT
    assert "--fork-session" in argv


def test_cmd_fork_records_lineage_and_commits(seeded, config, project,
                                              fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(commands.ui, "ask_select", _answers([seeded, PARENT]))
    commands.cmd_fork(replace=False)
    reloaded = Task.load(seeded.dir)
    assert len(reloaded.sessions) == 2
    assert reloaded.sessions[-1].forked_from == PARENT

    from ctui import gitutil
    log = gitutil.run(config.tasks_repo, "log", "-1", "--pretty=%s").out
    assert "fork session" in log
    assert PARENT[:8] in log


def test_cmd_fork_records_access(seeded, config, project, fake_claude,
                                 monkeypatch):
    from ctui.tasks import read_access

    monkeypatch.chdir(project)
    monkeypatch.setattr(commands.ui, "ask_select", _answers([seeded, PARENT]))
    commands.cmd_fork(replace=False)
    assert [e.task_id for e in read_access(config.tasks_repo)] == [seeded.task_id]


def test_cmd_fork_offers_back_to_the_task_list(seeded, config, project,
                                               fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(commands.ui, "ask_select",
                        _answers([seeded, commands.BACK_CHOICE, seeded, PARENT]))
    assert commands.cmd_fork(replace=False) == 0
    assert len(invocations(fake_claude)) == 1


def test_cmd_fork_skips_a_task_with_no_sessions(task, config, project,
                                                fake_claude, monkeypatch, capsys):
    monkeypatch.chdir(project)
    monkeypatch.setattr(commands.ui, "ask_select", _answers([task]))
    monkeypatch.setattr(commands.ui, "ask_confirm", lambda *a, **k: False)
    assert commands.cmd_fork(replace=False) == 1
    assert "nothing to fork" in capsys.readouterr().err


def test_cmd_fork_warns_on_a_missing_root(seeded, config, project, monkeypatch,
                                          capsys):
    seeded.root = project.parent / "gone"
    seeded.save()
    monkeypatch.setattr(commands.ui, "ask_select", _answers([seeded]))
    monkeypatch.setattr(commands.ui, "ask_confirm", lambda *a, **k: False)
    assert commands.cmd_fork(scope=commands.SCOPE_GLOBAL, replace=False) == 1
    assert "does not exist" in capsys.readouterr().err


def test_cmd_fork_errors_when_no_task_covers_cwd(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(commands.CommandError, match="No ctui task covers"):
        commands.cmd_fork(replace=False)


def test_fork_choices_show_lineage(seeded):
    seeded.add_session("child-id", forked_from=PARENT)
    titles = [c.title for c in commands._fork_choices(seeded)]
    assert any("forked from 11111111" in t for t in titles)
    assert all("open a shell" not in t for t in titles)


def test_resume_choices_show_lineage(seeded):
    seeded.add_session("child-id", forked_from=PARENT)
    titles = [c.title for c in commands._session_choices(seeded)]
    assert any("forked from 11111111" in t for t in titles)


# ---- cli -------------------------------------------------------------

def test_cli_dispatches_fork(config, monkeypatch):
    seen = {}

    def fake(extra, scope):
        seen.update(extra=extra, scope=scope)
        return 0

    monkeypatch.setattr(commands, "cmd_fork", fake)
    assert main(["--fork"]) == 0
    assert seen == {"extra": [], "scope": commands.SCOPE_LOCAL}


@pytest.mark.parametrize("flag,expected", [
    ("--global", "global"),
    ("--recent", "recent"),
])
def test_cli_fork_scopes(config, monkeypatch, flag, expected):
    seen = {}
    monkeypatch.setattr(commands, "cmd_fork",
                        lambda extra, scope: seen.setdefault("scope", scope) and 0 or 0)
    assert main(["--fork", flag]) == 0
    assert seen["scope"] == expected


def test_cli_fork_passes_claude_args(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "cmd_fork",
                        lambda extra, scope: seen.setdefault("extra", extra) and 0 or 0)
    assert main(["--fork", "--", "--model", "opus"]) == 0
    assert seen["extra"] == ["--model", "opus"]


def test_cli_rejects_fork_with_resume(capsys):
    with pytest.raises(SystemExit):
        main(["--fork", "--resume"])
    assert "not allowed with" in capsys.readouterr().err


# ---- the view --------------------------------------------------------

def test_view_shows_fork_lineage(seeded, config):
    seeded.add_session("cafebabe-0000-0000-0000-000000000000", forked_from=PARENT)
    body = view.handle(config, view.task_url(seeded)).body.decode()
    assert "forked from" in body
    assert "11111111" in body


def test_view_shows_a_dash_for_unforked_sessions(seeded, config):
    body = view.handle(config, view.task_url(seeded)).body.decode()
    assert "forked from" in body
    assert "—" in body


def _answers(values):
    remaining = list(values)

    def fake(*args, **kwargs):
        return remaining.pop(0)

    return fake
