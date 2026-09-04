"""The wiki's dated pages."""

from datetime import date

from ctui import wiki

DAY = date(2026, 9, 2)
HOST = "furiosa.stanford.edu"


def test_dated_path_is_partitioned_by_host(tmp_path):
    assert wiki.dated_path(tmp_path, DAY, HOST) == \
        tmp_path / "dated" / HOST / "2026-09-02.md"


def test_hosts_get_separate_pages(tmp_path):
    """Two hosts distilling the same night must never touch the same file."""
    mine = wiki.ensure_page(tmp_path, DAY, "host-a")
    theirs = wiki.ensure_page(tmp_path, DAY, "host-b")
    assert mine != theirs
    assert mine.parent != theirs.parent
    assert "host-a" in mine.read_text()
    assert "host-b" in theirs.read_text()


def test_ensure_page_creates_from_template(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    assert page.exists()
    text = page.read_text()
    assert text.startswith(f"# 2026-09-02 — {HOST}")
    for title in wiki.section_titles():
        assert f"## {title}" in text
    assert wiki.FOLDED_HEADING in text


def test_template_states_the_methods_not_results_rule(tmp_path):
    """The scope rule lives on the page, for human and later-pass readers."""
    text = wiki.ensure_page(tmp_path, DAY, HOST).read_text()
    assert "methods, not results" in text
    assert "different" in text


def test_ensure_page_is_idempotent(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    page.write_text("# 2026-09-02\n\nhand written\n")
    again = wiki.ensure_page(tmp_path, DAY, HOST)
    assert again.read_text() == "# 2026-09-02\n\nhand written\n"


def test_folded_sessions_starts_empty(tmp_path):
    assert wiki.folded_sessions(tmp_path / "absent.md") == set()
    assert wiki.folded_sessions(wiki.ensure_page(tmp_path, DAY, HOST)) == set()


def test_record_and_read_folded(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    wiki.record_folded(page, "bbbb-2222", "TASK_B", "second")

    assert wiki.folded_sessions(page) == {"aaaa-1111", "bbbb-2222"}
    text = page.read_text()
    assert "TASK_A (first)" in text and "TASK_B (second)" in text


def test_folded_markers_survive_reformatting(tmp_path):
    """Provenance is tracked by comment marker, not by prose shape."""
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    text = page.read_text().replace("- `aaaa1111` · TASK_A (first)",
                                    "* reworded by the model entirely")
    page.write_text(text)
    assert wiki.folded_sessions(page) == {"aaaa-1111"}


def test_record_folded_preserves_learning_sections(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    page.write_text(page.read_text().replace(
        "## Libraries & tools\n", "## Libraries & tools\n\n- a learning\n"))
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    assert "- a learning" in page.read_text()


def test_record_folded_recreates_a_missing_heading(tmp_path):
    page = wiki.dated_path(tmp_path, DAY, HOST)
    page.parent.mkdir(parents=True)
    page.write_text("# 2026-09-02\n\n## Libraries & tools\n\n- kept\n")
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    text = page.read_text()
    assert wiki.FOLDED_HEADING in text
    assert "- kept" in text
    assert wiki.folded_sessions(page) == {"aaaa-1111"}


def test_template_has_an_empty_tags_line_in_the_header(tmp_path):
    text = wiki.ensure_page(tmp_path, DAY, HOST).read_text()
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines[0].startswith("# ")
    assert lines[1] == wiki.TAGS_PREFIX
    assert wiki.page_tags(wiki.dated_path(tmp_path, DAY, HOST)) == []


def test_template_says_sections_are_optional(tmp_path):
    text = wiki.ensure_page(tmp_path, DAY, HOST).read_text()
    assert "No section is compulsory" in text


def test_page_tags_parses_a_filled_line(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    page.write_text(page.read_text().replace(
        wiki.TAGS_PREFIX, f"{wiki.TAGS_PREFIX} python, slurm, prompt-engineering", 1))
    assert wiki.page_tags(page) == ["python", "slurm", "prompt-engineering"]


def test_page_tags_tolerates_odd_spacing_and_trailing_commas(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    page.write_text(page.read_text().replace(
        wiki.TAGS_PREFIX, f"{wiki.TAGS_PREFIX}  python ,, git,  ", 1))
    assert wiki.page_tags(page) == ["python", "git"]


def test_page_tags_on_a_page_without_the_line(tmp_path):
    page = wiki.dated_path(tmp_path, DAY, HOST)
    page.parent.mkdir(parents=True)
    page.write_text("# 2026-09-02\n\nhand written\n")
    assert wiki.page_tags(page) == []
    assert wiki.page_tags(tmp_path / "absent.md") == []


def test_record_folded_preserves_tags(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY, HOST)
    page.write_text(page.read_text().replace(
        wiki.TAGS_PREFIX, f"{wiki.TAGS_PREFIX} python, git", 1))
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    assert wiki.page_tags(page) == ["python", "git"]
