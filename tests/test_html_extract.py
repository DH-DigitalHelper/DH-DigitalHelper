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
    assert "Impressum" not in doc["text"]
    assert "Nav A" not in doc["text"]
    assert doc["word_count"] > 20
    assert doc["markdown"]


def test_leading_ordinals_survive_the_markdown_stripper():
    """_LIST_MARKER matched a leading "N." on ANY line, not just real list items."""
    assert "1. Semester" in _markdown_to_text("## 1. Semester")
    assert "2. Fachsemester" in _markdown_to_text("### 2. Fachsemester")
    assert "1. Semester" in _markdown_to_text("Studienplan\n\n1. Semester\n")


def test_returns_none_for_contentless_html():
    assert extract_html("<html><body></body></html>") is None


def test_extract_html_detects_language():
    html = (
        "<html><body><main><p>"
        "Die Duale Hochschule Baden-Württemberg verbindet ein wissenschaftliches "
        "Studium mit der praktischen Berufsausbildung in einem Unternehmen und "
        "richtet sich an Studierende aus der ganzen Region."
        "</p></main></body></html>"
    )
    doc = extract_html(html)
    assert doc is not None
    assert doc["lang"] == "de"
