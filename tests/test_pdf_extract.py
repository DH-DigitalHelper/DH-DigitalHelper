from scraper.pdf_extract import extract_pdf
from scraper.quality import evaluate

MARKDOWN = "# Modulhandbuch\n\nInhalt des Moduls mit ausreichend Text."

TABLE_HEAVY = """\
# Modulhandbuch Wirtschaftsinformatik

## 1. Semester

| Modul | ECTS | Dozent |
| --- | --- | --- |
| Programmieren | 5 | Meier |
| Mathematik | 5 | Schmidt |

> Hinweis zur Pruefung

- Erste Anmerkung zum Modul
- Zweite Anmerkung zum Modul

Der eigentliche Fliesstext dieser Seite ist bewusst kurz gehalten und
umfasst nur wenige echte Woerter fuer diesen Test.
"""


def test_pdf_word_count_ignores_markdown_syntax():
    """word_count must count words, not markdown punctuation."""
    doc = extract_pdf(b"%PDF fake", to_markdown=lambda data: TABLE_HEAVY)

    assert doc is not None
    assert "|" not in doc["text"]
    assert "---" not in doc["text"]
    assert not any(line.startswith("#") for line in doc["text"].splitlines())
    assert "Programmieren" in doc["text"]
    assert "Modulhandbuch" in doc["text"]
    assert doc["word_count"] == len(doc["text"].split())
    raw_tokens = len(TABLE_HEAVY.split())
    assert doc["word_count"] < raw_tokens, (
        f"word_count {doc['word_count']} still counts markdown syntax "
        f"(raw markdown has {raw_tokens} tokens)"
    )
    assert "| Modul | ECTS | Dozent |" in doc["markdown"]


def test_pdf_below_the_gate_is_rejected_despite_markdown_padding():
    """The end-to-end consequence: a short PDF whose real word count is under the gate must be rejected, even when its markdown syntax pads it over."""
    doc = extract_pdf(b"%PDF fake", to_markdown=lambda data: TABLE_HEAVY)
    real_words = len(doc["text"].split())
    assert real_words < 50, "fixture precondition: genuinely a short page"

    accepted, reason = evaluate(doc, min_words=50)

    assert accepted is False
    assert "short" in reason


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


def test_extract_pdf_detects_language():
    text_md = (
        "# Modulhandbuch\n\n"
        "Die Studierenden erwerben in diesem Modul grundlegende Kenntnisse der "
        "Wirtschaftsinformatik und wenden sie in praktischen Projekten an."
    )
    doc = extract_pdf(b"%PDF fake", to_markdown=lambda data: text_md)
    assert doc["lang"] == "de"


def test_extract_pdf_falls_back_to_metadata_title_when_no_heading():
    no_heading = "Inhalt ohne Ueberschrift, aber mit ausreichend echtem Fliesstext."
    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: no_heading,
        meta_title=lambda data: "  Studien- und Pruefungsordnung  ",
    )
    assert doc["title"] == "Studien- und Pruefungsordnung"


def test_extract_pdf_rejects_junk_metadata_title():
    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: "Fliesstext ohne jede Ueberschrift in dem Dokument.",
        meta_title=lambda data: "Microsoft Word - egal.docx",
    )
    assert doc["title"] is None


def test_extract_pdf_prefers_heading_over_metadata():
    called = {"meta": 0}

    def meta(data):
        called["meta"] += 1
        return "Metadata Title"

    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: "# Heading Wins\n\nEnough real body text here too.",
        meta_title=meta,
    )
    assert doc["title"] == "Heading Wins"
    assert called["meta"] == 0
