# ctui

A task-oriented wrapper around [claude-code](https://claude.com/claude-code).

`ctui` gives every piece of work a **task**: a durable directory, tracked in git and
syncable across machines, that remembers which project directory the work is rooted
in and which claude-code sessions belong to it. Sessions launched through `ctui` are
told to keep their intermediate and output artifacts inside the task directory, so
scratch work stops piling up in `/tmp` and in your project tree.

## Install

Dependencies are managed by this project's own pixi environment.

```sh
cd ctui
pixi install                 # creates .pixi/envs/default with python + questionary
pixi run where               # prints the absolute path of the `ctui` shim
```

Put that shim on your `PATH`, e.g. for fish:

```fish
fish_add_path /path/to/ctui/.pixi/envs/default/bin
```

Or run it without installing: `pixi run ctui --help`.

`claude` must be on your `PATH` (or point `CTUI_CLAUDE_BIN` at it).

## Commands

| Command | What it does |
| --- | --- |
| `ctui --setup` | Interactively configure the tasks/wiki repos and write `~/.ctuirc`. Run once per machine. |
| `ctui --init` | Create a task rooted at the current directory, then launch a claude session in it. |
| `ctui --launch` | Launch another claude session in the task covering the current directory. |
| `ctui --resume` | Pick any task, then resume one of its sessions (or open a shell in its root). |
| `ctui --sync` | Commit local changes, pull, and push the tasks and wiki repos. |
| `ctui --list` | Print every known task, non-interactively. |

Extra arguments after `--` are passed straight to claude:

```sh
ctui --init -n "fix the parser" -- --model opus --effort high
```

### `ctui --setup`

Prompts for:

1. **Git remote for the tasks repo** — optional; blank keeps it local-only.
2. **Git remote for the wiki repo** — blank keeps it local-only.
3. **Path to hold the local checkouts** — defaults to your home directory; the repos
   are created as `<path>/ctui-tasks` and `<path>/ctui-wiki`.

For each repo it clones the remote if one was given (falling back to `git init` when
the remote is empty), wires up `origin`, checks out the branch the *remote* actually
uses rather than whatever `git init` defaulted to, and pulls. The resulting paths are
recorded in `~/.ctuirc`.

### `ctui --init`

Run it in the directory you want to work in. It prompts for a task name (defaulting
to the directory name), creates the task, commits it to the tasks repo, and launches
a claude session. `--no-launch` creates the task without starting a session.

### `ctui --resume`

Lists every task across every host, labelled `TASK_<datetime>` plus its name and
session count. Tasks whose root directory does not exist on the current machine are
flagged `root missing`. After picking a task you can resume any of its sessions
(newest first) or open a shell in the task root.

`ctui` does not change your calling shell's directory — it `chdir`s and `exec`s
claude (or a shell) in the task root, so your own shell is where you left it.

## Layout

`~/.ctuirc`:

```json
{
  "tasks_repo": "/home/you/ctui-tasks",
  "wiki_repo": "/home/you/ctui-wiki",
  "tasks_remote": "git@example.com:you/tasks.git",
  "wiki_remote": "git@example.com:you/wiki.git",
  "version": 1
}
```

The tasks repo is hierarchical, with the **fully-qualified** hostname at the first
level so several machines can share one remote without colliding:

```
ctui-tasks/
└── furiosa.stanford.edu/
    └── TASK_20260901_234905/
        ├── root -> /abs/path/to/project     # symlink to the task's root directory
        └── task.json
```

The hostname comes from `socket.gethostname()` when that is already qualified — no
name resolution, so it cannot change under you when DNS is unavailable — falling back
to `socket.getfqdn()` for machines whose `gethostname()` is unqualified, and to the
short name if that yields only a `localhost` placeholder. `CTUI_HOSTNAME` overrides it.

`task.json`:

```json
{
  "name": "fix the parser",
  "id": "TASK_20260901_234905",
  "hostname": "furiosa.stanford.edu",
  "root": "/abs/path/to/project",
  "created_at": "2026-09-01T23:49:05-07:00",
  "sessions": [
    { "session_id": "f93a9f07-0e81-4fcb-be12-ce13ed6ee6d1", "created_at": "..." }
  ]
}
```

The absolute root path is stored alongside the symlink, so a task synced from another
machine is still identifiable even when its symlink dangles.

### The root index

Each host keeps an `index.json` beside its task directories, mapping root directory to
the task ids rooted there:

```json
{
  "version": 1,
  "hostname": "furiosa.stanford.edu",
  "roots": { "/abs/path/to/project": ["TASK_20260901_234905"] },
  "entries": { "TASK_20260901_234905": 1756800000000000000 }
}
```

`ctui --init` and `ctui --launch` need to answer "is there a task for this directory?",
which otherwise means opening every `task.json` on the host — O(tasks) file reads for a
question usually answered "no". With the index, a lookup opens only the `task.json`
files that actually match: with 500 tasks, a miss goes from 500 reads (~50 ms) to zero.

It is a cache, never the source of truth — `task.json` owns the root path. `entries`
records each `task.json`'s mtime, so a lookup validates the index with one directory
listing plus a stat per task, and rebuilds if anything was added, removed, edited, or
arrived by `git pull`. If the index points at a task whose `task.json` disagrees, the
file wins and the index is rebuilt. Deleting `index.json` is always safe.

Kernel timestamps advance on a coarse tick, so an mtime comparison alone cannot see an
edit that landed in the same tick as ctui's own write. As git does for its own index,
any entry whose mtime is not strictly *older* than `index.json` is treated as suspect
and re-read instead of trusted. Normally that is nothing; right after creating a task
it is that one task, and rewriting the index moves it past them so the next lookup is
a plain cache hit.

It is stored per hostname rather than as one shared file: the lookups are host-scoped
anyway, and a single global file would be rewritten by every machine and conflict on
every `ctui --sync`. Each host only ever writes its own.

Session IDs are generated by `ctui` and handed to claude via `--session-id`, so a
session is recorded in `task.json` *before* it starts rather than scraped afterwards.
`ctui --resume` replays them with `claude --resume <id>`.

## Artifact policy

Every session launched through `ctui` gets the task directory added with `--add-dir`
and an appended system prompt (`launcher.artifact_system_prompt`) that tells it:

- **where to look** — the task directory is the task's persistent workspace, shared by
  every session ever launched for it, so artifacts from earlier sessions are already
  there and should be read before starting work;
- **where to write** — intermediate and output artifacts (scratch files, notes, logs,
  generated data, reports) belong in the task directory, not `/tmp`, and should be
  left in a state a later session can pick up;
- **no inline multi-line code** — a bright line, not a judgement call: any
  `python3 -c`, `node -e`, `bash -c` or heredoc carrying more than one line of code
  goes into `<task dir>/scripts/<name>` first and is invoked from there, with no
  triviality exception. Single-line commands and pipelines are unaffected. A script
  that is the deliverable, or that belongs to the project, still goes in a file —
  just in the location it was asked for rather than the task directory;
- **the exception** — changes to the task root itself are edited in place as normal
  and must not be mirrored into the task directory.

The task directory path is interpolated absolutely. This matters because `ctui`
`chdir`s into the task *root* before exec'ing claude, so a relative path would be
resolved against the wrong directory: `~/.ctuirc` repo paths are therefore required to
be absolute (`ctui --setup` always writes them that way), and the launcher refuses to
start a session whose task directory or root is relative.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `CTUI_RC` | Override the config location (default `~/.ctuirc`). |
| `CTUI_HOSTNAME` | Override the hostname used for the tasks-repo subdirectory. |
| `CTUI_CLAUDE_BIN` | Full path to the `claude` executable. |

## Development

```sh
pixi run -e dev test
```
