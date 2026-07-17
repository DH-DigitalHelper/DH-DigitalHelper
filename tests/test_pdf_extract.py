from scraper.pdf_extract import extract_pdf
from scraper.quality import evaluate

MARKDOWN = "# Modulhandbuch\n\nInhalt des Moduls mit ausreichend Text."

# A heading/table-heavy PDF page: 40 real words, but ~30 extra markdown tokens
# (#, |, ---, -, >) that split() happily counts as words.
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
    """word_count must count words, not markdown punctuation.

    The PDF path used its raw markdown as `text`, so `len(text.split())` counted
    `#`, `|`, `---`, `-` and `>` as words -- while the HTML path strips all markup
    before counting. The same min_words gate was therefore applied to two
    different notions of "word", and a heading/table-heavy PDF sailed past it on
    punctuation alone.
    """
    doc = extract_pdf(b"%PDF fake", to_markdown=lambda data: TABLE_HEAVY)

    assert doc is not None
    # The markup itself must not appear in the indexed text ...
    assert "|" not in doc["text"]
    assert "---" not in doc["text"]
    assert not any(line.startswith("#") for line in doc["text"].splitlines())
    # ... but every real word must survive, including the ordinal.
    assert "Programmieren" in doc["text"]
    assert "Modulhandbuch" in doc["text"]
    # ... and the count must reflect real words only.
    assert doc["word_count"] == len(doc["text"].split())
    raw_tokens = len(TABLE_HEAVY.split())
    assert doc["word_count"] < raw_tokens, (
        f"word_count {doc['word_count']} still counts markdown syntax "
        f"(raw markdown has {raw_tokens} tokens)"
    )
    # The markdown itself is still stored verbatim for downstream consumers.
    assert "| Modul | ECTS | Dozent |" in doc["markdown"]


def test_pdf_below_the_gate_is_rejected_despite_markdown_padding():
    """The end-to-end consequence: a short PDF whose *real* word count is under
    the gate must be rejected, even when its markdown syntax pads it over."""
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
