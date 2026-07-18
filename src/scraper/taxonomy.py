"""Reference data for classifying documents into Standort / Studienabteilung / Studiengang."""

from __future__ import annotations

CLASSIFY_VERSION = 2

DEPARTMENTS: list[tuple[str, str]] = [
    ("technik", "Technik"),
    ("wirtschaft", "Wirtschaft"),
    ("sozialwesen", "Sozialwesen"),
    ("gesundheit", "Gesundheit"),
    ("unknown", "Unbekannt"),
]

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

ENUMERATION_URL_MARKERS: tuple[str, ...] = (
    "liste-dualer-partner",
    "/unternehmen/",
    "detailansicht",
)

SATELLITE_RULES: list[tuple[str, tuple[str, ...], str]] = [
    ("stuttgart-horb", ("/horb/", "/horb-", "horb.", "dateien-horb"), "stuttgart"),
    (
        "ravensburg-friedrichshafen",
        ("/fn/", "/fn-", "campus-friedrichshafen", "technikcampus-friedrichshafen"),
        "ravensburg",
    ),
    ("mosbach-bad-mergentheim", ("bad-mergentheim", "mergentheim"), "mosbach"),
]

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

STUDY_PROGRAMS: list[tuple[str, str, str, tuple[str, ...]]] = [
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
    ("informatik", "Informatik", "technik", ("informatik",)),
    ("soziale-arbeit", "Soziale Arbeit", "sozialwesen", ("soziale-arbeit",)),
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
