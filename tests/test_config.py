from pathlib import Path

import pytest

from scraper.config import load_config


def test_load_config_parses_sites_and_sections(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        """
[[sites]]
name = "heidenheim"
seed_url = "https://www.heidenheim.dhbw.de/startseite"
allowed_domain = "heidenheim.dhbw.de"

[[sites]]
name = "dhbw"
seed_url = "https://www.dhbw.de"
allowed_domain = "www.dhbw.de"

[crawl]
use_sitemap = true
max_pages = 5
request_delay_seconds = 0.5
respect_robots = false
workers_per_host = 2
recheck = "changed-only"
user_agent = "ua"

[extract]
workers = 3
min_words = 40

[storage]
db_file = "data/db.sqlite3"
raw_dir = "data/raw"
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.toml")

    assert cfg.root == tmp_path
    assert [s.name for s in cfg.sites] == ["heidenheim", "dhbw"]
    assert cfg.sites[1].allowed_domain == "www.dhbw.de"
    assert cfg.crawl.max_pages == 5
    assert cfg.crawl.workers_per_host == 2
    assert cfg.crawl.recheck == "changed-only"
    assert cfg.extract.min_words == 40
    assert cfg.storage.db_file == (tmp_path / "data/db.sqlite3").resolve()
    assert cfg.storage.raw_dir == (tmp_path / "data/raw").resolve()


def _write(tmp_path, recheck):
    (tmp_path / "config.toml").write_text(
        f"""
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
recheck = "{recheck}"
[extract]
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )
    return tmp_path / "config.toml"


def test_load_config_accepts_new_only(tmp_path):
    cfg = load_config(_write(tmp_path, "new-only"))
    assert cfg.crawl.recheck == "new-only"


def test_load_config_rejects_unknown_recheck(tmp_path):
    with pytest.raises(ValueError, match="recheck"):
        load_config(_write(tmp_path, "sometimes"))


def test_load_config_max_pages_per_host_defaults_to_zero(tmp_path):
    cfg = load_config(_write(tmp_path, "all"))
    assert cfg.crawl.max_pages_per_host == 0


def test_load_config_parses_max_pages_per_host(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
max_pages_per_host = 50000
[extract]
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.crawl.max_pages_per_host == 50000
