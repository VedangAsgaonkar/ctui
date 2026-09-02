"""Locating and digesting claude-code session transcripts.

claude stores a session at
``~/.claude/projects/<cwd with / replaced by ->/<session-id>.jsonl``,
which is exactly reconstructible from a task's root and the session ids ctui
already records in task.json — so no extra bookkeeping is needed to find the
sessions that ran on a given day.

Transcripts are large (tens of KB to megabytes) and mostly tool-result payloads.
`digest` reduces one to the text a reader actually learns from: user messages
verbatim, assistant prose, and tool *calls*. Tool *results* and thinking blocks
are dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS_ENV = "CTUI_CLAUDE_PROJECTS"

# Per-block truncation. Enough to carry a command or an explanation, short
# enough that one pasted file cannot dominate a day's digest.
MAX_TEXT = 2000
MAX_TOOL_INPUT = 600


def projects_dir() -> Path:
    override = os.environ.get(PROJECTS_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects"


def encode_root(root: Path) -> str:
    """claude's project-directory name for an absolute path."""
    return str(root).replace("/", "-")


def transcript_path(root: Path, session_id: str) -> Path:
    return projects_dir() / encode_root(root) / f"{session_id}.jsonl"


def iter_records(path: Path):
    """Yield JSON records, skipping unparseable lines rather than failing.

    A transcript is appended to by a live session, so the final line can be
    a partial write.
    """
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def record_local_date(record: dict) -> Date | None:
    """The record's timestamp as a *local* calendar date.

    Transcript timestamps are UTC; a session at 00:45 local on the 2nd is
    stamped 07:45Z on the 2nd, and one at 18:00 local on the 2nd is stamped
    01:00Z on the 3rd. Comparing raw would put days in the wrong bucket.
    """
    stamp = record.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date()


def _day_bounds(day: Date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time()).astimezone()
    return start, start + timedelta(days=1)


def first_record_date(path: Path) -> Date | None:
    """Local date of the earliest timestamped record, or None if unreadable.

    Reads from the head of the file and stops as soon as it finds one, so it
    stays cheap on a multi-megabyte transcript.
    """
    for record in iter_records(path):
        found = record_local_date(record)
        if found is not None:
            return found
    return None


def could_contain(path: Path, day: Date) -> bool:
    """Can this transcript hold records from `day`?

    Bounds the day from the file itself, cheaply, from both directions:

    * mtime before the day began — a transcript is only appended to, so its
      last write cannot precede a record it contains. One stat.
    * first record after the day — the session started later. One line.

    Everything surviving both is parsed in full. This is deliberately all the
    filtering there is: measurement showed that a session-state index layered
    on top of the mtime check saved stat calls but no parses at all, which did
    not justify the bookkeeping.
    """
    start, _ = _day_bounds(day)
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return False
    if mtime < start:
        return False
    first = first_record_date(path)
    return first is None or first <= day


def _blocks(message) -> list[dict]:
    """Normalise a message's content to a list of blocks."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [clipped, {len(text) - limit} more chars]"


def _tool_call(block: dict) -> str:
    """One compact line (or few) describing a tool call.

    Bash commands carry most of the transferable "how to do X", so they are
    rendered as the command itself; other tools get their salient inputs.
    """
    name = str(block.get("name") or "tool")
    payload = block.get("input")
    if not isinstance(payload, dict):
        return f"### tool: {name}\n"

    if name in {"Bash", "BashOutput"} and payload.get("command"):
        return f"### tool: {name}\n$ {_clip(str(payload['command']), MAX_TOOL_INPUT)}\n"

    interesting = ("file_path", "path", "pattern", "url", "query",
                   "notebook_path", "command", "prompt", "skill")
    parts = [f"{k}: {_clip(str(payload[k]), MAX_TOOL_INPUT)}"
             for k in interesting if payload.get(k)]
    if not parts:
        return f"### tool: {name}\n"
    return f"### tool: {name}\n" + "\n".join(parts) + "\n"


@dataclass
class SessionDigest:
    session_id: str
    task_id: str
    task_name: str
    task_root: Path
    day: Date
    body: str
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0

    @property
    def empty(self) -> bool:
        return not self.body.strip()

    def to_markdown(self) -> str:
        head = (
            f"## Session {self.session_id[:8]} — task {self.task_id} ({self.task_name})\n"
            f"Root: {self.task_root}\n"
            f"Activity on {self.day.isoformat()}: {self.user_messages} user messages, "
            f"{self.assistant_messages} assistant replies, {self.tool_calls} tool calls.\n\n"
        )
        return head + self.body


def digest(root: Path, session_id: str, day: Date, task_id: str = "",
           task_name: str = "") -> SessionDigest | None:
    """Digest one session's activity on `day`, or None if it had none.

    Only main-chain records are kept: sidechain (subagent) traffic and the
    meta records claude writes for its own bookkeeping carry little that
    generalises and a great deal of volume.
    """
    path = transcript_path(root, session_id)
    if not path.exists() or not could_contain(path, day):
        return None

    out: list[str] = []
    users = assistants = tools = 0

    for record in iter_records(path):
        if record.get("isMeta") or record.get("isSidechain"):
            continue
        if record.get("type") not in {"user", "assistant"}:
            continue
        if record_local_date(record) != day:
            continue

        message = record.get("message")
        role = message.get("role") if isinstance(message, dict) else None

        if role == "user":
            # tool_result blocks arrive as user-role content; they are the bulk
            # of a transcript and almost never carry a reusable learning.
            texts = [b.get("text", "") for b in _blocks(message)
                     if b.get("type") == "text"]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                users += 1
                out.append(f"### user\n{_clip(joined, MAX_TEXT)}\n")
        elif role == "assistant":
            said = False
            for block in _blocks(message):
                kind = block.get("type")
                if kind == "text" and block.get("text", "").strip():
                    if not said:
                        assistants += 1
                        said = True
                    out.append(f"### assistant\n{_clip(block['text'], MAX_TEXT)}\n")
                elif kind == "tool_use":
                    tools += 1
                    out.append(_tool_call(block))

    if not out:
        return None
    return SessionDigest(
        session_id=session_id,
        task_id=task_id,
        task_name=task_name,
        task_root=root,
        day=day,
        body="\n".join(out),
        user_messages=users,
        assistant_messages=assistants,
        tool_calls=tools,
    )
