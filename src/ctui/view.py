from __future__ import annotations

import html
import os
import posixpath
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import render, transcripts
from .tasks import (
    TASK_PREFIX,
    Task,
    TaskError,
    host_dir,
    hostname,
    load_tasks,
    recent_tasks,
)

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RECENT_ON_INDEX = 5

CSP = "default-src 'self'; img-src 'self' data:; frame-src 'self'; base-uri 'none'"

HTML_TYPE = "text/html; charset=utf-8"


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = HTML_TYPE
    headers: dict[str, str] = field(default_factory=dict)


class ViewError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _url(*parts: str) -> str:
    return "/" + "/".join(quote(p, safe="") for p in parts if p != "")


def task_url(task: Task) -> str:
    return _url("task", task.host, task.task_id)


def _rel_url(prefix: str, task: Task, rel: str) -> str:
    base = _url(prefix, task.host, task.task_id)
    if not rel:
        return base
    return base + "/" + quote(rel, safe="/")


def file_url(task: Task, rel: str = "") -> str:
    return _rel_url("file", task, rel)


def raw_url(task: Task, rel: str = "") -> str:
    return _rel_url("raw", task, rel)


# ---- page chrome -----------------------------------------------------

def page(title: str, crumbs: str, body: str) -> bytes:
    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header>
<nav class="crumbs">{crumbs}</nav>
</header>
<main>
{body}
</main>
<script src="/app.js"></script>
</body>
</html>
"""
    return document.encode("utf-8")


def crumbs(*items: tuple[str, str | None]) -> str:
    parts = []
    for label, href in items:
        if href:
            parts.append(f'<a href="{_e(href)}">{_e(label)}</a>')
        else:
            parts.append(f"<span>{_e(label)}</span>")
    return '<span class="sep">/</span>'.join(parts)


def html_response(title: str, crumb_html: str, body: str, status: int = 200) -> Response:
    return Response(
        status=status,
        body=page(title, crumb_html, body),
        headers={"Content-Security-Policy": CSP},
    )


# ---- task lookup -----------------------------------------------------

def _load_task(config, host: str, task_id: str) -> Task:
    if not task_id.startswith(TASK_PREFIX):
        raise ViewError(404, f"{task_id} is not a task id")
    if any(c in host or c in task_id for c in ("/", "\\")) or ".." in (host, task_id):
        raise ViewError(403, "bad path")
    try:
        return Task.load(host_dir(config.tasks_repo, host) / task_id)
    except TaskError as exc:
        raise ViewError(404, str(exc)) from exc


def resolve_artifact(task: Task, rel_parts: list[str]) -> Path:
    if any(part in ("..", "") for part in rel_parts):
        raise ViewError(403, "bad path")
    base = task.dir.resolve()
    target = task.dir.joinpath(*rel_parts) if rel_parts else task.dir
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ViewError(404, str(exc)) from exc
    if resolved != base and not resolved.is_relative_to(base):
        raise ViewError(403, "outside the task directory")
    if not resolved.exists():
        raise ViewError(404, f"no such artifact: {'/'.join(rel_parts)}")
    return resolved


# ---- listings --------------------------------------------------------

def _entry_rows(task: Task, rel: str, directory: Path) -> str:
    base = task.dir.resolve()
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        return render.notice(f"could not list this directory: {exc}", "error")
    if not entries:
        return render.notice("this directory is empty")

    entries.sort(key=lambda e: (not _is_dir(e), e.name.lower()))
    rows = []
    for entry in entries:
        path = Path(entry.path)
        child = f"{rel}/{entry.name}" if rel else entry.name
        size, modified = _entry_stat(entry)
        outside = _escapes(path, base)

        if entry.is_symlink() and outside:
            try:
                target = os.readlink(path)
            except OSError:
                target = "?"
            rows.append(
                f'<tr data-filter="{_e(entry.name.lower())}">'
                f'<td class="icon">↗</td>'
                f'<td><span class="muted">{_e(entry.name)}</span>'
                f'<span class="linktarget"> → {_e(target)}</span></td>'
                f'<td class="right muted">symlink</td>'
                f'<td class="right muted">{_e(modified)}</td></tr>'
            )
            continue

        if not path.exists():
            rows.append(
                f'<tr data-filter="{_e(entry.name.lower())}">'
                f'<td class="icon">⚠</td>'
                f'<td><span class="muted">{_e(entry.name)}</span></td>'
                f'<td class="right muted">broken link</td>'
                f'<td class="right muted"></td></tr>'
            )
            continue

        kind = render.classify(path)
        icon = render.KIND_ICONS.get(kind, "·")
        name = entry.name + ("/" if kind == "dir" else "")
        rows.append(
            f'<tr data-filter="{_e(entry.name.lower())}">'
            f'<td class="icon" title="{_e(kind)}">{icon}</td>'
            f'<td><a href="{_e(file_url(task, child))}">{_e(name)}</a></td>'
            f'<td class="right muted">{_e(size)}</td>'
            f'<td class="right muted">{_e(modified)}</td></tr>'
        )

    return (
        '<div class="tablewrap"><table class="listing">'
        "<thead><tr><th></th><th>name</th><th class=\"right\">size</th>"
        "<th class=\"right\">modified</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _escapes(path: Path, base: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    return resolved != base and not resolved.is_relative_to(base)


def _entry_stat(entry: os.DirEntry) -> tuple[str, str]:
    try:
        info = entry.stat(follow_symlinks=not entry.is_symlink())
    except OSError:
        return "", ""
    size = "" if _is_dir(entry) else render.human_size(info.st_size)
    return size, render.human_time(info.st_mtime)


# ---- pages -----------------------------------------------------------

def _task_row(task: Task, this_host: str) -> str:
    flags = []
    if task.host != this_host:
        flags.append(_e(task.host))
    if not task.root_exists:
        flags.append('<span class="warnflag">root missing</span>')
    note = f' <span class="muted">{" · ".join(flags)}</span>' if flags else ""
    haystack = f"{task.task_id} {task.name} {task.host} {task.root}".lower()
    return (
        f'<tr data-filter="{_e(haystack)}">'
        f'<td><a href="{_e(task_url(task))}">{_e(task.task_id)}</a></td>'
        f'<td>{_e(task.name)}{note}</td>'
        f'<td class="right">{len(task.sessions)}</td>'
        f'<td class="muted path">{_e(task.root)}</td>'
        f'<td class="right muted">{_e(task.created_display)}</td>'
        "</tr>"
    )


def index_page(config) -> Response:
    tasks = load_tasks(config.tasks_repo)
    this_host = hostname()

    if not tasks:
        body = (
            f"<h1>ctui</h1>"
            + render.notice(f"No tasks in {config.tasks_repo}.")
            + "<p>Run <code>ctui --init</code> in a project directory to create one.</p>"
        )
        return html_response("ctui", crumbs(("ctui", None)), body)

    recent = recent_tasks(config.tasks_repo, RECENT_ON_INDEX)
    recent_html = ""
    if recent:
        chips = "".join(
            f'<a class="chip" href="{_e(task_url(t))}">'
            f'<strong>{_e(t.name)}</strong><span>{_e(t.task_id)}</span></a>'
            for t in recent
        )
        recent_html = f"<h2>Recently opened</h2><div class=\"chips\">{chips}</div>"

    rows = "".join(_task_row(t, this_host) for t in tasks)
    body = f"""<h1>ctui</h1>
<p class="meta">{len(tasks)} task(s) in <code>{_e(config.tasks_repo)}</code> · this host is <code>{_e(this_host)}</code></p>
{recent_html}
<h2>All tasks</h2>
<input id="filter" type="search" placeholder="filter by task id, name, host or root  (press /)" autocomplete="off">
<p class="meta"><span id="count"></span></p>
<div class="tablewrap"><table>
<thead><tr><th>task</th><th>name</th><th class="right">sessions</th><th>root</th><th class="right">created</th></tr></thead>
<tbody>{rows}</tbody>
</table></div>
"""
    return html_response("ctui", crumbs(("ctui", None)), body)


def _session_rows(task: Task) -> str:
    if not task.sessions:
        return render.notice("no sessions have been launched in this task yet")
    rows = []
    for session in reversed(task.sessions):
        path = transcripts.transcript_path(task.root, session.session_id)
        try:
            info = path.stat()
            transcript = render.human_size(info.st_size)
        except OSError:
            transcript = "—"
        label = _e(session.label) if session.label else ""
        if session.forked_from:
            origin = (f'<span class="forkmark">↳</span> '
                      f'<code>{_e(session.forked_from[:8])}</code>')
        else:
            origin = '<span class="muted">—</span>'
        rows.append(
            f"<tr><td><code>{_e(session.session_id[:8])}</code></td>"
            f'<td class="muted">{_e(session.created_display)}</td>'
            f"<td>{label}</td>"
            f"<td>{origin}</td>"
            f'<td class="right muted">{_e(transcript)}</td>'
            f'<td class="muted path">{_e(session.session_id)}</td></tr>'
        )
    return (
        '<div class="tablewrap"><table>'
        "<thead><tr><th>session</th><th>created</th><th>label</th>"
        '<th>forked from</th><th class="right">transcript</th>'
        "<th>full id</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def task_page(config, host: str, task_id: str) -> Response:
    task = _load_task(config, host, task_id)
    root_state = "" if task.root_exists else ' <span class="warnflag">missing</span>'
    meta = f"""<div class="tablewrap"><table class="kv">
<tbody>
<tr><th>name</th><td>{_e(task.name)}</td></tr>
<tr><th>task id</th><td><code>{_e(task.task_id)}</code></td></tr>
<tr><th>host</th><td><code>{_e(task.host)}</code></td></tr>
<tr><th>root</th><td><code>{_e(task.root)}</code>{root_state}</td></tr>
<tr><th>task dir</th><td><code>{_e(task.dir)}</code></td></tr>
<tr><th>created</th><td>{_e(task.created_display)}</td></tr>
</tbody></table></div>"""

    body = (
        f"<h1>{_e(task.name)}</h1>"
        f'<p class="meta"><code>{_e(task.task_id)}</code> on <code>{_e(task.host)}</code></p>'
        + meta
        + f"<h2>Sessions <span class=\"count\">{len(task.sessions)}</span></h2>"
        + _session_rows(task)
        + "<h2>Artifacts</h2>"
        + _entry_rows(task, "", task.dir)
    )
    return html_response(
        f"{task.task_id} — {task.name}",
        crumbs(("ctui", "/"), (task.task_id, None)),
        body,
    )


def _file_crumbs(task: Task, rel_parts: list[str]) -> str:
    items: list[tuple[str, str | None]] = [("ctui", "/"), (task.task_id, task_url(task))]
    for depth, part in enumerate(rel_parts):
        href = None if depth == len(rel_parts) - 1 else file_url(task, "/".join(rel_parts[: depth + 1]))
        items.append((part, href))
    return crumbs(*items)


def artifact_resolver(task: Task, rel_parts: list[str]):
    """Map a markdown URL, relative to this artifact, onto a ctui route.

    Images go to /raw (the bytes) and everything else to /file (the viewer).
    Without this the browser resolves `plot.png` on a page served at
    /file/<host>/<task>/results/x.md against /file/..., and gets the viewer
    page for the image rather than the image.
    """
    base = "/".join(rel_parts[:-1])
    root = task.dir.resolve()

    def resolve(url: str, is_image: bool) -> str | None:
        if url.startswith("/"):
            # An absolute filesystem path, usable only if it is in this task.
            try:
                inside = Path(url).resolve().relative_to(root)
            except (ValueError, OSError):
                return None
            target = inside.as_posix()
        else:
            target = posixpath.normpath(posixpath.join(base, url) if base else url)
        if target == ".." or target.startswith("../") or target == ".":
            return None
        return raw_url(task, target) if is_image else file_url(task, target)

    return resolve


def file_page(config, host: str, task_id: str, rel_parts: list[str],
              query: dict[str, list[str]]) -> Response:
    task = _load_task(config, host, task_id)
    target = resolve_artifact(task, rel_parts)
    rel = "/".join(rel_parts)
    title = rel or task.task_id

    if target.is_dir():
        body = (
            f"<h1>{_e(rel or 'Artifacts')}</h1>"
            f'<p class="meta"><code>{_e(target)}</code></p>'
            + _entry_rows(task, rel, target)
        )
        return html_response(title, _file_crumbs(task, rel_parts), body)

    source = query.get("source", ["0"])[0] not in ("0", "", "false")
    raw = raw_url(task, rel)
    kind, rendered = render.render_file(
        target, raw, source=source, resolve=artifact_resolver(task, rel_parts))

    try:
        info = target.stat()
        stats = f"{render.human_size(info.st_size)} · {render.human_time(info.st_mtime)}"
    except OSError:
        stats = ""

    actions = [f'<a class="button" href="{_e(raw)}">raw</a>']
    if kind in render.HAS_SOURCE_VIEW:
        if source:
            actions.insert(0, f'<a class="button" href="{_e(file_url(task, rel))}">rendered</a>')
        else:
            actions.insert(0, f'<a class="button" href="{_e(file_url(task, rel))}?source=1">source</a>')

    body = f"""<h1>{_e(target.name)}</h1>
<p class="meta"><span class="kindtag">{_e(kind)}</span> {_e(stats)}</p>
<p class="actions">{''.join(actions)}</p>
{rendered}
"""
    return html_response(title, _file_crumbs(task, rel_parts), body)


def raw_response(config, host: str, task_id: str, rel_parts: list[str]) -> Response:
    task = _load_task(config, host, task_id)
    target = resolve_artifact(task, rel_parts)
    if target.is_dir():
        raise ViewError(404, "that is a directory")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ViewError(404, str(exc)) from exc

    content_type = render.raw_content_type(target)
    headers = {"X-Content-Type-Options": "nosniff"}
    if not render.servable_inline(content_type):
        content_type = "application/octet-stream"
        headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
    return Response(body=data, content_type=content_type, headers=headers)


def error_page(status: int, message: str) -> Response:
    body = (
        f"<h1>{status}</h1>"
        + render.notice(message, "error")
        + '<p><a class="button" href="/">back to tasks</a></p>'
    )
    return html_response(f"{status}", crumbs(("ctui", "/")), body, status=status)


# ---- routing ---------------------------------------------------------

def handle(config, target: str) -> Response:
    parsed = urlsplit(target)
    try:
        segments = [unquote(s) for s in parsed.path.split("/") if s]
    except UnicodeDecodeError:
        return error_page(400, "undecodable path")
    query = parse_qs(parsed.query)

    try:
        if not segments:
            return index_page(config)
        if segments == ["style.css"]:
            return Response(body=STYLE.encode("utf-8"), content_type="text/css; charset=utf-8")
        if segments == ["app.js"]:
            return Response(body=SCRIPT.encode("utf-8"),
                            content_type="text/javascript; charset=utf-8")
        if segments == ["favicon.ico"]:
            return Response(status=404, body=b"", content_type="text/plain; charset=utf-8")
        if segments[0] == "task" and len(segments) == 3:
            return task_page(config, segments[1], segments[2])
        if segments[0] == "file" and len(segments) >= 3:
            return file_page(config, segments[1], segments[2], segments[3:], query)
        if segments[0] == "raw" and len(segments) >= 4:
            return raw_response(config, segments[1], segments[2], segments[3:])
        return error_page(404, f"no route for /{'/'.join(segments)}")
    except ViewError as exc:
        return error_page(exc.status, exc.message)
    except TaskError as exc:
        return error_page(404, str(exc))
    except Exception as exc:
        traceback.print_exc()
        return error_page(500, f"{type(exc).__name__}: {exc}")


# ---- server ----------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "ctui-view"
    protocol_version = "HTTP/1.1"

    def _respond(self, response: Response, body: bool) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body and response.body:
            self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._respond(handle(self.server.config, self.path), body=True)

    def do_HEAD(self) -> None:
        self._respond(handle(self.server.config, self.path), body=False)

    def log_message(self, fmt: str, *args) -> None:
        return


class ViewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config):
        self.config = config
        super().__init__(address, _Handler)


def make_server(config, port: int, host: str = BIND_HOST) -> ViewServer:
    return ViewServer((host, port), config)


# ---- assets ----------------------------------------------------------

STYLE = """
:root {
  --bg: #fbfbfa; --fg: #1a1a18; --muted: #6b6b66; --line: #e2e1dd;
  --panel: #ffffff; --accent: #0b6fa4; --code-bg: #f4f3f0;
  --warn: #9a5b00; --error: #a8200d; --chip: #f0efec;
  --tk-comment: #767c86; --tk-keyword: #8046a8; --tk-string: #17693f;
  --tk-number: #a8560a; --tk-func: #1a5fb4; --tk-class: #8a5a00;
  --tk-builtin: #0d6b70; --tk-op: #545a63; --tk-meta: #a03e3e;
  --tk-name: #2b3038; --tk-added: #17693f; --tk-removed: #a8200d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e6e5e1; --muted: #93938d; --line: #2c2e33;
    --panel: #1c1d21; --accent: #6fb8e0; --code-bg: #212328;
    --warn: #d8a13a; --error: #e2715e; --chip: #24262b;
    --tk-comment: #8b8f99; --tk-keyword: #c49ae8; --tk-string: #8ad196;
    --tk-number: #e8b177; --tk-func: #82b8f5; --tk-class: #e5c07b;
    --tk-builtin: #6fd0c8; --tk-op: #b6bdc9; --tk-meta: #e8968f;
    --tk-name: #dcdbd6; --tk-added: #8ad196; --tk-removed: #e2715e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
header {
  position: sticky; top: 0; z-index: 5; background: var(--panel);
  border-bottom: 1px solid var(--line); padding: 10px 24px;
}
.crumbs { font-size: 13px; }
.crumbs a { color: var(--accent); text-decoration: none; }
.crumbs a:hover { text-decoration: underline; }
.crumbs .sep { color: var(--muted); margin: 0 8px; }
main { max-width: 1080px; margin: 0 auto; padding: 24px 24px 96px; }
h1 { font-size: 24px; margin: 8px 0 4px; font-weight: 650; letter-spacing: -0.01em; }
h2 { font-size: 16px; margin: 32px 0 10px; font-weight: 650;
     text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
h3 { font-size: 14px; margin: 20px 0 8px; }
a { color: var(--accent); }
code, pre, .path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
code { font-size: 0.9em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
pre code { background: none; padding: 0; }
.meta { color: var(--muted); font-size: 13px; margin: 4px 0 12px; }
.muted { color: var(--muted); }
.count { color: var(--muted); font-weight: 400; }
.right { text-align: right; }
.center { text-align: center; }
.warnflag { color: var(--warn); font-size: 12px; }
.kindtag {
  background: var(--chip); border: 1px solid var(--line); border-radius: 4px;
  padding: 1px 6px; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.04em;
}
.tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
             background: var(--panel); }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
thead th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
           color: var(--muted); font-weight: 600; white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--code-bg); }
td a { text-decoration: none; }
td a:hover { text-decoration: underline; }
.listing .icon, .icon { width: 28px; text-align: center; color: var(--muted); }
.linktarget { color: var(--muted); font-size: 12px; }
.forkmark { color: var(--accent); }
.extimg { font-size: 13px; border: 1px dashed var(--line); border-radius: 6px;
          padding: 3px 8px; display: inline-block; }
table.kv th { width: 130px; color: var(--muted); font-weight: 500; }
.path { font-size: 12px; word-break: break-all; }
#filter {
  width: 100%; padding: 9px 12px; font-size: 14px; color: var(--fg);
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
}
#filter:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: flex; flex-direction: column; gap: 2px; text-decoration: none;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 12px; min-width: 150px;
}
.chip:hover { border-color: var(--accent); }
.chip span { color: var(--muted); font-size: 11.5px;
             font-family: ui-monospace, Menlo, monospace; }
.actions { display: flex; gap: 8px; margin: 12px 0 18px; }
.button {
  display: inline-block; text-decoration: none; font-size: 13px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: 5px 12px; color: var(--accent);
}
.button:hover { border-color: var(--accent); }
.notice {
  border-left: 3px solid var(--line); background: var(--panel);
  padding: 9px 14px; margin: 12px 0; font-size: 13.5px; border-radius: 0 6px 6px 0;
}
.notice.warn { border-left-color: var(--warn); color: var(--warn); }
.notice.error { border-left-color: var(--error); color: var(--error); }
.codewrap {
  position: relative; border: 1px solid var(--line); border-radius: 8px;
  background: var(--code-bg); margin: 12px 0; overflow: hidden;
}
.lang {
  position: absolute; top: 0; right: 0; font-size: 10.5px; color: var(--muted);
  background: var(--panel); border-left: 1px solid var(--line);
  border-bottom: 1px solid var(--line); border-radius: 0 8px 0 6px;
  padding: 2px 8px; text-transform: uppercase; letter-spacing: 0.05em;
}
pre.code {
  margin: 0; padding: 12px 14px; overflow-x: auto; font-size: 12.5px;
  line-height: 1.55; tab-size: 4;
}
.numbered { display: flex; align-items: stretch; }
pre.gutter {
  margin: 0; padding: 12px 10px; text-align: right; font-size: 12.5px;
  line-height: 1.55; color: var(--muted); background: var(--panel);
  border-right: 1px solid var(--line); user-select: none; min-width: 3.2em;
}
.numbered pre.code { flex: 1; min-width: 0; }
.media { margin: 12px 0; }
.media img, .media video { max-width: 100%; height: auto; border-radius: 8px;
                           border: 1px solid var(--line); background: var(--panel); }
.media audio { width: 100%; }
iframe.embed {
  width: 100%; height: 78vh; border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel);
}
details.record { border: 1px solid var(--line); border-radius: 8px;
                 background: var(--panel); margin: 8px 0; }
details.record summary { cursor: pointer; padding: 7px 12px; font-size: 13px;
                         color: var(--muted); }
details.record .codewrap { margin: 0; border: none; border-top: 1px solid var(--line);
                           border-radius: 0; }
.md { font-size: 15px; }
.md h1 { font-size: 22px; margin: 26px 0 8px; }
.md h2 { font-size: 17px; margin: 24px 0 8px; text-transform: none;
         letter-spacing: 0; color: var(--fg); }
.md h3 { font-size: 15px; margin: 20px 0 6px; }
.md h4, .md h5, .md h6 { font-size: 14px; margin: 16px 0 6px; }
.md p { margin: 10px 0; }
.md ul, .md ol { margin: 10px 0; padding-left: 26px; }
.md li { margin: 4px 0; }
.md li > p:first-child { margin-top: 0; }
.md blockquote {
  margin: 12px 0; padding: 2px 16px; border-left: 3px solid var(--line);
  color: var(--muted);
}
.md hr { border: none; border-top: 1px solid var(--line); margin: 22px 0; }
.md img { max-width: 100%; border-radius: 6px; }
.md .tablewrap { margin: 12px 0; }
.notebook .cell { margin: 16px 0; }
.cellno { font-size: 11px; color: var(--muted); margin-bottom: 4px;
          font-family: ui-monospace, Menlo, monospace; }
.md-cell article { border-left: 3px solid var(--line); padding-left: 14px; }
.cellerror .codewrap { border-color: var(--error); }
.cellerror { color: var(--error); font-size: 12px; }

pre.code .tk-c, pre.code .tk-ch, pre.code .tk-cm, pre.code .tk-c1,
pre.code .tk-cs, pre.code .tk-cd { color: var(--tk-comment); font-style: italic; }
pre.code .tk-cp, pre.code .tk-cpf { color: var(--tk-meta); }
pre.code .tk-k, pre.code .tk-kc, pre.code .tk-kd, pre.code .tk-kn,
pre.code .tk-kp, pre.code .tk-kr { color: var(--tk-keyword); }
pre.code .tk-kt { color: var(--tk-class); }
pre.code .tk-s, pre.code .tk-s1, pre.code .tk-s2, pre.code .tk-sa,
pre.code .tk-sb, pre.code .tk-sc, pre.code .tk-dl, pre.code .tk-sd,
pre.code .tk-sh, pre.code .tk-si, pre.code .tk-sx, pre.code .tk-ss,
pre.code .tk-se { color: var(--tk-string); }
pre.code .tk-l, pre.code .tk-ld { color: var(--tk-string); }
pre.code .tk-sr { color: var(--tk-builtin); }
pre.code .tk-m, pre.code .tk-mb, pre.code .tk-mf, pre.code .tk-mh,
pre.code .tk-mi, pre.code .tk-mo, pre.code .tk-il { color: var(--tk-number); }
pre.code .tk-nf, pre.code .tk-fm { color: var(--tk-func); }
pre.code .tk-nc, pre.code .tk-ne, pre.code .tk-nn { color: var(--tk-class); }
pre.code .tk-nb, pre.code .tk-bp, pre.code .tk-no { color: var(--tk-builtin); }
pre.code .tk-nd { color: var(--tk-meta); }
pre.code .tk-nt { color: var(--tk-keyword); }
pre.code .tk-na, pre.code .tk-nv, pre.code .tk-vc, pre.code .tk-vg,
pre.code .tk-vi, pre.code .tk-vm { color: var(--tk-name); }
pre.code .tk-o, pre.code .tk-ow, pre.code .tk-p, pre.code .tk-pi {
  color: var(--tk-op);
}
pre.code .tk-n, pre.code .tk-nx, pre.code .tk-nl, pre.code .tk-ni,
pre.code .tk-py, pre.code .tk-w { color: inherit; }
pre.code .tk-err { color: var(--tk-error, var(--error)); }
pre.code .tk-gd { color: var(--tk-removed); }
pre.code .tk-gi { color: var(--tk-added); }
pre.code .tk-gh, pre.code .tk-gu { color: var(--tk-func); font-weight: 600; }
pre.code .tk-gp { color: var(--tk-comment); }
pre.code .tk-ge { font-style: italic; }
pre.code .tk-gs { font-weight: 600; }
pre.code .tk-gr { color: var(--error); }
"""

SCRIPT = """
(function () {
  var box = document.getElementById('filter');
  if (!box) { return; }
  var rows = Array.prototype.slice.call(document.querySelectorAll('tbody [data-filter]'));
  var count = document.getElementById('count');
  function apply() {
    var needle = box.value.toLowerCase().trim();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var hit = !needle || rows[i].getAttribute('data-filter').indexOf(needle) !== -1;
      rows[i].hidden = !hit;
      if (hit) { shown++; }
    }
    if (count) {
      count.textContent = needle ? shown + ' of ' + rows.length + ' shown' : '';
    }
  }
  box.addEventListener('input', apply);
  document.addEventListener('keydown', function (event) {
    if (event.key === '/' && document.activeElement !== box) {
      event.preventDefault();
      box.focus();
      box.select();
    } else if (event.key === 'Escape' && document.activeElement === box) {
      box.value = '';
      apply();
      box.blur();
    }
  });
  apply();
})();
"""
