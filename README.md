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
pixi install                 # creates .pixi/envs/default with python, questionary, pygments
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
| `ctui --resume` | Pick a task covering the current directory, then resume one of its sessions (or open a shell in its root). |
| `ctui --resume --global` | ... offer every known task instead. |
| `ctui --resume --recent` | ... offer the 5 most recently opened tasks. |
| `ctui --fork` | Branch a new session off an existing one, leaving the original untouched. |
| `ctui --sync` | Commit local changes, pull, and push the tasks and wiki repos. |
| `ctui --list` | Print every known task, non-interactively. |
| `ctui --view [PORT]` | Serve a browser for tasks and their artifacts on `127.0.0.1:PORT` (default 8765). |
| `ctui --dream` | Distil a day's claude sessions into the wiki's dated page (run nightly by cron). |
| `ctui --weave` | Fold a completed week's dated pages into the topic pages (run weekly by cron). |
| `ctui --install-dream` | Install or replace the nightly cron job. `--uninstall-dream` removes it. |
| `ctui --install-weave` | Install or replace the weekly cron job. `--uninstall-weave` removes it. |

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

Tasks are labelled `TASK_<datetime>` plus their name and session count. **Typing
filters the list** by substring, matched case-insensitively against that whole label,
so `pars`, `2609` or a hostname all narrow it. Tasks whose root directory does not
exist on the current machine are flagged `root missing`. After picking a task you can
resume any of its sessions (newest first) or open a shell in the task root.

Three scopes:

| | Offers |
| --- | --- |
| `ctui --resume` | tasks rooted at the current directory, or at the nearest enclosing directory that is a task root — the same search `--launch` does, so it works from a subdirectory |
| `--global` | every task, across every host |
| `--recent` | the 5 most recently opened tasks, most recent first |

`ctui` does not change your calling shell's directory — it `chdir`s and `exec`s
claude (or a shell) in the task root, so your own shell is where you left it.

### `ctui --fork`

Branch a new session off an existing one. The fork starts with the parent's whole
conversation already in context and then diverges; the parent is left exactly as it
was. Use it to try a second approach from a known-good point without losing the first.

```sh
ctui --fork                  # tasks covering this directory
ctui --fork --global         # ... or pick from every task
ctui --fork -- --model opus  # pass-through args work as everywhere else
```

It prompts the same way `--resume` does — task first, then session — except the second
list offers *fork* rather than *resume*, and sessions that are themselves forks are
labelled `↳ forked from <id>` in both lists.

Underneath it is `claude --resume <parent> --fork-session --session-id <new>`.
`--fork-session` honours an explicit `--session-id`, which is what lets ctui keep its
usual invariant: the fork is minted and written into `task.json` *before* claude
starts, not scraped afterwards.

**ctui records the lineage because claude does not.** A forked transcript is rewritten
to carry only the new session's id and never mentions the parent anywhere, so the
`forked_from` field in `task.json` is the only record that the branch happened.

Replayed records keep their original timestamps, so `--dream` does not re-digest a
parent's earlier day through its fork — the fork only contributes the turns that
actually happened on the day being distilled. Forking a session on the same day it was
created does overlap, which dream's own no-duplicates rule absorbs.

### `ctui --dream`

The nightly pass that turns the day's work into a wiki. `ctui --setup` offers to
install it as a cron job (default 04:30); `--install-dream --at HH:MM` changes the
time, `--uninstall-dream` removes it. ctui wraps its own crontab entry in marker
comments, so installing replaces its block and never disturbs your other entries,
and the entry uses absolute paths because cron runs with a near-empty environment.

For a given day (default: yesterday) it:

1. Finds every session active that day, from this host's tasks and the session ids in
   their `task.json`. A session's transcript is at
   `~/.claude/projects/<root with / as ->/<session-id>.jsonl`, so no bookkeeping is
   needed to locate it. Whether it is worth parsing is decided from the file itself,
   from both directions: its mtime cannot precede a record it holds (one stat), and
   its first record cannot postdate the day (one line). Transcript timestamps are UTC,
   so they are converted to local dates before bucketing. On 2000 sessions, the
   first-record bound alone cuts a run by ~37% versus filtering on mtime only, because
   a transcript that started later is rejected after one line instead of a full parse.
2. Digests each one: user messages verbatim, assistant prose, and the tool *calls*.
   Tool *results* and thinking blocks are dropped, which is most of the volume — a
   53 KB transcript becomes ~5 KB. Digests are staged under `~/.cache/ctui/dream/`.
3. Runs a claude session per digest, in the wiki repo, appending to
   `dated/<hostname>/<date>.md`. Each run sees what earlier ones wrote, so the page
   accumulates rather than being rewritten.
4. Records each session in `## Sessions folded in`, then commits.

`--dream --dry-run` stages the digests and reports without calling claude, and
`--date YYYY-MM-DD` re-runs an earlier day.

**Only reusable learnings are recorded** — methods, not results. The prompt makes this
the top rule with worked examples: record how to compute a constant with a named
series, never the digits; how to run a class of benchmark, never the numbers it
produced; how a metric is defined, never its value. Anything that only makes sense for
one task, dataset or run is generalised or dropped. The dream session is given
`Read,Edit,Glob,Grep` and deliberately **not** `Write`, so it cannot replace the page
wholesale however the instructions are read.

Re-running is safe: sessions already listed under `## Sessions folded in` are skipped,
which is tracked by ctui via HTML comment markers rather than by the model, so it
survives any reformatting. A session that fails is left unmarked and retried next
time, and one bad session never aborts the rest of the day.

### `ctui --view`

A read-only HTTP browser for what the tasks accumulated: the task list, each task's
metadata and sessions, and its artifacts rendered by type.

```sh
ctui --view 8080      # or bare `ctui --view` for port 8765
```

It listens on `127.0.0.1` only — never the wildcard — because on a shared compute node
binding `0.0.0.0` would publish every task's artifacts to everyone else on the box. For
a remote machine, tunnel it; the command prints the exact `ssh -L` line to use. Nothing
is written and no claude session is started, so it is safe to leave running.

| Route | Shows |
| --- | --- |
| `/` | every task, newest first, with a recently-opened strip and a type-to-filter box (press `/`), matching `--resume`'s filtering |
| `/task/<host>/<id>` | name, root, task dir, created; every session with its transcript size; the top-level artifact listing |
| `/file/<host>/<id>/<path>` | one artifact, rendered; or a directory listing |
| `/raw/<host>/<id>/<path>` | the bytes, for `<img>`/`<iframe>` sources and downloads |

**Artifacts means the task directory**, in the same sense as the artifact policy below:
the byproducts of the work, as against the project itself. The task root is shown as a
path and flagged when missing, but is not browsable, which keeps the served surface
exactly one directory per task.

Rendering is per kind, all in-process — nothing is fetched from a CDN, so it works on
a node with no outbound network:

| Kind | Treatment |
| --- | --- |
| markdown | rendered, with a source toggle; HTML comments are hidden, so a dream page reads as prose and its `ctui:session` markers stay out of the way |
| notebooks | per cell — markdown rendered, code line-numbered, outputs including base64 images, tracebacks ANSI-stripped |
| JSON / JSONL | pretty-printed; one collapsible record per line for JSONL |
| CSV / TSV | a real table, parsed with the `csv` module so quoting and embedded newlines survive |
| code | syntax highlighted by [pygments](https://pygments.org), line-numbered, language labelled |
| logs / text | line-numbered, ANSI escapes stripped; diffs and patches get highlighted too |
| images, PDF, video, audio | shown inline |
| zip / tar | member listing |
| binary | size, guessed type, hexdump preview, download link |

Generated data lands in task directories, so everything is capped (2 MB per rendered
file, 500 table rows, 200 JSONL records, 512 KB for highlighting): over the cap the page
shows a head and a link to the raw bytes rather than trying to render a gigabyte.

Highlighting picks a lexer from the filename first and the fenced-block language second,
so a `.diff` or a `Makefile` is recognised even though ctui has no extension entry for
it, and a ```` ```python ```` block inside a markdown artifact is coloured too. Token
colours come from the same CSS variables as the rest of the page, so light and dark stay
consistent. If no lexer matches, the file is served as plain line-numbered text rather
than guessed at — and if highlighting would ever change the line count, it is discarded,
because the gutter must stay aligned with the code.

Because it serves file contents over HTTP, four things are deliberate. A request is
confined to its task directory by resolving the path and requiring the result to stay
inside — which rejects `..`, and rejects the `root` symlink by the same test rather than
a special case. `/raw` serves only images, PDF, media, `text/plain` and `text/html`
inline and forces everything else to download, so an artifact cannot choose its own
renderer. HTML artifacts render in a sandboxed iframe with scripts and network access
off. And every page carries `default-src 'self'`, which is why the CSS and JS are
served as their own routes rather than inlined — no `unsafe-inline` anywhere, and no
CDN, so it works on a node with no outbound network.

### `ctui --weave`

The weekly pass that turns dated pages into durable ones. `--dream` writes what was
learned on a given day; `--weave` folds a completed week of those into one page per
topic, so a pattern met three times in three weeks ends up as one sharp line rather
than three similar bullets on three different days.

```sh
ctui --weave                          # last completed ISO week
ctui --weave --week 2026-W38          # a specific week
ctui --weave --week 2026-09-16        # ... or any date inside it
ctui --weave --dry-run                # what would be folded in, without claude
ctui --install-weave --at 05:30       # Monday 05:30 by default
```

**Topics are the dated pages' sections**, not the `Tags:` line. That is the one axis
every page shares: dream may never add, rename or remove a `## ` heading, so
"Commands & workflows" means the same thing on every page ever written, while tags
are free-form and drift. It also makes routing deterministic — ctui slices section X
out of each of the week's pages and hands only that slice to topic X's run — so a
week costs one claude session per topic rather than one per (topic, day), and each
session sees a few KB instead of the whole week. Sections that are empty all week
cost nothing at all.

It reads **every host's** dated pages, because a recurring theme is only visible once
the machines are read together, and writes the shared `topics/` pages. That makes it
a single-writer job: install the cron on one machine. Two hosts weaving the same week
would conflict on `ctui --sync`, which is exactly why the *dated* pages are per-host
and these are not.

The pass has two jobs and the second is the one a careless run gets wrong:

1. **Add** what is genuinely new.
2. **Integrate** it with what is there — which means editing bullets this week never
   touched.

So the prompt does not merely ask for that. It requires a decision per incoming item
— `MERGE` into an existing bullet, `SHARPEN` an existing bullet into the general rule
the new instance reveals, `ADD`, or `DROP` — with `ADD` named as the last resort and
a new sub-heading called out as the strongest signal that a pass is appending. A
sweep step then re-reads the whole page for redundancy that earlier weeks left
behind. Each run ends with a tally:

```
MERGED 7 SHARPENED 4 ADDED 3 DROPPED 2
```

which ctui parses and prints, so a run that only ever `ADD`s is visible in the cron
log rather than silently growing the page. Getting this wrong is the default
behaviour: an earlier version of the prompt that only *asked* for merging left 88% of
existing bullets untouched and landed within 4% of a naive append.

Re-running is safe: a topic page records the weeks folded into it with
`<!-- ctui:week 2026-W38 -->` markers that ctui owns, so a week is never folded twice
however the model reformats the list, a topic that fails is retried next time, and
one failing topic never costs the rest of the week. The weave session gets
`Read,Edit,Glob,Grep` and, like dream, deliberately **not** `Write` — ctui creates the
page, so the model can rewrite parts of it but cannot replace it wholesale.

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
    ├── index.json                           # root directory -> task ids (a cache)
    ├── access.json                          # what was opened when, newest first
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
    { "session_id": "f93a9f07-0e81-4fcb-be12-ce13ed6ee6d1", "created_at": "..." },
  { "session_id": "2b7c1d90-55aa-4c31-9f0e-7d2b6e4a8c11", "created_at": "...",
    "forked_from": "f93a9f07-0e81-4fcb-be12-ce13ed6ee6d1" }
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

### The access index

`access.json` sits beside it: one row per task ctui has opened, ordered most recently
accessed first, with the first and last access times and a count.

```json
{
  "version": 1,
  "hostname": "furiosa.stanford.edu",
  "entries": [
    {"host": "furiosa.stanford.edu", "task_id": "TASK_20260902_004505",
     "first_at": "2026-09-02T00:45:05-07:00",
     "last_at": "2026-09-02T09:12:44-07:00", "count": 3}
  ]
}
```

This exists solely for `ctui --resume --recent`, which needs an ordering the task
directories cannot give: `TASK_<datetime>` is creation time, not last use. `--init`,
`--launch` and `--resume` all record; re-opening bumps a row in place rather than
appending, so the file is bounded by task count.

`--dream` deliberately reads none of it. An earlier version tracked per-session rows
with open/closed state so that dream could pick candidates without touching the
filesystem, and measurement showed it saved stat calls but **no transcript parses at
all** — the transcripts' own timestamps were already doing the filtering. It was
deleted; dream consults the transcripts directly.

Like the root index it is written only by the host doing the accessing, so two
machines cannot conflict on it, and rows name their own host so a task resumed across
hosts via `--global` still resolves.

Session IDs are generated by `ctui` and handed to claude via `--session-id`, so a
session is recorded in `task.json` *before* it starts rather than scraped afterwards.
`ctui --resume` replays them with `claude --resume <id>`.

## The wiki

```
ctui-wiki/
├── dated/
│   └── furiosa.stanford.edu/
│       ├── 2026-09-01.md
│       └── 2026-09-02.md
└── topics/
    ├── libraries-and-tools.md
    ├── commands-and-workflows.md
    ├── metrics-and-evaluation.md
    ├── codebase-notes.md
    ├── practices-and-conventions.md
    └── steering-and-preferences.md
```

The dated pages are the record of what was learned *when*; the topic pages are what
is worth keeping. `--dream` writes the first nightly, `--weave` reduces them into the
second weekly.

One page per day **per host**, written by `ctui --dream`. A machine only ever writes
its own directory, using only the sessions of its own tasks, so two hosts distilling
the same night cannot conflict on `ctui --sync`. The hostname sits under `dated/`
rather than above it, leaving other wiki sections at the top level. Each has fixed headings — Libraries &
tools, Commands & workflows, Metrics & evaluation, Codebase notes, Practices &
conventions, Steering & preferences — so pages stay comparable and each kind of
learning has an obvious home. A section with nothing for it is left empty rather than
padded. The page carries its own scope note, since it is read by people and by later
dream passes and both need to know that results and measurements are deliberately
absent.

There is one topic page per section, shared across hosts, written by `ctui --weave`.
The sections are the topic axis because they are the one thing every dated page has
in common: dream may never add, rename or remove a `## ` heading, so
"Commands & workflows" means the same thing on every page ever written. (The `Tags:`
line is free-form and varies day to day, so it cannot key a stable set of files.)

Unlike a dated page, a topic page is **merged rather than appended to**, and its
`## ` sub-headings belong to the model: it groups recurring themes under them and
renames or merges them as the material shifts. Only `## Weeks folded in` is ctui's.

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
| `CTUI_CLAUDE_PROJECTS` | Override where claude stores transcripts (default `~/.claude/projects`). |
| `CTUI_CACHE` | Override the cache/log location (default `~/.cache/ctui`). |
| `CTUI_CRONTAB_BIN` | Full path to `crontab`. |

## Development

```sh
pixi run -e dev test
```
