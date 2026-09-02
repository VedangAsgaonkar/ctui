import json
from pathlib import Path

import pytest

from ctui import launcher as L
from ctui import tasks as T


def _read_log(log: Path, index: int = 0) -> tuple[str, list[str]]:
    """cwd and argv of the `index`-th recorded claude invocation."""
    record = json.loads(log.read_text().splitlines()[index])
    return record["cwd"], record["argv"]


@pytest.fixture
def task(tasks_repo, project):
    return T.create_task(tasks_repo, project, "demo task")


def test_launch_registers_session_and_runs_in_root(task, project, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    session_id, code = L.launch_session(task, replace=False)
    assert code == 0

    assert [s.session_id for s in T.Task.load(task.dir).sessions] == [session_id]

    cwd, args = _read_log(fake_claude)
    assert Path(cwd).resolve() == project.resolve()
    assert args[args.index("--session-id") + 1] == session_id
    assert args[args.index("--name") + 1] == "demo task"
    assert args[args.index("--add-dir") + 1] == str(task.dir)


def test_launch_appends_artifact_system_prompt(task, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    L.launch_session(task, replace=False)
    _, args = _read_log(fake_claude)
    prompt = args[args.index("--append-system-prompt") + 1]
    assert str(task.dir) in prompt
    assert str(task.root) in prompt
    assert "intermediate" in prompt and "output" in prompt


def test_artifact_prompt_carves_out_the_task_root(task):
    prompt = L.artifact_system_prompt(task)
    assert str(task.dir) in prompt
    assert "exception" in prompt.lower()
    assert str(task.root) in prompt


def test_passthrough_args_reach_claude(task, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    L.launch_session(task, extra=["--model", "opus"], replace=False)
    _, args = _read_log(fake_claude)
    assert args[-2:] == ["--model", "opus"]


def test_resume_passes_session_id(task, project, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    assert L.resume_session(task, "abc-123", replace=False) == 0
    cwd, args = _read_log(fake_claude)
    assert Path(cwd).resolve() == project.resolve()
    assert args[args.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in args


def test_resume_does_not_add_a_session(task, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    L.resume_session(task, "abc-123", replace=False)
    assert T.Task.load(task.dir).sessions == []


def test_launch_fails_when_root_is_gone(tasks_repo, tmp_path, fake_claude, monkeypatch):
    root = tmp_path / "temp-root"
    root.mkdir()
    task = T.create_task(tasks_repo, root, "t")
    root.rmdir()
    monkeypatch.chdir(Path.home())
    with pytest.raises(L.LauncherError, match="does not exist"):
        L.launch_session(task, replace=False)


def test_missing_claude_binary_is_reported(task, monkeypatch):
    monkeypatch.delenv("CTUI_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(L.shutil, "which", lambda _: None)
    with pytest.raises(L.LauncherError, match="claude"):
        L.claude_bin()


def test_open_shell_uses_task_root(task, project, tmp_path, monkeypatch):
    marker = tmp_path / "shell-cwd.txt"
    fake_shell = tmp_path / "fake-shell"
    fake_shell.write_text(f'#!/bin/sh\necho "$PWD" > "{marker}"\n')
    fake_shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_shell))
    monkeypatch.chdir(Path.home())

    assert L.open_shell(task, replace=False) == 0
    assert Path(marker.read_text().strip()).resolve() == project.resolve()


# ---- past-artifact awareness and path correctness -------------------

def test_prompt_points_at_past_artifacts(task):
    prompt = L.artifact_system_prompt(task)
    assert "Artifacts from earlier sessions" in prompt
    assert "look before you start" in prompt
    assert "first session" in prompt          # tells it an empty dir is fine


def test_prompt_paths_are_absolute_and_real(task):
    prompt = L.artifact_system_prompt(task)
    for line in prompt.splitlines():
        if line.startswith("- Task directory:"):
            p = Path(line.split(":", 1)[1].strip())
            assert p.is_absolute() and p.is_dir()
            assert p == task.dir
        if line.startswith("- Task root"):
            p = Path(line.split(":", 1)[1].strip())
            assert p.is_absolute() and p.is_dir()


def test_prompt_task_dir_is_under_the_configured_tasks_repo(task, tasks_repo):
    """The path must name this machine's configured tasks repo, not a guess."""
    assert task.dir.is_relative_to(tasks_repo)
    assert str(task.dir) in L.artifact_system_prompt(task)


def test_launch_refuses_a_relative_task_dir(task, fake_claude, monkeypatch):
    task.dir = Path("relative/task/dir")
    monkeypatch.chdir(Path.home())
    with pytest.raises(L.LauncherError, match="absolute"):
        L.launch_session(task, replace=False)


def test_add_dir_and_prompt_agree_on_the_path(task, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    L.launch_session(task, replace=False)
    _, args = _read_log(fake_claude)
    add_dir = args[args.index("--add-dir") + 1]
    prompt = args[args.index("--append-system-prompt") + 1]
    assert Path(add_dir).is_absolute()
    assert f"Task directory: {add_dir}" in prompt


def test_resumed_session_also_sees_past_artifacts(task, fake_claude, monkeypatch):
    monkeypatch.chdir(Path.home())
    L.resume_session(task, "abc-123", replace=False)
    _, args = _read_log(fake_claude)
    prompt = args[args.index("--append-system-prompt") + 1]
    assert "Artifacts from earlier sessions" in prompt
    assert str(task.dir) in prompt


def test_refused_launch_records_no_session(tasks_repo, project, fake_claude, monkeypatch):
    real = T.create_task(tasks_repo, project, "t")
    broken = T.Task.load(real.dir)
    broken.dir = Path("relative/task/dir")
    monkeypatch.chdir(Path.home())

    with pytest.raises(L.LauncherError):
        L.launch_session(broken, replace=False)

    assert T.Task.load(real.dir).sessions == []
    assert not fake_claude.exists()      # claude was never invoked


def test_script_rule_is_a_mechanical_bright_line(task):
    prompt = L.artifact_system_prompt(task)
    assert "Never pass multi-line code to an interpreter inline" in prompt
    # the concrete triggers, so there is nothing to interpret
    for trigger in ("python3 -c", "node -e", "bash -c", "heredoc"):
        assert trigger in prompt
    assert f"{task.dir}/scripts/" in prompt
    assert "invoked from there" in prompt


def test_script_rule_has_no_triviality_exemption(task):
    """The judgement call was deliberately removed; it must not creep back."""
    prompt = L.artifact_system_prompt(task)
    assert "No triviality exception" in prompt
    for weasel in ("significance to the task", "trivial one-liner",
                   "annoying to reconstruct", "if the script took any thought",
                   "might plausibly want"):
        assert weasel not in prompt
    # and the obvious evasion is closed off
    assert "do not compact multi-line logic onto one line" in prompt


def test_deliverable_script_still_goes_in_a_file(task):
    """The deliverable carve-out must not puncture the bright line."""
    prompt = L.artifact_system_prompt(task)
    assert "still goes in a file rather than inline" in prompt
    assert "the location it was asked for" in prompt


def test_prompt_keeps_project_scripts_out_of_the_task_dir(task):
    """The script rule must not override the task-root carve-out."""
    prompt = L.artifact_system_prompt(task)
    exception = prompt.split("The one exception")[1]
    assert "belongs to that project" in exception
    assert "Do not mirror or stage those in the task directory." in exception


def test_script_rule_names_the_task_dir(task):
    paragraph = [p for p in L.artifact_system_prompt(task).split("\n\n")
                 if "Never pass multi-line code" in p][0]
    assert f"{task.dir}/scripts/<name>" in paragraph
