"""Load and validate config.toml into typed dataclasses.

Paths resolve relative to the directory containing config.toml, so the tool
works from any working directory.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


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
    # Per-hostname page budget (0 = unlimited). Defaulted because the config.toml key
    # is optional; every caller passes fields by keyword, so this trailing position
    # carries no meaning -- keep it that way and the field order stays free to change.
    max_pages_per_host: int = 0


@dataclass(frozen=True)
class ExtractConfig:
    workers: int
    min_words: int


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
    storage: StorageConfig


def find_config(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        cfg = candidate / "config.toml"
        if cfg.is_file():
            return cfg
    raise FileNotFoundError("config.toml not found in current directory or any parent.")


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

    recheck = str(crawl_raw.get("recheck", "all"))
    if recheck not in {"all", "changed-only", "new-only"}:
        raise ValueError(
            "crawl.recheck must be one of 'all', 'changed-only', 'new-only'; "
            f"got {recheck!r}"
        )

    return Config(
        root=root,
        sites=sites,
        crawl=CrawlConfig(
            use_sitemap=bool(crawl_raw.get("use_sitemap", True)),
            max_pages=int(crawl_raw.get("max_pages", 0)),
            max_pages_per_host=int(crawl_raw.get("max_pages_per_host", 0)),
            request_delay_seconds=float(crawl_raw.get("request_delay_seconds", 1.0)),
            respect_robots=bool(crawl_raw.get("respect_robots", False)),
            workers_per_host=int(crawl_raw.get("workers_per_host", 1)),
            recheck=recheck,
            user_agent=crawl_raw["user_agent"],
        ),
        extract=ExtractConfig(
            workers=int(extract_raw.get("workers", 4)),
            min_words=int(extract_raw.get("min_words", 50)),
        ),
        storage=StorageConfig(
            db_file=resolve(storage_raw["db_file"]),
            raw_dir=resolve(storage_raw["raw_dir"]),
        ),
    )
