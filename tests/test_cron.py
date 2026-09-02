"""Managing the nightly dream job in the user's crontab."""

import pytest

from ctui import cron


def test_no_crontab_binary_is_reported(monkeypatch):
    monkeypatch.delenv("CTUI_CRONTAB_BIN", raising=False)
    monkeypatch.setattr(cron.shutil, "which", lambda _: None)
    assert not cron.available()
    with pytest.raises(cron.CronError, match="crontab"):
        cron.crontab_bin()


def test_empty_crontab_reads_as_empty(fake_cron):
    """`crontab -l` exits non-zero when there is no crontab; that is not an error."""
    assert not fake_cron.exists()
    assert cron.read_crontab() == ""
    assert not cron.installed()


def test_broken_crontab_is_an_error(monkeypatch, tmp_path):
    script = tmp_path / "angry-crontab"
    script.write_text("#!/bin/sh\necho 'permission denied' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CRONTAB_BIN", str(script))
    with pytest.raises(cron.CronError, match="permission denied"):
        cron.read_crontab()


def test_install_writes_a_marked_block(fake_cron):
    line = cron.install("/abs/ctui", "/abs/claude", 4, 30)
    text = fake_cron.read_text()

    assert cron.MARKER_BEGIN in text and cron.MARKER_END in text
    assert "30 4 * * *" in text
    assert "/abs/ctui --dream" in text
    assert "CTUI_CLAUDE_BIN=/abs/claude" in text
    assert cron.installed()
    assert cron.current_line() == line


def test_installed_line_is_absolute_and_redirects_output(fake_cron):
    """cron has a near-empty PATH, so nothing may be resolved by name."""
    line = cron.install("/abs/ctui", "/abs/claude")
    assert line.startswith(f"{cron.DEFAULT_MINUTE} {cron.DEFAULT_HOUR} * * *")
    assert " /abs/ctui " in line
    assert ">>" in line and "2>&1" in line


def test_install_preserves_other_entries(fake_cron):
    fake_cron.write_text("MAILTO=me\n0 1 * * * /do/something\n")
    cron.install("/abs/ctui", None)
    text = fake_cron.read_text()
    assert "MAILTO=me" in text
    assert "0 1 * * * /do/something" in text
    assert cron.MARKER_BEGIN in text


def test_reinstall_replaces_rather_than_duplicates(fake_cron):
    cron.install("/abs/ctui", None, 4, 30)
    cron.install("/abs/ctui", None, 3, 15)
    text = fake_cron.read_text()
    assert text.count(cron.MARKER_BEGIN) == 1
    assert "15 3 * * *" in text and "30 4 * * *" not in text


def test_uninstall_removes_only_our_block(fake_cron):
    fake_cron.write_text("0 1 * * * /keep/me\n")
    cron.install("/abs/ctui", None)
    assert cron.uninstall() is True

    text = fake_cron.read_text()
    assert "0 1 * * * /keep/me" in text
    assert cron.MARKER_BEGIN not in text
    assert "--dream" not in text
    assert not cron.installed()


def test_uninstall_when_absent_reports_false(fake_cron):
    fake_cron.write_text("0 1 * * * /keep/me\n")
    assert cron.uninstall() is False
    assert fake_cron.read_text().strip() == "0 1 * * * /keep/me"


def test_omitting_claude_path_omits_the_env_prefix(fake_cron):
    line = cron.install("/abs/ctui", None)
    assert "CTUI_CLAUDE_BIN" not in line


def test_install_creates_the_log_directory(fake_cron):
    cron.install("/abs/ctui", None)
    assert cron.log_path().parent.is_dir()


@pytest.mark.parametrize("value,expected", [
    ("04:30", (4, 30)), ("0:00", (0, 0)), ("23:59", (23, 59)), (" 3:05 ", (3, 5)),
])
def test_parse_time(value, expected):
    assert cron.parse_time(value) == expected


@pytest.mark.parametrize("value", ["", "4", "4:", "abc", "24:00", "4:60", "-1:00", "4:30:00"])
def test_parse_time_rejects_junk(value):
    with pytest.raises(cron.CronError):
        cron.parse_time(value)


def test_strip_block_is_a_noop_without_markers():
    text = "MAILTO=me\n0 1 * * * /do/something"
    assert cron.strip_block(text) == text
