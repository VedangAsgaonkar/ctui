"""The wiki's dated pages: one markdown file per day of distilled learnings."""

from __future__ import annotations

import re
from datetime import date as Date
from pathlib import Path

DATED_DIRNAME = "dated"
FOLDED_HEADING = "## Sessions folded in"
TAGS_PREFIX = "Tags:"

# One marker per session already distilled into a page. An HTML comment rather
# than prose so `--dream` can re-run safely however the model reformats the
# list around it.
FOLDED_MARKER = re.compile(r"<!--\s*ctui:session\s+(\S+)\s*-->")

# The headings a dream pass may write under. Fixed, so pages stay comparable
# across days and the model has somewhere obvious to put each kind of learning.
SECTIONS = [
    ("Libraries & tools", "How to use a library, framework or CLI: setup, key APIs, flags, gotchas."),
    ("Commands & workflows", "How to run a class of thing — a build, an experiment, a deployment — kept generic."),
    ("Metrics & evaluation", "How a metric or check is computed and interpreted. Never the measured values."),
    ("Codebase notes", "Salient structure of a repo: where things live, key abstractions, invariants."),
    ("Practices & conventions", "Programming practices worth repeating, and traps worth avoiding."),
    ("Steering & preferences", "Instructions that corrected course, and standing preferences to honour next time."),
]


def dated_dir(wiki_repo: Path, host: str) -> Path:
    """Dated pages are partitioned by host.

    Only the machine running the cron writes its own directory, so two hosts
    distilling the same night can never conflict on `ctui --sync`. Host sits
    under `dated/` rather than above it so other wiki sections stay top level.
    """
    return wiki_repo / DATED_DIRNAME / host


def dated_path(wiki_repo: Path, day: Date, host: str) -> Path:
    return dated_dir(wiki_repo, host) / f"{day.isoformat()}.md"


def page_template(day: Date, host: str) -> str:
    lines = [
        f"# {day.isoformat()} — {host}",
        "",
        f"{TAGS_PREFIX}",
        "",
        "<!-- Optional, comma-separated topics this day's learnings are about, "
        "e.g. python, git, slurm. Extend the list; leave it empty if nothing fits. -->",
        "",
        f"Reusable learnings distilled from the claude sessions run on {host} "
        "on this day.",
        "",
        "Only what stays useful on a *different* task months from now belongs here:",
        "methods, not results. How an experiment is run, not what it showed. How a",
        "metric is computed, not what it measured.",
        "",
        "No section is compulsory. Most days fill one or two; an empty section",
        "means there was nothing worth recording, which is a fine outcome.",
        "",
    ]
    for title, blurb in SECTIONS:
        lines += [f"## {title}", "", f"<!-- {blurb} -->", ""]
    lines += [FOLDED_HEADING, "", "<!-- appended by ctui, one line per session -->", ""]
    return "\n".join(lines)


def page_tags(page: Path) -> list[str]:
    if not page.exists():
        return []
    for line in page.read_text().splitlines():
        if line.startswith(TAGS_PREFIX):
            raw = line[len(TAGS_PREFIX):]
            return [tag.strip() for tag in raw.split(",") if tag.strip()]
    return []


def ensure_page(wiki_repo: Path, day: Date, host: str) -> Path:
    """The page for `day` on `host`, created from the template if absent."""
    path = dated_path(wiki_repo, day, host)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page_template(day, host))
    return path


def folded_sessions(page: Path) -> set[str]:
    """Session ids already distilled into this page."""
    if not page.exists():
        return set()
    return set(FOLDED_MARKER.findall(page.read_text()))


def record_folded(page: Path, session_id: str, task_id: str, task_name: str) -> None:
    """Note that a session has been distilled, so a re-run skips it.

    ctui owns this section rather than the model: it is what makes `--dream`
    idempotent, so it must not depend on the model remembering to write it.
    """
    text = page.read_text() if page.exists() else ""
    entry = (f"<!-- ctui:session {session_id} -->\n"
             f"- `{session_id[:8]}` · {task_id} ({task_name})\n")

    if FOLDED_HEADING in text:
        head, _, tail = text.partition(FOLDED_HEADING)
        text = f"{head}{FOLDED_HEADING}{tail.rstrip()}\n{entry}"
    else:
        text = f"{text.rstrip()}\n\n{FOLDED_HEADING}\n\n{entry}"
    page.write_text(text)


def section_titles() -> list[str]:
    return [title for title, _ in SECTIONS]
