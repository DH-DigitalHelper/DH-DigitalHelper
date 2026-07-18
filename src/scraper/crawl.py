"""Phase 1: queue-driven crawl with conditional-GET change detection."""

from __future__ import annotations

from . import _engine
from .progress import Progress


def _engine_config(config) -> dict:
    """Flatten the typed Config into the dict the Rust engine consumes."""
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
    """Run Phase 1 via the Rust engine and return per-site counts."""
    if progress is None:
        progress = Progress()
    return _engine.run_fetch(_engine_config(config), run_id, progress)
