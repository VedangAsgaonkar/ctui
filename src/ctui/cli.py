"""ctui — a task-oriented wrapper around claude-code."""

from __future__ import annotations

import argparse
import sys

from . import commands, cron, ui, view
from .config import ConfigError

VERSION = "0.1.0"

EPILOG = """\
examples:
  ctui --setup                 configure the tasks and wiki repos (run once)
  ctui --init                  make a task rooted at the current directory
                               and launch a claude session in it
  ctui --init -n "fix parser"  ... with the name given up front
  ctui --launch                start another claude session in this task
  ctui --resume                resume a session in a task rooted at (or
                               above) the current directory
  ctui --resume --global       ... pick from every known task instead
  ctui --resume --recent       ... pick from the 5 most recently opened
  ctui --fork                  branch a new session off an existing one
  ctui --fork --global         ... picking from every known task
  ctui --sync                  pull and push the tasks and wiki repos
  ctui --dream                 distil yesterday's sessions into the wiki
  ctui --dream --date 2026-09-01 --dry-run
                               show what would be distilled, without claude
  ctui --install-dream --at 03:00
                               (re)install the nightly cron job
  ctui --list                  print all tasks (non-interactive)
  ctui --view 8080             browse tasks and their artifacts in a browser

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
                      help="pick a task covering this directory and resume one of "
                           "its claude sessions")
    mode.add_argument("--fork", action="store_true",
                      help="branch a new claude session off an existing one, "
                           "leaving the original untouched")
    mode.add_argument("--list", action="store_true",
                      help="list every known task")
    mode.add_argument("--view", nargs="?", const=str(view.DEFAULT_PORT),
                      metavar="PORT",
                      help=f"serve a task/artifact browser on "
                           f"{view.BIND_HOST}:PORT (default {view.DEFAULT_PORT})")
    mode.add_argument("--dream", action="store_true",
                      help="distil a day's claude sessions into the wiki's dated "
                           "page (run nightly by cron)")
    mode.add_argument("--install-dream", action="store_true",
                      help="install or replace the daily dream cron job")
    mode.add_argument("--uninstall-dream", action="store_true",
                      help="remove the daily dream cron job")

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="global_scope", action="store_true",
                       help="with --resume/--fork, offer every known task instead "
                            "of only those covering this directory")
    scope.add_argument("--recent", action="store_true",
                       help=f"with --resume/--fork, offer the {commands.RECENT_LIMIT} "
                            "most recently opened tasks")

    parser.add_argument("-n", "--name", help="task name for --init (skips the prompt)")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="with --dream, the day to distil (default: yesterday)")
    parser.add_argument("--at", metavar="HH:MM",
                        help=f"with --install-dream, the time to run "
                             f"(default {cron.DEFAULT_HOUR:02d}:{cron.DEFAULT_MINUTE:02d})")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --dream, stage the digests and report without "
                             "calling claude")
    parser.add_argument("--no-launch", action="store_true",
                        help="with --init, create the task without starting a session")
    parser.add_argument("--version", action="version", version=f"ctui {VERSION}")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER,
                        help=argparse.SUPPRESS)
    return parser


def _scope(args) -> str:
    if args.recent:
        return commands.SCOPE_RECENT
    if args.global_scope:
        return commands.SCOPE_GLOBAL
    return commands.SCOPE_LOCAL


def _claude_passthrough(rest: list[str]) -> list[str]:
    """argparse.REMAINDER keeps the leading `--`; drop it."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    extra = _claude_passthrough(args.claude_args)

    if extra and not (args.init or args.launch or args.resume or args.fork
                      or args.dream):
        parser.error("pass-through claude arguments only apply to --init, "
                     "--launch, --resume, --fork or --dream")

    port = None
    if args.view is not None:
        try:
            port = int(args.view)
        except ValueError:
            parser.error(f"--view needs a port number, got {args.view!r}")
        if not 0 <= port <= 65535:
            parser.error(f"--view port must be between 0 and 65535, got {port}")

    if args.date and not args.dream:
        parser.error("--date only applies to --dream")
    if args.dry_run and not args.dream:
        parser.error("--dry-run only applies to --dream")
    if args.at and not args.install_dream:
        parser.error("--at only applies to --install-dream")

    if (args.global_scope or args.recent) and not (args.resume or args.fork):
        parser.error("--global and --recent only apply to --resume and --fork")

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
            return commands.cmd_resume(extra=extra, scope=_scope(args))
        if args.fork:
            return commands.cmd_fork(extra=extra, scope=_scope(args))
        if args.list:
            return commands.cmd_list()
        if port is not None:
            return commands.cmd_view(port)
        if args.dream:
            return commands.cmd_dream(day=args.date, extra=extra,
                                      dry_run=args.dry_run)
        if args.install_dream:
            return commands.cmd_install_dream(at=args.at)
        if args.uninstall_dream:
            return commands.cmd_uninstall_dream()
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
