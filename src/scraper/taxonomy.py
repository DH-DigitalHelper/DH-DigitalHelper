"""Reference data for classifying documents into Standort / Studienabteilung /
Studiengang. Pure data + version, no behavior (see classify.py). This is the ONE
place rules live; bump CLASSIFY_VERSION when they change so a re-run of the
backfill is meaningfully different. NOT config.toml (which is tuning-only)."""

from __future__ import annotations

CLASSIFY_VERSION = 1

# Fixed 5 faculties. 'unknown' is a real row so department_id is never NULL.
DEPARTMENTS: list[tuple[str, str]] = [
    ("technik", "Technik"),
    ("wirtschaft", "Wirtschaft"),
    ("sozialwesen", "Sozialwesen"),
    ("gesundheit", "Gesundheit"),
    ("unknown", "Unbekannt"),
]

# (slug, display_name, kind, parent_slug). kind: campus|satellite|central|cas.
STANDORTE: list[tuple[str, str, str, str | None]] = [
    ("heidenheim", "DHBW Heidenheim", "campus", None),
    ("mannheim", "DHBW Mannheim", "campus", None),
    ("stuttgart", "DHBW Stuttgart", "campus", None),
    ("karlsruhe", "DHBW Karlsruhe", "campus", None),
    ("mosbach", "DHBW Mosbach", "campus", None),
    ("heilbronn", "DHBW Heilbronn", "campus", None),
    ("ravensburg", "DHBW Ravensburg", "campus", None),
    ("loerrach", "DHBW Lörrach", "campus", None),
    ("villingen_schwenningen", "DHBW Villingen-Schwenningen", "campus", None),
    ("cas", "DHBW Center for Advanced Studies", "cas", None),
    ("dhbw", "DHBW (Zentrale)", "central", None),
    ("stuttgart-horb", "DHBW Stuttgart – Campus Horb", "satellite", "stuttgart"),
    (
        "ravensburg-friedrichshafen",
        "DHBW Ravensburg – Campus Friedrichshafen",
        "satellite",
        "ravensburg",
    ),
    (
        "mosbach-bad-mergentheim",
        "DHBW Mosbach – Campus Bad Mergentheim",
        "satellite",
        "mosbach",
    ),
]

# documents.site (the crawl's allowed_domain) -> base standort slug.
SITE_TO_STANDORT: dict[str, str] = {
    "heidenheim.dhbw.de": "heidenheim",
    "www.dhbw.de": "dhbw",
    "mannheim.dhbw.de": "mannheim",
    "dhbw-stuttgart.de": "stuttgart",
    "karlsruhe.dhbw.de": "karlsruhe",
    "mosbach.dhbw.de": "mosbach",
    "heilbronn.dhbw.de": "heilbronn",
    "ravensburg.dhbw.de": "ravensburg",
    "dhbw-loerrach.de": "loerrach",
    "dhbw-vs.de": "villingen_schwenningen",
    "cas.dhbw.de": "cas",
}

# (satellite_slug, url_substrings, parent_slug). Applied only when the doc's base
# standort == parent_slug, so "horb" never mis-tags a non-Stuttgart page.
SATELLITE_RULES: list[tuple[str, tuple[str, ...], str]] = [
    ("stuttgart-horb", ("/horb/", "/horb-", "horb."), "stuttgart"),
    ("ravensburg-friedrichshafen", ("friedrichshafen", "/fn/", "/fn-"), "ravensburg"),
    ("mosbach-bad-mergentheim", ("bad-mergentheim", "mergentheim"), "mosbach"),
]

# (url_substring, department_slug), priority order. Kept SPECIFIC to preserve
# precision (e.g. no bare "gesundheit", which would catch BWL-Gesundheitsmanagement).
DEPARTMENT_URL_RULES: list[tuple[str, str]] = [
    ("soziale-arbeit", "sozialwesen"),
    ("sozialwesen", "sozialwesen"),
    ("sozialpaedagogik", "sozialwesen"),
    ("gesundheitswissenschaft", "gesundheit"),
    ("angewandte-gesundheit", "gesundheit"),
    ("hebamme", "gesundheit"),
    ("physiotherapie", "gesundheit"),
    ("maschinenbau", "technik"),
    ("elektrotechnik", "technik"),
    ("mechatronik", "technik"),
    ("fakultaet-technik", "technik"),
    ("fakultaet-wirtschaft", "wirtschaft"),
    ("fakultaet-gesundheit", "gesundheit"),
    ("fakultaet-sozialwesen", "sozialwesen"),
    ("betriebswirtschaft", "wirtschaft"),
]

# department_slug -> word-boundary keywords, scanned over title+description+text
# ONLY when the URL rules find nothing. A tie across faculties -> 'unknown'.
DEPARTMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "technik": ("maschinenbau", "elektrotechnik", "mechatronik"),
    "wirtschaft": ("betriebswirtschaftslehre", "bwl"),
    "sozialwesen": ("sozialwesen", "sozialpädagogik", "sozialpaedagogik"),
    "gesundheit": (
        "gesundheitswissenschaften",
        "physiotherapie",
        "hebammenwissenschaft",
    ),
}

# (slug, display_name, department_slug, url_or_title_substrings), priority order.
# Seed catalog — grows over time. A detected program sets the faculty (precedence).
STUDY_PROGRAMS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("maschinenbau", "Maschinenbau", "technik", ("maschinenbau",)),
    ("elektrotechnik", "Elektrotechnik", "technik", ("elektrotechnik",)),
    ("mechatronik", "Mechatronik", "technik", ("mechatronik",)),
    (
        "wirtschaftsinformatik",
        "Wirtschaftsinformatik",
        "wirtschaft",
        ("wirtschaftsinformatik",),
    ),
    ("bwl-industrie", "BWL – Industrie", "wirtschaft", ("bwl-industrie",)),
    ("soziale-arbeit", "Soziale Arbeit", "sozialwesen", ("soziale-arbeit",)),
    (
        "angewandte-gesundheitswissenschaften",
        "Angewandte Gesundheitswissenschaften",
        "gesundheit",
        ("angewandte-gesundheitswissenschaften",),
    ),
]
