from pathlib import Path

from dhbw_scraper.html_extract import extract_html

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


def test_returns_none_for_contentless_html():
    assert extract_html("<html><body></body></html>") is None
