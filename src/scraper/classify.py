"""Pure, deterministic classification of a document into Standort / Studienabteilung / Studiengang."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import taxonomy

_SATELLITE_SLUGS = frozenset(
    slug for slug, _display, kind, _parent in taxonomy.STANDORTE if kind == "satellite"
)


def _norm_url(url: str) -> str:
    """Lowercase and fold '_' to '-' so underscore paths match the hyphenated taxonomy slugs."""
    return url.lower().replace("_", "-")


def _is_enumeration(low_url: str) -> bool:
    """True for dual-partner company-directory or enumeration listing URLs, which are never a campus or study-program page."""
    return any(marker in low_url for marker in taxonomy.ENUMERATION_URL_MARKERS)


def _path_segments(low_url: str) -> list[str]:
    return urlsplit(low_url).path.split("/")


def _segment_matches(segments: list[str], slug: str) -> bool:
    """True when the slug matches a whole path segment or the head of one (segment == slug, or segment starts with slug + '-')."""
    return any(seg == slug or seg.startswith(slug + "-") for seg in segments)


def _base_from_host(url: str) -> str | None:
    """Longest-suffix match of the URL host against the known allowed_domains, so a subdomain resolves to its campus."""
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
        return base
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
    """First seed-catalog entry whose slug matches a URL path segment, else None."""
    low = _norm_url(url)
    if _is_enumeration(low):
        return None
    segments = _path_segments(low)
    for slug, display, dept, patterns in taxonomy.STUDY_PROGRAMS:
        if any(_segment_matches(segments, p) for p in patterns):
            return slug, display, dept
    return None


def _department_with_source(url: str, title: str, description: str) -> tuple[str, str]:
    """Classify the department by URL rules first, else keyword matching over title+description, returning (department_slug, source)."""
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
    """Classify the department by URL rules first, else keyword matching over title+description; a tie yields 'unknown'."""
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
