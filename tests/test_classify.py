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
    # Ambiguity is judged over title/description (body text is not scanned); two
    # faculties tie -> unknown, never a coin-flip.
    c = classify.classify(
        "https://www.dhbw.de/x",
        "www.dhbw.de",
        _doc(
            description="Das Modul verbindet Maschinenbau und Sozialwesen thematisch."
        ),
    )
    assert c.department == "unknown"


def test_underscore_path_matches_gesundheit_program():
    # Real Gesundheit paths use underscores; '_'->'-' normalization must let the
    # (previously dead) program pattern match (audit §B1).
    c = classify.classify(
        "https://www.karlsruhe.dhbw.de/studiengang/Angewandte_Gesundheitswissenschaften/",
        "karlsruhe.dhbw.de",
        _doc(),
    )
    assert c.program == "angewandte-gesundheitswissenschaften"
    assert c.department == "gesundheit"
    assert c.meta["department"] == "program"


def test_enumeration_company_stub_gets_no_program_or_faculty():
    # A dual-partner company page whose slug contains a program name must NOT be
    # tagged with that program/faculty (audit §B1 employer-stub false positives).
    c = classify.classify(
        "https://www.mannheim.dhbw.de/informatik/liste-dualer-partner/unternehmen/"
        "fujitsu-services-gmbh-13464/",
        "mannheim.dhbw.de",
        _doc(),
    )
    assert c.program is None
    assert c.department == "unknown"


def test_program_segment_anchoring_rejects_coincidental_matches():
    # 'informatik' must not fire on a '10-informatiktag' event slug (segment-bounded).
    assert (
        classify.classify_program("https://www.dhbw.de/aktuelles/10-informatiktag/")
        is None
    )


def test_wirtschaftsinformatik_and_informatik_are_distinct_programs():
    wi = classify.classify(
        "https://www.mannheim.dhbw.de/studienangebot/wirtschaftsinformatik/",
        "mannheim.dhbw.de",
        _doc(),
    )
    assert wi.program == "wirtschaftsinformatik" and wi.department == "wirtschaft"
    inf = classify.classify(
        "https://www.mannheim.dhbw.de/studienangebot/informatik/",
        "mannheim.dhbw.de",
        _doc(),
    )
    assert inf.program == "informatik" and inf.department == "technik"


def test_news_list_body_teaser_does_not_leak_a_faculty():
    # The 3,185-page defect: a news archive whose body teasers mention 'bwl' must
    # stay 'unknown' -- body text is not scanned for faculty keywords (audit §B2).
    c = classify.classify(
        "https://www.mannheim.dhbw.de/aktuelles/page-3",
        "mannheim.dhbw.de",
        _doc(
            title="Aktuelles aus der DHBW Mannheim",
            text="Neues aus dem BWL-Studiengang und dem Maschinenbau ...",
        ),
    )
    assert c.department == "unknown"
    assert c.program is None


def test_department_recovered_from_studienangebot_path():
    # audit §B4: genuine faculty pages recovered by path-scoped URL rules.
    c = classify.classify(
        "https://www.mosbach.dhbw.de/bachelor-studienangebot/technik/uebersicht/",
        "mosbach.dhbw.de",
        _doc(),
    )
    assert c.department == "technik"
    assert c.program is None
    assert c.meta["department"] == "url"


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
