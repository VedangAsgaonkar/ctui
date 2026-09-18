"""The weekly "weave" pass: fold a week of dated pages into the topic pages.

`dream` runs nightly and writes one page per day per host. `weave` runs weekly
and reduces those into one durable page per *section*, merging the new week
into what is already there rather than appending to it.

The sections are the topic axis because they are the one thing every dated page
shares: dream may never add, rename or remove a "## " heading, so
"Commands & workflows" means the same thing on every page ever written. That
makes routing deterministic — ctui slices section X out of each of the week's
pages and hands only that slice to topic X's run — so a week costs one claude
session per section rather than one per (section, day).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

from . import wiki
from .launcher import claude_bin

# Consolidation reads a whole topic page and rewrites parts of it, so it is a
# heavier call than digesting one session.
SESSION_TIMEOUT = 1800

# No Write, as for dream: ctui creates the page, so withholding the tool that
# replaces a whole file enforces "merge, do not clobber" rather than trusting it.
ALLOWED_TOOLS = "Read,Edit,Glob,Grep"

NOTHING_MARKER = "NOTHING TO RECORD"

WEEK_RE = re.compile(r"^(\d{4})-[Ww](\d{1,2})$")

# The tally the pass is asked to end on. Parsed so the ratio of merges to
# additions is visible in the cron log: a run that only ever ADDs is appending,
# which is the failure this pass exists to avoid.
REPORT_RE = re.compile(
    r"MERGED\s+(\d+)\s+SHARPENED\s+(\d+)\s+ADDED\s+(\d+)\s+DROPPED\s+(\d+)",
    re.I,
)


class WeaveError(Exception):
    pass


@dataclass
class TopicWeek:
    """One section's material for one week, gathered across hosts."""

    title: str
    blurb: str
    week: str
    start: Date
    end: Date
    chunks: list[tuple[str, Date, str]] = field(default_factory=list)

    @property
    def days(self) -> int:
        return len({day for _, day, _ in self.chunks})

    @property
    def pages(self) -> int:
        return len(self.chunks)

    @property
    def hosts(self) -> list[str]:
        return sorted({host for host, _, _ in self.chunks})

    def to_markdown(self) -> str:
        head = (
            f"# {self.title} — week {self.week}\n"
            f"{self.start.isoformat()} to {self.end.isoformat()}, "
            f"{self.pages} dated page(s) across {len(self.hosts)} host(s).\n\n"
            "Each block below is one host's page for one day. This is the new\n"
            "material only; the topic page holds everything from before.\n"
        )
        blocks = [
            f"\n## {day.isoformat()} — {host}\n\n{body.strip()}\n"
            for host, day, body in self.chunks
        ]
        return head + "".join(blocks)


def week_id(day: Date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_bounds(day: Date) -> tuple[Date, Date]:
    """Monday and Sunday of the ISO week containing `day`."""
    monday = day - timedelta(days=day.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


def last_week() -> Date:
    """A day inside the most recently completed ISO week."""
    return (datetime.now().astimezone() - timedelta(days=7)).date()


def parse_week(value: str) -> Date:
    """Accept `2026-W38` or any date inside the wanted week."""
    match = WEEK_RE.match(value.strip())
    if match:
        year, number = int(match.group(1)), int(match.group(2))
        if not 1 <= number <= 53:
            raise WeaveError(f"{value!r} is not a valid ISO week.")
        try:
            return Date.fromisocalendar(year, number, 1)
        except ValueError as exc:
            raise WeaveError(f"{value!r} is not a valid ISO week.") from exc
    try:
        return Date.fromisoformat(value.strip())
    except ValueError:
        raise WeaveError(
            f"Expected a week like 2026-W38 or a date like 2026-09-14, got {value!r}."
        ) from None


def cache_dir(week: str) -> Path:
    """Where the per-topic inputs are staged: outside both repos, kept for debugging."""
    base = os.environ.get("CTUI_CACHE") or (Path.home() / ".cache" / "ctui")
    return Path(base) / "weave" / week


def dated_pages(wiki_repo: Path, start: Date, end: Date) -> list[tuple[str, Date, Path]]:
    """Every host's dated pages falling in [start, end], oldest first.

    Across hosts, not just this one: a recurring theme is only visible once
    every machine's dailies are read together.
    """
    root = wiki_repo / wiki.DATED_DIRNAME
    if not root.is_dir():
        return []
    found = []
    for host_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for page in sorted(host_dir.glob("*.md")):
            try:
                day = Date.fromisoformat(page.stem)
            except ValueError:
                continue
            if start <= day <= end:
                found.append((host_dir.name, day, page))
    return sorted(found, key=lambda item: (item[1], item[0]))


def collect(wiki_repo: Path, day: Date) -> list[TopicWeek]:
    """One TopicWeek per section that has any material in `day`'s week.

    Sections with nothing but their template comment are dropped, so a quiet
    week costs no claude sessions at all.
    """
    start, end = week_bounds(day)
    week = week_id(day)
    pages = dated_pages(wiki_repo, start, end)

    topics = {
        title: TopicWeek(title=title, blurb=blurb, week=week, start=start, end=end)
        for title, blurb in wiki.SECTIONS
    }
    for host, when, path in pages:
        try:
            sections = wiki.split_sections(path.read_text())
        except OSError:
            continue
        for title, body in sections.items():
            if title not in topics or wiki.section_is_empty(body):
                continue
            topics[title].chunks.append((host, when, body))

    return [topic for _, topic in
            sorted(topics.items(), key=lambda kv: wiki.section_titles().index(kv[0]))
            if topic.chunks]


def stage(topic: TopicWeek) -> Path:
    out = cache_dir(topic.week)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{wiki.topic_slug(topic.title)}.md"
    path.write_text(topic.to_markdown())
    return path


def weave_prompt(digest_path: Path, page: Path, topic: TopicWeek,
                 index: int, total: int) -> str:
    return f"""\
You are the weekly "weave" pass for ctui. Nightly runs write one dated page per
day; you fold a completed week of those into one durable page per topic.

Topic: {topic.title} ({index} of {total})
  {topic.blurb}

Page: {page}
  The durable page for this topic, holding every week folded in before now.
  It is inside the current directory. Read it in full before you write
  anything. The path is absolute; do not go looking for it elsewhere.

Input: {digest_path}
  This week's material only ({topic.week}, {topic.start.isoformat()} to
  {topic.end.isoformat()}): the "{topic.title}" section of {topic.pages} dated
  page(s) from {len(topic.hosts)} host(s). Everything older is already on the
  page.

This is a merge, not an import. The page already holds everything from before,
and most of what this week taught is a second encounter with something already
written down. Editing existing bullets is the main work; adding new ones is the
exception. A pass that leaves the old bullets untouched and appends the week
underneath them has failed, however well written the additions are.

Work in this order.

STEP 1 — Inventory. Read the page and hold its existing sub-headings and
bullets in mind. Everything below is decided *against that inventory*, not
alongside it.

STEP 2 — Take each item in the input and choose exactly one of:

  MERGE    An existing bullet is about the same tool, command, flag, API,
           failure or concept. Rewrite that bullet so it covers both. Do not
           leave two bullets a reader has to reconcile.
  SHARPEN  The page states a specific case of what this item generalises, or
           this item is a second instance of a pattern recorded once. Rewrite
           the existing bullet as the general rule, and let the per-instance
           detail go.
  ADD      Nothing on the page is about this. Put it under the existing
           sub-heading it belongs to.
  DROP     Already fully covered, or too tied to one task to generalise.

ADD is the last resort, not the default. Reaching for a *new* sub-heading is
the strongest signal that you are appending rather than merging: place material
under an existing heading unless the topic genuinely has no home yet.

STEP 3 — Sweep. Now read the whole page again and fix what is redundant,
whether this week caused it or an earlier one did:
  - two bullets a reader would have to reconcile become one;
  - a sub-heading holding a single bullet is merged away;
  - a sub-heading holding more than about eight is split, or tightened until
    it does not need to be.
This step edits bullets no part of this week touched. That is expected.

STEP 4 — End your reply with exactly one line, and nothing after it:

  MERGED <n> SHARPENED <n> ADDED <n> DROPPED <n>

counting the decisions you made in step 2. Report what you actually did. (The one
exception is the nothing-to-record case at the end of this prompt, which needs no
tally.)

The page should grow by markedly less than the size of the input, and a week
that mostly confirms what is already known should leave it barely longer — or
shorter and sharper.

Rules:

a. Methods, never results. Record how something is done; never what it
   produced, which config won, or any measured value. This is the rule the
   dated pages were written under and it is not relaxed here.
b. Generic over specific. Anything that only makes sense for one task, dataset,
   ticket or run should be generalised as you fold it in, or dropped.
c. Terse and concrete. One learning per bullet. Keep the actual command, flag,
   API or path pattern when it is what makes the bullet useful. No preamble and
   no narrative of the week.
d. You own the "## " sub-headings on this page: add, rename, merge, split and
   reorder them as the material warrants. This is the opposite of the dated
   pages, where the headings are fixed — here the structure is yours to
   maintain, and letting it drift out of date is a failure.
e. Never touch "## Weeks folded in". ctui maintains that section, and it is
   what stops a week being folded in twice.
f. Do not record where a learning came from: no dates, no hostnames, no "as of
   week N". The page is timeless, and the week list already says what has been
   read.
g. Losing information is allowed when it is subsumed. Deleting a specific line
   because you generalised it into a better one is the job working. Deleting
   something the week did not speak to is not.

If this week adds nothing to this topic that is not already on the page, change
nothing and reply exactly:
{NOTHING_MARKER}
"""


def parse_report(stdout: str) -> dict[str, int] | None:
    """The MERGED/SHARPENED/ADDED/DROPPED tally, or None if it was not given."""
    match = None
    for match in REPORT_RE.finditer(stdout):
        pass
    if match is None:
        return None
    return {
        "merged": int(match.group(1)),
        "sharpened": int(match.group(2)),
        "added": int(match.group(3)),
        "dropped": int(match.group(4)),
    }


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
