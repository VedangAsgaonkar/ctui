
import pytest

from ctui import gitutil as G


def test_init_and_is_repo(tmp_path):
    repo = tmp_path / "r"
    assert not G.is_repo(repo)
    G.init(repo)
    assert G.is_repo(repo)
    assert not G.has_commits(repo)


def test_commit_all_is_a_noop_when_clean(tmp_path):
    repo = tmp_path / "r"
    G.init(repo)
    (repo / "f.txt").write_text("hi\n")
    assert G.commit_all(repo, "one") is True
    assert G.has_commits(repo)
    assert G.commit_all(repo, "two") is False


def test_is_dirty(tmp_path):
    repo = tmp_path / "r"
    G.init(repo)
    (repo / "f.txt").write_text("hi\n")
    assert G.is_dirty(repo)
    G.commit_all(repo, "c")
    assert not G.is_dirty(repo)


def test_set_remote_adds_then_updates(tmp_path):
    repo = tmp_path / "r"
    G.init(repo)
    assert G.get_remote_url(repo) is None
    G.set_remote(repo, "https://example.invalid/a.git")
    assert G.get_remote_url(repo) == "https://example.invalid/a.git"
    G.set_remote(repo, "https://example.invalid/b.git")
    assert G.get_remote_url(repo) == "https://example.invalid/b.git"


def test_default_branch_on_unborn_head(tmp_path):
    repo = tmp_path / "r"
    G.init(repo)
    # No commits yet, but symbolic-ref still resolves the branch git init chose.
    assert G.default_branch(repo) == "main"


def test_remote_default_branch_reads_remote_head(tmp_path, bare_remote):
    seed = tmp_path / "seed"
    G.init(seed)
    G.checkout_branch(seed, "trunk")
    (seed / "f.txt").write_text("x\n")
    G.commit_all(seed, "c")
    G.set_remote(seed, str(bare_remote))
    G.push(seed, "trunk")

    probe = tmp_path / "probe"
    G.init(probe)
    G.set_remote(probe, str(bare_remote))
    # The bare remote's HEAD says `main`, which has no commits; `trunk` is the
    # only real branch, so that is what a fresh checkout should follow.
    assert G.remote_branches(probe, "origin") == ["trunk"]
    assert G.remote_default_branch(probe) == "trunk"


def test_remote_default_branch_none_when_ambiguous(tmp_path, bare_remote):
    seed = tmp_path / "seed"
    G.init(seed)
    for branch in ("one", "two"):
        G.checkout_branch(seed, branch)
        (seed / f"{branch}.txt").write_text("x\n")
        G.commit_all(seed, branch)
    G.set_remote(seed, str(bare_remote))
    G.run(seed, "push", "origin", "one", "two")

    probe = tmp_path / "probe"
    G.init(probe)
    G.set_remote(probe, str(bare_remote))
    assert sorted(G.remote_branches(probe)) == ["one", "two"]
    assert G.remote_default_branch(probe) is None


def test_pull_is_noop_when_remote_lacks_branch(tmp_path, bare_remote):
    repo = tmp_path / "r"
    G.init(repo)
    (repo / "f.txt").write_text("x\n")
    G.commit_all(repo, "c")
    G.set_remote(repo, str(bare_remote))
    res = G.pull(repo, G.default_branch(repo))
    assert res.ok
    assert "nothing to pull" in res.out


def test_push_then_pull_round_trip(tmp_path, bare_remote):
    branch = "main"

    first = tmp_path / "first"
    G.init(first)
    G.run(first, "checkout", "-B", branch)
    (first / "f.txt").write_text("v1\n")
    G.commit_all(first, "c1")
    G.set_remote(first, str(bare_remote))
    assert G.push(first, branch).ok

    second = tmp_path / "second"
    G.init(second)
    G.run(second, "checkout", "-B", branch)
    G.set_remote(second, str(bare_remote))
    # Unborn HEAD locally, real branch on the remote: must adopt the remote.
    assert G.pull(second, branch).ok
    assert (second / "f.txt").read_text() == "v1\n"


def test_pull_rebases_local_commits(tmp_path, bare_remote):
    branch = "main"
    a = tmp_path / "a"
    G.init(a)
    G.run(a, "checkout", "-B", branch)
    (a / "shared.txt").write_text("base\n")
    G.commit_all(a, "base")
    G.set_remote(a, str(bare_remote))
    G.push(a, branch)

    b = tmp_path / "b"
    assert G.clone(str(bare_remote), b).ok

    (a / "from-a.txt").write_text("a\n")
    G.commit_all(a, "a work")
    G.push(a, branch)

    (b / "from-b.txt").write_text("b\n")
    G.commit_all(b, "b work")
    assert G.pull(b, branch).ok
    assert (b / "from-a.txt").exists()
    assert (b / "from-b.txt").exists()


def test_run_raises_on_failure(tmp_path):
    repo = tmp_path / "r"
    G.init(repo)
    with pytest.raises(G.GitError):
        G.run(repo, "checkout", "no-such-branch")
    assert not G.run(repo, "checkout", "no-such-branch", check=False).ok


# ---- nesting: a directory inside a repo is not itself a repo --------

def test_is_repo_only_at_the_worktree_root(tmp_path):
    outer = tmp_path / "outer"
    G.init(outer)
    plain = outer / "plain"
    plain.mkdir()

    assert G.is_repo(outer)
    assert not G.is_repo(plain)          # inside a repo, but not a repo


def test_is_repo_false_for_a_nested_non_repo(tmp_path):
    """The exact shape that misdirected git at an enclosing repo."""
    home = tmp_path / "home"
    G.init(home)
    (home / "tracked.txt").write_text("dotfile\n")
    G.commit_all(home, "dotfiles")

    tasks = home / "ctui-tasks"
    tasks.mkdir()
    assert not G.is_repo(tasks)
    # ... and once initialised, it is its own repo, not the parent
    G.init(tasks)
    assert G.is_repo(tasks)
    assert G.run(tasks, "rev-parse", "--show-toplevel").out == str(tasks)


def test_is_repo_true_for_a_nested_repo(tmp_path):
    outer = tmp_path / "outer"
    G.init(outer)
    inner = outer / "inner"
    G.init(inner)
    assert G.is_repo(inner) and G.is_repo(outer)


def test_commit_all_never_stages_an_enclosing_worktree(tmp_path):
    """Regression: `git add -A` aimed at a non-repo swept up the parent tree."""
    home = tmp_path / "home"
    G.init(home)
    G.commit_all(home, "base")
    heavy = home / "heavy"
    heavy.mkdir()
    for i in range(5):
        (heavy / f"big{i}.bin").write_text("x" * 100)

    tasks = home / "ctui-tasks"
    tasks.mkdir()
    (tasks / "task.json").write_text("{}\n")

    G.commit_all(tasks, "ctui: task")

    staged = G.run(home, "diff", "--cached", "--name-only", check=False).out
    committed = G.run(home, "show", "--name-only", "--pretty=", "HEAD").out
    assert "heavy/" not in staged and "heavy/" not in committed
    for name in (staged + committed).splitlines():
        assert name.startswith("ctui-tasks/"), name


def test_is_dirty_ignores_an_enclosing_worktree(tmp_path):
    home = tmp_path / "home"
    G.init(home)
    (home / "noise.txt").write_text("untracked\n")
    tasks = home / "ctui-tasks"
    tasks.mkdir()

    assert not G.is_dirty(tasks)
    (tasks / "new.json").write_text("{}\n")
    assert G.is_dirty(tasks)


def test_containing_repo(tmp_path):
    home = tmp_path / "home"
    G.init(home)
    nested = home / "nested"
    nested.mkdir()

    assert G.containing_repo(nested) == home
    G.init(nested)
    assert G.containing_repo(nested) == home     # not masked by its own repo
    assert G.containing_repo(tmp_path / "loose") is None
