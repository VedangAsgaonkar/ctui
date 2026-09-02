"""The tasks repo: layout, task.json, and lookup.

Layout:

    <tasks_repo>/
        <hostname>/
            TASK_<YYYYmmdd_HHMMSS>/
                root -> /absolute/path/to/task/root   (symlink)
                task.json
"""

from __future__ import annotations

import functools
import json
import os
import platform
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TASK_PREFIX = "TASK_"
TASK_STAMP_FMT = "%Y%m%d_%H%M%S"
ROOT_LINK = "root"
TASK_JSON = "task.json"
INDEX_JSON = "index.json"
INDEX_VERSION = 1
ACCESS_JSON = "access.json"
ACCESS_VERSION = 1
LEGACY_RECENT_JSON = "recent.json"



class TaskError(Exception):
    pass


@functools.lru_cache(maxsize=1)
def _detect_hostname() -> str:
    """This machine's fully-qualified hostname.

    Prefer gethostname() when it is already qualified: it needs no name
    resolution, so the answer stays stable even with DNS down — which matters
    because the hostname keys a directory in the tasks repo, and a machine that
    silently changed its answer would stop finding its own tasks. Only fall back
    to getfqdn() (which may hit DNS) when gethostname() is unqualified, and
    ignore the localhost placeholders it returns when resolution is unhelpful.
    """
    name = socket.gethostname() or platform.node() or ""
    if "." not in name:
        fqdn = socket.getfqdn(name) if name else socket.getfqdn()
        if "." in fqdn and not fqdn.split(".")[0].startswith("localhost"):
            name = fqdn
    name = name.strip().strip("/")
    return name or "unknown-host"


def hostname() -> str:
    """Fully-qualified hostname, overridable with CTUI_HOSTNAME."""
    override = os.environ.get("CTUI_HOSTNAME")
    if override:
        return override
    return _detect_hostname()


def _now() -> datetime:
    return datetime.now().astimezone()


def new_task_id(when: datetime | None = None) -> str:
    return TASK_PREFIX + (when or _now()).strftime(TASK_STAMP_FMT)


@dataclass
class Session:
    session_id: str
    created_at: str
    label: str | None = None

    def to_dict(self) -> dict:
        d = {"session_id": self.session_id, "created_at": self.created_at}
        if self.label:
            d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=str(d.get("session_id", "")),
            created_at=str(d.get("created_at", "")),
            label=d.get("label") or None,
        )

    @property
    def created_display(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return self.created_at or "unknown"


@dataclass
class Task:
    """A task directory plus its parsed task.json."""

    dir: Path
    name: str
    task_id: str
    host: str
    root: Path
    created_at: str
    sessions: list[Session] = field(default_factory=list)

    # ---- persistence -------------------------------------------------

    @classmethod
    def load(cls, task_dir: Path) -> "Task":
        meta_path = task_dir / TASK_JSON
        if not meta_path.exists():
            raise TaskError(f"{task_dir} has no {TASK_JSON}")
        try:
            raw = json.loads(meta_path.read_text())
        except json.JSONDecodeError as exc:
            raise TaskError(f"{meta_path} is not valid JSON: {exc}") from exc

        root = raw.get("root")
        if not root:
            # Fall back to the symlink if task.json predates/loses the field.
            link = task_dir / ROOT_LINK
            root = os.readlink(link) if link.is_symlink() else ""

        return cls(
            dir=task_dir,
            name=str(raw.get("name") or task_dir.name),
            task_id=str(raw.get("id") or task_dir.name),
            host=str(raw.get("hostname") or task_dir.parent.name),
            root=Path(root),
            created_at=str(raw.get("created_at") or ""),
            sessions=[Session.from_dict(s) for s in raw.get("sessions", []) if isinstance(s, dict)],
        )

    def save(self) -> None:
        payload = {
            "name": self.name,
            "id": self.task_id,
            "hostname": self.host,
            "root": str(self.root),
            "created_at": self.created_at,
            "sessions": [s.to_dict() for s in self.sessions],
        }
        (self.dir / TASK_JSON).write_text(json.dumps(payload, indent=2) + "\n")

    # ---- behaviour ---------------------------------------------------

    @property
    def root_exists(self) -> bool:
        return self.root.is_dir()

    @property
    def created_display(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            stamp = self.task_id.removeprefix(TASK_PREFIX)
            try:
                return datetime.strptime(stamp, TASK_STAMP_FMT).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return "unknown"

    def add_session(self, session_id: str, label: str | None = None) -> Session:
        session = Session(
            session_id=session_id,
            created_at=_now().isoformat(timespec="seconds"),
            label=label,
        )
        self.sessions.append(session)
        self.save()
        return session


def host_dir(tasks_repo: Path, host: str | None = None) -> Path:
    return tasks_repo / (host or hostname())


def create_task(tasks_repo: Path, root: Path, name: str, host: str | None = None) -> Task:
    """Create a new TASK_<stamp> directory for `root`.

    If a task with this second-resolution timestamp already exists, a numeric
    suffix is appended so two rapid --init calls cannot collide.
    """
    root = root.resolve()
    if not root.is_dir():
        raise TaskError(f"Task root {root} is not a directory.")

    host = host or hostname()
    parent = host_dir(tasks_repo, host)
    parent.mkdir(parents=True, exist_ok=True)

    created = _now()
    base = new_task_id(created)
    task_id, suffix = base, 1
    while (parent / task_id).exists():
        suffix += 1
        task_id = f"{base}_{suffix}"

    task_dir = parent / task_id
    task_dir.mkdir()
    (task_dir / ROOT_LINK).symlink_to(root, target_is_directory=True)

    task = Task(
        dir=task_dir,
        name=name,
        task_id=task_id,
        host=host,
        root=root,
        created_at=created.isoformat(timespec="seconds"),
        sessions=[],
    )
    task.save()
    add_to_index(tasks_repo, task)
    return task


def iter_task_dirs(tasks_repo: Path, host: str | None = None) -> list[Path]:
    """All task directories, newest first. `host` restricts to one hostname."""
    if not tasks_repo.is_dir():
        return []
    hosts = [host_dir(tasks_repo, host)] if host else [
        d for d in sorted(tasks_repo.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]
    found: list[Path] = []
    for h in hosts:
        if not h.is_dir():
            continue
        found += [d for d in h.iterdir() if d.is_dir() and d.name.startswith(TASK_PREFIX)]
    return sorted(found, key=lambda d: (d.name, d.parent.name), reverse=True)


def load_tasks(tasks_repo: Path, host: str | None = None) -> list[Task]:
    """Load every task, skipping any whose task.json is missing or corrupt."""
    tasks: list[Task] = []
    for d in iter_task_dirs(tasks_repo, host):
        try:
            tasks.append(Task.load(d))
        except TaskError:
            continue
    return tasks


# =====================================================================
# root -> task index
#
# Resolving "is there already a task for this directory?" by opening every
# task.json on the host is O(tasks) file reads for a question that is usually
# answered "no". Each host keeps an index next to its task directories mapping
# root directory -> task ids, so a lookup opens only the task.json files that
# actually match (usually none).
#
# The index is a cache, never the source of truth: task.json still owns the
# root path. It is stored per hostname rather than as one shared file because
# the lookups are host-scoped anyway, and a single global file would be rewritten
# by every machine and conflict on `ctui --sync`. A host only ever writes its own.
# =====================================================================


@dataclass
class RootIndex:
    """Maps absolute root directories to the task ids rooted there."""

    host: str
    roots: dict[str, list[str]] = field(default_factory=dict)
    # task id -> task.json mtime_ns when indexed; this is the staleness key.
    entries: dict[str, int] = field(default_factory=dict)

    def task_ids_for(self, root: Path) -> list[str]:
        return self.roots.get(_key(root), [])

    def add(self, task_id: str, root: Path, mtime_ns: int) -> None:
        ids = self.roots.setdefault(_key(root), [])
        if task_id not in ids:
            ids.append(task_id)
        self.entries[task_id] = mtime_ns

    def to_dict(self) -> dict:
        return {
            "version": INDEX_VERSION,
            "hostname": self.host,
            "roots": {k: sorted(v) for k, v in sorted(self.roots.items())},
            "entries": dict(sorted(self.entries.items())),
        }


def _key(root: Path) -> str:
    """Index key for a root directory: resolved, so the same directory reached
    through different symlinks collapses to one entry."""
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def index_path(tasks_repo: Path, host: str | None = None) -> Path:
    return host_dir(tasks_repo, host) / INDEX_JSON


def _mtime_ns(task_json: Path) -> int:
    try:
        return task_json.stat().st_mtime_ns
    except OSError:
        return 0


def _scan_entries(tasks_repo: Path, host: str) -> dict[str, int]:
    """task id -> task.json mtime for a host: one listing plus a stat each.

    Stat'ing is far cheaper than opening and parsing every task.json, and unlike
    a plain directory count it also notices a task.json whose *contents* changed
    — a hand edit, or a `git pull` rewriting it.
    """
    parent = host_dir(tasks_repo, host)
    if not parent.is_dir():
        return {}
    return {
        e.name: _mtime_ns(Path(e.path) / TASK_JSON)
        for e in os.scandir(parent)
        if e.is_dir() and e.name.startswith(TASK_PREFIX)
    }


def build_index(tasks_repo: Path, host: str | None = None) -> RootIndex:
    """Rebuild a host's index from its task.json files and save it."""
    host = host or hostname()
    index = RootIndex(host=host)
    for task_id, mtime in _scan_entries(tasks_repo, host).items():
        # Record every directory seen, even an unreadable one, so the staleness
        # check below cannot flap between rebuilds.
        index.entries[task_id] = mtime
        try:
            task = Task.load(host_dir(tasks_repo, host) / task_id)
        except TaskError:
            continue
        index.add(task_id, task.root, mtime)
    save_index(tasks_repo, index)
    return index


def save_index(tasks_repo: Path, index: RootIndex) -> None:
    parent = host_dir(tasks_repo, index.host)
    parent.mkdir(parents=True, exist_ok=True)
    (parent / INDEX_JSON).write_text(json.dumps(index.to_dict(), indent=2) + "\n")


def read_index(tasks_repo: Path, host: str | None = None) -> RootIndex | None:
    """Read a host's index without validating it against the filesystem."""
    host = host or hostname()
    path = index_path(tasks_repo, host)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
        return None
    roots = raw.get("roots")
    if not isinstance(roots, dict):
        return None
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return None
    try:
        parsed = {str(k): int(v) for k, v in entries.items()}
    except (TypeError, ValueError):
        return None
    return RootIndex(
        host=host,
        roots={str(k): [str(i) for i in v] for k, v in roots.items() if isinstance(v, list)},
        entries=parsed,
    )


def get_index(tasks_repo: Path, host: str | None = None) -> RootIndex:
    """A host's index, rebuilt if it is missing or out of step with the repo.

    The check compares each task.json's mtime against the mtime the index
    recorded for it: one directory listing plus a stat per task, versus the
    O(tasks) open-and-parse a rebuild costs. That catches tasks added, removed,
    edited, or arrived by `git pull` since the index was written.
    """
    host = host or hostname()
    index = read_index(tasks_repo, host)
    if index is None:
        return build_index(tasks_repo, host)

    scanned = _scan_entries(tasks_repo, host)
    if index.entries != scanned:
        return build_index(tasks_repo, host)

    # "Racily clean" entries, in the sense git uses for its own index: kernel
    # timestamps advance on a coarse tick, so a task.json written in the same
    # tick as the index is indistinguishable by mtime from one written just
    # before it. An external edit can therefore hide behind an unchanged mtime.
    # Anything not strictly older than the index is re-read rather than trusted;
    # normally that is nothing, and at most the task just created.
    horizon = _mtime_ns(index_path(tasks_repo, host))
    racy = [task_id for task_id, mtime in scanned.items() if mtime >= horizon]
    if racy:
        index = _refresh(tasks_repo, host, index, racy)
    return index


def _refresh(tasks_repo: Path, host: str, index: RootIndex,
             task_ids: list[str]) -> RootIndex:
    """Re-read `task_ids` from disk and rewrite the index.

    Saving also moves the index's own mtime past these task.json files, so they
    stop being racy and the next lookup is a plain cache hit.
    """
    stale = set(task_ids)
    index.roots = {
        root: kept
        for root, ids in index.roots.items()
        if (kept := [i for i in ids if i not in stale])
    }
    for task_id in stale:
        try:
            task = Task.load(host_dir(tasks_repo, host) / task_id)
        except TaskError:
            continue
        index.add(task_id, task.root, index.entries.get(task_id, 0))
    save_index(tasks_repo, index)
    return index


def add_to_index(tasks_repo: Path, task: Task) -> None:
    """Record a newly created task. O(1) when an index already exists.

    Deliberately skips get_index's staleness check: create_task has just made a
    directory the on-disk set knows about and the index does not, which would
    force a full rebuild on every single task creation.
    """
    index = read_index(tasks_repo, task.host)
    if index is None:
        # No usable index yet: a full build also picks up pre-existing tasks.
        build_index(tasks_repo, task.host)
        return
    index.add(task.task_id, task.root, _mtime_ns(task.dir / TASK_JSON))
    save_index(tasks_repo, index)


# =====================================================================
# index-backed lookups
# =====================================================================

def _tasks_at(tasks_repo: Path, host: str, index: RootIndex,
              candidate: Path) -> tuple[list[Task], bool]:
    """Tasks rooted exactly at `candidate`, plus whether the index looks stale.

    Stale means the index claimed task ids for this path but none of them
    verified against their own task.json — the authority on where a task is
    rooted stays task.json, never the cache.
    """
    ids = index.task_ids_for(candidate)
    if not ids:
        return [], False
    tasks = []
    for task_id in ids:
        try:
            task = Task.load(host_dir(tasks_repo, host) / task_id)
        except TaskError:
            continue
        if _same_path(task.root, candidate):
            tasks.append(task)
    return tasks, not tasks


def _lookup(tasks_repo: Path, host: str, candidates: list[Path]) -> list[Task]:
    """Tasks at the first of `candidates` that has any."""
    index = get_index(tasks_repo, host)
    for candidate in candidates:
        tasks, stale = _tasks_at(tasks_repo, host, index, candidate)
        if tasks:
            return tasks
        if stale:
            # Rebuild once from task.json and retry this candidate.
            index = build_index(tasks_repo, host)
            tasks, _ = _tasks_at(tasks_repo, host, index, candidate)
            if tasks:
                return tasks
    return []


def tasks_for_root(tasks_repo: Path, root: Path, host: str | None = None) -> list[Task]:
    """Tasks on this host whose root is exactly `root`."""
    return _lookup(tasks_repo, host or hostname(), [root])


def find_tasks_from_cwd(tasks_repo: Path, cwd: Path, host: str | None = None) -> list[Task]:
    """Tasks for the nearest enclosing task root, searching cwd then upwards.

    Returns every task at that nearest root (a directory can host several tasks
    over time); empty if no ancestor of `cwd` is a task root.
    """
    cwd = cwd.resolve()
    return _lookup(tasks_repo, host or hostname(), [cwd, *cwd.parents])


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


# =====================================================================
# access index
#
# One row per (task, session) ctui has opened, ordered most recently accessed
# first, carrying the first and last access times and a count.
#
# Two callers, one structure:
#   * `--resume --recent` wants tasks by recency, which is this list deduped
#     by task.
#   * `dream` wants the sessions that could have been active on a given day.
#     Without this it had to stat and parse every transcript on the host, at a
#     cost that grew with total sessions rather than with the day.
#
# Bounded by session count rather than by an event log, so it stays small and
# its git history stays readable. Written only by the host doing the accessing,
# like the root index, so machines cannot conflict on it.
# =====================================================================


@dataclass
class Access:
    """When a task was opened. Ordering only — `dream` does not read this."""

    host: str
    task_id: str
    first_at: str
    last_at: str
    count: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return (self.host, self.task_id)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "task_id": self.task_id,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Access | None":
        if not (raw.get("host") and raw.get("task_id")):
            return None
        # `at` is the pre-index recent.json field.
        last = str(raw.get("last_at") or raw.get("at") or "")
        if not last:
            return None
        try:
            count = int(raw.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        return cls(
            host=str(raw["host"]),
            task_id=str(raw["task_id"]),
            first_at=str(raw.get("first_at") or last),
            last_at=last,
            count=count,
        )


def access_path(tasks_repo: Path, host: str | None = None) -> Path:
    return host_dir(tasks_repo, host) / ACCESS_JSON


def read_access(tasks_repo: Path, host: str | None = None) -> list[Access]:
    """The access index, most recently accessed first."""
    host = host or hostname()
    path = access_path(tasks_repo, host)
    if not path.exists():
        return _import_legacy_recent(tasks_repo, host)
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != ACCESS_VERSION:
        return []
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return []
    parsed = [Access.from_dict(e) for e in entries if isinstance(e, dict)]
    return [e for e in parsed if e is not None]


def _import_legacy_recent(tasks_repo: Path, host: str) -> list[Access]:
    """Carry over the pre-index recent.json, which had no session ids.

    Enough to keep `--resume --recent` working across the upgrade; those rows
    contribute nothing to dream, which needs a session id.
    """
    legacy = host_dir(tasks_repo, host) / LEGACY_RECENT_JSON
    if not legacy.exists():
        return []
    try:
        raw = json.loads(legacy.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, dict):
        return []
    out = []
    for entry in raw.get("entries", []):
        if isinstance(entry, dict):
            parsed = Access.from_dict(entry)
            if parsed is not None:
                out.append(parsed)
    return out


def save_access(tasks_repo: Path, entries: list[Access], host: str | None = None) -> None:
    host = host or hostname()
    parent = host_dir(tasks_repo, host)
    parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": ACCESS_VERSION,
        "hostname": host,
        "entries": [e.to_dict() for e in entries],
    }
    (parent / ACCESS_JSON).write_text(json.dumps(payload, indent=2) + "\n")


def record_access(tasks_repo: Path, task: Task, host: str | None = None) -> None:
    """Note that `task` was just opened, moving it to the front of the index."""
    host = host or hostname()
    now = _now().isoformat(timespec="seconds")
    key = (task.host, task.task_id)

    kept, found = [], None
    for entry in read_access(tasks_repo, host):
        if entry.key == key:
            found = entry
        else:
            kept.append(entry)

    if found is None:
        found = Access(host=task.host, task_id=task.task_id,
                       first_at=now, last_at=now)
    else:
        found.last_at = now
        found.count += 1

    save_access(tasks_repo, [found, *kept], host)


def recent_tasks(tasks_repo: Path, limit: int = 5, host: str | None = None) -> list[Task]:
    """The `limit` most recently accessed tasks, most recent first.

    The index is per session, so it is deduped by task here. Entries whose
    task directory has gone are skipped rather than occupying a slot.
    """
    found: list[Task] = []
    seen: set[tuple[str, str]] = set()
    for entry in read_access(tasks_repo, host):
        if len(found) >= limit:
            break
        ident = (entry.host, entry.task_id)
        if ident in seen:
            continue
        seen.add(ident)
        try:
            found.append(Task.load(host_dir(tasks_repo, entry.host) / entry.task_id))
        except TaskError:
            continue
    return found


def new_session_id() -> str:
    """A UUID for `claude --session-id`, so ctui knows the ID before launch."""
    return str(uuid.uuid4())
