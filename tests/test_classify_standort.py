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
    assert (
        classify.classify_standort(
            "https://www.ravensburg.dhbw.de/friedrichshafen/", "ravensburg.dhbw.de"
        )
        == "ravensburg-friedrichshafen"
    )
    assert (
        classify.classify_standort(
            "https://www.mosbach.dhbw.de/bad-mergentheim/", "mosbach.dhbw.de"
        )
        == "mosbach-bad-mergentheim"
    )
