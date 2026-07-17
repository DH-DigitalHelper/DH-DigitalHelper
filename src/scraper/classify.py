"""Pure, deterministic classification of a document into Standort /
Studienabteilung / Studiengang. No DB, no I/O, no network -- classify(url, site,
doc) is a pure function of its inputs, so it is unit-testable from fixtures and
safe to call from both the Phase-2 write path and the one-time backfill."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import taxonomy

_SATELLITE_SLUGS = frozenset(
    slug for slug, _display, kind, _parent in taxonomy.STANDORTE if kind == "satellite"
)


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
    hay = url.lower()
    for sat_slug, patterns, parent in taxonomy.SATELLITE_RULES:
        if parent == base and any(p in hay for p in patterns):
            return sat_slug
    return base


@dataclass
class Classification:
    standort: str | None
    department: str
    program: str | None
    program_display: str | None
    meta: dict


def classify_program(url, title, description, text) -> tuple[str, str, str] | None:
    """First seed-catalog entry whose substring appears in the url or title.
    Returns (slug, display_name, department_slug); None when nothing matches."""
    hay = f"{url}\n{title}".lower()
    for slug, display, dept, patterns in taxonomy.STUDY_PROGRAMS:
        if any(p in hay for p in patterns):
            return slug, display, dept
    return None


def _department_with_source(url, title, description, text) -> tuple[str, str]:
    """URL rules first (high precision); else content keywords over
    title+description+text with word-boundary matching; a tie -> 'unknown'.
    Returns (department_slug, source) where source is one of
    'url' | 'keyword' | 'default'."""
    low_url = url.lower()
    for substr, dept in taxonomy.DEPARTMENT_URL_RULES:
        if substr in low_url:
            return dept, "url"
    blob = f"{title}\n{description}\n{text}".lower()
    hits = {
        dept
        for dept, keywords in taxonomy.DEPARTMENT_KEYWORDS.items()
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords)
    }
    if len(hits) == 1:
        return next(iter(hits)), "keyword"
    return "unknown", "default"


def classify_department(url, title, description, text) -> str:
    """URL rules first (high precision); else content keywords over
    title+description+text with word-boundary matching; a tie -> 'unknown'."""
    return _department_with_source(url, title, description, text)[0]


def classify(url: str, site: str, doc: dict) -> Classification:
    title = doc.get("title") or ""
    meta = doc.get("metadata") or {}
    description = (meta.get("description") if isinstance(meta, dict) else None) or ""
    text = doc.get("text") or ""

    standort = classify_standort(url, site)
    standort_src = "url" if standort in _SATELLITE_SLUGS else "site"

    prog = classify_program(url, title, description, text)
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

    department, dept_src = _department_with_source(url, title, description, text)
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
