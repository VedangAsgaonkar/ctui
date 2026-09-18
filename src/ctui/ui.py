"""Terminal UI helpers.

All interaction goes through questionary (prompt_toolkit). When there is no TTY
we refuse to prompt rather than silently guessing, so scripted use fails loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import questionary
from questionary import Choice, Style

STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:cyan"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:cyan"),
    ("separator", "fg:#666666"),
    ("instruction", "fg:#888888"),
])


class Aborted(Exception):
    """The user pressed Ctrl-C / Esc at a prompt."""


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_interactive(what: str) -> None:
    if not interactive():
        raise Aborted(f"{what} needs an interactive terminal.")


def _unwrap(value):
    if value is None:
        raise Aborted("Cancelled.")
    return value


def ask_text(message: str, default: str = "", allow_empty: bool = False) -> str:
    require_interactive(message)
    while True:
        answer = _unwrap(questionary.text(message, default=default, style=STYLE).ask())
        answer = answer.strip()
        if answer or allow_empty:
            return answer
        info("A value is required.")


def ask_path(message: str, default: str = "") -> Path:
    return Path(ask_text(message, default=default)).expanduser()


def ask_confirm(message: str, default: bool = True) -> bool:
    require_interactive(message)
    return bool(_unwrap(questionary.confirm(message, default=default, style=STYLE).ask()))


def ask_select(message: str, choices: list[Choice], default=None,
               filterable: bool = True):
    """A select prompt; typing filters the list by substring.

    questionary matches the typed text against each choice's rendered title
    (case-insensitively), so for the task list that covers the task id, name and
    host at once. j/k must be off: with type-to-filter they would be swallowed
    as filter text, and questionary raises if both are enabled.
    """
    require_interactive(message)
    if filterable:
        instruction = "(↑/↓ to move, type to filter, enter to select)"
    else:
        instruction = "(↑/↓ to move, enter to select)"
    return _unwrap(questionary.select(
        message,
        choices=choices,
        default=default,
        style=STYLE,
        use_shortcuts=False,
        use_jk_keys=not filterable,
        use_search_filter=filterable,
        instruction=instruction,
    ).ask())


# ---- plain output ----------------------------------------------------
#
# Everything flushes. stdout is block-buffered when it is not a tty, so the
# long unattended commands — `--dream`, `--weave`, `--view` — would otherwise
# write nothing to their cron log until they exited, which is exactly when
# progress stops being useful for diagnosing a hang.

def info(msg: str) -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"  \033[36m→\033[0m {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"\033[31merror:\033[0m {msg}", file=sys.stderr, flush=True)


def heading(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m", flush=True)
