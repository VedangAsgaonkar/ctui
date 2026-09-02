"""The wiki's dated pages."""

from datetime import date

from ctui import wiki

DAY = date(2026, 9, 2)


def test_dated_path(tmp_path):
    assert wiki.dated_path(tmp_path, DAY) == tmp_path / "dated" / "2026-09-02.md"


def test_ensure_page_creates_from_template(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY)
    assert page.exists()
    text = page.read_text()
    assert text.startswith("# 2026-09-02")
    for title in wiki.section_titles():
        assert f"## {title}" in text
    assert wiki.FOLDED_HEADING in text


def test_template_states_the_methods_not_results_rule(tmp_path):
    """The scope rule lives on the page, for human and later-pass readers."""
    text = wiki.ensure_page(tmp_path, DAY).read_text()
    assert "methods, not results" in text
    assert "different" in text


def test_ensure_page_is_idempotent(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY)
    page.write_text("# 2026-09-02\n\nhand written\n")
    again = wiki.ensure_page(tmp_path, DAY)
    assert again.read_text() == "# 2026-09-02\n\nhand written\n"


def test_folded_sessions_starts_empty(tmp_path):
    assert wiki.folded_sessions(tmp_path / "absent.md") == set()
    assert wiki.folded_sessions(wiki.ensure_page(tmp_path, DAY)) == set()


def test_record_and_read_folded(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY)
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    wiki.record_folded(page, "bbbb-2222", "TASK_B", "second")

    assert wiki.folded_sessions(page) == {"aaaa-1111", "bbbb-2222"}
    text = page.read_text()
    assert "TASK_A (first)" in text and "TASK_B (second)" in text


def test_folded_markers_survive_reformatting(tmp_path):
    """Provenance is tracked by comment marker, not by prose shape."""
    page = wiki.ensure_page(tmp_path, DAY)
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    text = page.read_text().replace("- `aaaa1111` · TASK_A (first)",
                                    "* reworded by the model entirely")
    page.write_text(text)
    assert wiki.folded_sessions(page) == {"aaaa-1111"}


def test_record_folded_preserves_learning_sections(tmp_path):
    page = wiki.ensure_page(tmp_path, DAY)
    page.write_text(page.read_text().replace(
        "## Libraries & tools\n", "## Libraries & tools\n\n- a learning\n"))
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    assert "- a learning" in page.read_text()


def test_record_folded_recreates_a_missing_heading(tmp_path):
    page = wiki.dated_path(tmp_path, DAY)
    page.parent.mkdir(parents=True)
    page.write_text("# 2026-09-02\n\n## Libraries & tools\n\n- kept\n")
    wiki.record_folded(page, "aaaa-1111", "TASK_A", "first")
    text = page.read_text()
    assert wiki.FOLDED_HEADING in text
    assert "- kept" in text
    assert wiki.folded_sessions(page) == {"aaaa-1111"}
