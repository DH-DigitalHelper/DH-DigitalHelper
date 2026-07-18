from scraper import lang


def test_detects_german():
    text = (
        "Die Duale Hochschule Baden-Württemberg verbindet ein wissenschaftliches "
        "Studium mit der praktischen Ausbildung in einem Unternehmen."
    )
    assert lang.detect(text) == "de"


def test_detects_english():
    text = (
        "The cooperative state university combines academic study with practical "
        "training at a partner company over the course of three years."
    )
    assert lang.detect(text) == "en"


def test_empty_and_none_return_none():
    assert lang.detect("") is None
    assert lang.detect(None) is None
    assert lang.detect("   \n\t ") is None


def test_too_short_returns_none():
    assert lang.detect("Hallo") is None


def test_is_deterministic():
    text = "Studierende absolvieren Praxisphasen bei ihrem dualen Partner."
    assert lang.detect(text) == lang.detect(text)


def test_low_confidence_returns_none():
    assert lang.detect("1234567890 3141592653 2718281828 !@#$%^&*() 1234567890") is None


def test_long_text_is_capped_before_detection(monkeypatch):
    seen = {}

    class _Recorder:
        def classify(self, text):
            seen["len"] = len(text)
            return ("de", 0.99)

    monkeypatch.setattr(lang, "_identifier", _Recorder())
    huge = "wort " * 500_000
    assert lang.detect(huge) == "de"
    assert seen["len"] <= lang._MAX_CHARS


def test_detector_failure_is_nonfatal(monkeypatch):
    class _Boom:
        def classify(self, text):
            raise OverflowError("boom")

    monkeypatch.setattr(lang, "_identifier", _Boom())
    assert lang.detect("Ein hinreichend langer deutscher Satz zum Testen.") is None
