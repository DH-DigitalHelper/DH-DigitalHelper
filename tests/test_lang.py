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
    # Below the character floor -> no trustworthy guess.
    assert lang.detect("Hallo") is None


def test_is_deterministic():
    text = "Studierende absolvieren Praxisphasen bei ihrem dualen Partner."
    assert lang.detect(text) == lang.detect(text)


def test_low_confidence_returns_none():
    # Long enough to pass the length floor, but non-linguistic digits/symbols so
    # py3langid's top probability lands below the confidence floor -> None (this
    # exercises the confidence branch, which the length-based cases never reach).
    assert lang.detect("1234567890 3141592653 2718281828 !@#$%^&*() 1234567890") is None
