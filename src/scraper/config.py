"""Load and validate config.toml into typed dataclasses."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

RECHECK_MODES = ("all", "changed-only", "new-only", "force-full")


@dataclass(frozen=True)
class Site:
    name: str
    seed_url: str
    allowed_domain: str


@dataclass(frozen=True)
class CrawlConfig:
    use_sitemap: bool
    max_pages: int
    request_delay_seconds: float
    respect_robots: bool
    workers_per_host: int
    recheck: str
    user_agent: str
    max_pages_per_host: int = 0
    retry_transient_errors: bool = True


@dataclass(frozen=True)
class ExtractConfig:
    workers: int
    min_words: int


@dataclass(frozen=True)
class DedupConfig:
    batch_size: int = 500
    vacuum: bool = True


@dataclass(frozen=True)
class StorageConfig:
    db_file: Path
    raw_dir: Path


@dataclass(frozen=True)
class Config:
    root: Path
    sites: list[Site]
    crawl: CrawlConfig
    extract: ExtractConfig
    dedup: DedupConfig
    storage: StorageConfig


def find_config(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        cfg = candidate / "config.toml"
        if cfg.is_file():
            return cfg
    raise FileNotFoundError("config.toml not found in current directory or any parent.")


def _bounded(raw: dict, section: str, key: str, *, default, minimum, cast=int):
    """Read section.key, cast it, and reject anything below minimum."""
    value = cast(raw.get(key, default))
    if value < minimum:
        raise ValueError(f"{section}.{key} must be >= {minimum}; got {value!r}")
    return value


def load_config(path: Path | None = None) -> Config:
    cfg_path = (path or find_config()).resolve()
    root = cfg_path.parent
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)

    def resolve(p: str) -> Path:
        return (root / p).resolve()

    sites = [
        Site(name=s["name"], seed_url=s["seed_url"], allowed_domain=s["allowed_domain"])
        for s in data["sites"]
    ]
    if not sites:
        raise ValueError("config.toml must define at least one [[sites]] entry.")

    crawl_raw = data["crawl"]
    extract_raw = data["extract"]
    storage_raw = data["storage"]
    dedup_raw = data.get("dedup", {})

    recheck = str(crawl_raw.get("recheck", "all"))
    if recheck not in RECHECK_MODES:
        raise ValueError(
            "crawl.recheck must be one of "
            + ", ".join(repr(m) for m in RECHECK_MODES)
            + f"; got {recheck!r}"
        )

    return Config(
        root=root,
        sites=sites,
        crawl=CrawlConfig(
            use_sitemap=bool(crawl_raw.get("use_sitemap", True)),
            max_pages=_bounded(crawl_raw, "crawl", "max_pages", default=0, minimum=0),
            max_pages_per_host=_bounded(
                crawl_raw, "crawl", "max_pages_per_host", default=0, minimum=0
            ),
            request_delay_seconds=_bounded(
                crawl_raw,
                "crawl",
                "request_delay_seconds",
                default=1.0,
                minimum=0.0,
                cast=float,
            ),
            respect_robots=bool(crawl_raw.get("respect_robots", False)),
            workers_per_host=_bounded(
                crawl_raw, "crawl", "workers_per_host", default=1, minimum=1
            ),
            recheck=recheck,
            retry_transient_errors=bool(crawl_raw.get("retry_transient_errors", True)),
            user_agent=crawl_raw["user_agent"],
        ),
        extract=ExtractConfig(
            workers=_bounded(extract_raw, "extract", "workers", default=4, minimum=1),
            min_words=_bounded(
                extract_raw, "extract", "min_words", default=50, minimum=0
            ),
        ),
        dedup=DedupConfig(
            batch_size=_bounded(
                dedup_raw, "dedup", "batch_size", default=500, minimum=1
            ),
            vacuum=bool(dedup_raw.get("vacuum", True)),
        ),
        storage=StorageConfig(
            db_file=resolve(storage_raw["db_file"]),
            raw_dir=resolve(storage_raw["raw_dir"]),
        ),
    )
