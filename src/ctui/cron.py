"""Installing the nightly `dream` job in the user's crontab.

The job is wrapped in marker comments so ctui can replace or remove exactly its
own block and never disturb anything else in the crontab.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

DREAM_JOB = "dream"
WEAVE_JOB = "weave"


def markers(job: str = DREAM_JOB) -> tuple[str, str]:
    """Begin/end comments delimiting one job's block.

    Parameterised by job so `dream` and `weave` own separate blocks and
    installing one never disturbs the other. The dream strings are unchanged,
    so a crontab written by an older ctui is still recognised.
    """
    return f"# >>> ctui {job} >>>", f"# <<< ctui {job} <<<"


MARKER_BEGIN, MARKER_END = markers(DREAM_JOB)

CRONTAB_ENV = "CTUI_CRONTAB_BIN"

DEFAULT_HOUR = 4
DEFAULT_MINUTE = 30

# Weave runs after the last nightly dream of the week has landed.
DEFAULT_WEAVE_HOUR = 5
DEFAULT_WEAVE_MINUTE = 30
DEFAULT_WEEKDAY = 1  # Monday, so a week is distilled once it is complete


class CronError(Exception):
    pass


def crontab_bin() -> str:
    override = os.environ.get(CRONTAB_ENV)
    if override:
        return override
    found = shutil.which("crontab")
    if not found:
        raise CronError("No `crontab` on PATH; cannot manage the dream job here.")
    return found


def available() -> bool:
    try:
        crontab_bin()
    except CronError:
        return False
    return True


def read_crontab() -> str:
    """The current crontab, or "" when the user has none."""
    proc = subprocess.run([crontab_bin(), "-l"], capture_output=True, text=True)
    if proc.returncode != 0:
        # An empty crontab is reported as an error by most implementations.
        if "no crontab" in (proc.stderr + proc.stdout).lower():
            return ""
        raise CronError(f"`crontab -l` failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def write_crontab(text: str) -> None:
    if text and not text.endswith("\n"):
        text += "\n"
    proc = subprocess.run([crontab_bin(), "-"], input=text,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise CronError(f"`crontab -` failed: {(proc.stderr or proc.stdout).strip()}")


def log_path(job: str = DREAM_JOB) -> Path:
    base = os.environ.get("CTUI_CACHE") or (Path.home() / ".cache" / "ctui")
    return Path(base) / f"{job}.log"


def job_line(job: str, ctui_bin: str, claude_bin: str | None,
             hour: int, minute: int, home: str | None = None,
             weekday: int | None = None) -> str:
    env = [f"HOME={home or Path.home()}"]
    if claude_bin:
        env.append(f"CTUI_CLAUDE_BIN={claude_bin}")
    dow = "*" if weekday is None else str(weekday)
    return (f"{minute} {hour} * * {dow} {' '.join(env)} {ctui_bin} --{job} "
            f">> {log_path(job)} 2>&1")


def job_block(job: str, ctui_bin: str, claude_bin: str | None = None,
              hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
              home: str | None = None, weekday: int | None = None) -> str:
    begin, end = markers(job)
    return "\n".join([
        begin,
        f"# Managed by ctui: `ctui --install-{job}` rewrites this block,",
        f"# `ctui --uninstall-{job}` removes it. Hand edits will be lost.",
        job_line(job, ctui_bin, claude_bin, hour, minute, home, weekday),
        end,
    ])


def strip_block(text: str, job: str = DREAM_JOB) -> str:
    """Remove one job's block, leaving the rest of the crontab untouched."""
    begin, end = markers(job)
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == begin:
            skipping = True
            continue
        if line.strip() == end:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).strip("\n")


def installed(job: str = DREAM_JOB) -> bool:
    return markers(job)[0] in read_crontab()


def current_line(job: str = DREAM_JOB) -> str | None:
    """The installed schedule line, for reporting."""
    begin, end = markers(job)
    inside = False
    for line in read_crontab().splitlines():
        if line.strip() == begin:
            inside = True
            continue
        if line.strip() == end:
            inside = False
            continue
        if inside and line.strip() and not line.lstrip().startswith("#"):
            return line.strip()
    return None


def install(ctui_bin: str, claude_bin: str | None = None,
            hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
            home: str | None = None, job: str = DREAM_JOB,
            weekday: int | None = None) -> str:
    home = home or str(Path.home())
    existing = strip_block(read_crontab(), job)
    block = job_block(job, ctui_bin, claude_bin, hour, minute, home, weekday)
    combined = f"{existing}\n\n{block}" if existing else block
    write_crontab(combined)
    log_path(job).parent.mkdir(parents=True, exist_ok=True)
    return job_line(job, ctui_bin, claude_bin, hour, minute, home, weekday)


def uninstall(job: str = DREAM_JOB) -> bool:
    """Remove a job's block. Returns whether anything was removed."""
    text = read_crontab()
    if markers(job)[0] not in text:
        return False
    write_crontab(strip_block(text, job))
    return True


def parse_time(value: str) -> tuple[int, int]:
    """Parse "HH:MM" into (hour, minute)."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise CronError(f"Expected a time like 04:30, got {value!r}.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise CronError(f"Expected a time like 04:30, got {value!r}.") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise CronError(f"{value!r} is not a valid time of day.")
    return hour, minute
