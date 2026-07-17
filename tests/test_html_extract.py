from pathlib import Path

from scraper.html_extract import _markdown_to_text, extract_html

FIXTURE = Path(__file__).parent / "fixtures" / "sample.html"


def test_extracts_main_content_and_strips_boilerplate():
    doc = extract_html(
        FIXTURE.read_text(encoding="utf-8"),
        url="https://www.heidenheim.dhbw.de/bewerbung",
    )
    assert doc is not None
    assert "Dualen Partner" in doc["text"]
    assert "Impressum" not in doc["text"]  # footer stripped
    assert "Nav A" not in doc["text"]  # nav stripped
    assert doc["word_count"] > 20
    assert doc["markdown"]


def test_leading_ordinals_survive_the_markdown_stripper():
    """`_LIST_MARKER` matched a leading "N." on ANY line, not just real list items.

    `_HEADING` runs first, so "## 1. Semester" is already reduced to a line
    starting "1. " by the time the list rule sees it -- and the ordinal was
    stripped, indexing the heading as bare "Semester". For DHBW module and
    semester content those ordinals are the meaningful part: "1. Semester" and
    "2. Semester" both collapsed to "Semester", so full-text search could no
    longer tell them apart.

    Asserted on the stripper directly: routing this through the real
    trafilatura.extract() would depend on whether that version renders the
    heading as "## 1. Semester" or as an ordered-list item.
    """
    assert "1. Semester" in _markdown_to_text("## 1. Semester")
    assert "2. Fachsemester" in _markdown_to_text("### 2. Fachsemester")
    assert "1. Semester" in _markdown_to_text("Studienplan\n\n1. Semester\n")


def test_returns_none_for_contentless_html():
    assert extract_html("<html><body></body></html>") is None
