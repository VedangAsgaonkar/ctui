import json
import re
import threading
import urllib.error
import urllib.request
from html import unescape

import pytest

from ctui import commands, render, view
from ctui.cli import main
from ctui.tasks import create_task

HOST = "testhost"


@pytest.fixture
def task(config, project):
    return create_task(config.tasks_repo, project, "demo task")


def get(config, path):
    return view.handle(config, path)


def text_of(response):
    return response.body.decode("utf-8")


TAGS = re.compile(r"<[^>]+>")


def visible(markup):
    return unescape(TAGS.sub("", markup))


def file_path(task, rel=""):
    return view.file_url(task, rel)


# ---- index -----------------------------------------------------------

def test_index_lists_tasks(config, task):
    response = get(config, "/")
    assert response.status == 200
    body = text_of(response)
    assert task.task_id in body
    assert "demo task" in body
    assert view.task_url(task) in body


def test_index_with_no_tasks(config):
    response = get(config, "/")
    assert response.status == 200
    assert "No tasks in" in text_of(response)


def test_index_marks_missing_root(config, project, task):
    task.root = project.parent / "gone"
    task.save()
    assert "root missing" in text_of(get(config, "/"))


def test_index_sets_csp_and_no_inline_assets(config, task):
    response = get(config, "/")
    assert "Content-Security-Policy" in response.headers
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    body = text_of(response)
    assert "<style" not in body
    assert "<script>" not in body


def test_static_assets(config):
    css = get(config, "/style.css")
    assert css.status == 200 and css.content_type.startswith("text/css")
    js = get(config, "/app.js")
    assert js.status == 200 and "filter" in text_of(js)


# ---- task page -------------------------------------------------------

def test_task_page_shows_metadata_and_sessions(config, task):
    task.add_session("11111111-2222-3333-4444-555555555555")
    response = get(config, view.task_url(task))
    body = text_of(response)
    assert response.status == 200
    assert "demo task" in body
    assert str(task.root) in body
    assert "11111111" in body


def test_task_page_without_sessions(config, task):
    assert "no sessions" in text_of(get(config, view.task_url(task)))


def test_task_page_lists_artifacts(config, task):
    (task.dir / "notes.md").write_text("# hi\n")
    body = text_of(get(config, view.task_url(task)))
    assert "notes.md" in body
    assert view.file_url(task, "notes.md") in body


def test_unknown_task_is_404(config):
    assert get(config, f"/task/{HOST}/TASK_19990101_000000").status == 404


def test_non_task_id_is_404(config):
    assert get(config, f"/task/{HOST}/etc").status == 404


def test_unknown_route_is_404(config):
    assert get(config, "/nope").status == 404


def test_unexpected_failure_becomes_a_500(config, task, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(view, "index_page", boom)
    response = get(config, "/")
    assert response.status == 500
    assert "kaboom" in text_of(response)


def test_awkward_filenames_round_trip(config, task):
    for name in ("a b.md", "has#hash.txt", "has?query.txt", "100%.txt", "héllo.md"):
        (task.dir / name).write_text(f"# {name}\n")
    listing = text_of(get(config, view.task_url(task)))
    for name in ("a b.md", "has#hash.txt", "has?query.txt", "100%.txt", "héllo.md"):
        url = view.file_url(task, name)
        assert url in listing, name
        assert get(config, url).status == 200, name


# ---- containment -----------------------------------------------------

def test_parent_traversal_is_refused(config, task):
    assert get(config, file_path(task) + "/../../../etc/passwd").status == 403


def test_encoded_traversal_is_refused(config, task):
    assert get(config, file_path(task) + "/..%2F..%2Fetc%2Fpasswd").status == 403


def test_root_symlink_is_not_browsable(config, task):
    assert (task.dir / "root").is_symlink()
    assert get(config, file_path(task, "root")).status == 403


def test_symlink_out_of_the_task_dir_is_refused(config, task, project):
    (project / "secret.txt").write_text("secret\n")
    (task.dir / "escape.txt").symlink_to(project / "secret.txt")
    assert get(config, file_path(task, "escape.txt")).status == 403
    assert get(config, view.raw_url(task, "escape.txt")).status == 403


def test_raw_through_root_symlink_is_refused(config, task, project):
    (project / "main.py").write_text("print(1)\n")
    assert get(config, view.raw_url(task, "root/main.py")).status == 403


def test_missing_artifact_is_404(config, task):
    assert get(config, file_path(task, "nope.txt")).status == 404


# ---- directories -----------------------------------------------------

def test_directory_listing_and_navigation(config, task):
    (task.dir / "data").mkdir()
    (task.dir / "data" / "inner.txt").write_text("x\n")
    listing = text_of(get(config, file_path(task, "data")))
    assert "inner.txt" in listing
    assert view.file_url(task, "data/inner.txt") in listing
    assert get(config, file_path(task, "data/inner.txt")).status == 200


def test_empty_directory(config, task):
    (task.dir / "hollow").mkdir()
    assert "empty" in text_of(get(config, file_path(task, "hollow")))


def test_breadcrumbs_link_parent_directories(config, task):
    (task.dir / "a" / "b").mkdir(parents=True)
    (task.dir / "a" / "b" / "c.txt").write_text("c\n")
    body = text_of(get(config, file_path(task, "a/b/c.txt")))
    assert view.file_url(task, "a") in body
    assert view.file_url(task, "a/b") in body


# ---- per-kind rendering ----------------------------------------------

def test_markdown_is_rendered(config, task):
    (task.dir / "n.md").write_text("# Title\n\n- one\n- two\n")
    body = text_of(get(config, file_path(task, "n.md")))
    assert "<h1" in body and "Title" in body
    assert "<li>one</li>" in body


def test_markdown_source_view(config, task):
    (task.dir / "n.md").write_text("# Title\n")
    body = text_of(get(config, file_path(task, "n.md") + "?source=1"))
    assert "# Title" in body
    assert "<h1 id=" not in body


def test_json_is_pretty_printed(config, task):
    (task.dir / "r.json").write_text('{"a":{"b":[1,2]}}')
    body = visible(text_of(get(config, file_path(task, "r.json"))))
    assert '"b": [' in body


def test_invalid_json_falls_back_to_source(config, task):
    (task.dir / "bad.json").write_text("{nope")
    body = text_of(get(config, file_path(task, "bad.json")))
    assert "not valid JSON" in body
    assert "{nope" in visible(body)


def test_jsonl_records(config, task):
    (task.dir / "e.jsonl").write_text('{"i":1}\n{"i":2}\n')
    body = text_of(get(config, file_path(task, "e.jsonl")))
    assert body.count("<details") == 2
    assert "2 record(s)" in body


def test_csv_becomes_a_table(config, task):
    (task.dir / "m.csv").write_text('a,b\n1,"x, y"\n')
    body = text_of(get(config, file_path(task, "m.csv")))
    assert "<th>a</th>" in body
    assert "x, y" in body


def test_tsv_becomes_a_table(config, task):
    (task.dir / "m.tsv").write_text("a\tb\n1\t2\n")
    assert "<th>b</th>" in text_of(get(config, file_path(task, "m.tsv")))


def test_code_is_line_numbered(config, task):
    (task.dir / "s.py").write_text("import os\nprint(os)\n")
    body = text_of(get(config, file_path(task, "s.py")))
    assert "gutter" in body
    assert "python" in body


def test_log_ansi_is_stripped(config, task):
    (task.dir / "t.log").write_text("\x1b[32mINFO\x1b[0m ok\n")
    body = text_of(get(config, file_path(task, "t.log")))
    assert "INFO ok" in body
    assert "\x1b[" not in body


def test_image_points_at_raw(config, task):
    (task.dir / "f.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    body = text_of(get(config, file_path(task, "f.png")))
    assert f'<img src="{view.raw_url(task, "f.png")}"' in body


def test_pdf_is_embedded(config, task):
    (task.dir / "d.pdf").write_bytes(b"%PDF-1.4\n" + b"\x00" * 16)
    assert "<iframe" in text_of(get(config, file_path(task, "d.pdf")))


def test_html_is_sandboxed(config, task):
    (task.dir / "r.html").write_text("<h1>hi</h1><script>alert(1)</script>")
    body = text_of(get(config, file_path(task, "r.html")))
    assert "<iframe class=\"embed\" sandbox" in body
    assert "alert(1)" not in body


def test_empty_file(config, task):
    (task.dir / "void.txt").write_text("")
    assert "empty file" in text_of(get(config, file_path(task, "void.txt")))


def test_binary_gets_a_hexdump(config, task):
    (task.dir / "w.bin").write_bytes(bytes(range(64)))
    body = text_of(get(config, file_path(task, "w.bin")))
    assert "binary file" in body
    assert "00000000" in body
    assert "download" in body


def test_zip_members_are_listed(config, task):
    import zipfile

    with zipfile.ZipFile(task.dir / "b.zip", "w") as archive:
        archive.writestr("inside.txt", "hello")
    body = text_of(get(config, file_path(task, "b.zip")))
    assert "inside.txt" in body
    assert "1 member(s)" in body


def test_tar_members_are_listed(config, task):
    import tarfile

    (task.dir / "x.txt").write_text("x\n")
    with tarfile.open(task.dir / "b.tar.gz", "w:gz") as archive:
        archive.add(task.dir / "x.txt", arcname="x.txt")
    body = text_of(get(config, file_path(task, "b.tar.gz")))
    assert "x.txt" in body
    assert "member(s)" in body


def test_notebook_renders_cells_and_outputs(config, task):
    book = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Heading\n"]},
            {"cell_type": "code", "execution_count": 1, "source": ["print(1)\n"],
             "outputs": [{"output_type": "stream", "text": ["1\n"]}]},
            {"cell_type": "code", "execution_count": 2, "source": ["boom()\n"],
             "outputs": [{"output_type": "error", "ename": "ValueError",
                          "traceback": ["\x1b[31mValueError\x1b[0m: boom"]}]},
        ],
        "metadata": {"language_info": {"name": "python"}},
    }
    (task.dir / "a.ipynb").write_text(json.dumps(book))
    body = visible(text_of(get(config, file_path(task, "a.ipynb"))))
    assert "Heading" in body
    assert "print(1)" in body
    assert "ValueError" in body
    assert "\x1b[" not in body


def test_notebook_image_output_is_inlined(config, task):
    book = {"cells": [{"cell_type": "code", "source": ["p()\n"], "outputs": [
        {"output_type": "display_data", "data": {"image/png": "QUJD"}}]}]}
    (task.dir / "a.ipynb").write_text(json.dumps(book))
    body = text_of(get(config, file_path(task, "a.ipynb")))
    assert "data:image/png;base64,QUJD" in body


def test_invalid_notebook_falls_back(config, task):
    (task.dir / "a.ipynb").write_text("not json")
    assert "not valid notebook JSON" in text_of(get(config, file_path(task, "a.ipynb")))


def test_oversized_text_is_clipped(config, task, monkeypatch):
    monkeypatch.setattr(render, "MAX_RENDER_BYTES", 64)
    (task.dir / "big.txt").write_text("x" * 500)
    assert "clipped" in text_of(get(config, file_path(task, "big.txt")))


# ---- raw -------------------------------------------------------------

def test_raw_serves_bytes_inline_for_images(config, task):
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    (task.dir / "f.png").write_bytes(payload)
    response = get(config, view.raw_url(task, "f.png"))
    assert response.status == 200
    assert response.body == payload
    assert response.content_type == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_raw_serves_text_as_plain(config, task):
    (task.dir / "n.md").write_text("# hi\n")
    response = get(config, view.raw_url(task, "n.md"))
    assert response.content_type.startswith("text/plain")
    assert "Content-Disposition" not in response.headers


def test_raw_forces_download_for_unknown_types(config, task):
    (task.dir / "w.bin").write_bytes(bytes(range(200)))
    response = get(config, view.raw_url(task, "w.bin"))
    assert response.content_type == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]


def test_raw_of_a_directory_is_404(config, task):
    (task.dir / "data").mkdir()
    assert get(config, view.raw_url(task, "data")).status == 404


# ---- markdown renderer ----------------------------------------------

def test_markdown_escapes_html():
    out = render.markdown_to_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_markdown_rejects_javascript_urls():
    out = render.markdown_to_html("[x](javascript:alert(1))")
    assert "javascript:" not in out
    assert 'href="#"' in out


def test_markdown_keeps_code_spans_literal():
    out = render.markdown_to_html("use `**not bold**` here")
    assert "<code>**not bold**</code>" in out


def test_markdown_hides_html_comments():
    out = render.markdown_to_html("a\n\n<!-- hidden -->\n\nb")
    assert "hidden" not in out


def test_markdown_fenced_code_keeps_language():
    out = render.markdown_to_html("```python\nx = 1\n```")
    assert "python" in out
    assert "x = 1" in visible(out)


def test_markdown_nested_and_ordered_lists():
    out = render.markdown_to_html("- a\n  - b\n\n1. one\n2. two\n")
    assert "<ul>" in out and "<ol>" in out
    assert out.index("<ol>") > out.index("<ul>")


def test_markdown_table_alignment():
    out = render.markdown_to_html("| a | b |\n| :-: | --: |\n| 1 | 2 |\n")
    assert 'class="center"' in out
    assert 'class="right"' in out


def test_markdown_blockquote_and_rule():
    out = render.markdown_to_html("> quoted\n\n---\n")
    assert "<blockquote>" in out
    assert "<hr>" in out


def test_markdown_line_starting_with_pipe_is_not_a_table():
    out = render.markdown_to_html("| not a table\n")
    assert "<table" not in out


# ---- syntax highlighting --------------------------------------------

def test_python_is_highlighted(config, task):
    (task.dir / "s.py").write_text("import os\n\n\ndef f(x):\n    return f'{x!r}'\n")
    body = text_of(get(config, file_path(task, "s.py")))
    assert 'class="tk-k"' in body
    assert 'class="tk-nf"' in body
    assert "import os" in visible(body)


def test_shell_is_highlighted(config, task):
    (task.dir / "r.sh").write_text('#!/bin/bash\nset -e\necho "hi $HOME"\n')
    assert "tk-" in text_of(get(config, file_path(task, "r.sh")))


def test_diff_is_highlighted_by_filename_alone(config, task):
    (task.dir / "p.diff").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")
    body = text_of(get(config, file_path(task, "p.diff")))
    assert 'class="tk-gd"' in body
    assert 'class="tk-gi"' in body


def test_plain_text_is_not_highlighted(config, task):
    (task.dir / "n.txt").write_text("just some prose\nover two lines\n")
    assert "tk-" not in text_of(get(config, file_path(task, "n.txt")))


def test_unknown_extension_is_not_highlighted(config, task):
    (task.dir / "thing.zzz").write_text("nothing to lex here\n")
    assert "tk-" not in text_of(get(config, file_path(task, "thing.zzz")))


def test_highlighting_preserves_content_exactly(config, task):
    source = "x = '<a & b>'\nif x:\n    pass\n"
    (task.dir / "s.py").write_text(source)
    body = text_of(get(config, file_path(task, "s.py")))
    assert "<a & b>" not in body
    assert source.strip() in visible(body)


def test_gutter_matches_line_count_with_leading_blank(config, task):
    (task.dir / "s.py").write_text("\n\nimport os\nimport sys\n")
    body = text_of(get(config, file_path(task, "s.py")))
    gutter = body.split('class="gutter" aria-hidden="true">')[1].split("</pre>")[0]
    assert gutter.split("\n") == ["1", "2", "3", "4"]


def test_gutter_matches_line_count_when_highlighted(config, task):
    lines = [f"a{i} = {i}" for i in range(12)]
    (task.dir / "s.py").write_text("\n".join(lines) + "\n")
    body = text_of(get(config, file_path(task, "s.py")))
    gutter = body.split('class="gutter" aria-hidden="true">')[1].split("</pre>")[0]
    assert gutter.split("\n")[-1] == "12"


def test_oversized_files_skip_highlighting(config, task, monkeypatch):
    monkeypatch.setattr(render, "MAX_HIGHLIGHT_BYTES", 32)
    (task.dir / "s.py").write_text("import os\n" * 40)
    body = text_of(get(config, file_path(task, "s.py")))
    assert "tk-" not in body
    assert "import os" in visible(body)


def test_markdown_fence_with_unknown_language_still_renders():
    out = render.markdown_to_html("```nosuchlang\nsome text\n```")
    assert "some text" in visible(out)
    assert "tk-" not in out


def test_markdown_fence_highlights_known_language():
    out = render.markdown_to_html("```python\nimport os\n```")
    assert 'class="tk-kn"' in out


def test_notebook_code_cells_are_highlighted(config, task):
    book = {"cells": [{"cell_type": "code", "execution_count": 1,
                       "source": ["import os\n"], "outputs": []}],
            "metadata": {"language_info": {"name": "python"}}}
    (task.dir / "a.ipynb").write_text(json.dumps(book))
    assert 'class="tk-kn"' in text_of(get(config, file_path(task, "a.ipynb")))


def test_json_is_highlighted(config, task):
    (task.dir / "r.json").write_text('{"a": 1}')
    assert "tk-" in text_of(get(config, file_path(task, "r.json")))


def test_highlight_code_labels_the_language(config):
    marked = render.highlight_code("import os\n", filename="a.py")
    assert marked is not None
    assert marked.language == "Python"


def test_highlight_code_declines_blank_and_unknown():
    assert render.highlight_code("   \n") is None
    assert render.highlight_code("hello", filename="a.unknownext") is None


def test_hexdump_is_not_highlighted(config, task):
    (task.dir / "w.bin").write_bytes(bytes(range(64)))
    body = text_of(get(config, file_path(task, "w.bin")))
    assert "tk-" not in body


def test_theme_covers_the_emitted_token_classes(config, task):
    samples = {
        "s.py": "import os\n\n\nclass A:\n    x = 1  # note\n    def f(self):\n"
                "        return os.path.join('a', \"b\")\n",
        "r.sh": '#!/bin/sh\nset -eu\nfor i in 1 2; do echo "$i"; done\n',
        "c.c": '#include <stdio.h>\nint main(void) { return 0; }\n',
        "t.toml": '[table]\nkey = "value"\nnum = 3\n',
        "y.yaml": "key: value\nlist:\n  - one\n",
        "p.diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
    }
    for name, source in samples.items():
        (task.dir / name).write_text(source)

    css = text_of(get(config, "/style.css"))
    styled = set(re.findall(r"\.(tk-[a-z0-9]+)", css))

    emitted = set()
    for name in samples:
        body = text_of(get(config, file_path(task, name)))
        emitted |= set(re.findall(r'class="(tk-[a-z0-9]+)"', body))

    assert emitted, "nothing was highlighted at all"
    assert not emitted - styled, f"unstyled token classes: {sorted(emitted - styled)}"


def test_classify_covers_the_common_kinds(tmp_path):
    cases = {
        "a.md": "markdown", "a.ipynb": "notebook", "a.json": "json",
        "a.jsonl": "jsonl", "a.csv": "csv", "a.tsv": "tsv", "a.png": "image",
        "a.pdf": "pdf", "a.html": "html", "a.mp4": "video", "a.mp3": "audio",
        "a.zip": "zip", "a.tar": "tar", "a.py": "code", "a.log": "text",
        "Makefile": "code",
    }
    for name, expected in cases.items():
        path = tmp_path / name
        path.write_text("x")
        assert render.classify(path) == expected, name


def test_classify_detects_binary(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"\x00\x01\x02binary")
    assert render.classify(path) == "binary"


# ---- server + cli ----------------------------------------------------

def test_real_server_round_trip(config, task):
    (task.dir / "n.md").write_text("# served\n")
    server = view.make_server(config, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://{view.BIND_HOST}:{port}"
        with urllib.request.urlopen(f"{base}/") as response:
            assert response.status == 200
            assert task.task_id in response.read().decode()
        with urllib.request.urlopen(f"{base}{view.file_url(task, 'n.md')}") as response:
            assert "served" in response.read().decode()
        request = urllib.request.Request(f"{base}/", method="HEAD")
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert response.read() == b""
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base}/task/{HOST}/TASK_19990101_000000")
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_binds_localhost_only(config):
    server = view.make_server(config, 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_cmd_view_reports_the_bound_port(config, capsys):
    assert commands.cmd_view(0, serve=False) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert "ssh -N -L" in out


def test_cmd_view_errors_on_a_taken_port(config):
    server = view.make_server(config, 0)
    try:
        port = server.server_address[1]
        with pytest.raises(commands.CommandError, match="could not listen"):
            commands.cmd_view(port, serve=False)
    finally:
        server.server_close()


def _record_port(seen):
    def fake(port):
        seen["port"] = port
        return 0
    return fake


def test_cli_passes_the_port_through(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "cmd_view", _record_port(seen))
    assert main(["--view", "9123"]) == 0
    assert seen["port"] == 9123


def test_cli_defaults_the_port(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "cmd_view", _record_port(seen))
    assert main(["--view"]) == 0
    assert seen["port"] == view.DEFAULT_PORT


def test_cli_rejects_a_bad_port(capsys):
    with pytest.raises(SystemExit):
        main(["--view", "nope"])
    assert "port" in capsys.readouterr().err


def test_cli_rejects_an_out_of_range_port(capsys):
    with pytest.raises(SystemExit):
        main(["--view", "99999"])
    assert "between 0 and 65535" in capsys.readouterr().err


def test_cli_view_rejects_claude_passthrough(capsys):
    with pytest.raises(SystemExit):
        main(["--view", "8080", "--", "--model", "opus"])
    assert "pass-through" in capsys.readouterr().err


# ---- relative URLs in markdown artifacts ------------------------------

def article(body):
    """Just the rendered markdown — the page chrome has its own /raw and /file links."""
    return body.split('<article class="md">')[1].split("</article>")[0]


def md_at(task, rel, body):
    path = task.dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return view.file_url(task, rel)


def test_relative_image_points_at_raw(config, task):
    (task.dir / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    url = md_at(task, "notes.md", "![p](plot.png)\n")
    body = text_of(get(config, url))
    assert f'src="{view.raw_url(task, "plot.png")}"' in body
    assert 'src="plot.png"' not in body


def test_relative_image_resolves_against_the_files_directory(config, task):
    (task.dir / "results" / "figs").mkdir(parents=True)
    (task.dir / "results" / "figs" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    url = md_at(task, "results/r.md", "![a](figs/a.png)\n")
    body = text_of(get(config, url))
    assert f'src="{view.raw_url(task, "results/figs/a.png")}"' in body


def test_dot_slash_is_normalised(config, task):
    (task.dir / "d").mkdir()
    (task.dir / "d" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    body = text_of(get(config, md_at(task, "d/r.md", "![a](./a.png)\n")))
    assert f'src="{view.raw_url(task, "d/a.png")}"' in body


def test_relative_link_points_at_the_viewer_not_raw(config, task):
    (task.dir / "other.md").write_text("# other\n")
    body = text_of(get(config, md_at(task, "notes.md", "[o](other.md)\n")))
    assert f'href="{view.file_url(task, "other.md")}"' in body


def test_images_on_a_real_page_serve_as_images(config, task):
    (task.dir / "figs").mkdir()
    (task.dir / "figs" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    url = md_at(task, "notes.md", "![a](figs/a.png)\n")
    body = text_of(get(config, url))
    src = re.search(r'<img src="([^"]+)"', body).group(1)
    served = get(config, src)
    assert served.status == 200
    assert served.content_type.startswith("image/")


def test_escaping_relative_image_is_not_rewritten(config, task):
    body = article(text_of(get(config, md_at(task, "notes.md",
                                             "![x](../../../etc/passwd)\n"))))
    assert "/raw/" not in body and "/file/" not in body
    assert 'src="../../../etc/passwd"' in body


def test_markdown_image_cannot_reach_through_the_root_symlink(config, task, project):
    (project / "secret.txt").write_text("secret\n")
    body = text_of(get(config, md_at(task, "notes.md", "![x](root/secret.txt)\n")))
    src = re.search(r'<img src="([^"]+)"', body).group(1)
    assert get(config, src).status == 403


def test_absolute_path_inside_the_task_is_rewritten(config, task):
    (task.dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    body = text_of(get(config, md_at(task, "notes.md",
                                     f"![a]({task.dir / 'a.png'})\n")))
    assert f'src="{view.raw_url(task, "a.png")}"' in body


def test_absolute_path_outside_the_task_is_not_rewritten(config, task):
    body = article(text_of(get(config, md_at(task, "notes.md", "![x](/etc/passwd)\n"))))
    assert "/raw/" not in body
    assert 'src="/etc/passwd"' in body


def test_external_image_becomes_a_link(config, task):
    body = text_of(get(config, md_at(task, "notes.md",
                                     "![remote](https://example.com/a.png)\n")))
    assert "<img" not in article(body)
    assert "external image" in body
    assert 'href="https://example.com/a.png"' in body


def test_data_uri_image_survives(config, task):
    body = text_of(get(config, md_at(task, "notes.md",
                                     "![d](data:image/png;base64,QUJD)\n")))
    assert 'src="data:image/png;base64,QUJD"' in body


def test_anchor_links_are_left_alone(config, task):
    body = text_of(get(config, md_at(task, "notes.md", "# H\n\n[go](#h)\n")))
    assert 'href="#h"' in body


def test_notebook_markdown_cell_images_are_resolved(config, task):
    (task.dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    book = {"cells": [{"cell_type": "markdown", "source": ["![a](a.png)\n"]}],
            "metadata": {}}
    (task.dir / "n.ipynb").write_text(json.dumps(book))
    body = text_of(get(config, view.file_url(task, "n.ipynb")))
    assert f'src="{view.raw_url(task, "a.png")}"' in body


def test_markdown_without_a_resolver_is_unchanged():
    out = render.markdown_to_html("![a](plot.png)\n[l](other.md)")
    assert 'src="plot.png"' in out
    assert 'href="other.md"' in out
