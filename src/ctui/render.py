from __future__ import annotations

import csv
import html
import io
import json
import mimetypes
import re
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pygments
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

MAX_RENDER_BYTES = 2 * 1024 * 1024
MAX_TABLE_ROWS = 500
MAX_JSONL_RECORDS = 200
MAX_ARCHIVE_MEMBERS = 500
MAX_CELL_OUTPUT = 20000
HEX_PREVIEW_BYTES = 512
SNIFF_BYTES = 4096

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif", ".svg"}
VIDEO_EXT = {".mp4", ".webm", ".ogv", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac"}
ZIP_EXT = {".zip", ".whl", ".egg"}
TAR_EXT = {".tar", ".tgz", ".tbz2", ".txz"}
TAR_COMPOUND = {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"}
MARKDOWN_EXT = {".md", ".markdown", ".mdown", ".mkd"}
TEXT_EXT = {
    ".txt", ".text", ".log", ".out", ".err", ".rst", ".diff", ".patch",
    ".gitignore", ".gitattributes", ".editorconfig", ".lock",
}

LANGS = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".fish": "fish",
    ".ps1": "powershell", ".bat": "batch",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".ini": "ini",
    ".cfg": "ini", ".conf": "conf", ".properties": "ini",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".cu": "cuda",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".scala": "scala", ".swift": "swift", ".rb": "ruby", ".pl": "perl",
    ".lua": "lua", ".php": "php", ".r": "r", ".jl": "julia", ".m": "matlab",
    ".sql": "sql", ".css": "css", ".scss": "scss", ".less": "less",
    ".xml": "xml", ".tex": "tex", ".bib": "bibtex",
    ".mk": "make", ".cmake": "cmake", ".nix": "nix", ".env": "shell",
    ".slurm": "shell", ".sbatch": "shell", ".graphql": "graphql",
    ".proto": "protobuf", ".rules": "conf", ".service": "ini",
}
NAME_LANGS = {
    "Makefile": "make", "makefile": "make", "GNUmakefile": "make",
    "Dockerfile": "dockerfile", "Containerfile": "dockerfile",
    "CMakeLists.txt": "cmake", "Justfile": "just", "justfile": "just",
    "Vagrantfile": "ruby", "Rakefile": "ruby", "Gemfile": "ruby",
    "PKGBUILD": "shell", ".bashrc": "shell", ".zshrc": "shell",
    "config.fish": "fish",
}

INLINE_TYPES = ("image/", "video/", "audio/")
INLINE_EXACT = {"application/pdf", "text/plain", "text/html"}

RAW_TYPES = {
    ".svg": "image/svg+xml", ".avif": "image/avif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".pdf": "application/pdf",
    ".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".m4v": "video/mp4", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".oga": "audio/ogg",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".html": "text/html", ".htm": "text/html",
}

KIND_ICONS = {
    "dir": "▸", "markdown": "¶", "notebook": "◆", "json": "{}", "jsonl": "{}",
    "csv": "⊞", "tsv": "⊞", "image": "▢", "pdf": "▤", "html": "◰",
    "video": "▶", "audio": "♪", "zip": "▣", "tar": "▣", "code": "‹›",
    "text": "≡", "binary": "⬛", "empty": "∅",
}


def compound_suffix(path: Path) -> str:
    suffixes = [s.lower() for s in path.suffixes]
    if len(suffixes) >= 2:
        return "".join(suffixes[-2:])
    return ""


def language(path: Path) -> str:
    if path.name in NAME_LANGS:
        return NAME_LANGS[path.name]
    return LANGS.get(path.suffix.lower(), "")


def looks_textual(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
        return printable / len(chunk) > 0.85
    return True


def classify(path: Path) -> str:
    if path.is_dir():
        return "dir"
    ext = path.suffix.lower()
    try:
        if path.stat().st_size == 0:
            return "empty"
    except OSError:
        pass
    if ext in MARKDOWN_EXT:
        return "markdown"
    if ext == ".ipynb":
        return "notebook"
    if ext == ".json":
        return "json"
    if ext in {".jsonl", ".ndjson"}:
        return "jsonl"
    if ext == ".csv":
        return "csv"
    if ext in {".tsv", ".tab"}:
        return "tsv"
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in {".html", ".htm"}:
        return "html"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in ZIP_EXT:
        return "zip"
    if ext in TAR_EXT or compound_suffix(path) in TAR_COMPOUND:
        return "tar"
    if language(path):
        return "code"
    if ext in TEXT_EXT or path.name in TEXT_EXT:
        return "text"
    return "text" if looks_textual(path) else "binary"


TEXTUAL_KINDS = {"markdown", "json", "jsonl", "csv", "tsv", "code", "text", "empty"}


def raw_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in RAW_TYPES:
        return RAW_TYPES[ext]
    if classify(path) in TEXTUAL_KINDS:
        return "text/plain; charset=utf-8"
    guessed, encoding = mimetypes.guess_type(path.name)
    if guessed and not encoding:
        return guessed
    return "application/octet-stream"


def servable_inline(content_type: str) -> bool:
    base = content_type.split(";")[0].strip()
    return base in INLINE_EXACT or base.startswith(INLINE_TYPES)


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{size} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size} B"


def human_time(stamp: float) -> str:
    try:
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "unknown"


def read_text(path: Path, limit: int | None = None) -> tuple[str, bool]:
    limit = MAX_RENDER_BYTES if limit is None else limit
    try:
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        return f"[could not read: {exc}]", False
    clipped = len(data) > limit
    if clipped:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), clipped


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


# ---- syntax highlighting ---------------------------------------------

MAX_HIGHLIGHT_BYTES = 512 * 1024

TOKEN_PREFIX = "tk-"

FORMATTER = HtmlFormatter(nowrap=True, classprefix=TOKEN_PREFIX)

LEXER_OPTIONS = {"stripnl": False, "stripall": False, "ensurenl": False}


@dataclass
class Highlighted:
    markup: str
    language: str


def _lexer(lang: str, filename: str | None, text: str):
    if filename:
        try:
            return get_lexer_for_filename(filename, **LEXER_OPTIONS)
        except ClassNotFound:
            pass
    if lang:
        try:
            return get_lexer_by_name(lang, **LEXER_OPTIONS)
        except ClassNotFound:
            pass
    return None


def highlight_code(text: str, lang: str = "",
                   filename: str | None = None) -> Highlighted | None:
    if not text.strip() or len(text) > MAX_HIGHLIGHT_BYTES:
        return None
    lexer = _lexer(lang, filename, text)
    if lexer is None or isinstance(lexer, TextLexer):
        return None
    try:
        markup = pygments.highlight(text, lexer, FORMATTER)
    except Exception:
        return None
    if markup.endswith("\n") and not text.endswith("\n"):
        markup = markup[:-1]
    if markup.count("\n") != text.count("\n"):
        return None
    return Highlighted(markup, lexer.name)


# ---- html fragments --------------------------------------------------

def code_block(text: str, lang: str = "", numbered: bool = False,
               filename: str | None = None) -> str:
    body = text[:-1] if text.endswith("\n") else text
    marked = highlight_code(body, lang, filename)
    rendered = marked.markup if marked else html.escape(body, quote=False)
    shown = lang or (marked.language if marked else "")
    label = f'<div class="lang">{html.escape(shown, quote=False)}</div>' if shown else ""
    if not numbered:
        return f'<div class="codewrap">{label}<pre class="code"><code>{rendered}</code></pre></div>'
    count = len(body.split("\n")) if body else 0
    gutter = "\n".join(str(i) for i in range(1, count + 1))
    return (
        f'<div class="codewrap">{label}<div class="numbered">'
        f'<pre class="gutter" aria-hidden="true">{gutter}</pre>'
        f'<pre class="code"><code>{rendered}</code></pre></div></div>'
    )


def notice(text: str, level: str = "info") -> str:
    return f'<p class="notice {level}">{html.escape(text, quote=False)}</p>'


def table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None,
          escape: bool = True) -> str:
    def cell(value: str, tag: str, index: int) -> str:
        align = ""
        if aligns and index < len(aligns) and aligns[index]:
            align = f' class="{aligns[index]}"'
        body = html.escape(value, quote=False) if escape else value
        return f"<{tag}{align}>{body}</{tag}>"

    head = "".join(cell(h, "th", i) for i, h in enumerate(headers))
    out = [f"<thead><tr>{head}</tr></thead>", "<tbody>"]
    for row in rows:
        cells = "".join(cell(c, "td", i) for i, c in enumerate(row))
        out.append(f"<tr>{cells}</tr>")
    out.append("</tbody>")
    return '<div class="tablewrap"><table>' + "".join(out) + "</table></div>"


# ---- markdown --------------------------------------------------------

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_CODESPAN = re.compile(r"(`+)([^`]|[^`].*?[^`])\1", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_AUTOLINK = re.compile(r"(?<![\"'=(>])\b(https?://[^\s<>\"')\]]+)")
_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.S)
_BOLD_ALT = re.compile(r"(?<![\w_])__(\S(?:.*?\S)?)__(?![\w_])", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*([^\*\n]+)\*(?!\*)")
_ITALIC_ALT = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_STRIKE = re.compile(r"~~(\S(?:.*?\S)?)~~", re.S)
_SLUG_DROP = re.compile(r"[^\w\s-]")
_SAFE_SCHEME = re.compile(r"^(https?:|mailto:|#|/|\./|\.\./|[^:]*$)", re.I)


def slug(text: str) -> str:
    cleaned = _SLUG_DROP.sub("", text.strip().lower())
    return re.sub(r"[\s_]+", "-", cleaned).strip("-") or "section"


def safe_url(url: str) -> str:
    url = url.strip()
    if not url or not _SAFE_SCHEME.match(url):
        return "#"
    return url


def _inline(text: str) -> str:
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(2).strip())
        return f"\x00{len(spans) - 1}\x00"

    text = _CODESPAN.sub(stash, text)
    text = html.escape(text, quote=False)

    def image(match: re.Match) -> str:
        alt = match.group(1)
        src = safe_url(html.unescape(match.group(2)))
        return f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}">'

    def link(match: re.Match) -> str:
        label = match.group(1)
        href = safe_url(html.unescape(match.group(2)))
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    text = _IMAGE.sub(image, text)
    text = _LINK.sub(link, text)
    text = _AUTOLINK.sub(
        lambda m: f'<a href="{html.escape(m.group(1), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _BOLD_ALT.sub(r"<strong>\1</strong>", text)
    text = _STRIKE.sub(r"<del>\1</del>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _ITALIC_ALT.sub(r"<em>\1</em>", text)
    return re.sub(
        r"\x00(\d+)\x00",
        lambda m: f"<code>{html.escape(spans[int(m.group(1))])}</code>",
        text,
    )


def _indent_of(line: str) -> int:
    stripped = line.lstrip()
    return len(line[: len(line) - len(stripped)].expandtabs(4))


def _starts_block(line: str) -> bool:
    return bool(
        _FENCE.match(line)
        or _HEADING.match(line)
        or _HR.match(line)
        or _LIST.match(line)
        or line.lstrip().startswith(">")
        or line.lstrip().startswith("<!--")
    )


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _align(spec: str) -> str:
    spec = spec.strip()
    if spec.startswith(":") and spec.endswith(":"):
        return "center"
    if spec.endswith(":"):
        return "right"
    return ""


def _md_table(lines: list[str], i: int) -> tuple[str | None, int]:
    if "|" not in lines[i] or i + 1 >= len(lines):
        return None, i
    if not _TABLE_SEP.match(lines[i + 1]) or "|" not in lines[i + 1]:
        return None, i
    headers = _cells(lines[i])
    aligns = [_align(c) for c in _cells(lines[i + 1])]
    j = i + 2
    rows = []
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        cells = _cells(lines[j])
        cells += [""] * (len(headers) - len(cells))
        rows.append([_inline(c) for c in cells[: len(headers)]])
        j += 1
    head = [_inline(h) for h in headers]
    return table(head, rows, aligns, escape=False), j


def _md_list(lines: list[str], i: int) -> tuple[str, int]:
    first = _LIST.match(lines[i])
    base = _indent_of(lines[i])
    ordered = first.group(2)[0].isdigit()
    items: list[tuple[str, list[str]]] = []
    total = len(lines)

    while i < total:
        line = lines[i]
        if not line.strip():
            j = i + 1
            while j < total and not lines[j].strip():
                j += 1
            if j >= total:
                break
            nxt = _LIST.match(lines[j])
            if (nxt and _indent_of(lines[j]) >= base) or _indent_of(lines[j]) > base:
                if items:
                    items[-1][1].append("")
                i = j
                continue
            break

        match = _LIST.match(line)
        indent = _indent_of(line)
        if match and indent <= base:
            if indent < base or match.group(2)[0].isdigit() != ordered:
                break
            items.append((match.group(3), []))
            i += 1
            continue
        if items and indent > base:
            items[-1][1].append(line.expandtabs(4))
            i += 1
            continue
        break

    parts = []
    for head, rest in items:
        filled = [line for line in rest if line.strip()]
        if filled:
            pad = min(_indent_of(line) for line in filled)
            body = [line[pad:] if len(line) > pad else "" for line in rest]
            inner = _md_blocks([head, *body])
        else:
            inner = _inline(head.strip())
        parts.append(f"<li>{inner}</li>")
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(parts) + f"</{tag}>", i


def _md_blocks(lines: list[str]) -> str:
    out: list[str] = []
    i, total = 0, len(lines)

    while i < total:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            lang = fence.group(2)
            closer = re.compile(rf"^\s*{re.escape(marker)}{{3,}}\s*$")
            i += 1
            buf = []
            while i < total and not closer.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(code_block("\n".join(buf), lang))
            continue

        if line.lstrip().startswith("<!--"):
            while i < total and "-->" not in lines[i]:
                i += 1
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            raw = heading.group(2).strip().rstrip("#").strip()
            out.append(f'<h{level} id="{slug(raw)}">{_inline(raw)}</h{level}>')
            i += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < total and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{_md_blocks(buf)}</blockquote>")
            continue

        if _LIST.match(line):
            block, i = _md_list(lines, i)
            out.append(block)
            continue

        built, nxt = _md_table(lines, i)
        if built is not None:
            out.append(built)
            i = nxt
            continue

        buf = []
        while i < total and lines[i].strip() and not _starts_block(lines[i]):
            if buf and _md_table(lines, i)[0] is not None:
                break
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append(f"<p>{_inline(' '.join(buf))}</p>")
        else:
            i += 1

    return "".join(out)


def markdown_to_html(text: str) -> str:
    cleaned = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return _md_blocks(cleaned.split("\n"))


# ---- per-kind renderers ----------------------------------------------

def _render_markdown(path: Path, _raw: str) -> str:
    text, clipped = read_text(path)
    body = markdown_to_html(text)
    head = notice(f"clipped at {human_size(MAX_RENDER_BYTES)}", "warn") if clipped else ""
    return head + f'<article class="md">{body}</article>'


def _render_text(path: Path, _raw: str) -> str:
    text, clipped = read_text(path)
    head = notice(f"clipped at {human_size(MAX_RENDER_BYTES)}", "warn") if clipped else ""
    return head + code_block(strip_ansi(text), language(path), numbered=True,
                             filename=path.name)


def _render_json(path: Path, _raw: str) -> str:
    text, clipped = read_text(path)
    if clipped:
        return notice("too large to reformat; showing the raw head", "warn") + \
            code_block(text, "json", numbered=True)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return notice(f"not valid JSON ({exc}); showing it verbatim", "warn") + \
            code_block(text, "json", numbered=True)
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=False)
    return code_block(pretty, "json", numbered=True)


def _render_jsonl(path: Path, _raw: str) -> str:
    out: list[str] = []
    shown = 0
    truncated = False
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if shown >= MAX_JSONL_RECORDS:
                    truncated = True
                    break
                shown += 1
                try:
                    pretty = json.dumps(json.loads(line), indent=2, ensure_ascii=False)
                    lang = "json"
                except json.JSONDecodeError:
                    pretty, lang = line, ""
                out.append(
                    f'<details class="record"><summary>record {shown}</summary>'
                    + code_block(pretty, lang)
                    + "</details>"
                )
    except OSError as exc:
        return notice(f"could not read: {exc}", "error")
    head = notice(f"showing the first {MAX_JSONL_RECORDS} records", "warn") if truncated else ""
    if not out:
        return notice("no records")
    return head + f'<p class="meta">{shown} record(s)</p>' + "".join(out)


def _render_separated(path: Path, delimiter: str) -> str:
    text, clipped = read_text(path)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        return notice(f"could not parse ({exc}); showing it verbatim", "warn") + \
            code_block(text, numbered=True)
    if not rows:
        return notice("no rows")
    headers, body = rows[0], rows[1:]
    truncated = len(body) > MAX_TABLE_ROWS
    width = max(len(headers), max((len(r) for r in body), default=0))
    headers = headers + [""] * (width - len(headers))
    shown = [r + [""] * (width - len(r)) for r in body[:MAX_TABLE_ROWS]]
    head = ""
    if clipped:
        head += notice(f"clipped at {human_size(MAX_RENDER_BYTES)}", "warn")
    if truncated:
        head += notice(f"showing the first {MAX_TABLE_ROWS} of {len(body)} rows", "warn")
    meta = f'<p class="meta">{len(body)} row(s), {width} column(s)</p>'
    return head + meta + table(headers, shown)


def _render_image(path: Path, raw: str) -> str:
    return (
        f'<div class="media"><img src="{html.escape(raw, quote=True)}" '
        f'alt="{html.escape(path.name, quote=True)}"></div>'
    )


def _render_pdf(_path: Path, raw: str) -> str:
    return (
        f'<iframe class="embed" src="{html.escape(raw, quote=True)}" '
        f'title="PDF"></iframe>'
    )


def _render_html(_path: Path, raw: str) -> str:
    return (
        notice("rendered in a sandbox — scripts, forms and network access are disabled")
        + f'<iframe class="embed" sandbox src="{html.escape(raw, quote=True)}" '
        f'title="HTML artifact"></iframe>'
    )


def _render_video(_path: Path, raw: str) -> str:
    return (
        f'<div class="media"><video controls '
        f'src="{html.escape(raw, quote=True)}"></video></div>'
    )


def _render_audio(_path: Path, raw: str) -> str:
    return (
        f'<div class="media"><audio controls '
        f'src="{html.escape(raw, quote=True)}"></audio></div>'
    )


def _render_zip(path: Path, _raw: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()[:MAX_ARCHIVE_MEMBERS]
            rows = [
                [m.filename, human_size(m.file_size), human_size(m.compress_size)]
                for m in members
            ]
            count = len(archive.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        return notice(f"not a readable zip ({exc})", "warn") + _render_binary(path, _raw)
    head = notice(f"showing the first {MAX_ARCHIVE_MEMBERS} members", "warn") \
        if count > MAX_ARCHIVE_MEMBERS else ""
    return head + f'<p class="meta">{count} member(s)</p>' + \
        table(["member", "size", "compressed"], rows)


def _render_tar(path: Path, _raw: str) -> str:
    try:
        with tarfile.open(path) as archive:
            rows = []
            count = 0
            for member in archive:
                count += 1
                if len(rows) < MAX_ARCHIVE_MEMBERS:
                    rows.append([
                        member.name,
                        "dir" if member.isdir() else human_size(member.size),
                        human_time(member.mtime),
                    ])
    except (tarfile.TarError, OSError) as exc:
        return notice(f"not a readable tar ({exc})", "warn") + _render_binary(path, _raw)
    head = notice(f"showing the first {MAX_ARCHIVE_MEMBERS} members", "warn") \
        if count > MAX_ARCHIVE_MEMBERS else ""
    return head + f'<p class="meta">{count} member(s)</p>' + \
        table(["member", "size", "modified"], rows)


def _hexdump(data: bytes) -> str:
    out = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hexed = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{offset:08x}  {hexed:<47}  {text}")
    return "\n".join(out)


def _render_binary(path: Path, raw: str) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            preview = fh.read(HEX_PREVIEW_BYTES)
    except OSError as exc:
        return notice(f"could not read: {exc}", "error")
    return (
        notice(f"binary file, {human_size(size)} — {raw_content_type(path)}")
        + f'<p><a class="button" href="{html.escape(raw, quote=True)}" download>download</a></p>'
        + f'<h3>first {human_size(len(preview))}</h3>'
        + code_block(_hexdump(preview))
    )


def _render_empty(_path: Path, _raw: str) -> str:
    return notice("empty file")


def _notebook_output(output: dict) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        text = "".join(output.get("text") or [])
        return code_block(strip_ansi(text)[:MAX_CELL_OUTPUT])
    if kind == "error":
        text = "\n".join(output.get("traceback") or [])
        name = output.get("ename", "Error")
        return (
            f'<div class="cellerror">{html.escape(str(name))}'
            + code_block(strip_ansi(text)[:MAX_CELL_OUTPUT])
            + "</div>"
        )
    if kind in {"execute_result", "display_data"}:
        data = output.get("data") or {}
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            if data.get(mime):
                payload = data[mime]
                if isinstance(payload, list):
                    payload = "".join(payload)
                payload = payload.replace("\n", "")
                return (
                    f'<div class="media"><img src="data:{mime};base64,'
                    f'{html.escape(payload, quote=True)}" alt="cell output"></div>'
                )
        if data.get("image/svg+xml"):
            return notice("svg output — open the notebook raw to see it")
        if data.get("text/html"):
            return notice("html output, shown as source") + code_block(
                "".join(data["text/html"])[:MAX_CELL_OUTPUT], "html"
            )
        if data.get("text/plain"):
            return code_block("".join(data["text/plain"])[:MAX_CELL_OUTPUT])
    return ""


def _render_notebook(path: Path, _raw: str) -> str:
    text, clipped = read_text(path)
    if clipped:
        return notice("too large to render as a notebook", "warn") + \
            code_block(text, "json", numbered=True)
    try:
        book = json.loads(text)
    except json.JSONDecodeError as exc:
        return notice(f"not valid notebook JSON ({exc})", "warn") + \
            code_block(text, "json", numbered=True)

    meta = book.get("metadata") or {}
    kernel = ((meta.get("kernelspec") or {}).get("display_name")
              or (meta.get("language_info") or {}).get("name") or "")
    lang = (meta.get("language_info") or {}).get("name") or "python"

    cells = book.get("cells")
    if not isinstance(cells, list):
        return notice("no cells in this notebook", "warn")

    out = []
    if kernel:
        out.append(f'<p class="meta">kernel: {html.escape(str(kernel))}</p>')
    for number, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        source = cell.get("source")
        if isinstance(source, list):
            source = "".join(source)
        source = source or ""
        kind = cell.get("cell_type")
        if kind == "markdown":
            out.append(
                f'<section class="cell md-cell"><div class="cellno">{number} · md</div>'
                f'<article class="md">{markdown_to_html(source)}</article></section>'
            )
            continue
        if kind == "raw":
            out.append(
                f'<section class="cell"><div class="cellno">{number} · raw</div>'
                + code_block(source) + "</section>"
            )
            continue
        count = cell.get("execution_count")
        marker = f"[{count}]" if count else "[ ]"
        rendered = [code_block(source, lang, numbered=True)]
        for output in cell.get("outputs") or []:
            if isinstance(output, dict):
                rendered.append(_notebook_output(output))
        out.append(
            f'<section class="cell"><div class="cellno">{number} · '
            f'{html.escape(marker)}</div>' + "".join(rendered) + "</section>"
        )
    return f'<div class="notebook">{"".join(out)}</div>'


RENDERERS = {
    "markdown": _render_markdown,
    "notebook": _render_notebook,
    "json": _render_json,
    "jsonl": _render_jsonl,
    "csv": lambda path, raw: _render_separated(path, ","),
    "tsv": lambda path, raw: _render_separated(path, "\t"),
    "image": _render_image,
    "pdf": _render_pdf,
    "html": _render_html,
    "video": _render_video,
    "audio": _render_audio,
    "zip": _render_zip,
    "tar": _render_tar,
    "code": _render_text,
    "text": _render_text,
    "binary": _render_binary,
    "empty": _render_empty,
}

HAS_SOURCE_VIEW = {"markdown", "notebook", "html", "json"}


def render_file(path: Path, raw_url: str, source: bool = False) -> tuple[str, str]:
    kind = classify(path)
    if source and kind in HAS_SOURCE_VIEW:
        text, clipped = read_text(path)
        head = notice(f"clipped at {human_size(MAX_RENDER_BYTES)}", "warn") if clipped else ""
        lang = {"markdown": "markdown", "notebook": "json",
                "html": "html", "json": "json"}.get(kind, "")
        return kind, head + code_block(text, lang, numbered=True)
    renderer = RENDERERS.get(kind, _render_binary)
    return kind, renderer(path, raw_url)
