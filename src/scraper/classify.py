"""Pure, deterministic classification of a document into Standort /
Studienabteilung / Studiengang. No DB, no I/O, no network -- classify(url, site,
doc) is a pure function of its inputs, so it is unit-testable from fixtures and
safe to call from both the Phase-2 write path and the one-time backfill."""

from __future__ import annotations

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
