"""Thin wrappers over the `git` CLI.

We shell out rather than depend on a git library: ctui only needs a handful of
plumbing commands, and this way it uses the user's own git config and credentials.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    def __init__(self, args: list[str], result: subprocess.CompletedProcess):
        self.args_run = args
        self.result = result
        detail = (result.stderr or result.stdout or "").strip()
        super().__init__(f"git {' '.join(args)} failed ({result.returncode}): {detail}")


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


def run(repo: Path | None, *args: str, check: bool = True) -> GitResult:
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(list(args), proc)
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def is_repo(path: Path) -> bool:
    """True only if `path` is the root of its own git worktree.

    `rev-parse --git-dir` succeeds for *any* path inside a repo, so checking it
    would call a plain directory nested in an enclosing repo (e.g. ~/ctui-tasks
    under a dotfiles repo in $HOME) a repo — and every later `git -C` would then
    silently operate on the enclosing repo instead.
    """
    if not path.is_dir():
        return False
    res = run(path, "rev-parse", "--show-toplevel", check=False)
    if not res.ok or not res.out:
        return False
    try:
        return Path(res.out).resolve() == path.resolve()
    except OSError:
        return False


def containing_repo(path: Path) -> Path | None:
    """Root of the nearest git repo that contains `path`, ignoring `path` itself.

    Asked from the parent, so a repo created *at* `path` does not mask an
    enclosing one.
    """
    parent = path.parent
    if not parent.is_dir():
        return None
    res = run(parent, "rev-parse", "--show-toplevel", check=False)
    if not res.ok or not res.out:
        return None
    return Path(res.out)


def init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run(path, "init")


def default_branch(repo: Path) -> str:
    """Current branch name, falling back to init.defaultBranch then 'main'.

    A freshly `git init`ed repo with no commits has an unborn HEAD, so
    `rev-parse --abbrev-ref HEAD` is unreliable; `symbolic-ref` still works.
    """
    res = run(repo, "symbolic-ref", "--short", "HEAD", check=False)
    if res.ok and res.out:
        return res.out
    res = run(repo, "config", "init.defaultBranch", check=False)
    if res.ok and res.out:
        return res.out
    return "main"


def has_commits(repo: Path) -> bool:
    return run(repo, "rev-parse", "--verify", "HEAD", check=False).ok


def get_remote_url(repo: Path, name: str = "origin") -> str | None:
    res = run(repo, "remote", "get-url", name, check=False)
    return res.out if res.ok and res.out else None


def set_remote(repo: Path, url: str, name: str = "origin") -> None:
    if get_remote_url(repo, name) is None:
        run(repo, "remote", "add", name, url)
    else:
        run(repo, "remote", "set-url", name, url)


def is_dirty(repo: Path) -> bool:
    res = run(repo, "status", "--porcelain", "--", ".")
    return bool(res.out)


def commit_all(repo: Path, message: str) -> bool:
    """Stage everything and commit. Returns True if a commit was created."""
    # `-- .` bounds the stage to `repo` itself. At a real repo root that is the
    # whole worktree; if `repo` is somehow not a root, it stops us staging the
    # enclosing repo's entire tree.
    run(repo, "add", "-A", "--", ".")
    staged = run(repo, "diff", "--cached", "--quiet", "--", ".", check=False)
    if staged.ok:  # exit 0 => nothing staged
        return False
    run(repo, "commit", "-m", message)
    return True


def clone(url: str, path: Path) -> GitResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    return run(None, "clone", url, str(path), check=False)


def remote_default_branch(repo: Path, remote: str = "origin") -> str | None:
    """The branch the remote's HEAD points at, or its only branch.

    Needed because a local `git init` picks its own default (often `master`)
    which may not be the branch the remote actually uses.
    """
    res = run(repo, "ls-remote", "--symref", remote, "HEAD", check=False)
    if res.ok:
        for line in res.stdout.splitlines():
            if line.startswith("ref:"):
                ref = line.split()[1]
                if ref.startswith("refs/heads/"):
                    return ref.removeprefix("refs/heads/")
    heads = remote_branches(repo, remote)
    if len(heads) == 1:
        return heads[0]
    return None


def remote_branches(repo: Path, remote: str = "origin") -> list[str]:
    res = run(repo, "ls-remote", "--heads", remote, check=False)
    if not res.ok:
        return []
    names = []
    for line in res.out.splitlines():
        parts = line.split("refs/heads/", 1)
        if len(parts) == 2:
            names.append(parts[1].strip())
    return names


def checkout_branch(repo: Path, branch: str) -> None:
    """Move an unborn/existing HEAD onto `branch`."""
    run(repo, "checkout", "-B", branch)


def remote_has_branch(repo: Path, branch: str, remote: str = "origin") -> bool:
    res = run(repo, "ls-remote", "--heads", remote, branch, check=False)
    return res.ok and bool(res.out)


def pull(repo: Path, branch: str, remote: str = "origin") -> GitResult:
    """Pull with rebase, tolerating a remote that has no such branch yet."""
    if not remote_has_branch(repo, branch, remote):
        return GitResult(0, f"remote {remote} has no branch {branch}; nothing to pull", "")
    if not has_commits(repo):
        # Unborn local HEAD: rebase has nothing to replay onto, so fetch + reset.
        run(repo, "fetch", remote, branch)
        run(repo, "reset", "--hard", f"{remote}/{branch}")
        run(repo, "branch", f"--set-upstream-to={remote}/{branch}", branch, check=False)
        return GitResult(0, f"initialised from {remote}/{branch}", "")
    return run(repo, "pull", "--rebase", remote, branch, check=False)


def push(repo: Path, branch: str, remote: str = "origin") -> GitResult:
    if not has_commits(repo):
        return GitResult(0, "no commits to push", "")
    return run(repo, "push", "--set-upstream", remote, branch, check=False)
