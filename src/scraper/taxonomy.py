"""Reference data for classifying documents into Standort / Studienabteilung /
Studiengang. Pure data + version, no behavior (see classify.py). This is the ONE
place rules live; bump CLASSIFY_VERSION when they change so a re-run of the
reclassify pass is meaningfully different. NOT config.toml (which is tuning-only)."""

from __future__ import annotations

CLASSIFY_VERSION = 2

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

# URL markers of the dual-partner company directory / enumeration listing pages. A
# page under any of these is a company/enumeration stub, never a campus or study
# program page, so satellite promotion and program detection are suppressed on it
# (the base campus is kept; department URL rules still apply). See classify.py.
ENUMERATION_URL_MARKERS: tuple[str, ...] = (
    "liste-dualer-partner",
    "/unternehmen/",
    "detailansicht",
)

# (satellite_slug, url_substrings, parent_slug). Applied only when the doc's base
# standort == parent_slug, so "horb" never mis-tags a non-Stuttgart page. Patterns
# must be path-anchored: a bare city name ("friedrichshafen") saturates the
# dual-partner directory, so match "/fn/" or "campus-friedrichshafen" instead.
SATELLITE_RULES: list[tuple[str, tuple[str, ...], str]] = [
    ("stuttgart-horb", ("/horb/", "/horb-", "horb.", "dateien-horb"), "stuttgart"),
    (
        "ravensburg-friedrichshafen",
        ("/fn/", "/fn-", "campus-friedrichshafen", "technikcampus-friedrichshafen"),
        "ravensburg",
    ),
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
    # Verified path-scoped recovery rules (audit §B4): ~1,686 genuine faculty pages
    # sat in 'unknown' because the rules above were too narrow. These run before the
    # keyword fallback, so they also override the news-list keyword leak (§B2). Keep
    # 'bwl-' as a URL rule (not a bare 'bwl' text keyword) for precision.
    ("/bachelor-studienangebot/technik", "technik"),
    ("/bachelor-studienangebot/wirtschaft", "wirtschaft"),
    ("/bachelor-studienangebot/gesundheit", "gesundheit"),
    ("/bachelor-studienangebot/sozialwesen", "sozialwesen"),
    ("bwl-", "wirtschaft"),
    ("bauingenieurwesen", "technik"),
    ("holztechnik", "technik"),
    ("/technik/", "technik"),
    ("/wirtschaft/", "wirtschaft"),
    ("/gesundheit/", "gesundheit"),
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

# (slug, display_name, department_slug, url_path_slugs), priority order. Seed
# catalog — grows over time. A detected program sets the faculty (precedence).
# Patterns are matched as whole URL path SEGMENTS (see classify._segment_matches),
# so "informatik" matches /informatik/ but not an "xyz-informatik-gmbh" company or
# a "10-informatiktag" event; '_'->'-' normalization makes underscore paths match.
# NOTE: faculty assignments for programs that vary by campus (informatik,
# wirtschaftsingenieurwesen) are best-effort — one-column edits + a reclassify.
STUDY_PROGRAMS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # --- Technik ---
    ("maschinenbau", "Maschinenbau", "technik", ("maschinenbau",)),
    ("elektrotechnik", "Elektrotechnik", "technik", ("elektrotechnik",)),
    ("mechatronik", "Mechatronik", "technik", ("mechatronik",)),
    ("bauingenieurwesen", "Bauingenieurwesen", "technik", ("bauingenieurwesen",)),
    ("holztechnik", "Holztechnik", "technik", ("holztechnik",)),
    ("papiertechnik", "Papiertechnik", "technik", ("papiertechnik",)),
    ("medizintechnik", "Medizintechnik", "technik", ("medizintechnik",)),
    (
        "wirtschaftsingenieurwesen",
        "Wirtschaftsingenieurwesen",
        "technik",
        ("wirtschaftsingenieurwesen",),
    ),
    (
        "angewandte-informatik",
        "Angewandte Informatik",
        "technik",
        ("angewandte-informatik",),
    ),
    # --- Wirtschaft ('wirtschaftsinformatik' before the generic 'informatik') ---
    (
        "wirtschaftsinformatik",
        "Wirtschaftsinformatik",
        "wirtschaft",
        ("wirtschaftsinformatik",),
    ),
    ("bwl-industrie", "BWL – Industrie", "wirtschaft", ("bwl-industrie",)),
    (
        "betriebswirtschaftslehre",
        "Betriebswirtschaftslehre",
        "wirtschaft",
        ("betriebswirtschaftslehre",),
    ),
    ("food-management", "Food Management", "wirtschaft", ("food-management",)),
    # 'informatik' AFTER 'wirtschaftsinformatik' so the more specific program wins
    # if a path ever holds both; segment matching already keeps them distinct.
    ("informatik", "Informatik", "technik", ("informatik",)),
    # --- Sozialwesen ---
    ("soziale-arbeit", "Soziale Arbeit", "sozialwesen", ("soziale-arbeit",)),
    # --- Gesundheit (replaces the dead 'angewandte-gesundheitswissenschaften' seed;
    # '_'->'-' normalization now makes Angewandte_Gesundheitswissenschaften match) ---
    (
        "angewandte-gesundheitswissenschaften",
        "Angewandte Gesundheitswissenschaften",
        "gesundheit",
        ("angewandte-gesundheitswissenschaften",),
    ),
    (
        "hebammenwissenschaft",
        "Hebammenwissenschaft",
        "gesundheit",
        ("hebammenwissenschaft", "angewandte-hebammenwissenschaft"),
    ),
    ("pflegewissenschaft", "Pflegewissenschaft", "gesundheit", ("pflegewissenschaft",)),
    ("physiotherapie", "Physiotherapie", "gesundheit", ("physiotherapie",)),
]
