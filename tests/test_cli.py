import json

import pytest

from ctui import cli
from ctui import commands as C
from ctui import ui


def test_no_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    assert "ctui --init" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert cli.VERSION in capsys.readouterr().out


def test_modes_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--init", "--resume"])
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("flag,fn", [
    ("--setup", "cmd_setup"),
    ("--sync", "cmd_sync"),
    ("--list", "cmd_list"),
])
def test_flags_dispatch(flag, fn, monkeypatch):
    called = {}

    def _stub(*a, **k):
        called["hit"] = True
        return 0

    monkeypatch.setattr(C, fn, _stub)
    assert cli.main([flag]) == 0
    assert called["hit"]


def test_init_passes_name_and_no_launch(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_init", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--init", "-n", "my task", "--no-launch"]) == 0
    assert seen["name"] == "my task"
    assert seen["launch"] is False


def test_claude_args_are_passed_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_launch", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--launch", "--", "--model", "opus", "--effort", "high"]) == 0
    assert seen["extra"] == ["--model", "opus", "--effort", "high"]


def test_passthrough_rejected_for_sync(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--sync", "--", "--model", "opus"])
    assert "pass-through" in capsys.readouterr().err


def test_missing_config_is_a_clean_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CTUI_RC", str(tmp_path / "nope.json"))
    assert cli.main(["--list"]) == 1
    assert "ctui --setup" in capsys.readouterr().err


def test_command_error_is_a_clean_error(config, project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    assert cli.main(["--launch"]) == 1
    assert "ctui --init" in capsys.readouterr().err


def test_abort_at_a_prompt_exits_130(monkeypatch, capsys):
    monkeypatch.setattr(C, "cmd_setup", lambda *a, **k: (_ for _ in ()).throw(
        ui.Aborted("Cancelled.")))
    assert cli.main(["--setup"]) == 130


def test_keyboard_interrupt_exits_130(monkeypatch):
    monkeypatch.setattr(C, "cmd_sync", lambda *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    assert cli.main(["--sync"]) == 130


def test_corrupt_ctuirc_is_reported(monkeypatch, tmp_path, capsys):
    rc = tmp_path / "bad.json"
    rc.write_text("{oops")
    monkeypatch.setenv("CTUI_RC", str(rc))
    assert cli.main(["--list"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_ctuirc_without_tasks_repo_is_reported(monkeypatch, tmp_path, capsys):
    rc = tmp_path / "bad.json"
    rc.write_text(json.dumps({"wiki_repo": "/tmp/w"}))
    monkeypatch.setenv("CTUI_RC", str(rc))
    assert cli.main(["--list"]) == 1
    assert "tasks_repo" in capsys.readouterr().err


# ---- resume scope flags --------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["--resume"], "local"),
    (["--resume", "--global"], "global"),
    (["--resume", "--recent"], "recent"),
])
def test_resume_scope_flags(argv, expected, monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_resume", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(argv) == 0
    assert seen["scope"] == expected


def test_global_and_recent_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--resume", "--global", "--recent"])
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--global", "--recent"])
def test_scope_flags_require_resume(flag, capsys):
    with pytest.raises(SystemExit):
        cli.main([flag])
    assert "only apply to --resume" in capsys.readouterr().err


def test_scope_flags_reject_other_modes(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--list", "--global"])
    assert "only apply to --resume" in capsys.readouterr().err


def test_resume_still_takes_passthrough_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_resume", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--resume", "--global", "--", "--model", "opus"]) == 0
    assert seen["extra"] == ["--model", "opus"]
    assert seen["scope"] == "global"


# ---- dream flags ---------------------------------------------------

def test_dream_dispatches_with_date_and_dry_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_dream", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--dream", "--date", "2026-09-01", "--dry-run"]) == 0
    assert seen["day"] == "2026-09-01"
    assert seen["dry_run"] is True


def test_dream_takes_passthrough_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_dream", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--dream", "--", "--model", "opus"]) == 0
    assert seen["extra"] == ["--model", "opus"]


def test_install_dream_dispatches_with_at(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "cmd_install_dream", lambda **kw: (seen.update(kw), 0)[1])
    assert cli.main(["--install-dream", "--at", "05:00"]) == 0
    assert seen["at"] == "05:00"


def test_uninstall_dream_dispatches(monkeypatch):
    called = {}

    def _stub(*a, **k):
        called["hit"] = True
        return 0

    monkeypatch.setattr(C, "cmd_uninstall_dream", _stub)
    assert cli.main(["--uninstall-dream"]) == 0
    assert called["hit"]


@pytest.mark.parametrize("argv,message", [
    (["--date", "2026-09-01"], "--date only applies to --dream"),
    (["--dry-run"], "--dry-run only applies to --dream"),
    (["--at", "04:00"], "--at only applies to --install-dream"),
    (["--sync", "--date", "2026-09-01"], "--date only applies to --dream"),
    (["--dream", "--at", "04:00"], "--at only applies to --install-dream"),
])
def test_dream_flag_validation(argv, message, capsys):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert message in capsys.readouterr().err


def test_dream_modes_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--dream", "--install-dream"])
    assert "not allowed with" in capsys.readouterr().err
