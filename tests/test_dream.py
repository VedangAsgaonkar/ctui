"""The nightly dream pass end to end, with claude stubbed."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctui import commands as C
from ctui import config as CFG
from ctui import dream
from ctui import gitutil as G
from ctui import tasks as T
from ctui import transcripts as X
from ctui import wiki

DAY = date(2026, 9, 2)
BEFORE = date(2026, 9, 1)


def _stamp(day, hour=12):
    local = datetime.combine(day, datetime.min.time()).astimezone().replace(hour=hour)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _transcript(root, session_id, day, texts):
    path = X.transcript_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for text in texts:
        records.append({"type": "user", "timestamp": _stamp(day),
                        "message": {"role": "user",
                                    "content": [{"type": "text", "text": text}]}})
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def wiki_repo(tmp_path):
    repo = tmp_path / "wiki"
    G.init(repo)
    (repo / "README.md").write_text("wiki\n")
    G.commit_all(repo, "init")
    return repo


@pytest.fixture
def dreamworld(tasks_repo, wiki_repo, tmp_path):
    """A task with one session that was active on DAY."""
    project = tmp_path / "proj"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "the task")
    task.add_session("aaaaaaaa-1111-2222-3333-444444444444")
    _transcript(project, "aaaaaaaa-1111-2222-3333-444444444444", DAY,
                ["use mpmath for arbitrary precision", "put scripts in the task dir"])
    T.record_access(tasks_repo, task, "aaaaaaaa-1111-2222-3333-444444444444")
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=wiki_repo).save()
    return task


@pytest.fixture
def scribe(tmp_path, monkeypatch):
    """A fake claude that records its invocation and appends to the page."""
    log = tmp_path / "dream-calls.jsonl"
    script = tmp_path / "fake-claude"
    script.write_text(f"""#!/usr/bin/env python3
import json, os, re, sys, pathlib
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"cwd": os.getcwd(), "argv": argv, "prompt": prompt}}) + "\\n")
# Behave like an obedient pass: append one bullet under a real heading.
page = re.search(r"^Page: (/\\S+)", prompt, re.M).group(1)
p = pathlib.Path(page)
text = p.read_text().replace(
    "## Libraries & tools\\n", "## Libraries & tools\\n\\n- a distilled learning\\n", 1)
p.write_text(text)
print("done")
""")
    script.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(script))
    return log


def _calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


# ---- collection ----------------------------------------------------

def test_collect_finds_sessions_active_that_day(dreamworld, tasks_repo):
    got = dream.collect(tasks_repo, DAY)
    assert [d.session_id for d in got] == ["aaaaaaaa-1111-2222-3333-444444444444"]
    assert got[0].task_name == "the task"
    assert "mpmath" in got[0].body


def test_collect_ignores_other_days(dreamworld, tasks_repo):
    assert dream.collect(tasks_repo, BEFORE) == []


def test_collect_skips_sessions_without_transcripts(tasks_repo, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "t")
    task.add_session("no-transcript-at-all")
    T.record_access(tasks_repo, task, "no-transcript-at-all")
    assert dream.collect(tasks_repo, DAY) == []


def test_collect_is_scoped_to_this_host(tasks_repo, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "elsewhere", host="otherhost")
    task.add_session("bbbb-1111")
    _transcript(project, "bbbb-1111", DAY, ["hello"])
    T.record_access(tasks_repo, task, "bbbb-1111", host="otherhost")
    assert dream.collect(tasks_repo, DAY) == []


def test_yesterday_is_local():
    assert dream.yesterday() == (datetime.now().astimezone() - timedelta(days=1)).date()


# ---- staging -------------------------------------------------------

def test_stage_digests_writes_readable_files(dreamworld, tasks_repo):
    staged = dream.stage_digests(DAY, dream.collect(tasks_repo, DAY))
    assert len(staged) == 1
    item, path = staged[0]
    assert path.is_file()
    assert path.parent == dream.cache_dir(DAY)
    assert "mpmath" in path.read_text()


def test_cache_dir_is_outside_both_repos(dreamworld, tasks_repo, wiki_repo):
    out = dream.cache_dir(DAY)
    assert not out.is_relative_to(tasks_repo)
    assert not out.is_relative_to(wiki_repo)


# ---- the prompt ----------------------------------------------------

def test_prompt_states_the_methods_not_results_rule(dreamworld, tasks_repo, wiki_repo):
    item = dream.collect(tasks_repo, DAY)[0]
    page = wiki.ensure_page(wiki_repo, DAY)
    prompt = dream.dream_prompt(Path("/tmp/d.md"), page, item, 1, 1)

    assert "Methods, never results" in prompt
    assert "never the digits" in prompt
    assert "never its value" in prompt
    assert "Generic over specific" in prompt
    assert "DIFFERENT task" in prompt


def test_prompt_forbids_destroying_other_sessions_work(dreamworld, tasks_repo, wiki_repo):
    item = dream.collect(tasks_repo, DAY)[0]
    page = wiki.ensure_page(wiki_repo, DAY)
    prompt = dream.dream_prompt(Path("/tmp/d.md"), page, item, 1, 1)

    assert "Append only" in prompt
    assert "Never delete" in prompt
    assert wiki.FOLDED_HEADING.lstrip("# ") in prompt      # told to leave it alone
    assert dream.NOTHING_MARKER in prompt


def test_prompt_names_the_digest_the_page_and_the_sections(dreamworld, tasks_repo, wiki_repo):
    item = dream.collect(tasks_repo, DAY)[0]
    page = wiki.ensure_page(wiki_repo, DAY)
    prompt = dream.dream_prompt(Path("/tmp/digest.md"), page, item, 2, 5)

    assert "/tmp/digest.md" in prompt
    assert str(page) in prompt           # absolute, not just the file name
    assert page.is_absolute()
    assert "(2 of 5)" in prompt
    for title in wiki.section_titles():
        assert title in prompt


# ---- the run -------------------------------------------------------

def test_dream_creates_the_page_and_calls_claude(dreamworld, wiki_repo, scribe):
    assert C.cmd_dream(day=DAY.isoformat()) == 0

    page = wiki.dated_path(wiki_repo, DAY)
    assert page.exists()
    assert "- a distilled learning" in page.read_text()

    calls = _calls(scribe)
    assert len(calls) == 1
    assert Path(calls[0]["cwd"]).resolve() == wiki_repo.resolve()


def test_dream_runs_claude_without_the_write_tool(dreamworld, scribe):
    """No Write means the model cannot replace the page wholesale."""
    C.cmd_dream(day=DAY.isoformat())
    argv = _calls(scribe)[0]["argv"]
    tools = argv[argv.index("--allowed-tools") + 1]
    assert "Write" not in tools
    assert "Read" in tools and "Edit" in tools
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "-p" in argv


def test_dream_grants_access_to_the_digest_directory(dreamworld, scribe):
    C.cmd_dream(day=DAY.isoformat())
    argv = _calls(scribe)[0]["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(dream.cache_dir(DAY))


def test_dream_records_provenance_and_commits(dreamworld, wiki_repo, scribe):
    C.cmd_dream(day=DAY.isoformat())
    page = wiki.dated_path(wiki_repo, DAY)
    assert wiki.folded_sessions(page) == {"aaaaaaaa-1111-2222-3333-444444444444"}
    assert not G.is_dirty(wiki_repo)
    assert "ctui dream: 2026-09-02" in G.run(wiki_repo, "log", "-1", "--pretty=%s").out


def test_dream_is_idempotent(dreamworld, wiki_repo, scribe):
    """A second run must not re-distil sessions already folded in."""
    C.cmd_dream(day=DAY.isoformat())
    assert len(_calls(scribe)) == 1

    assert C.cmd_dream(day=DAY.isoformat()) == 0
    assert len(_calls(scribe)) == 1                    # claude not called again
    assert page_bullets(wiki_repo) == 1


def page_bullets(wiki_repo):
    return wiki.dated_path(wiki_repo, DAY).read_text().count("- a distilled learning")


def test_dream_with_no_activity_does_nothing(dreamworld, wiki_repo, scribe):
    assert C.cmd_dream(day=BEFORE.isoformat()) == 0
    assert not wiki.dated_path(wiki_repo, BEFORE).exists()
    assert _calls(scribe) == []


def test_dream_dry_run_stages_without_calling_claude(dreamworld, wiki_repo, scribe, capsys):
    assert C.cmd_dream(day=DAY.isoformat(), dry_run=True) == 0
    assert _calls(scribe) == []
    out = capsys.readouterr().out
    assert "would update" in out
    assert list(dream.cache_dir(DAY).glob("*.md"))


def test_dream_defaults_to_yesterday(dreamworld, tasks_repo, wiki_repo, scribe, monkeypatch):
    monkeypatch.setattr(dream, "yesterday", lambda: DAY)
    assert C.cmd_dream() == 0
    assert wiki.dated_path(wiki_repo, DAY).exists()


def test_dream_rejects_a_bad_date(dreamworld):
    with pytest.raises(C.CommandError, match="2026-09-01"):
        C.cmd_dream(day="last tuesday")


def test_dream_requires_a_wiki_repo(tasks_repo, monkeypatch):
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=None).save()
    with pytest.raises(C.CommandError, match="wiki"):
        C.cmd_dream(day=DAY.isoformat())


def test_dream_requires_the_wiki_to_be_a_repo(tasks_repo, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=plain).save()
    with pytest.raises(C.CommandError, match="not a git repo"):
        C.cmd_dream(day=DAY.isoformat())


def test_one_failing_session_does_not_abort_the_day(tasks_repo, wiki_repo, tmp_path,
                                                    monkeypatch):
    """A cron job must salvage the rest of the day's learnings."""
    project = tmp_path / "proj"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "t")
    for sid in ("1111aaaa-0000", "2222bbbb-0000"):
        task.add_session(sid)
        _transcript(project, sid, DAY, [f"message for {sid}"])
        T.record_access(tasks_repo, task, sid)
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=wiki_repo).save()

    failing = tmp_path / "flaky-claude"
    failing.write_text("#!/bin/sh\ncase \"$*\" in *1111aaaa*) exit 7 ;; esac\nexit 0\n")
    failing.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(failing))

    assert C.cmd_dream(day=DAY.isoformat()) == 1        # reports the failure
    page = wiki.dated_path(wiki_repo, DAY)
    # the failing session is not marked done, so a re-run retries it
    assert wiki.folded_sessions(page) == {"2222bbbb-0000"}


def test_nothing_to_record_is_still_marked_done(dreamworld, wiki_repo, tmp_path,
                                                monkeypatch, capsys):
    quiet = tmp_path / "quiet-claude"
    quiet.write_text(f"#!/bin/sh\necho '{dream.NOTHING_MARKER}'\n")
    quiet.chmod(0o755)
    monkeypatch.setenv("CTUI_CLAUDE_BIN", str(quiet))

    assert C.cmd_dream(day=DAY.isoformat()) == 0
    assert "nothing generalised" in capsys.readouterr().out
    # marked done so tomorrow's run does not pay for it again
    assert wiki.folded_sessions(wiki.dated_path(wiki_repo, DAY))


def test_dream_passes_through_claude_args(dreamworld, scribe):
    C.cmd_dream(day=DAY.isoformat(), extra=["--model", "opus"])
    assert _calls(scribe)[0]["argv"][-2:] == ["--model", "opus"]


# ---- candidates come from the access index -------------------------

@pytest.fixture
def counted_parses(monkeypatch):
    """Count transcripts opened, so 'does not scan everything' is asserted."""
    calls = {"n": 0, "paths": []}
    original = X.iter_records

    def _iter(path):
        calls["n"] += 1
        calls["paths"].append(Path(path).name)
        return original(path)

    monkeypatch.setattr(X, "iter_records", _iter)
    return calls


def _task_with_sessions(tasks_repo, tmp_path, name, sessions, day=DAY):
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    task = T.create_task(tasks_repo, project, name)
    for sid in sessions:
        task.add_session(sid)
        _transcript(project, sid, day, [f"work in {sid}"])
    return task


def test_collect_only_opens_indexed_candidates(tasks_repo, wiki_repo, tmp_path,
                                               counted_parses):
    """The whole point: cost tracks the day, not the host's total sessions."""
    old = _task_with_sessions(tasks_repo, tmp_path, "old",
                              [f"old{i}-0000" for i in range(20)], day=BEFORE)
    fresh = _task_with_sessions(tasks_repo, tmp_path, "fresh", ["new1-0000"])
    T.record_access(tasks_repo, fresh, "new1-0000")

    counted_parses["n"] = 0
    got = dream.collect(tasks_repo, DAY)

    assert [d.session_id for d in got] == ["new1-0000"]
    assert counted_parses["n"] == 1                    # not 21
    assert counted_parses["paths"] == ["new1-0000.jsonl"]


def test_collect_ignores_sessions_indexed_for_other_days(tasks_repo, tmp_path,
                                                         counted_parses):
    task = _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"], day=DAY)
    T.record_access(tasks_repo, task, "s1-0000")
    entries = T.read_access(tasks_repo)
    long_ago = datetime.combine(date(2026, 1, 1), datetime.min.time()).astimezone()
    entries[0].first_at = entries[0].last_at = long_ago.isoformat()
    entries[0].state, entries[0].activity_at = T.STATE_CLOSED, long_ago.isoformat()
    T.save_access(tasks_repo, entries)

    counted_parses["n"] = 0
    assert dream.collect(tasks_repo, DAY) == []
    assert counted_parses["n"] == 0                    # nothing even opened


def test_backfill_seeds_sessions_that_predate_the_index(tasks_repo, tmp_path):
    """A repo predating the index must still yield its learnings.

    The span is recovered from the transcript's own timestamps, so this is the
    exhaustive scan paid once rather than on every nightly run.
    """
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    assert not T.access_path(tasks_repo).exists()
    assert dream.collect(tasks_repo, DAY) == []          # invisible until seeded

    assert dream.ensure_access_index(tasks_repo) == 1
    row = T.read_access(tasks_repo)[0]
    assert row.session_id == "s1-0000"
    assert row.covers(DAY)
    assert [d.session_id for d in dream.collect(tasks_repo, DAY)] == ["s1-0000"]


def test_backfill_is_idempotent(tasks_repo, tmp_path):
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    assert dream.ensure_access_index(tasks_repo) == 1
    assert dream.ensure_access_index(tasks_repo) == 0
    assert len(T.read_access(tasks_repo)) == 1


def test_backfill_preserves_real_access_rows(tasks_repo, tmp_path):
    """A genuine access record must not be overwritten by a coarse span."""
    task = _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000", "s2-0000"])
    T.record_access(tasks_repo, task, "s1-0000")
    exact = T.read_access(tasks_repo)[0].last_at

    assert dream.ensure_access_index(tasks_repo) == 1     # only s2 is new
    rows = {r.session_id: r for r in T.read_access(tasks_repo)}
    assert rows["s1-0000"].last_at == exact
    assert rows["s2-0000"].covers(DAY)


def test_backfill_skips_sessions_without_transcripts(tasks_repo, tmp_path):
    project = tmp_path / "t"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "t")
    task.add_session("ghost-0000")
    assert dream.ensure_access_index(tasks_repo) == 0
    assert T.read_access(tasks_repo) == []


def test_dream_backfills_then_distils(tasks_repo, wiki_repo, tmp_path, scribe, capsys):
    """The whole path for a repo that predates the index."""
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=wiki_repo).save()

    assert C.cmd_dream(day=DAY.isoformat()) == 0
    out = capsys.readouterr().out
    assert "indexed 1 session" in out
    assert len(_calls(scribe)) == 1
    assert not G.is_dirty(tasks_repo)                    # backfill was committed


def test_index_present_but_empty_does_not_trigger_a_scan(tasks_repo, tmp_path,
                                                         counted_parses):
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    T.save_access(tasks_repo, [])                      # index exists, says nothing

    counted_parses["n"] = 0
    assert dream.collect(tasks_repo, DAY) == []
    assert counted_parses["n"] == 0


def test_a_session_opened_before_midnight_lands_on_the_right_day(tasks_repo, tmp_path):
    """Opened 23:00 on the 1st, all the work done on the 2nd.

    Both halves matter: the index makes it a candidate for the 1st *and* the
    2nd (only its opening is recorded, so the span is generous), and the
    transcript is what decides it contributes to the 2nd and not the 1st.
    """
    project = tmp_path / "t"
    project.mkdir()
    task = T.create_task(tasks_repo, project, "t")
    task.add_session("span-0000")
    _transcript(project, "span-0000", DAY, ["carried past midnight"])

    T.record_access(tasks_repo, task, "span-0000")
    entries = T.read_access(tasks_repo)
    opened = datetime.combine(BEFORE, datetime.min.time()).astimezone().replace(hour=23)
    entries[0].first_at = entries[0].last_at = opened.isoformat()
    entries[0].state = T.STATE_CLOSED
    entries[0].activity_at = (opened + timedelta(hours=3)).isoformat()   # 02:00 on DAY
    T.save_access(tasks_repo, entries)

    assert T.sessions_touching(tasks_repo, BEFORE)          # candidate for both days
    assert T.sessions_touching(tasks_repo, DAY)
    assert dream.collect(tasks_repo, BEFORE) == []          # transcript decides
    assert [d.session_id for d in dream.collect(tasks_repo, DAY)] == ["span-0000"]


def test_collect_survives_a_deleted_task_in_the_index(tasks_repo, tmp_path):
    import shutil
    task = _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    other = _task_with_sessions(tasks_repo, tmp_path, "u", ["s2-0000"])
    T.record_access(tasks_repo, task, "s1-0000")
    T.record_access(tasks_repo, other, "s2-0000")
    shutil.rmtree(task.dir)

    assert [d.session_id for d in dream.collect(tasks_repo, DAY)] == ["s2-0000"]


def test_dream_reconciles_before_collecting(tasks_repo, wiki_repo, tmp_path,
                                            scribe, capsys):
    """A session idle long enough to be closed is not opened for a later day.

    Dated relative to now rather than to a fixed calendar day, since whether a
    transcript counts as idle depends on how long ago it was actually written.
    """
    import os
    now = datetime.now().astimezone()
    ran = (now - (T.IDLE_CLOSE + timedelta(days=2))).date()
    target = (now - timedelta(days=1)).date()

    task = _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"], day=ran)
    T.record_access(tasks_repo, task, "s1-0000")
    when = datetime.combine(ran, datetime.min.time()).astimezone().replace(hour=10)
    rows = T.read_access(tasks_repo)
    rows[0].first_at = rows[0].last_at = when.isoformat(timespec="seconds")
    T.save_access(tasks_repo, rows)
    path = X.transcript_path(task.root, "s1-0000")
    os.utime(path, (when.timestamp(), when.timestamp()))
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=wiki_repo).save()

    assert C.cmd_dream(day=target.isoformat()) == 0
    out = capsys.readouterr().out
    assert "settled 1 session" in out
    assert "no sessions were active" in out
    assert _calls(scribe) == []
    assert T.read_access(tasks_repo)[0].closed


def test_backfilled_rows_are_recorded_closed(tasks_repo, tmp_path):
    """Their span comes from the transcript and is already final."""
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    dream.ensure_access_index(tasks_repo)
    row = T.read_access(tasks_repo)[0]
    assert row.closed
    assert row.activity_at
    assert row.covers(DAY)


def test_dream_commits_index_updates(tasks_repo, wiki_repo, tmp_path, scribe):
    _task_with_sessions(tasks_repo, tmp_path, "t", ["s1-0000"])
    CFG.Config(tasks_repo=tasks_repo, wiki_repo=wiki_repo).save()

    C.cmd_dream(day=DAY.isoformat())
    assert not G.is_dirty(tasks_repo)
    assert "access index" in G.run(tasks_repo, "log", "-1", "--pretty=%s").out
