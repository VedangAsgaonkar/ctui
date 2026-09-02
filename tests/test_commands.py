import json
from pathlib import Path

import pytest

from ctui import commands as C
from ctui import config as CFG
from ctui import gitutil as G
from ctui import tasks as T
from ctui import ui


@pytest.fixture
def answers(monkeypatch):
    """Queue scripted answers for the ui prompts.

    Each prompt pops the next entry from its queue, so a test declares exactly
    the interaction it expects and an unexpected extra prompt fails loudly.
    """
    state = {"text": [], "confirm": [], "select": [], "path": []}

    def _pop(kind):
        if not state[kind]:
            raise AssertionError(f"unexpected {kind} prompt")
        return state[kind].pop(0)

    monkeypatch.setattr(ui, "ask_text", lambda *a, **k: _pop("text"))
    monkeypatch.setattr(ui, "ask_confirm", lambda *a, **k: _pop("confirm"))
    monkeypatch.setattr(ui, "ask_path", lambda *a, **k: Path(_pop("path")))

    def _select(message, choices, default=None):
        want = _pop("select")
        if callable(want):
            return want(choices)
        for ch in choices:
            if ch.title == want or ch.value == want:
                return ch.value
        raise AssertionError(f"no choice matching {want!r} in {[c.title for c in choices]}")

    monkeypatch.setattr(ui, "ask_select", _select)
    return state


# ---- setup ---------------------------------------------------------

def test_setup_creates_local_only_repos(tmp_path, answers, monkeypatch):
    base = tmp_path / "checkouts"
    answers["text"] = ["", ""]          # no tasks remote, no wiki remote
    answers["path"] = [str(base)]

    assert C.cmd_setup() == 0

    tasks = base / CFG.DEFAULT_TASKS_DIRNAME
    wiki = base / CFG.DEFAULT_WIKI_DIRNAME
    assert G.is_repo(tasks) and G.has_commits(tasks)
    assert G.is_repo(wiki) and G.has_commits(wiki)
    assert G.get_remote_url(tasks) is None

    cfg = CFG.load()
    assert cfg.tasks_repo == tasks
    assert cfg.wiki_repo == wiki
    assert cfg.tasks_remote is None and cfg.wiki_remote is None


def test_setup_registers_paths_in_ctuirc(tmp_path, answers):
    answers["text"] = ["", ""]
    answers["path"] = [str(tmp_path / "c")]
    C.cmd_setup()
    raw = json.loads(Path(CFG.rc_path()).read_text())
    assert raw["tasks_repo"].endswith(CFG.DEFAULT_TASKS_DIRNAME)
    assert raw["wiki_repo"].endswith(CFG.DEFAULT_WIKI_DIRNAME)


def test_setup_defaults_checkout_base_to_home(tmp_path, answers, monkeypatch):
    seen = {}

    def _path(message, default=""):
        seen["default"] = default
        return Path(default)

    monkeypatch.setattr(ui, "ask_path", _path)
    answers["text"] = ["", ""]
    C.cmd_setup()
    assert seen["default"] == str(Path.home())
    assert (Path.home() / CFG.DEFAULT_TASKS_DIRNAME).is_dir()


def test_setup_wires_and_pulls_remotes(tmp_path, answers, bare_remote):
    # Seed the remote so setup has something to pull.
    seed = tmp_path / "seed"
    G.init(seed)
    G.run(seed, "checkout", "-B", "main")
    (seed / "seeded.txt").write_text("hello\n")
    G.commit_all(seed, "seed")
    G.set_remote(seed, str(bare_remote))
    G.push(seed, "main")

    base = tmp_path / "checkouts"
    answers["text"] = [str(bare_remote), ""]
    answers["path"] = [str(base)]

    assert C.cmd_setup() == 0

    tasks = base / CFG.DEFAULT_TASKS_DIRNAME
    assert (tasks / "seeded.txt").read_text() == "hello\n"
    assert G.get_remote_url(tasks) == str(bare_remote)
    assert CFG.load().tasks_remote == str(bare_remote)


def test_setup_handles_empty_remote(tmp_path, answers, bare_remote):
    base = tmp_path / "checkouts"
    answers["text"] = ["", str(bare_remote)]
    answers["path"] = [str(base)]

    assert C.cmd_setup() == 0
    wiki = base / CFG.DEFAULT_WIKI_DIRNAME
    assert G.is_repo(wiki)
    assert G.get_remote_url(wiki) == str(bare_remote)
    assert G.has_commits(wiki)


def test_setup_follows_the_remote_branch_name(tmp_path, answers, bare_remote,
                                              monkeypatch):
    """A remote on `main` while git init defaults to `master` must still sync."""
    seed = tmp_path / "seed"
    G.init(seed)
    G.checkout_branch(seed, "main")
    (seed / "seeded.txt").write_text("hello\n")
    G.commit_all(seed, "seed")
    G.set_remote(seed, str(bare_remote))
    G.push(seed, "main")

    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[init]\n\tdefaultBranch = master\n")

    base = tmp_path / "checkouts"
    answers["text"] = ["", str(bare_remote)]
    answers["path"] = [str(base)]
    assert C.cmd_setup() == 0

    wiki = base / CFG.DEFAULT_WIKI_DIRNAME
    assert G.default_branch(wiki) == "main"
    assert (wiki / "seeded.txt").read_text() == "hello\n"


def test_setup_declining_reconfigure_leaves_config_alone(config, answers):
    answers["confirm"] = [False]
    assert C.cmd_setup() == 0
    assert CFG.load().tasks_repo == config.tasks_repo


# ---- sync ----------------------------------------------------------

def test_sync_requires_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("CTUI_RC", str(tmp_path / "missing.json"))
    with pytest.raises(CFG.ConfigError, match="ctui --setup"):
        C.cmd_sync()


def test_sync_commits_local_changes(config):
    (config.tasks_repo / "new.txt").write_text("x\n")
    assert C.cmd_sync() == 0
    assert not G.is_dirty(config.tasks_repo)


def test_sync_pushes_and_pulls(tmp_path, bare_remote, monkeypatch):
    branch = "main"

    tasks = tmp_path / "tasks"
    G.init(tasks)
    G.run(tasks, "checkout", "-B", branch)
    (tasks / "a.txt").write_text("a\n")
    G.commit_all(tasks, "a")
    G.set_remote(tasks, str(bare_remote))

    cfg = CFG.Config(tasks_repo=tasks, wiki_repo=None, tasks_remote=str(bare_remote))
    cfg.save()

    assert C.cmd_sync() == 0

    # A second clone must see the pushed work, and its own work must land too.
    other = tmp_path / "other"
    G.clone(str(bare_remote), other)
    assert (other / "a.txt").exists()

    (other / "b.txt").write_text("b\n")
    G.commit_all(other, "b")
    G.push(other, branch)

    (tasks / "c.txt").write_text("c\n")
    assert C.cmd_sync() == 0
    assert (tasks / "b.txt").exists()   # pulled
    other2 = tmp_path / "other2"
    G.clone(str(bare_remote), other2)
    assert (other2 / "c.txt").exists()  # pushed


def test_sync_reports_failure_on_bad_remote(tmp_path):
    tasks = tmp_path / "tasks"
    G.init(tasks)
    (tasks / "a.txt").write_text("a\n")
    G.commit_all(tasks, "a")
    G.set_remote(tasks, str(tmp_path / "does-not-exist.git"))
    CFG.Config(tasks_repo=tasks, wiki_repo=None,
               tasks_remote=str(tmp_path / "does-not-exist.git")).save()
    assert C.cmd_sync() == 1


# ---- init ----------------------------------------------------------

def test_init_creates_task_and_launches(config, project, answers, fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    answers["text"] = ["parser work"]

    assert C.cmd_init(replace=False) == 0

    tasks = T.load_tasks(config.tasks_repo)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.name == "parser work"
    assert task.root == project.resolve()
    assert len(task.sessions) == 1

    record = json.loads(fake_claude.read_text().splitlines()[0])
    assert Path(record["cwd"]).resolve() == project.resolve()


def test_init_with_name_skips_the_prompt(config, project, answers, fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    assert C.cmd_init(name="given", replace=False) == 0
    assert T.load_tasks(config.tasks_repo)[0].name == "given"


def test_init_no_launch_creates_task_only(config, project, answers, monkeypatch):
    monkeypatch.chdir(project)
    assert C.cmd_init(name="t", launch=False) == 0
    task = T.load_tasks(config.tasks_repo)[0]
    assert task.sessions == []


def test_init_commits_the_task_to_the_repo(config, project, monkeypatch):
    monkeypatch.chdir(project)
    C.cmd_init(name="t", launch=False)
    assert not G.is_dirty(config.tasks_repo)
    log = G.run(config.tasks_repo, "log", "-1", "--pretty=%s").out
    assert "new task TASK_" in log and "(t)" in log


def test_init_defaults_name_to_directory_name(config, project, monkeypatch):
    monkeypatch.chdir(project)
    seen = {}

    def _text(message, default="", **kw):
        seen["default"] = default
        return default

    monkeypatch.setattr(ui, "ask_text", _text)
    C.cmd_init(launch=False)
    assert seen["default"] == project.name


def test_init_warns_before_a_duplicate_task(config, project, answers, monkeypatch):
    monkeypatch.chdir(project)
    C.cmd_init(name="first", launch=False)

    answers["confirm"] = [False]
    assert C.cmd_init(launch=False) == 0
    assert len(T.load_tasks(config.tasks_repo)) == 1  # declined

    answers["confirm"] = [True]
    answers["text"] = ["second"]
    assert C.cmd_init(launch=False) == 0
    assert {t.name for t in T.load_tasks(config.tasks_repo)} == {"first", "second"}


def test_init_requires_setup(project, monkeypatch, tmp_path):
    monkeypatch.chdir(project)
    monkeypatch.setenv("CTUI_RC", str(tmp_path / "nope.json"))
    with pytest.raises(CFG.ConfigError):
        C.cmd_init(name="t", launch=False)


# ---- launch --------------------------------------------------------

def test_launch_finds_task_from_subdirectory(config, project, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project / "sub" / "deep")

    assert C.cmd_launch(replace=False) == 0

    reloaded = T.Task.load(task.dir)
    assert len(reloaded.sessions) == 1
    record = json.loads(fake_claude.read_text().splitlines()[0])
    assert Path(record["cwd"]).resolve() == project.resolve()


def test_launch_appends_to_the_session_list(config, project, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    C.cmd_launch(replace=False)
    C.cmd_launch(replace=False)
    ids = [s.session_id for s in T.Task.load(task.dir).sessions]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_launch_without_a_task_tells_you_to_init(config, project, monkeypatch):
    monkeypatch.chdir(project)
    with pytest.raises(C.CommandError, match="ctui --init"):
        C.cmd_launch(replace=False)


def test_launch_disambiguates_multiple_tasks(config, project, answers, fake_claude, monkeypatch):
    T.create_task(config.tasks_repo, project, "one")
    wanted = T.create_task(config.tasks_repo, project, "two")
    monkeypatch.chdir(project)

    answers["select"] = [lambda choices: next(
        c.value for c in choices if c.value.name == "two")]
    assert C.cmd_launch(replace=False) == 0
    assert len(T.Task.load(wanted.dir).sessions) == 1


# ---- resume --------------------------------------------------------

def test_resume_lists_tasks_then_sessions(config, project, answers, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    task.add_session("aaaaaaaa-0000-0000-0000-000000000000")
    monkeypatch.chdir(project)

    answers["select"] = [
        lambda choices: choices[0].value,
        "aaaaaaaa-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(replace=False) == 0

    record = json.loads(fake_claude.read_text().splitlines()[0])
    assert Path(record["cwd"]).resolve() == project.resolve()
    argv = record["argv"]
    assert argv[argv.index("--resume") + 1] == "aaaaaaaa-0000-0000-0000-000000000000"


def test_resume_spans_hosts(config, project, answers, fake_claude, monkeypatch):
    other = T.create_task(config.tasks_repo, project, "remote task", host="otherhost")
    other.add_session("bbbbbbbb-0000-0000-0000-000000000000")
    monkeypatch.chdir(Path.home())

    answers["select"] = [
        lambda choices: next(c.value for c in choices if c.value.host == "otherhost"),
        "bbbbbbbb-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(scope=C.SCOPE_GLOBAL, replace=False) == 0


def test_resume_shows_task_id_and_name(config, project, answers, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "the name")
    monkeypatch.chdir(project)
    titles = {}

    answers["select"] = [
        lambda choices: (titles.setdefault("t", [c.title for c in choices]),
                         choices[0].value)[1],
        C.SHELL_CHOICE,
    ]
    monkeypatch.setenv("SHELL", "/bin/true")
    C.cmd_resume(replace=False)
    assert any(task.task_id in t and "the name" in t for t in titles["t"])


def test_resume_can_open_a_shell_in_the_task_root(config, project, answers,
                                                  tmp_path, monkeypatch):
    T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    marker = tmp_path / "cwd.txt"
    shell = tmp_path / "sh"
    shell.write_text(f'#!/bin/sh\necho "$PWD" > "{marker}"\n')
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))

    answers["select"] = [lambda choices: choices[0].value, C.SHELL_CHOICE]
    assert C.cmd_resume(replace=False) == 0
    assert Path(marker.read_text().strip()).resolve() == project.resolve()


def test_resume_back_returns_to_the_task_list(config, project, answers,
                                              fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    task.add_session("cccccccc-0000-0000-0000-000000000000")
    monkeypatch.chdir(project)

    answers["select"] = [
        lambda choices: choices[0].value,
        C.BACK_CHOICE,
        lambda choices: choices[0].value,
        "cccccccc-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(replace=False) == 0


def test_resume_with_no_tasks_errors(config, monkeypatch):
    with pytest.raises(C.CommandError, match="ctui --init"):
        C.cmd_resume(scope=C.SCOPE_GLOBAL, replace=False)


def test_resume_flags_a_task_whose_root_is_missing(config, tmp_path, answers, monkeypatch):
    root = tmp_path / "gone"
    root.mkdir()
    task = T.create_task(config.tasks_repo, root, "t")
    root.rmdir()
    monkeypatch.chdir(Path.home())

    answers["select"] = [lambda choices: choices[0].value]
    answers["confirm"] = [False]     # decline picking another task
    assert C.cmd_resume(scope=C.SCOPE_GLOBAL, replace=False) == 1


# ---- list ----------------------------------------------------------

def test_list_prints_tasks(config, project, capsys):
    task = T.create_task(config.tasks_repo, project, "listed task")
    assert C.cmd_list() == 0
    out = capsys.readouterr().out
    assert task.task_id in out and "listed task" in out


def test_list_with_no_tasks(config, capsys):
    assert C.cmd_list() == 0
    assert "No tasks" in capsys.readouterr().out


# ---- launch bookkeeping --------------------------------------------

def test_launch_commits_the_session_registration(config, project, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    G.commit_all(config.tasks_repo, "task")
    monkeypatch.chdir(project)

    assert C.cmd_launch(replace=False) == 0

    assert not G.is_dirty(config.tasks_repo)
    subject = G.run(config.tasks_repo, "log", "-1", "--pretty=%s").out
    session_id = T.Task.load(task.dir).sessions[0].session_id
    assert session_id[:8] in subject and task.task_id in subject


def test_launch_propagates_the_claude_exit_code(config, project, tmp_path, monkeypatch):
    T.create_task(config.tasks_repo, project, "t")
    failing = tmp_path / "failing-claude"
    failing.write_text("#!/bin/sh\nexit 3\n")
    failing.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(failing))
    monkeypatch.chdir(project)

    assert C.cmd_launch(replace=False) == 3


def test_init_commits_task_and_session_separately(config, project, fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    assert C.cmd_init(name="t", replace=False) == 0

    subjects = G.run(config.tasks_repo, "log", "--pretty=%s").out.splitlines()
    assert any(s.startswith("ctui: new task") for s in subjects)
    assert any(s.startswith("ctui: session") for s in subjects)


# ---- setup inside an enclosing repo --------------------------------

def test_setup_initialises_repos_nested_in_another_repo(tmp_path, answers, capsys):
    """Regression: a non-repo directory inside a repo was treated as ready."""
    home = tmp_path / "home"
    G.init(home)
    (home / "dotfile").write_text("x\n")
    G.commit_all(home, "dotfiles")
    # Pre-create the checkout dirs as plain directories, as a re-run would find.
    (home / CFG.DEFAULT_TASKS_DIRNAME).mkdir()
    (home / CFG.DEFAULT_WIKI_DIRNAME).mkdir()

    answers["text"] = ["", ""]
    answers["path"] = [str(home)]
    assert C.cmd_setup() == 0

    tasks = home / CFG.DEFAULT_TASKS_DIRNAME
    assert G.is_repo(tasks)
    assert (tasks / ".git").exists()
    assert G.has_commits(tasks)
    assert "sits inside the git repo" in capsys.readouterr().err


def test_init_commits_to_the_tasks_repo_not_the_enclosing_one(tmp_path, project,
                                                              monkeypatch, capsys):
    home = tmp_path / "home"
    G.init(home)
    (home / "dotfile").write_text("x\n")
    G.commit_all(home, "dotfiles")
    tasks = home / "ctui-tasks"
    G.init(tasks)
    (tasks / "README.md").write_text("tasks\n")
    G.commit_all(tasks, "init")
    CFG.Config(tasks_repo=tasks, wiki_repo=None).save()

    monkeypatch.chdir(project)
    assert C.cmd_init(name="t", launch=False) == 0

    assert "ctui: new task" in G.run(tasks, "log", "-1", "--pretty=%s").out
    assert "ctui: new task" not in G.run(home, "log", "-1", "--pretty=%s").out
    assert not G.is_dirty(tasks)


def test_sync_refuses_a_non_repo_instead_of_using_the_parent(tmp_path, capsys):
    home = tmp_path / "home"
    G.init(home)
    (home / "dotfile").write_text("x\n")
    G.commit_all(home, "dotfiles")
    tasks = home / "ctui-tasks"
    tasks.mkdir()
    CFG.Config(tasks_repo=tasks, wiki_repo=None).save()

    assert C.cmd_sync() == 1
    assert "not a git repo" in capsys.readouterr().err
    assert G.run(home, "log", "-1", "--pretty=%s").out == "dotfiles"


# ---- resume scopes -------------------------------------------------

def test_resume_defaults_to_tasks_covering_cwd(config, project, tmp_path, answers,
                                               fake_claude, monkeypatch):
    here = T.create_task(config.tasks_repo, project, "here")
    here.add_session("11111111-0000-0000-0000-000000000000")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    T.create_task(config.tasks_repo, elsewhere, "elsewhere")

    monkeypatch.chdir(project)
    offered = {}
    answers["select"] = [
        lambda choices: (offered.setdefault("t", [c.value.name for c in choices]),
                         choices[0].value)[1],
        "11111111-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(replace=False) == 0
    assert offered["t"] == ["here"]          # the other task is not offered


def test_resume_walks_up_from_a_subdirectory(config, project, answers,
                                             fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    task.add_session("22222222-0000-0000-0000-000000000000")
    monkeypatch.chdir(project / "sub" / "deep")

    answers["select"] = [lambda choices: choices[0].value,
                         "22222222-0000-0000-0000-000000000000"]
    assert C.cmd_resume(replace=False) == 0


def test_resume_outside_any_task_points_at_global(config, project, tmp_path, monkeypatch):
    T.create_task(config.tasks_repo, project, "t")
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.chdir(away)

    with pytest.raises(C.CommandError, match=r"--resume --global"):
        C.cmd_resume(replace=False)


def test_resume_global_offers_everything(config, project, tmp_path, answers,
                                         fake_claude, monkeypatch):
    T.create_task(config.tasks_repo, project, "here")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = T.create_task(config.tasks_repo, elsewhere, "elsewhere")
    other.add_session("33333333-0000-0000-0000-000000000000")

    monkeypatch.chdir(project)
    offered = {}
    answers["select"] = [
        lambda choices: (offered.setdefault("t", {c.value.name for c in choices}),
                         next(c.value for c in choices if c.value.name == "elsewhere"))[1],
        "33333333-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(scope=C.SCOPE_GLOBAL, replace=False) == 0
    assert offered["t"] == {"here", "elsewhere"}


def test_resume_recent_offers_the_five_most_recent(config, tmp_path, answers,
                                                   fake_claude, monkeypatch):
    tasks = []
    for i in range(7):
        p = tmp_path / "projects" / f"p{i}"
        p.mkdir(parents=True)
        task = T.create_task(config.tasks_repo, p, f"t{i}")
        task.add_session(f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-000000000000")
        T.record_access(config.tasks_repo, task)
        tasks.append(task)

    monkeypatch.chdir(tmp_path)
    offered = {}
    answers["select"] = [
        lambda choices: (offered.setdefault("t", [c.value.name for c in choices]),
                         choices[0].value)[1],
        "66666666-0000-0000-0000-000000000000",
    ]
    assert C.cmd_resume(scope=C.SCOPE_RECENT, replace=False) == 0
    assert offered["t"] == ["t6", "t5", "t4", "t3", "t2"]   # newest first, 5 of 7


def test_resume_recent_with_empty_log_points_at_global(config, project, monkeypatch):
    T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    with pytest.raises(C.CommandError, match=r"--resume --global"):
        C.cmd_resume(scope=C.SCOPE_RECENT, replace=False)


def test_resume_prompt_names_the_scope(config, project, answers, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    monkeypatch.setenv("SHELL", "/bin/true")
    seen = {}

    def _select(message, choices, default=None):
        seen.setdefault("messages", []).append(message)
        return choices[0].value if len(seen["messages"]) == 1 else C.SHELL_CHOICE

    monkeypatch.setattr(ui, "ask_select", _select)
    C.cmd_resume(replace=False)
    assert C._tilde(project.resolve()) in seen["messages"][0]


def test_tilde_abbreviates_only_under_home(tmp_path):
    assert C._tilde(Path.home() / "proj" / "x") == "~/proj/x"
    assert C._tilde(Path("/elsewhere/proj")) == "/elsewhere/proj"


# ---- access recording ----------------------------------------------

def test_launching_records_an_access(config, project, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    assert T.recent_tasks(config.tasks_repo) == []

    C.cmd_launch(replace=False)
    assert [t.task_id for t in T.recent_tasks(config.tasks_repo)] == [task.task_id]


def test_init_records_an_access(config, project, fake_claude, monkeypatch):
    monkeypatch.chdir(project)
    C.cmd_init(name="t", replace=False)
    assert [t.name for t in T.recent_tasks(config.tasks_repo)] == ["t"]


def test_resuming_records_an_access(config, project, answers, fake_claude, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    task.add_session("44444444-0000-0000-0000-000000000000")
    monkeypatch.chdir(project)

    answers["select"] = [lambda choices: choices[0].value,
                         "44444444-0000-0000-0000-000000000000"]
    C.cmd_resume(replace=False)
    assert [t.task_id for t in T.recent_tasks(config.tasks_repo)] == [task.task_id]


def test_opening_a_shell_records_an_access(config, project, answers, monkeypatch):
    task = T.create_task(config.tasks_repo, project, "t")
    monkeypatch.chdir(project)
    monkeypatch.setenv("SHELL", "/bin/true")

    answers["select"] = [lambda choices: choices[0].value, C.SHELL_CHOICE]
    C.cmd_resume(replace=False)
    assert [t.task_id for t in T.recent_tasks(config.tasks_repo)] == [task.task_id]


def test_access_log_is_committed(config, project, fake_claude, monkeypatch):
    T.create_task(config.tasks_repo, project, "t")
    G.commit_all(config.tasks_repo, "task")
    monkeypatch.chdir(project)

    C.cmd_launch(replace=False)
    assert not G.is_dirty(config.tasks_repo)
    tracked = G.run(config.tasks_repo, "ls-files").out
    assert "recent.json" in tracked
