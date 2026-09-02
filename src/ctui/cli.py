"""ctui — a task-oriented wrapper around claude-code."""

from __future__ import annotations

import argparse
import sys

from . import commands, ui
from .config import ConfigError

VERSION = "0.1.0"

EPILOG = """\
examples:
  ctui --setup                 configure the tasks and wiki repos (run once)
  ctui --init                  make a task rooted at the current directory
                               and launch a claude session in it
  ctui --init -n "fix parser"  ... with the name given up front
  ctui --launch                start another claude session in this task
  ctui --resume                pick any task, then a session to resume
  ctui --sync                  pull and push the tasks and wiki repos
  ctui --list                  print all tasks (non-interactive)

Anything after `--` is passed through to claude, e.g.
  ctui --launch -- --model opus --effort high
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctui",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true",
                      help="interactively configure ~/.ctuirc and create the local repos")
    mode.add_argument("--sync", action="store_true",
                      help="commit, pull and push the tasks and wiki repos")
    mode.add_argument("--init", action="store_true",
                      help="create a task rooted at the current directory and launch a session")
    mode.add_argument("--launch", action="store_true",
                      help="launch a new claude session in the task covering this directory")
    mode.add_argument("--resume", action="store_true",
                      help="pick a task and resume one of its claude sessions")
    mode.add_argument("--list", action="store_true",
                      help="list every known task")

    parser.add_argument("-n", "--name", help="task name for --init (skips the prompt)")
    parser.add_argument("--no-launch", action="store_true",
                        help="with --init, create the task without starting a session")
    parser.add_argument("--version", action="version", version=f"ctui {VERSION}")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER,
                        help=argparse.SUPPRESS)
    return parser


def _claude_passthrough(rest: list[str]) -> list[str]:
    """argparse.REMAINDER keeps the leading `--`; drop it."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    extra = _claude_passthrough(args.claude_args)

    if extra and not (args.init or args.launch or args.resume):
        parser.error("pass-through claude arguments only apply to --init, --launch or --resume")

    try:
        if args.setup:
            return commands.cmd_setup()
        if args.sync:
            return commands.cmd_sync()
        if args.init:
            return commands.cmd_init(name=args.name, launch=not args.no_launch, extra=extra)
        if args.launch:
            return commands.cmd_launch(extra=extra)
        if args.resume:
            return commands.cmd_resume(extra=extra)
        if args.list:
            return commands.cmd_list()
    except ui.Aborted as exc:
        ui.info(f"\n{exc}")
        return 130
    except KeyboardInterrupt:
        ui.info("\nInterrupted.")
        return 130
    except (ConfigError, commands.CommandError) as exc:
        ui.error(str(exc))
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
