"""Implementations of the ctui subcommands."""

from __future__ import annotations

import os
from pathlib import Path

from questionary import Choice

from . import gitutil, ui
from .config import (
    DEFAULT_TASKS_DIRNAME,
    DEFAULT_WIKI_DIRNAME,
    Config,
    load,
    load_or_none,
    rc_path,
)
from .launcher import (
    LauncherError,
    launch_session,
    open_shell,
    register_session,
    resume_session,
)
from .tasks import (
    Task,
    TaskError,
    create_task,
    find_tasks_from_cwd,
    hostname,
    load_tasks,
    record_access,
    recent_tasks,
    tasks_for_root,
)


class CommandError(Exception):
    """A user-facing failure; cli.main turns it into an error message + exit 1."""


# =====================================================================
# setup
# =====================================================================

def _prepare_repo(path: Path, remote: str | None, label: str) -> None:
    """Make `path` a git repo, wired to `remote` and synced with it if given."""
    existed = gitutil.is_repo(path)

    if existed:
        ui.step(f"{label} repo already exists at {path}")
    elif remote:
        ui.step(f"cloning {label} from {remote}")
        res = gitutil.clone(remote, path)
        if not res.ok:
            # An empty remote cannot always be cloned into a usable checkout;
            # fall back to init + remote so setup still succeeds.
            ui.warn(f"clone failed ({res.stderr.strip() or 'unknown error'})")
            ui.step(f"initialising empty {label} repo at {path} instead")
            gitutil.init(path)
        else:
            ui.ok(f"cloned {label} into {path}")
    else:
        ui.step(f"initialising {label} repo at {path}")
        gitutil.init(path)

    if not gitutil.is_repo(path):
        raise CommandError(f"Failed to create a git repo at {path}.")

    outer = gitutil.containing_repo(path)
    if outer:
        ui.warn(f"note: this sits inside the git repo at {outer},")
        ui.warn(f"      which will list {path.name}/ as an untracked directory")

    if remote:
        gitutil.set_remote(path, remote)
        ui.ok(f"origin = {remote}")

        # Follow the remote's branch name rather than whatever `git init` chose
        # locally, or we would commit onto a parallel branch that never syncs.
        branch = gitutil.remote_default_branch(path) or gitutil.default_branch(path)
        if not gitutil.has_commits(path) and gitutil.default_branch(path) != branch:
            gitutil.checkout_branch(path, branch)

        res = gitutil.pull(path, branch)
        if res.ok:
            ui.ok(f"synced with origin/{branch}")
        else:
            ui.warn(f"could not pull origin/{branch}: {res.stderr.strip() or res.out}")

    # Give a still-empty repo something to commit so it has a branch to push.
    if not gitutil.has_commits(path):
        readme = path / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# ctui {label} repo\n\nManaged by ctui. Do not hand-edit task metadata.\n"
            )
        _ensure_gitignore(path)
        if gitutil.commit_all(path, f"ctui: initialise {label} repo"):
            ui.ok("created initial commit")
        if remote:
            ui.step("run `ctui --sync` to push it to origin")


def _ensure_gitignore(repo: Path) -> None:
    """Keep OS/editor noise out of the synced repos."""
    gitignore = repo / ".gitignore"
    wanted = [".DS_Store", "*.swp", "__pycache__/"]
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [line for line in wanted if line not in existing]
    if missing:
        body = "\n".join([*existing, *missing]).strip() + "\n"
        gitignore.write_text(body)


def cmd_setup(rc: Path | None = None) -> int:
    target_rc = rc or rc_path()
    ui.heading("ctui setup")

    current = load_or_none(target_rc)
    if current:
        ui.info(f"Found an existing config at {target_rc}.")
        if not ui.ask_confirm("Reconfigure it?", default=False):
            ui.info("Nothing changed.")
            return 0

    ui.info("\nRemotes are optional — leave one blank to keep that repo local-only.")
    tasks_remote = ui.ask_text(
        "Git remote for the tasks repo (optional):",
        default=(current.tasks_remote if current and current.tasks_remote else ""),
        allow_empty=True,
    )
    wiki_remote = ui.ask_text(
        "Git remote for the wiki repo:",
        default=(current.wiki_remote if current and current.wiki_remote else ""),
        allow_empty=True,
    )

    default_base = str(Path.home())
    if current:
        default_base = str(current.tasks_repo.parent)
    base = ui.ask_path(
        "Path to hold the local tasks and wiki checkouts:",
        default=default_base,
    ).resolve()

    tasks_repo = base / DEFAULT_TASKS_DIRNAME
    wiki_repo = base / DEFAULT_WIKI_DIRNAME
    if current:
        # Honour existing checkout locations if the base directory is unchanged.
        if current.tasks_repo.parent.resolve() == base:
            tasks_repo = current.tasks_repo
            if current.wiki_repo:
                wiki_repo = current.wiki_repo

    ui.heading("Tasks repo")
    _prepare_repo(tasks_repo, tasks_remote or None, "tasks")

    ui.heading("Wiki repo")
    _prepare_repo(wiki_repo, wiki_remote or None, "wiki")

    config = Config(
        tasks_repo=tasks_repo,
        wiki_repo=wiki_repo,
        tasks_remote=tasks_remote or None,
        wiki_remote=wiki_remote or None,
    )
    written = config.save(target_rc)

    ui.heading("Done")
    ui.ok(f"tasks repo: {tasks_repo}")
    ui.ok(f"wiki repo:  {wiki_repo}")
    ui.ok(f"config:     {written}")
    ui.info(f"\nRun `ctui --init` in a project directory to create your first task.")
    return 0


# =====================================================================
# sync
# =====================================================================

def _sync_repo(path: Path, remote: str | None, label: str) -> bool:
    """Commit, pull, then push one repo. Returns False if anything went wrong."""
    ui.heading(f"{label}: {path}")
    if not gitutil.is_repo(path):
        ui.warn("not a git repo — skipping (re-run `ctui --setup`?)")
        return False

    healthy = True

    if gitutil.is_dirty(path):
        if gitutil.commit_all(path, f"ctui sync from {hostname()}"):
            ui.ok("committed local changes")
    else:
        ui.step("no local changes")

    if not remote:
        ui.step("no remote configured — local only")
        return healthy

    branch = gitutil.default_branch(path)

    res = gitutil.pull(path, branch)
    if res.ok:
        ui.ok(f"pulled origin/{branch}")
    else:
        ui.warn(f"pull failed: {res.stderr.strip() or res.out}")
        ui.warn("skipping push so the conflict can be resolved by hand")
        return False

    res = gitutil.push(path, branch)
    if res.ok:
        ui.ok(f"pushed origin/{branch}")
    else:
        ui.warn(f"push failed: {res.stderr.strip() or res.out}")
        healthy = False

    return healthy


def cmd_sync() -> int:
    config = load()
    all_ok = _sync_repo(config.tasks_repo, config.tasks_remote, "tasks")
    if config.wiki_repo:
        all_ok = _sync_repo(config.wiki_repo, config.wiki_remote, "wiki") and all_ok
    else:
        ui.heading("wiki")
        ui.step("no wiki repo configured")
    return 0 if all_ok else 1


# =====================================================================
# init
# =====================================================================

def cmd_init(name: str | None = None, launch: bool = True,
             extra: list[str] | None = None, replace: bool = True) -> int:
    config = load()
    cwd = Path.cwd().resolve()

    same_root = tasks_for_root(config.tasks_repo, cwd)
    if same_root and not name:
        ui.warn(f"{cwd} already has {len(same_root)} task(s):")
        for t in same_root:
            ui.info(f"    {t.task_id}  {t.name}")
        if not ui.ask_confirm("Create another task for this directory?", default=False):
            ui.info("Nothing created. Use `ctui --launch` to start a session in an existing task.")
            return 0

    task_name = name or ui.ask_text("Task name:", default=cwd.name)

    try:
        task = create_task(config.tasks_repo, cwd, task_name)
    except TaskError as exc:
        raise CommandError(str(exc)) from exc

    ui.ok(f"created {task.dir}")
    ui.ok(f"root -> {task.root}")

    gitutil.commit_all(config.tasks_repo, f"ctui: new task {task.task_id} ({task.name}) on {task.host}")

    if not launch:
        ui.info("\nRun `ctui --launch` here to start a claude session in this task.")
        return 0

    return _launch(task, extra or [], config.tasks_repo, replace)


# =====================================================================
# launch
# =====================================================================

def _pick_task(tasks: list[Task], message: str) -> Task:
    if len(tasks) == 1:
        return tasks[0]
    choices = [
        Choice(title=f"{t.task_id}  {t.name}  [{t.host}]", value=t) for t in tasks
    ]
    return ui.ask_select(message, choices)


def _launch(task: Task, extra: list[str], tasks_repo: Path, replace: bool = True) -> int:
    session_id = register_session(task)
    record_access(tasks_repo, task)
    gitutil.commit_all(
        tasks_repo,
        f"ctui: session {session_id[:8]} in {task.task_id} ({task.name})",
    )
    ui.heading(f"launching claude in {task.root}")
    ui.step(f"session {session_id}")
    try:
        _, code = launch_session(task, extra, replace=replace, session_id=session_id)
    except LauncherError as exc:
        raise CommandError(str(exc)) from exc
    # execv replaced the process, so this is only reached in the no-exec path.
    return code


def cmd_launch(extra: list[str] | None = None, replace: bool = True) -> int:
    config = load()
    cwd = Path.cwd().resolve()

    tasks = find_tasks_from_cwd(config.tasks_repo, cwd)
    if not tasks:
        raise CommandError(
            f"No ctui task covers {cwd}.\nRun `ctui --init` here to create one."
        )

    task = _pick_task(tasks, "Which task should this session belong to?")
    return _launch(task, extra or [], config.tasks_repo, replace)


# =====================================================================
# resume
# =====================================================================

SHELL_CHOICE = "__shell__"
BACK_CHOICE = "__back__"

SCOPE_LOCAL = "local"
SCOPE_GLOBAL = "global"
SCOPE_RECENT = "recent"
RECENT_LIMIT = 5


def _task_choices(tasks: list[Task]) -> list[Choice]:
    this_host = hostname()
    choices = []
    for t in tasks:
        n = len(t.sessions)
        bits = [f"{n} session{'s' if n != 1 else ''}"]
        if t.host != this_host:
            bits.append(t.host)
        if not t.root_exists:
            bits.append("root missing")
        choices.append(Choice(
            title=f"{t.task_id}  {t.name}  ({', '.join(bits)})",
            value=t,
        ))
    return choices


def _session_choices(task: Task) -> list[Choice]:
    choices = [
        Choice(
            title=f"resume session {s.session_id[:8]}  ({s.created_display})"
                  + (f"  {s.label}" if s.label else ""),
            value=s.session_id,
        )
        for s in reversed(task.sessions)
    ]
    choices.append(Choice(title="open a shell in the task root", value=SHELL_CHOICE))
    choices.append(Choice(title="← back to task list", value=BACK_CHOICE))
    return choices


def _tilde(path: Path) -> str:
    """Abbreviate the home prefix, to keep prompt labels readable."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _resume_candidates(config, scope: str) -> tuple[list[Task], str]:
    """Tasks to offer for the given scope, plus a label for the prompt."""
    if scope == SCOPE_RECENT:
        tasks = recent_tasks(config.tasks_repo, RECENT_LIMIT)
        return tasks, f"{RECENT_LIMIT} most recently accessed"
    if scope == SCOPE_GLOBAL:
        return load_tasks(config.tasks_repo), "all tasks"
    cwd = Path.cwd().resolve()
    return find_tasks_from_cwd(config.tasks_repo, cwd), f"rooted at {_tilde(cwd)}"


def _no_candidates_error(config, scope: str) -> CommandError:
    if scope == SCOPE_RECENT:
        return CommandError(
            "No tasks have been opened on this machine yet.\n"
            "Try `ctui --resume --global` to pick from every task."
        )
    if scope == SCOPE_GLOBAL:
        return CommandError(
            f"No tasks found in {config.tasks_repo}.\n"
            "Run `ctui --init` in a project directory to create one."
        )
    return CommandError(
        f"No ctui task covers {Path.cwd().resolve()}.\n"
        "Run `ctui --init` here to create one, or `ctui --resume --global` "
        "to pick from every task."
    )


def cmd_resume(extra: list[str] | None = None, scope: str = SCOPE_LOCAL,
               replace: bool = True) -> int:
    config = load()
    tasks, label = _resume_candidates(config, scope)
    if not tasks:
        raise _no_candidates_error(config, scope)

    while True:
        task = _pick_task_for_resume(tasks, label)
        if not task.root_exists:
            ui.warn(f"task root {task.root} does not exist on this machine ({hostname()}).")
            if not ui.ask_confirm("Pick a different task?", default=True):
                return 1
            continue

        choice = ui.ask_select(
            f"{task.task_id} — {task.name}",
            _session_choices(task),
        )

        if choice == BACK_CHOICE:
            continue

        try:
            record_access(config.tasks_repo, task)
            if choice == SHELL_CHOICE:
                ui.heading(f"opening {os.environ.get('SHELL', 'a shell')} in {task.root}")
                ui.info("(exit the shell to come back)")
                return open_shell(task, replace=replace)
            ui.heading(f"resuming session {choice[:8]} in {task.root}")
            return resume_session(task, choice, extra or [], replace=replace)
        except LauncherError as exc:
            raise CommandError(str(exc)) from exc


def _pick_task_for_resume(tasks: list[Task], label: str) -> Task:
    return ui.ask_select(f"Select a task ({label}):", _task_choices(tasks))


# =====================================================================
# list (non-interactive convenience)
# =====================================================================

def cmd_list() -> int:
    config = load()
    tasks = load_tasks(config.tasks_repo)
    if not tasks:
        ui.info(f"No tasks in {config.tasks_repo}.")
        return 0
    this_host = hostname()
    for t in tasks:
        marker = " " if t.host == this_host else "*"
        flag = "" if t.root_exists else "  (root missing)"
        ui.info(f"{marker} {t.task_id}  {t.name}")
        ui.info(f"    host={t.host}  sessions={len(t.sessions)}  root={t.root}{flag}")
    if any(t.host != this_host for t in tasks):
        ui.info("\n* = task from another host")
    return 0
