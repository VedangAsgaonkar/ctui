import os
import subprocess
from pathlib import Path

import pytest

from ctui import gitutil
from ctui.config import Config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep every test off the real $HOME, git identity and hostname."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CTUI_RC", str(home / ".ctuirc"))
    monkeypatch.setenv("CTUI_HOSTNAME", "testhost")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ctui test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "ctui test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    return home


@pytest.fixture
def tasks_repo(tmp_path):
    repo = tmp_path / "tasks"
    gitutil.init(repo)
    (repo / "README.md").write_text("tasks\n")
    gitutil.commit_all(repo, "init")
    return repo


@pytest.fixture
def config(tasks_repo, tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    gitutil.init(wiki)
    cfg = Config(tasks_repo=tasks_repo, wiki_repo=wiki)
    cfg.save(Path(os.environ["CTUI_RC"]))
    return cfg


@pytest.fixture
def project(tmp_path):
    """A stand-in for a user's project directory."""
    p = tmp_path / "proj"
    (p / "sub" / "deep").mkdir(parents=True)
    return p


@pytest.fixture
def bare_remote(tmp_path):
    """An empty bare repo usable as an `origin`."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    return remote


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A fake `claude` that records its cwd and argv as JSON instead of running.

    JSON rather than one-arg-per-line because --append-system-prompt is multi-line.
    """
    log = tmp_path / "claude-invocations.jsonl"
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:]}) + '\\n')\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(script))
    return log
