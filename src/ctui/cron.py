"""Installing the nightly `dream` job in the user's crontab.

The job is wrapped in marker comments so ctui can replace or remove exactly its
own block and never disturb anything else in the crontab.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MARKER_BEGIN = "# >>> ctui dream >>>"
MARKER_END = "# <<< ctui dream <<<"

CRONTAB_ENV = "CTUI_CRONTAB_BIN"

DEFAULT_HOUR = 4
DEFAULT_MINUTE = 30


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


def log_path() -> Path:
    base = os.environ.get("CTUI_CACHE") or (Path.home() / ".cache" / "ctui")
    return Path(base) / "dream.log"


def dream_line(ctui_bin: str, claude_bin: str | None, hour: int, minute: int) -> str:
    """The crontab entry.

    cron runs with a near-empty environment, so the binaries are absolute and
    the claude path is passed explicitly rather than relying on PATH.
    """
    env = f"CTUI_CLAUDE_BIN={claude_bin} " if claude_bin else ""
    return (f"{minute} {hour} * * * {env}{ctui_bin} --dream "
            f">> {log_path()} 2>&1")


def dream_block(ctui_bin: str, claude_bin: str | None = None,
                hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> str:
    return "\n".join([
        MARKER_BEGIN,
        "# Managed by ctui: `ctui --install-dream` rewrites this block,",
        "# `ctui --uninstall-dream` removes it. Hand edits will be lost.",
        dream_line(ctui_bin, claude_bin, hour, minute),
        MARKER_END,
    ])


def strip_block(text: str) -> str:
    """Remove ctui's block, leaving the rest of the crontab untouched."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MARKER_BEGIN:
            skipping = True
            continue
        if line.strip() == MARKER_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).strip("\n")


def installed() -> bool:
    return MARKER_BEGIN in read_crontab()


def current_line() -> str | None:
    """The installed schedule line, for reporting."""
    inside = False
    for line in read_crontab().splitlines():
        if line.strip() == MARKER_BEGIN:
            inside = True
            continue
        if line.strip() == MARKER_END:
            inside = False
            continue
        if inside and line.strip() and not line.lstrip().startswith("#"):
            return line.strip()
    return None


def install(ctui_bin: str, claude_bin: str | None = None,
            hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> str:
    """Install or replace the dream block. Returns the schedule line."""
    existing = strip_block(read_crontab())
    block = dream_block(ctui_bin, claude_bin, hour, minute)
    combined = f"{existing}\n\n{block}" if existing else block
    write_crontab(combined)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    return dream_line(ctui_bin, claude_bin, hour, minute)


def uninstall() -> bool:
    """Remove the dream block. Returns whether anything was removed."""
    text = read_crontab()
    if MARKER_BEGIN not in text:
        return False
    write_crontab(strip_block(text))
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
