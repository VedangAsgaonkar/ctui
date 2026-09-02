"""Locating and digesting claude session transcripts."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctui import transcripts as X


def _stamp(day: date, hour: int = 12, minute: int = 0) -> str:
    """A UTC timestamp string for a given *local* wall time on `day`."""
    local = datetime.combine(day, datetime.min.time()).astimezone()
    local = local.replace(hour=hour, minute=minute)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(root: Path, session_id: str, records: list[dict]) -> Path:
    path = X.transcript_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _user(text, day, **kw):
    return {"type": "user", "timestamp": _stamp(day, **kw),
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(blocks, day, **kw):
    return {"type": "assistant", "timestamp": _stamp(day, **kw),
            "message": {"role": "assistant", "content": blocks}}


TODAY = date(2026, 9, 2)
BEFORE = date(2026, 9, 1)


# ---- paths ---------------------------------------------------------

def test_encode_root_matches_claude_layout():
    assert X.encode_root(Path("/lfs/furiosa/0/vedanga/tmp")) == "-lfs-furiosa-0-vedanga-tmp"


def test_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CTUI_CLAUDE_PROJECTS", str(tmp_path / "p"))
    got = X.transcript_path(Path("/a/b"), "sid-1")
    assert got == tmp_path / "p" / "-a-b" / "sid-1.jsonl"


def test_missing_transcript_digests_to_none(tmp_path):
    assert X.digest(tmp_path / "root", "nope", TODAY) is None


# ---- day bucketing -------------------------------------------------

def test_timestamps_are_bucketed_by_local_date():
    """UTC stamps must be converted, or days land in the wrong bucket.

    A session at 00:45 local is stamped 07:45Z the same day in PDT; one at
    18:00 local is stamped 01:00Z the *next* day.
    """
    early = {"timestamp": _stamp(TODAY, hour=0, minute=45)}
    late = {"timestamp": _stamp(TODAY, hour=23, minute=30)}
    assert X.record_local_date(early) == TODAY
    assert X.record_local_date(late) == TODAY


def test_record_local_date_handles_junk():
    assert X.record_local_date({}) is None
    assert X.record_local_date({"timestamp": ""}) is None
    assert X.record_local_date({"timestamp": "not a date"}) is None
    assert X.record_local_date({"timestamp": 12345}) is None


def test_only_the_requested_day_is_digested(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_user("yesterday's question", BEFORE),
                       _user("today's question", TODAY)])

    today = X.digest(root, "s", TODAY)
    assert "today's question" in today.body
    assert "yesterday's question" not in today.body


def test_could_contain_filters_by_mtime(tmp_path):
    root = tmp_path / "proj"
    path = _write(root, "s", [_user("hi", BEFORE)])
    import os
    old = datetime.combine(BEFORE, datetime.min.time()).timestamp()
    os.utime(path, (old, old))

    assert X.could_contain(path, BEFORE)
    assert not X.could_contain(path, TODAY)      # cannot hold later records
    assert not X.could_contain(root / "absent.jsonl", TODAY)


def test_mtime_filter_short_circuits_digest(tmp_path):
    """A stale transcript is skipped without being parsed."""
    root = tmp_path / "proj"
    path = _write(root, "s", [_user("hi", TODAY)])
    import os
    old = datetime.combine(BEFORE, datetime.min.time()).timestamp()
    os.utime(path, (old, old))
    assert X.digest(root, "s", TODAY) is None


# ---- what gets kept ------------------------------------------------

def test_user_messages_are_kept_verbatim(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_user("Use the task dir, not /tmp", TODAY)])
    d = X.digest(root, "s", TODAY)
    assert "### user\nUse the task dir, not /tmp" in d.body
    assert d.user_messages == 1


def test_tool_results_are_dropped(tmp_path):
    """Tool results arrive as user-role content and are the bulk of a file."""
    root = tmp_path / "proj"
    _write(root, "s", [
        {"type": "user", "timestamp": _stamp(TODAY), "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "SECRET-HUGE-PAYLOAD" * 100},
        ]}},
        _user("a real question", TODAY),
    ])
    d = X.digest(root, "s", TODAY)
    assert "SECRET-HUGE-PAYLOAD" not in d.body
    assert d.user_messages == 1


def test_thinking_blocks_are_dropped(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_assistant([
        {"type": "thinking", "thinking": "PRIVATE-REASONING"},
        {"type": "text", "text": "the answer"},
    ], TODAY)])
    d = X.digest(root, "s", TODAY)
    assert "PRIVATE-REASONING" not in d.body
    assert "the answer" in d.body


def test_bash_calls_are_rendered_as_commands(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_assistant([
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q tests/"}},
    ], TODAY)])
    d = X.digest(root, "s", TODAY)
    assert "### tool: Bash\n$ pytest -q tests/" in d.body
    assert d.tool_calls == 1


def test_other_tools_render_salient_inputs(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_assistant([
        {"type": "tool_use", "name": "Edit",
         "input": {"file_path": "/a/b.py", "old_string": "x" * 5000}},
        {"type": "tool_use", "name": "Mystery", "input": {"unknown": "z"}},
    ], TODAY)])
    d = X.digest(root, "s", TODAY)
    assert "### tool: Edit\nfile_path: /a/b.py" in d.body
    assert "x" * 200 not in d.body          # bulky inputs are not carried
    assert "### tool: Mystery" in d.body


def test_sidechain_and_meta_records_are_skipped(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [
        dict(_user("SUBAGENT-CHATTER", TODAY), isSidechain=True),
        dict(_user("META-NOISE", TODAY), isMeta=True),
        _user("kept", TODAY),
    ])
    d = X.digest(root, "s", TODAY)
    assert "SUBAGENT-CHATTER" not in d.body and "META-NOISE" not in d.body
    assert "kept" in d.body


def test_long_text_is_clipped(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_user("y" * (X.MAX_TEXT + 5000), TODAY)])
    d = X.digest(root, "s", TODAY)
    assert len(d.body) < X.MAX_TEXT + 500
    assert "clipped" in d.body


def test_string_content_is_handled(tmp_path):
    """Older records store content as a bare string."""
    root = tmp_path / "proj"
    _write(root, "s", [{"type": "user", "timestamp": _stamp(TODAY),
                        "message": {"role": "user", "content": "plain string"}}])
    assert "plain string" in X.digest(root, "s", TODAY).body


def test_a_day_with_no_activity_is_none(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_user("hi", TODAY)])
    assert X.digest(root, "s", BEFORE) is None


def test_malformed_lines_are_skipped(tmp_path):
    root = tmp_path / "proj"
    path = X.transcript_path(root, "s")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_user("first", TODAY)) + "\n"
        "{ this is a partial write\n"
        + json.dumps(_user("second", TODAY)) + "\n"
    )
    d = X.digest(root, "s", TODAY)
    assert "first" in d.body and "second" in d.body


def test_markdown_header_reports_provenance(tmp_path):
    root = tmp_path / "proj"
    _write(root, "s", [_user("q", TODAY),
                       _assistant([{"type": "text", "text": "a"}], TODAY)])
    d = X.digest(root, "s", TODAY, "TASK_X", "the task")
    md = d.to_markdown()
    assert "TASK_X" in md and "the task" in md
    assert str(root) in md
    assert TODAY.isoformat() in md
