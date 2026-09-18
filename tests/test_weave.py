import json
from datetime import date

import pytest

from ctui import commands, cron, weave, wiki
from ctui.cli import main

W38 = date(2026, 9, 16)


def write_page(wiki_repo, host, day, sections, tags=""):
    path = wiki.dated_path(wiki_repo, day, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {day.isoformat()} — {host}", "", f"{wiki.TAGS_PREFIX} {tags}", ""]
    for title, _ in wiki.SECTIONS:
        lines += [f"## {title}", "", sections.get(title, ""), ""]
    lines += [wiki.FOLDED_HEADING, "", "<!-- ctui:session abc -->", ""]
    path.write_text("\n".join(lines))
    return path


@pytest.fixture
def filled(config):
    write_page(config.wiki_repo, "hostA", date(2026, 9, 14),
               {"Commands & workflows": "- run it with `sbatch`",
                "Codebase notes": "- the loader lives in `src/load.py`"})
    write_page(config.wiki_repo, "hostB", date(2026, 9, 15),
               {"Commands & workflows": "- watch it with `squeue`"})
    return config


# ---- week arithmetic -------------------------------------------------

def test_week_id_is_iso():
    assert weave.week_id(W38) == "2026-W38"
    assert weave.week_id(date(2026, 1, 1)) == weave.week_id(date(2025, 12, 29))


def test_week_bounds_are_monday_to_sunday():
    start, end = weave.week_bounds(W38)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-14", "2026-09-20")
    assert start.isoweekday() == 1 and end.isoweekday() == 7


def test_week_bounds_are_stable_within_a_week():
    assert weave.week_bounds(date(2026, 9, 14)) == weave.week_bounds(date(2026, 9, 20))


@pytest.mark.parametrize("value,expected", [
    ("2026-W38", "2026-W38"),
    ("2026-w38", "2026-W38"),
    ("2026-W1", "2026-W01"),
    ("2026-09-16", "2026-W38"),
])
def test_parse_week_accepts_weeks_and_dates(value, expected):
    assert weave.week_id(weave.parse_week(value)) == expected


@pytest.mark.parametrize("value", ["junk", "2026-W99", "2026-W0", "2026-13-40", ""])
def test_parse_week_rejects_nonsense(value):
    with pytest.raises(weave.WeaveError):
        weave.parse_week(value)


def test_last_week_is_a_completed_week():
    assert weave.week_id(weave.last_week()) != weave.week_id(date.today())


# ---- gathering -------------------------------------------------------

def test_dated_pages_spans_hosts_and_respects_the_range(filled):
    start, end = weave.week_bounds(W38)
    found = weave.dated_pages(filled.wiki_repo, start, end)
    assert [(h, d.isoformat()) for h, d, _ in found] == [
        ("hostA", "2026-09-14"), ("hostB", "2026-09-15")]


def test_dated_pages_excludes_other_weeks(filled):
    write_page(filled.wiki_repo, "hostA", date(2026, 9, 7),
               {"Commands & workflows": "- older"})
    start, end = weave.week_bounds(W38)
    days = [d for _, d, _ in weave.dated_pages(filled.wiki_repo, start, end)]
    assert date(2026, 9, 7) not in days


def test_dated_pages_ignores_non_date_filenames(filled):
    (filled.wiki_repo / "dated" / "hostA" / "README.md").write_text("no")
    start, end = weave.week_bounds(W38)
    assert all(p.stem != "README"
               for _, _, p in weave.dated_pages(filled.wiki_repo, start, end))


def test_dated_pages_on_an_empty_wiki(config):
    start, end = weave.week_bounds(W38)
    assert weave.dated_pages(config.wiki_repo, start, end) == []


def test_collect_groups_by_section(filled):
    topics = {t.title: t for t in weave.collect(filled.wiki_repo, W38)}
    assert set(topics) == {"Commands & workflows", "Codebase notes"}
    assert topics["Commands & workflows"].pages == 2
    assert topics["Codebase notes"].pages == 1


def test_collect_skips_empty_sections(filled):
    titles = [t.title for t in weave.collect(filled.wiki_repo, W38)]
    assert "Metrics & evaluation" not in titles


def test_collect_never_treats_the_folded_heading_as_a_topic(filled):
    titles = [t.title for t in weave.collect(filled.wiki_repo, W38)]
    assert wiki.FOLDED_HEADING.removeprefix("## ") not in titles


def test_collect_orders_topics_like_sections(filled):
    write_page(filled.wiki_repo, "hostA", date(2026, 9, 16),
               {title: f"- {title}" for title, _ in wiki.SECTIONS})
    titles = [t.title for t in weave.collect(filled.wiki_repo, W38)]
    assert titles == wiki.section_titles()


def test_collect_is_empty_for_a_quiet_week(config):
    assert weave.collect(config.wiki_repo, W38) == []


def test_topic_markdown_carries_host_and_day(filled):
    topic = next(t for t in weave.collect(filled.wiki_repo, W38)
                 if t.title == "Commands & workflows")
    text = topic.to_markdown()
    assert "hostA" in text and "hostB" in text
    assert "2026-09-14" in text and "sbatch" in text
    assert topic.days == 2 and topic.hosts == ["hostA", "hostB"]


def test_stage_writes_one_file_per_topic(filled, tmp_path):
    topic = weave.collect(filled.wiki_repo, W38)[0]
    path = weave.stage(topic)
    assert path.exists()
    assert path.name == f"{wiki.topic_slug(topic.title)}.md"
    assert topic.week in str(path)


# ---- topic pages -----------------------------------------------------

@pytest.mark.parametrize("title,slug", [
    ("Libraries & tools", "libraries-and-tools"),
    ("Commands & workflows", "commands-and-workflows"),
    ("Metrics & evaluation", "metrics-and-evaluation"),
    ("Codebase notes", "codebase-notes"),
])
def test_topic_slugs(title, slug):
    assert wiki.topic_slug(title) == slug


def test_every_section_has_a_distinct_slug():
    slugs = [wiki.topic_slug(t) for t in wiki.section_titles()]
    assert len(set(slugs)) == len(slugs)


def test_ensure_topic_page_creates_once(config):
    page = wiki.ensure_topic_page(config.wiki_repo, "Codebase notes", "blurb")
    assert page.exists() and "# Codebase notes" in page.read_text()
    page.write_text("edited")
    assert wiki.ensure_topic_page(config.wiki_repo, "Codebase notes").read_text() == "edited"


def test_topic_template_has_the_weeks_heading(config):
    page = wiki.ensure_topic_page(config.wiki_repo, "Codebase notes")
    assert wiki.FOLDED_WEEKS_HEADING in page.read_text()


def test_record_and_read_folded_weeks(config):
    page = wiki.ensure_topic_page(config.wiki_repo, "Codebase notes")
    assert wiki.folded_weeks(page) == set()
    wiki.record_folded_week(page, "2026-W38", 2, 3)
    wiki.record_folded_week(page, "2026-W39", 1, 1)
    assert wiki.folded_weeks(page) == {"2026-W38", "2026-W39"}
    assert "2 day(s) from 3 dated page(s)" in page.read_text()


def test_folded_weeks_survive_reformatting(config):
    page = wiki.ensure_topic_page(config.wiki_repo, "Codebase notes")
    wiki.record_folded_week(page, "2026-W38", 1, 1)
    page.write_text(page.read_text().replace("- `2026-W38`", "* reworded"))
    assert wiki.folded_weeks(page) == {"2026-W38"}


def test_split_sections_and_emptiness():
    text = "# T\n\n## One\n\n<!-- blurb -->\n\n- a\n\n## Two\n\n<!-- blurb -->\n"
    sections = wiki.split_sections(text)
    assert set(sections) == {"One", "Two"}
    assert not wiki.section_is_empty(sections["One"])
    assert wiki.section_is_empty(sections["Two"])


def test_split_sections_ignores_deeper_headings():
    sections = wiki.split_sections("## One\n\n### Sub\n\n- a\n")
    assert set(sections) == {"One"}
    assert "### Sub" in sections["One"]


# ---- the command -----------------------------------------------------

def test_cmd_weave_dry_run_calls_no_claude(filled, fake_claude, capsys):
    assert commands.cmd_weave(week="2026-W38", dry_run=True) == 0
    assert not fake_claude.exists()
    assert "Commands & workflows" in capsys.readouterr().out


def test_cmd_weave_runs_once_per_topic(filled, fake_claude):
    assert commands.cmd_weave(week="2026-W38") == 0
    calls = [json.loads(line) for line in fake_claude.read_text().splitlines()]
    assert len(calls) == 2
    for call in calls:
        assert call["cwd"] == str(filled.wiki_repo)
        assert "--allowed-tools" in call["argv"]
        tools = call["argv"][call["argv"].index("--allowed-tools") + 1]
        assert "Write" not in tools


def test_cmd_weave_creates_and_marks_the_topic_pages(filled, fake_claude):
    commands.cmd_weave(week="2026-W38")
    page = wiki.topic_path(filled.wiki_repo, "Commands & workflows")
    assert page.exists()
    assert wiki.folded_weeks(page) == {"2026-W38"}


def test_cmd_weave_skips_weeks_already_folded_in(filled, fake_claude):
    commands.cmd_weave(week="2026-W38")
    first = len(fake_claude.read_text().splitlines())
    assert commands.cmd_weave(week="2026-W38") == 0
    assert len(fake_claude.read_text().splitlines()) == first


def test_cmd_weave_commits_the_wiki(filled, fake_claude):
    from ctui import gitutil

    commands.cmd_weave(week="2026-W38")
    assert "weave: 2026-W38" in gitutil.run(
        filled.wiki_repo, "log", "-1", "--pretty=%s").out


def test_cmd_weave_on_a_quiet_week(config, fake_claude, capsys):
    assert commands.cmd_weave(week="2026-W01") == 0
    assert "no dated pages" in capsys.readouterr().out
    assert not fake_claude.exists()


def test_cmd_weave_marks_the_week_even_when_nothing_is_new(filled, tmp_path,
                                                           monkeypatch):
    script = tmp_path / "quiet-claude"
    script.write_text("#!/bin/sh\necho '%s'\n" % weave.NOTHING_MARKER)
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(script))
    assert commands.cmd_weave(week="2026-W38") == 0
    page = wiki.topic_path(filled.wiki_repo, "Commands & workflows")
    assert wiki.folded_weeks(page) == {"2026-W38"}


def test_cmd_weave_reports_a_failing_topic_and_retries_it(filled, tmp_path,
                                                          monkeypatch, capsys):
    script = tmp_path / "failing-claude"
    script.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(script))
    assert commands.cmd_weave(week="2026-W38") == 1
    assert "exited 3" in capsys.readouterr().err
    page = wiki.topic_path(filled.wiki_repo, "Commands & workflows")
    assert wiki.folded_weeks(page) == set()


def test_cmd_weave_needs_a_wiki(filled, monkeypatch):
    from ctui.config import Config

    import os
    Config(tasks_repo=filled.tasks_repo, wiki_repo=None).save(
        __import__("pathlib").Path(os.environ["CTUI_RC"]))
    with pytest.raises(commands.CommandError, match="No wiki repo"):
        commands.cmd_weave(week="2026-W38")


def test_cmd_weave_rejects_a_bad_week(filled):
    with pytest.raises(commands.CommandError, match="Expected a week"):
        commands.cmd_weave(week="nonsense")


# ---- cron ------------------------------------------------------------

def test_weave_cron_block_is_weekly(fake_cron, config):
    line = cron.install("/bin/ctui", None, 5, 30, home="/home/x",
                        job=cron.WEAVE_JOB, weekday=1)
    assert line.startswith("30 5 * * 1 ")
    assert "--weave" in line
    assert "weave.log" in line


def test_weave_and_dream_coexist(fake_cron, config):
    cron.install("/bin/ctui", None, 4, 30)
    cron.install("/bin/ctui", None, 5, 30, job=cron.WEAVE_JOB, weekday=1)
    text = fake_cron.read_text()
    assert cron.installed() and cron.installed(cron.WEAVE_JOB)
    assert "--dream" in text and "--weave" in text


def test_uninstalling_weave_leaves_dream(fake_cron, config):
    cron.install("/bin/ctui", None, 4, 30)
    cron.install("/bin/ctui", None, 5, 30, job=cron.WEAVE_JOB, weekday=1)
    assert cron.uninstall(cron.WEAVE_JOB) is True
    assert cron.installed() and not cron.installed(cron.WEAVE_JOB)


def test_reinstalling_weave_replaces_its_block(fake_cron, config):
    cron.install("/bin/ctui", None, 5, 30, job=cron.WEAVE_JOB, weekday=1)
    cron.install("/bin/ctui", None, 6, 0, job=cron.WEAVE_JOB, weekday=2)
    text = fake_cron.read_text()
    assert text.count(cron.markers(cron.WEAVE_JOB)[0]) == 1
    assert "0 6 * * 2" in text


def test_dream_block_is_unchanged_by_the_refactor(fake_cron, config):
    line = cron.install("/bin/ctui", None, 4, 30, home="/home/x")
    assert line == ("30 4 * * * HOME=/home/x /bin/ctui --dream "
                    f">> {cron.log_path()} 2>&1")


def test_cmd_install_weave(fake_cron, config, capsys):
    assert commands.cmd_install_weave() == 0
    assert cron.installed(cron.WEAVE_JOB)
    assert "* * 1" in capsys.readouterr().out


def test_cmd_uninstall_weave_when_absent(fake_cron, config, capsys):
    assert commands.cmd_uninstall_weave() == 0
    assert "no weave job" in capsys.readouterr().out


# ---- cli -------------------------------------------------------------

def test_cli_dispatches_weave(config, monkeypatch):
    seen = {}

    def fake(week, extra, dry_run):
        seen.update(week=week, dry_run=dry_run)
        return 0

    monkeypatch.setattr(commands, "cmd_weave", fake)
    assert main(["--weave", "--week", "2026-W38", "--dry-run"]) == 0
    assert seen == {"week": "2026-W38", "dry_run": True}


def test_cli_week_requires_weave(capsys):
    with pytest.raises(SystemExit):
        main(["--dream", "--week", "2026-W38"])
    assert "--week only applies to --weave" in capsys.readouterr().err


def test_cli_dry_run_allowed_for_both(config, monkeypatch):
    monkeypatch.setattr(commands, "cmd_weave", lambda **kw: 0)
    monkeypatch.setattr(commands, "cmd_dream", lambda **kw: 0)
    assert main(["--weave", "--dry-run"]) == 0
    assert main(["--dream", "--dry-run"]) == 0


def test_cli_at_allowed_for_install_weave(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "cmd_install_weave",
                        lambda at: seen.setdefault("at", at) and 0 or 0)
    assert main(["--install-weave", "--at", "06:15"]) == 0
    assert seen["at"] == "06:15"


def test_cli_weave_is_exclusive_with_dream(capsys):
    with pytest.raises(SystemExit):
        main(["--weave", "--dream"])
    assert "not allowed with" in capsys.readouterr().err


def test_dry_run_stages_the_inputs(filled, fake_claude):
    commands.cmd_weave(week="2026-W38", dry_run=True)
    staged = weave.cache_dir("2026-W38") / "commands-and-workflows.md"
    assert staged.exists()
    assert "sbatch" in staged.read_text()
    assert not fake_claude.exists()


@pytest.mark.parametrize("stdout,expected", [
    ("MERGED 7 SHARPENED 4 ADDED 3 DROPPED 2",
     {"merged": 7, "sharpened": 4, "added": 3, "dropped": 2}),
    ("blah\nmerged 0 sharpened 0 added 9 dropped 0\n",
     {"merged": 0, "sharpened": 0, "added": 9, "dropped": 0}),
    ("no tally here", None),
    ("", None),
])
def test_parse_report(stdout, expected):
    assert weave.parse_report(stdout) == expected


def test_parse_report_takes_the_last_tally():
    text = ("I will end with MERGED 0 SHARPENED 0 ADDED 0 DROPPED 0\n"
            "...\nMERGED 5 SHARPENED 1 ADDED 2 DROPPED 0\n")
    assert weave.parse_report(text)["merged"] == 5


def test_cmd_weave_prints_the_tally(filled, tmp_path, monkeypatch, capsys):
    script = tmp_path / "tally-claude"
    script.write_text("#!/bin/sh\necho 'MERGED 4 SHARPENED 2 ADDED 1 DROPPED 3'\n")
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(script))
    commands.cmd_weave(week="2026-W38")
    out = capsys.readouterr().out
    assert "merged 4" in out and "sharpened 2" in out and "added 1" in out


def test_dry_run_does_not_touch_the_wiki(filled, fake_claude):
    before = sorted(p.relative_to(filled.wiki_repo)
                    for p in filled.wiki_repo.rglob("*") if ".git" not in p.parts)
    assert commands.cmd_weave(week="2026-W38", dry_run=True) == 0
    after = sorted(p.relative_to(filled.wiki_repo)
                   for p in filled.wiki_repo.rglob("*") if ".git" not in p.parts)
    assert before == after
    assert not wiki.topics_dir(filled.wiki_repo).exists()
