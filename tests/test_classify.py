from scraper import classify


def _doc(title="", description="", text=""):
    return {"title": title, "text": text, "metadata": {"description": description}}


def test_program_detected_sets_faculty_and_program():
    c = classify.classify(
        "https://www.mosbach.dhbw.de/studienangebot/maschinenbau",
        "mosbach.dhbw.de",
        _doc(),
    )
    assert c.program == "maschinenbau"
    assert c.program_display == "Maschinenbau"
    assert c.department == "technik"
    assert c.meta["department"] == "program"


def test_department_from_url_rule_without_program():
    c = classify.classify(
        "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/",
        "dhbw-stuttgart.de",
        _doc(),
    )
    assert c.department == "wirtschaft"
    assert c.program is None
    assert c.meta["department"] == "url"


def test_department_from_content_keyword_fallback():
    c = classify.classify(
        "https://www.dhbw.de/aktuelles/meldung",
        "www.dhbw.de",
        _doc(title="Neuer Studiengang", description="Bachelor Sozialwesen an der DHBW"),
    )
    assert c.department == "sozialwesen"
    assert c.meta["department"] == "keyword"


def test_faculty_agnostic_page_is_unknown():
    c = classify.classify(
        "https://www.heilbronn.dhbw.de/datenschutz/",
        "heilbronn.dhbw.de",
        _doc(title="Datenschutz", description="Datenschutzerklärung"),
    )
    assert c.department == "unknown"
    assert c.program is None
    assert c.meta["department"] == "default"


def test_keyword_ambiguity_resolves_to_unknown_not_a_coinflip():
    c = classify.classify(
        "https://www.dhbw.de/x",
        "www.dhbw.de",
        _doc(text="Das Modul verbindet Maschinenbau und Sozialwesen thematisch."),
    )
    assert c.department == "unknown"


def test_standort_provenance_site_vs_url():
    base = classify.classify(
        "https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de", _doc()
    )
    assert base.standort == "mannheim" and base.meta["standort"] == "site"
    sat = classify.classify(
        "https://www.dhbw-stuttgart.de/horb/x", "dhbw-stuttgart.de", _doc()
    )
    assert sat.standort == "stuttgart-horb" and sat.meta["standort"] == "url"


def test_meta_records_version():
    from scraper import taxonomy

    c = classify.classify("https://www.dhbw.de/x", "www.dhbw.de", _doc())
    assert c.meta["version"] == taxonomy.CLASSIFY_VERSION
