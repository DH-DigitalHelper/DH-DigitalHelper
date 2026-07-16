from dhbw_scraper.pdf_extract import extract_pdf

MARKDOWN = "# Modulhandbuch\n\nInhalt des Moduls mit ausreichend Text."


def test_extract_pdf_uses_to_markdown_and_shapes_doc():
    calls = {"n": 0}

    def fake_to_markdown(data):
        calls["n"] += 1
        assert data == b"%PDF-1.4 fake"
        return MARKDOWN

    doc = extract_pdf(b"%PDF-1.4 fake", to_markdown=fake_to_markdown)
    assert calls["n"] == 1
    assert doc is not None
    assert "Modulhandbuch" in doc["markdown"]
    assert "Inhalt des Moduls" in doc["text"]
    assert doc["word_count"] > 0
    assert doc["title"] == "Modulhandbuch"  # title pulled from first H1
    assert doc["metadata"] == {"extractor": "pymupdf4llm"}


def test_extract_pdf_returns_none_when_empty():
    assert extract_pdf(b"%PDF fake", to_markdown=lambda data: "   ") is None
