from scraper import taxonomy as tx

DEPT_SLUGS = {slug for slug, _ in tx.DEPARTMENTS}


def test_departments_include_the_four_faculties_and_unknown():
    assert DEPT_SLUGS == {
        "technik",
        "wirtschaft",
        "sozialwesen",
        "gesundheit",
        "unknown",
    }


def test_department_slugs_are_unique():
    slugs = [slug for slug, _ in tx.DEPARTMENTS]
    assert len(slugs) == len(set(slugs))


def test_every_satellite_parent_is_a_known_campus():
    campus_slugs = {slug for slug, _, kind, _ in tx.STANDORTE if kind == "campus"}
    for slug, _display, kind, parent in tx.STANDORTE:
        if kind == "satellite":
            assert parent in campus_slugs, f"{slug} -> unknown parent {parent}"


def test_satellite_rules_reference_defined_satellites():
    standort_slugs = {slug for slug, _, _, _ in tx.STANDORTE}
    for sat_slug, patterns, parent in tx.SATELLITE_RULES:
        assert sat_slug in standort_slugs
        assert patterns  # non-empty
        assert parent in standort_slugs


def test_site_to_standort_covers_every_configured_site():
    # The 11 allowed_domain values from config.toml.
    configured = {
        "heidenheim.dhbw.de",
        "www.dhbw.de",
        "mannheim.dhbw.de",
        "dhbw-stuttgart.de",
        "karlsruhe.dhbw.de",
        "mosbach.dhbw.de",
        "heilbronn.dhbw.de",
        "ravensburg.dhbw.de",
        "dhbw-loerrach.de",
        "dhbw-vs.de",
        "cas.dhbw.de",
    }
    assert configured <= set(tx.SITE_TO_STANDORT)
    base_slugs = {slug for slug, _, _, _ in tx.STANDORTE}
    for base in tx.SITE_TO_STANDORT.values():
        assert base in base_slugs


def test_department_rules_and_programs_map_to_known_faculties():
    for _substr, dept in tx.DEPARTMENT_URL_RULES:
        assert dept in DEPT_SLUGS
    assert set(tx.DEPARTMENT_KEYWORDS) <= DEPT_SLUGS
    for _slug, _display, dept, patterns in tx.STUDY_PROGRAMS:
        assert dept in DEPT_SLUGS and dept != "unknown"
        assert patterns
