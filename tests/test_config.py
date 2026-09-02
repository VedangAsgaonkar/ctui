import json
import os
from pathlib import Path

import pytest

from ctui import config as CFG


def test_round_trip(tmp_path):
    rc = tmp_path / "rc.json"
    CFG.Config(
        tasks_repo=Path("/a/tasks"),
        wiki_repo=Path("/a/wiki"),
        tasks_remote="git@example.invalid:t.git",
        wiki_remote="git@example.invalid:w.git",
    ).save(rc)

    loaded = CFG.load(rc)
    assert loaded.tasks_repo == Path("/a/tasks")
    assert loaded.wiki_repo == Path("/a/wiki")
    assert loaded.tasks_remote == "git@example.invalid:t.git"
    assert loaded.wiki_remote == "git@example.invalid:w.git"
    assert loaded.version == CFG.RC_VERSION


def test_saved_file_is_readable_json(tmp_path):
    rc = tmp_path / "rc.json"
    CFG.Config(tasks_repo=Path("/a/tasks")).save(rc)
    raw = json.loads(rc.read_text())
    assert raw["tasks_repo"] == "/a/tasks"
    assert raw["wiki_repo"] is None


def test_paths_are_expanded(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text(json.dumps({"tasks_repo": "~/t", "wiki_repo": "~/w"}))
    loaded = CFG.load(rc)
    assert loaded.tasks_repo == Path.home() / "t"
    assert loaded.wiki_repo == Path.home() / "w"


def test_empty_remotes_normalise_to_none(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text(json.dumps({"tasks_repo": "/t", "tasks_remote": "", "wiki_remote": ""}))
    loaded = CFG.load(rc)
    assert loaded.tasks_remote is None and loaded.wiki_remote is None


def test_rc_path_honours_env():
    assert CFG.rc_path() == Path(os.environ["CTUI_RC"])


def test_rc_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CTUI_RC")
    assert CFG.rc_path() == Path.home() / ".ctuirc"


def test_missing_file_raises(tmp_path):
    with pytest.raises(CFG.ConfigError, match="ctui --setup"):
        CFG.load(tmp_path / "absent.json")


def test_non_object_raises(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text("[]")
    with pytest.raises(CFG.ConfigError, match="JSON object"):
        CFG.load(rc)


def test_load_or_none(tmp_path):
    assert CFG.load_or_none(tmp_path / "absent.json") is None


def test_relative_repo_path_is_rejected(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text(json.dumps({"tasks_repo": "ctui-tasks"}))
    with pytest.raises(CFG.ConfigError, match="must be an absolute path"):
        CFG.load(rc)


def test_relative_wiki_path_is_rejected(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text(json.dumps({"tasks_repo": "/t", "wiki_repo": "../wiki"}))
    with pytest.raises(CFG.ConfigError, match="wiki_repo"):
        CFG.load(rc)


def test_tilde_paths_are_still_accepted(tmp_path):
    rc = tmp_path / "rc.json"
    rc.write_text(json.dumps({"tasks_repo": "~/t"}))
    assert CFG.load(rc).tasks_repo == Path.home() / "t"
