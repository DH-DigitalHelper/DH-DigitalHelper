"""Phase 1: queue-driven crawl with conditional-GET change detection.

The crawl engine itself lives in Rust (`scraper._engine`, built from
``src/scrape-engine/``): a tokio async crawler with a single dedicated SQLite
writer task and an in-memory frontier, which owns all Phase-1 writes to the same
SQLite database Phase 2 reads. This module is now a thin adapter that maps the
parsed :class:`~scraper.config.Config` into the plain dict the extension expects
and forwards the call.

Phase 2 (extraction) stays in Python and is untouched.
"""

from __future__ import annotations

from . import _engine
from .progress import Progress


def _engine_config(config) -> dict:
    """Flatten the typed Config into the dict the Rust engine consumes.

    config.toml is the only source of these values and nothing overrides them on
    the way through, so this is a straight projection. ``respect_robots`` is
    intentionally omitted: it was never enforced in Phase 1.
    """
    c = config.crawl
    s = config.storage
    return {
        "sites": [
            {
                "name": site.name,
                "seed_url": site.seed_url,
                "allowed_domain": site.allowed_domain,
            }
            for site in config.sites
        ],
        "use_sitemap": c.use_sitemap,
        "max_pages": c.max_pages,
        "max_pages_per_host": c.max_pages_per_host,
        "request_delay_seconds": c.request_delay_seconds,
        "workers_per_host": c.workers_per_host,
        "recheck": c.recheck,
        "retry_transient_errors": c.retry_transient_errors,
        "user_agent": c.user_agent,
        "db_file": str(s.db_file),
        "raw_dir": str(s.raw_dir),
    }


def run_fetch(config, run_id, progress=None) -> dict:
    """Run Phase 1 via the Rust engine and return per-site counts.

    Everything the crawl needs is in ``config`` — including whether to drop the
    stored validators, which the engine derives from ``crawl.recheck ==
    "force-full"``. Testing uses the engine's own injectable HTTP client (see
    ``tests/scrape-engine``) plus the end-to-end fixture-server test in
    ``tests/test_engine_run_fetch.py``.
    """
    if progress is None:
        progress = Progress()
    return _engine.run_fetch(_engine_config(config), run_id, progress)
