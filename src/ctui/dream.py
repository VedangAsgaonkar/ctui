"""The nightly "dream" pass: distil the day's sessions into a dated wiki page."""

from __future__ import annotations

import os
import subprocess
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

from . import wiki
from .launcher import claude_bin
from .tasks import hostname, load_tasks
from .transcripts import SessionDigest, digest

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


def collect(tasks_repo: Path, day: Date) -> list[SessionDigest]:
    """Digests of every session on this host that was active on `day`.

    Walks this host's tasks and their recorded session ids, and lets
    `transcripts.could_contain` decide which transcripts are worth parsing —
    one stat and one line each. No separate bookkeeping: an index of session
    state was measured to save stat calls but no parses, so the transcripts
    themselves are the only thing consulted.

    Scoped to this host: transcripts live on the machine that ran the session,
    and only that machine's wiki pages are written.
    """
    digests: list[SessionDigest] = []
    for task in sorted(load_tasks(tasks_repo, hostname()), key=lambda t: t.task_id):
        for session in task.sessions:
            found = digest(task.root, session.session_id, day,
                           task.task_id, task.name)
            if found is not None:
                digests.append(found)
    return digests


def dream_prompt(digest_path: Path, page: Path, item: SessionDigest,
                 index: int, total: int) -> str:
    sections = "\n".join(f"- **{title}** — {blurb}" for title, blurb in wiki.SECTIONS)
    tags_prefix = wiki.TAGS_PREFIX
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
   rename or remove a "## " heading. Never touch "## Sessions folded in"; ctui
   maintains that.
6. No section is compulsory. Most sessions have something for one or two of
   them. Leave the rest exactly as they are — do not invent a learning to fill
   a section, do not pad a thin one, and do not write "none", "N/A" or any
   other placeholder. An empty section is the correct output when the session
   taught nothing that belongs there, and changing nothing at all is a correct
   outcome for a whole session.
7. Terse and concrete. One learning per bullet. Include the actual command, API
   call, flag or path pattern when it is what makes the bullet useful. No
   preamble and no narrative of what happened in the session.
8. Tags. The header has an optional "{tags_prefix}" line naming the topics the day's
   learnings are about. If this session adds a topic that is not listed, append
   it: lowercase, hyphenated, comma-separated, one or two words each
   (`python`, `slurm`, `prompt-engineering`). Extend the line in place, never
   replace it, and never repeat a tag already there. A handful for the whole
   day is plenty. Leave it untouched if nothing new fits — it is optional.

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
