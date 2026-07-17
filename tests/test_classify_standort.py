from scraper import classify


def test_base_campus_from_site():
    assert (
        classify.classify_standort("https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de")
        == "mannheim"
    )
    assert (
        classify.classify_standort(
            "https://www.dhbw-stuttgart.de/", "dhbw-stuttgart.de"
        )
        == "stuttgart"
    )
    assert (
        classify.classify_standort("https://www.dhbw.de/alumni", "www.dhbw.de")
        == "dhbw"
    )
    assert (
        classify.classify_standort("https://www.cas.dhbw.de/", "cas.dhbw.de") == "cas"
    )


def test_base_campus_from_host_when_site_unmapped():
    # A subdomain page whose `site` was not the exact allowed_domain still resolves.
    assert (
        classify.classify_standort("https://events.mannheim.dhbw.de/e/1", "")
        == "mannheim"
    )


def test_unknown_site_yields_none():
    assert classify.classify_standort("https://x/a", "x") is None


def test_horb_satellite_only_under_stuttgart():
    assert (
        classify.classify_standort(
            "https://www.dhbw-stuttgart.de/horb/its/", "dhbw-stuttgart.de"
        )
        == "stuttgart-horb"
    )
    # the substring "horb" must NOT promote a non-Stuttgart page
    assert (
        classify.classify_standort(
            "https://www.mannheim.dhbw.de/horbach", "mannheim.dhbw.de"
        )
        == "mannheim"
    )


def test_friedrichshafen_and_bad_mergentheim_satellites():
    # Friedrichshafen must be matched by a path-anchored slug, not the bare city name.
    assert (
        classify.classify_standort(
            "https://www.ravensburg.dhbw.de/campus-friedrichshafen/",
            "ravensburg.dhbw.de",
        )
        == "ravensburg-friedrichshafen"
    )
    assert (
        classify.classify_standort(
            "https://www.ravensburg.dhbw.de/fn/studienangebot/", "ravensburg.dhbw.de"
        )
        == "ravensburg-friedrichshafen"
    )
    assert (
        classify.classify_standort(
            "https://www.mosbach.dhbw.de/bad-mergentheim/", "mosbach.dhbw.de"
        )
        == "mosbach-bad-mergentheim"
    )


def test_friedrichshafen_company_listing_is_not_a_satellite():
    # A dual-partner company page saturated with the city name must stay base
    # ravensburg -- the old bare-substring rule mis-tagged 44/59 FN docs (audit §B3).
    url = (
        "https://www.ravensburg.dhbw.de/liste-dualer-partner/unternehmen/"
        "detailansicht/zf-friedrichshafen-ag-12345/"
    )
    assert classify.classify_standort(url, "ravensburg.dhbw.de") == "ravensburg"


def test_horb_fileadmin_directory_is_horb_satellite():
    # /fileadmin/dateien-horb/ material is Horb (audit §B5).
    url = "https://www.dhbw-stuttgart.de/fileadmin/dateien-horb/studienplan.pdf"
    assert classify.classify_standort(url, "dhbw-stuttgart.de") == "stuttgart-horb"


def test_stuttgart_horbach_page_is_not_horb_satellite():
    # "/horbach" must NOT trigger the Horb satellite (segment-bounded match)
    assert (
        classify.classify_standort(
            "https://www.dhbw-stuttgart.de/horbach/x", "dhbw-stuttgart.de"
        )
        == "stuttgart"
    )
