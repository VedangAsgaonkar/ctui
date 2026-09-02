"""The nightly "dream" pass: distil the day's sessions into a dated wiki page."""

from __future__ import annotations

import os
import subprocess
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

from . import wiki
from .launcher import claude_bin
from .tasks import (
    STATE_CLOSED,
    Access,
    Task,
    TaskError,
    host_dir,
    hostname,
    load_tasks,
    read_access,
    reconcile_access,
    save_access,
    sessions_touching,
)
from .transcripts import SessionDigest, digest, record_span, transcript_path

# A cron job must not wedge forever on one session.
SESSION_TIMEOUT = 900

# No Write: the page is append-only by policy, and withholding the tool that
# can replace a whole file enforces it rather than trusting the instruction.
ALLOWED_TOOLS = "Read,Edit,Glob,Grep"

NOTHING_MARKER = "NOTHING TO RECORD"


class DreamError(Exception):
    pass


def yesterday() -> Date:
    return (datetime.now().astimezone() - timedelta(days=1)).date()


def cache_dir(day: Date) -> Path:
    """Where digests are staged: outside both repos, but kept for debugging."""
    base = os.environ.get("CTUI_CACHE") or (Path.home() / ".cache" / "ctui")
    return Path(base) / "dream" / day.isoformat()


def ensure_access_index(tasks_repo: Path, host: str | None = None) -> int:
    """Seed the access index for sessions it does not know about.

    Sessions that predate the index — or arrived with a `git pull` — have no
    access row, and would otherwise be invisible to `collect`. Their span is
    recovered from the transcript's own timestamps. This is the exhaustive scan,
    paid once, instead of on every nightly run.

    Returns the number of rows added.
    """
    host = host or hostname()
    known = {(e.host, e.task_id, e.session_id) for e in read_access(tasks_repo, host)}
    rows = read_access(tasks_repo, host)
    added = 0

    for task in load_tasks(tasks_repo, host):
        for session in task.sessions:
            key = (task.host, task.task_id, session.session_id)
            if key in known:
                continue
            span = record_span(transcript_path(task.root, session.session_id))
            if span is None:
                continue
            first, last = span
            last_stamp = datetime.combine(last, datetime.min.time()).astimezone().isoformat()
            rows.append(Access(
                host=task.host,
                task_id=task.task_id,
                session_id=session.session_id,
                first_at=datetime.combine(first, datetime.min.time()).astimezone().isoformat(),
                last_at=last_stamp,
                count=1,
                # The transcript's own span is known and final, so record the row
                # as closed rather than leaving it open-ended.
                state=STATE_CLOSED,
                activity_at=last_stamp,
            ))
            known.add(key)
            added += 1

    if added:
        rows.sort(key=lambda e: e.last_at, reverse=True)
        save_access(tasks_repo, rows, host)
    return added


def collect(tasks_repo: Path, day: Date) -> list[SessionDigest]:
    """Digests of every session on this host that was active on `day`.

    Candidates come from the access index, which records when each session was
    opened — so the work scales with the sessions opened around `day` rather
    than with every session that has ever existed on the host. The transcript is
    still the authority on which records belong to the day; the index only
    decides which transcripts are worth opening.

    Scoped to this host: transcripts live on the machine that ran the session.
    """
    host = hostname()
    tasks: dict[tuple[str, str], Task | None] = {}
    digests: list[SessionDigest] = []

    for entry in sessions_touching(tasks_repo, day, host):
        ident = (entry.host, entry.task_id)
        if ident not in tasks:
            try:
                tasks[ident] = Task.load(host_dir(tasks_repo, entry.host) / entry.task_id)
            except TaskError:
                tasks[ident] = None
        task = tasks[ident]
        if task is None:
            continue
        found = digest(task.root, entry.session_id, day, task.task_id, task.name)
        if found is not None:
            digests.append(found)
    return digests


def dream_prompt(digest_path: Path, page: Path, item: SessionDigest,
                 index: int, total: int) -> str:
    sections = "\n".join(f"- **{title}** — {blurb}" for title, blurb in wiki.SECTIONS)
    return f"""\
You are the "dream" pass for ctui: a nightly job that distils reusable knowledge
out of a day's claude-code sessions into a dated wiki page.

Input: {digest_path}
  A digest of ONE session ({index} of {total}) from {item.day.isoformat()} — user
  messages verbatim, assistant prose, and the tool calls that were made. Tool
  output has been stripped. It came from task {item.task_id} ({item.task_name}),
  rooted at {item.task_root}.

Page: {page}
  The dated wiki page for that day, inside the current directory. Read it before
  you write. The path is absolute; do not go looking for it elsewhere.

Your job: read the digest, then edit that page to record anything from this
session that will still be useful to someone working on a DIFFERENT task months
from now. Write under the existing "## " headings.

Rules, in order of precedence:

1. Methods, never results. Record how something is done; never what it produced.
   Record how to compute a constant to N digits with a named series and library;
   never the digits. Record how to run a class of benchmark and read its output;
   never the numbers, which config won, or how long it took. Record how a metric
   is defined and computed; never its value.
2. Generic over specific. If a learning only makes sense for this one task,
   dataset, ticket, run or file, generalise it or drop it. Prefer dropping it.
3. Only what this session taught. No background knowledge, no filler, no
   restating the page's own instructions.
4. No duplicates. Read the page first. If a point is already there, skip it, or
   sharpen the existing line in place. Never add a near-duplicate.
5. Append only. Never delete or reword another session's content. Never add,
   rename or remove a "## " heading — leave a section untouched if you have
   nothing for it. Never touch "## Sessions folded in"; ctui maintains that.
6. Terse and concrete. One learning per bullet. Include the actual command, API
   call, flag or path pattern when it is what makes the bullet useful. No
   preamble and no narrative of what happened in the session.

Where things go:
{sections}

If nothing in this session generalises, change nothing and reply exactly:
{NOTHING_MARKER}
"""


def _run_claude(prompt: str, cwd: Path, extra_dir: Path,
                extra: list[str] | None = None) -> subprocess.CompletedProcess:
    argv = [
        claude_bin(), "-p", prompt,
        "--add-dir", str(extra_dir),
        "--permission-mode", "acceptEdits",
        "--allowed-tools", ALLOWED_TOOLS,
        *(extra or []),
    ]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          timeout=SESSION_TIMEOUT)


def stage_digests(day: Date, digests: list[SessionDigest]) -> list[tuple[SessionDigest, Path]]:
    """Write digests to the cache directory for the dream session to read."""
    out = cache_dir(day)
    out.mkdir(parents=True, exist_ok=True)
    staged = []
    for item in digests:
        path = out / f"{item.task_id}-{item.session_id[:8]}.md"
        path.write_text(item.to_markdown())
        staged.append((item, path))
    return staged
