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


def ask_select(message: str, choices: list[Choice], default=None):
    require_interactive(message)
    return _unwrap(questionary.select(
        message,
        choices=choices,
        default=default,
        style=STYLE,
        use_shortcuts=False,
        instruction="(↑/↓ to move, enter to select)",
    ).ask())


# ---- plain output ----------------------------------------------------

def info(msg: str) -> None:
    print(msg)


def step(msg: str) -> None:
    print(f"  \033[36m→\033[0m {msg}")


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"\033[31merror:\033[0m {msg}", file=sys.stderr)


def heading(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m")
