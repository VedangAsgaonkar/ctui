"""Launching and resuming claude-code sessions inside a task."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .tasks import Task, new_session_id

CLAUDE_BIN_ENV = "CTUI_CLAUDE_BIN"


class LauncherError(Exception):
    pass


def claude_bin() -> str:
    override = os.environ.get(CLAUDE_BIN_ENV)
    if override:
        return override
    found = shutil.which("claude")
    if not found:
        raise LauncherError(
            "Could not find the `claude` executable on PATH. "
            f"Install claude-code or set {CLAUDE_BIN_ENV} to its full path."
        )
    return found


def artifact_system_prompt(task: Task) -> str:
    """Extra system prompt telling the session where its artifacts live.

    Both paths are absolute (see _require_absolute): claude is exec'd with cwd
    set to the task root, so a relative task-directory path would be read
    against the wrong directory.
    """
    return (
        "You are running inside a ctui-managed task.\n"
        f"- Task name: {task.name}\n"
        f"- Task directory: {task.dir}\n"
        f"- Task root (the working tree for this task): {task.root}\n"
        "\n"
        f"The task directory {task.dir} is this task's persistent workspace, shared "
        "by every claude session ever launched for this task. Artifacts from earlier "
        "sessions of this task are already in there, so look before you start: list "
        "that directory and read whatever notes, plans, logs, data or reports you "
        "find, and continue from where the previous session left off instead of "
        "redoing work or contradicting decisions already made. It is simply empty if "
        "this is the task's first session.\n"
        "\n"
        "Artifact policy for this session: write every intermediate and output "
        f"artifact into {task.dir} — scratch files, notes, logs, generated data, "
        "analyses, plans, reports, and anything else that is a byproduct of the work "
        "rather than the work itself. Prefer it over /tmp or ad-hoc scratch locations, "
        "and use subdirectories inside it to stay organised. Leave it in a state a "
        "later session can pick up.\n"
        "\n"
        "Never pass multi-line code to an interpreter inline. Any `python3 -c`, "
        "`node -e`, `perl -e`, `bash -c` or heredoc carrying more than one line of "
        f"code goes into {task.dir}/scripts/<name> first — with the extension for "
        "its language — and is invoked from there. No triviality exception: this "
        "holds however short, obvious or throwaway the code is, and whether or not "
        "you expect to run it again. Single-line shell commands and pipelines are "
        "unaffected, but do not compact multi-line logic onto one line to avoid "
        "this.\n"
        "\n"
        "A script that is itself the deliverable still goes in a file rather than "
        "inline — just in the location it was asked for, rather than the task "
        "directory.\n"
        "\n"
        f"The one exception is changes to the task root itself: edits to the project "
        f"under {task.root} (source files, configs, tests, docs that belong to that "
        "project) go in place, exactly as normal — including a script that genuinely "
        "belongs to that project, which goes wherever the project keeps its scripts. "
        "Do not mirror or stage those in the task directory."
    )


def _require_absolute(task: Task) -> None:
    """Guard the invariant the prompt and --add-dir depend on."""
    for label, path in (("task directory", task.dir), ("task root", task.root)):
        if not path.is_absolute():
            raise LauncherError(
                f"{label} {path} is not an absolute path; refusing to launch claude "
                "with a path it would resolve against the wrong directory."
            )


def _claude_argv(task: Task, extra: list[str], session_args: list[str]) -> list[str]:
    _require_absolute(task)
    argv = [claude_bin()]
    argv += session_args
    argv += ["--add-dir", str(task.dir)]
    argv += ["--append-system-prompt", artifact_system_prompt(task)]
    argv += extra
    return argv


def _exec(argv: list[str], cwd: Path, replace: bool) -> int:
    """Run claude with `cwd` as the working directory.

    `replace` uses execv so claude takes over this process (no wrapper left in the
    tree); otherwise we wait on a child, which the tests use.
    """
    if not cwd.is_dir():
        raise LauncherError(f"Task root {cwd} does not exist on this machine.")
    os.chdir(cwd)
    if replace:
        os.execv(argv[0], argv)  # never returns
    return subprocess.run(argv).returncode


def register_session(task: Task, forked_from: str | None = None) -> str:
    """Mint a session ID and record it in task.json.

    Split from launch_session so the caller can commit the registration before we
    exec claude — once execv takes over there is no "after" to commit in.

    `forked_from` records the parent when this session is a fork. claude does not
    keep that link itself: a forked transcript is rewritten to carry only the new
    session's id and never mentions the parent, so if ctui does not record the
    lineage nothing does.
    """
    session_id = new_session_id()
    task.add_session(session_id, forked_from=forked_from)
    return session_id


def launch_session(task: Task, extra: list[str] | None = None, replace: bool = True,
                   session_id: str | None = None) -> tuple[str, int]:
    """Start a new claude session in the task root.

    The ID is generated up front and passed via --session-id, so it is registered
    in task.json before claude starts rather than scraped afterwards.
    """
    # Validate before minting a session, so a bad path cannot leave a session
    # recorded in task.json for a claude that never started.
    _require_absolute(task)
    session_id = session_id or register_session(task)
    argv = _claude_argv(
        task,
        extra or [],
        ["--session-id", session_id, "--name", task.name],
    )
    code = _exec(argv, task.root, replace)
    return session_id, code


def resume_session(task: Task, session_id: str, extra: list[str] | None = None,
                   replace: bool = True) -> int:
    argv = _claude_argv(task, extra or [], ["--resume", session_id])
    return _exec(argv, task.root, replace)


def fork_session(task: Task, parent_id: str, extra: list[str] | None = None,
                 replace: bool = True, session_id: str | None = None) -> tuple[str, int]:
    """Branch a new session off `parent_id`, leaving the parent untouched.

    `--fork-session` honours an explicit `--session-id`, so a fork is registered
    in task.json before claude starts, exactly like a fresh launch. The parent's
    transcript is not appended to; the fork gets its own, carrying a copy of the
    parent's history with the original timestamps.
    """
    _require_absolute(task)
    session_id = session_id or register_session(task, forked_from=parent_id)
    argv = _claude_argv(
        task,
        extra or [],
        ["--resume", parent_id, "--fork-session",
         "--session-id", session_id, "--name", task.name],
    )
    code = _exec(argv, task.root, replace)
    return session_id, code


def open_shell(task: Task, replace: bool = True) -> int:
    """Drop the user into a shell rooted at the task root."""
    _require_absolute(task)
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return _exec([shell], task.root, replace)
