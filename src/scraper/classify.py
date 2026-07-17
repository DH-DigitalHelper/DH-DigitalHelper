"""Pure, deterministic classification of a document into Standort /
Studienabteilung / Studiengang. No DB, no I/O, no network -- classify(url, site,
doc) is a pure function of its inputs, so it is unit-testable from fixtures and
safe to call from both the Phase-2 write path and the reclassify maintenance pass.

Matching is over the URL (lowercased, '_'->'-' normalized) plus the document's
title/description -- never the page body text, whose article teasers on news-list
pages leak a faculty into it. Company/enumeration directory pages (the dual-partner
listing) are excluded from satellite and program detection: their slugs are
saturated with city and program names that are not about a campus or a program."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import taxonomy

_SATELLITE_SLUGS = frozenset(
    slug for slug, _display, kind, _parent in taxonomy.STANDORTE if kind == "satellite"
)


def _norm_url(url: str) -> str:
    """Lowercase and fold '_' to '-' so underscore paths (Angewandte_Gesundheits...)
    match the hyphenated slugs the taxonomy is written in."""
    return url.lower().replace("_", "-")


def _is_enumeration(low_url: str) -> bool:
    """True for dual-partner company-directory / enumeration listing URLs, which are
    never a campus or study-program page even though their slugs are saturated with
    city and program names (e.g. .../unternehmen/zf-friedrichshafen-ag-.../)."""
    return any(marker in low_url for marker in taxonomy.ENUMERATION_URL_MARKERS)


def _path_segments(low_url: str) -> list[str]:
    return urlsplit(low_url).path.split("/")


def _segment_matches(segments: list[str], slug: str) -> bool:
    """A slug matches only as a whole path segment (segment == slug) or as the head
    of one (segment startswith slug + '-'). This rejects coincidental substrings
    ('10-informatiktag', an 'xyz-informatik-gmbh' company) that a bare `in` test or
    a leading-'/' anchor would still catch, while matching '/informatik/' and
    '/informatik-technischer-vertrieb/'."""
    return any(seg == slug or seg.startswith(slug + "-") for seg in segments)


def _base_from_host(url: str) -> str | None:
    """Longest-suffix match of the URL host against the known allowed_domains, so
    a subdomain (events.mannheim.dhbw.de) resolves to its campus even when the
    row's `site` was not the exact allowed_domain."""
    host = (urlsplit(url).hostname or "").lower()
    best: tuple[str, str] | None = None
    for domain, slug in taxonomy.SITE_TO_STANDORT.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, slug)
    return best[1] if best else None


def classify_standort(url: str, site: str) -> str | None:
    base = taxonomy.SITE_TO_STANDORT.get(site) or _base_from_host(url)
    if base is None:
        return None
    low = _norm_url(url)
    if _is_enumeration(low):
        return base  # company/enumeration stub: keep the base campus, never a satellite
    for sat_slug, patterns, parent in taxonomy.SATELLITE_RULES:
        if parent == base and any(p in low for p in patterns):
            return sat_slug
    return base


@dataclass
class Classification:
    standort: str | None
    department: str
    program: str | None
    program_display: str | None
    meta: dict


def classify_program(url: str) -> tuple[str, str, str] | None:
    """First seed-catalog entry whose slug matches a URL path segment. URL-only and
    segment-anchored for precision; returns (slug, display_name, department_slug),
    or None when nothing matches or the page is a company/enumeration stub."""
    low = _norm_url(url)
    if _is_enumeration(low):
        return None
    segments = _path_segments(low)
    for slug, display, dept, patterns in taxonomy.STUDY_PROGRAMS:
        if any(_segment_matches(segments, p) for p in patterns):
            return slug, display, dept
    return None


def _department_with_source(url: str, title: str, description: str) -> tuple[str, str]:
    """URL rules first (high precision) over the normalized URL; else content
    keywords over title+description ONLY (never body text -- article teasers on
    news-list pages leak a faculty) with word-boundary matching; a tie -> 'unknown'.
    Returns (department_slug, source) where source is 'url' | 'keyword' | 'default'."""
    low_url = _norm_url(url)
    for substr, dept in taxonomy.DEPARTMENT_URL_RULES:
        if substr in low_url:
            return dept, "url"
    blob = f"{title}\n{description}".lower()
    hits = {
        dept
        for dept, keywords in taxonomy.DEPARTMENT_KEYWORDS.items()
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords)
    }
    if len(hits) == 1:
        return next(iter(hits)), "keyword"
    return "unknown", "default"


def classify_department(url: str, title: str, description: str) -> str:
    """URL rules first (high precision); else content keywords over title+description
    with word-boundary matching; a tie -> 'unknown'."""
    return _department_with_source(url, title, description)[0]


def classify(url: str, site: str, doc: dict) -> Classification:
    title = doc.get("title") or ""
    meta = doc.get("metadata") or {}
    description = (meta.get("description") if isinstance(meta, dict) else None) or ""

    standort = classify_standort(url, site)
    standort_src = "url" if standort in _SATELLITE_SLUGS else "site"

    prog = classify_program(url)
    if prog is not None:
        slug, display, dept = prog
        return Classification(
            standort,
            dept,
            slug,
            display,
            {
                "standort": standort_src,
                "department": "program",
                "program": "match",
                "version": taxonomy.CLASSIFY_VERSION,
            },
        )

    department, dept_src = _department_with_source(url, title, description)
    return Classification(
        standort,
        department,
        None,
        None,
        {
            "standort": standort_src,
            "department": dept_src,
            "program": None,
            "version": taxonomy.CLASSIFY_VERSION,
        },
    )
