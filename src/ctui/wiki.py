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


# =====================================================================
# topic pages
#
# One durable page per SECTION, consolidated weekly from the dated pages by
# `ctui --weave`. The sections are the topic axis because they are the one
# thing every dated page shares: dream may never add, rename or remove a "## "
# heading, so "Commands & workflows" means the same thing on every page ever
# written. The `Tags:` line is free-form and varies day to day, so it cannot
# key a stable set of files.
#
# Unlike a dated page, a topic page is *rewritten* rather than appended to:
# its whole purpose is to merge a new week into what is already there.
# =====================================================================

TOPICS_DIRNAME = "topics"
FOLDED_WEEKS_HEADING = "## Weeks folded in"

WEEK_MARKER = re.compile(r"<!--\s*ctui:week\s+(\S+)\s*-->")

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def topic_slug(title: str) -> str:
    """File-name form of a section title: 'Libraries & tools' -> libraries-and-tools."""
    text = title.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def topics_dir(wiki_repo: Path) -> Path:
    """Topic pages are shared across hosts, unlike the dated pages.

    A recurring theme is only visible once every machine's dailies land in the
    same file, which is the whole point of the weekly pass. The cost is that
    `--weave` is a single-writer job: two hosts distilling the same week would
    conflict on `ctui --sync`.
    """
    return wiki_repo / TOPICS_DIRNAME


def topic_path(wiki_repo: Path, title: str) -> Path:
    return topics_dir(wiki_repo) / f"{topic_slug(title)}.md"


def topic_template(title: str, blurb: str) -> str:
    return "\n".join([
        f"# {title}",
        "",
        f"<!-- {blurb} -->",
        "",
        f"Durable {title.lower()} learnings, consolidated weekly by `ctui --weave`",
        "from the dated pages of every host.",
        "",
        "This page is *merged*, not appended to. A new week's material is folded",
        "into what is already here: a second instance of a pattern sharpens the",
        "existing line rather than adding a neighbour to it. The page is meant to",
        "get denser over time, not longer.",
        "",
        "Methods, not results — the same rule the dated pages are written under.",
        "",
        FOLDED_WEEKS_HEADING,
        "",
        "<!-- appended by ctui, one line per week -->",
        "",
    ])


def ensure_topic_page(wiki_repo: Path, title: str, blurb: str = "") -> Path:
    """The topic page for `title`, created from the template if absent.

    ctui creates it so the weave session never needs Write, exactly as for the
    dated pages: with only Edit it cannot replace a page wholesale however the
    instructions are read.
    """
    path = topic_path(wiki_repo, title)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(topic_template(title, blurb))
    return path


def folded_weeks(page: Path) -> set[str]:
    """Week ids already folded into this topic page."""
    if not page.exists():
        return set()
    return set(WEEK_MARKER.findall(page.read_text()))


def record_folded_week(page: Path, week: str, days: int, pages: int) -> None:
    """Note that a week has been folded in, so a re-run skips it.

    ctui owns this section rather than the model, for the same reason it owns
    "## Sessions folded in": it is what makes the pass idempotent.
    """
    text = page.read_text() if page.exists() else ""
    entry = (f"<!-- ctui:week {week} -->\n"
             f"- `{week}` · {days} day(s) from {pages} dated page(s)\n")

    if FOLDED_WEEKS_HEADING in text:
        head, _, tail = text.partition(FOLDED_WEEKS_HEADING)
        text = f"{head}{FOLDED_WEEKS_HEADING}{tail.rstrip()}\n{entry}"
    else:
        text = f"{text.rstrip()}\n\n{FOLDED_WEEKS_HEADING}\n\n{entry}"
    page.write_text(text)


def split_sections(text: str) -> dict[str, str]:
    """Map each "## " heading of a page to its body.

    Used to slice a dated page along the one axis every page shares, so each
    topic page's weave run sees only the material that belongs to it.
    """
    out: dict[str, str] = {}
    matches = list(HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out[match.group(1)] = text[match.end():end].strip("\n")
    return out


def section_is_empty(body: str) -> bool:
    """True when a section holds nothing but its template comment."""
    return not COMMENT_RE.sub("", body).strip()
