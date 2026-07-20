import re
from pathlib import Path

import pytest

from scraper.config import ChunkConfig, DedupConfig, EmbeddingConfig, load_config


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
    assert cfg.chunk == ChunkConfig(target_words=500, overlap_words=75, batch_size=250)
    assert cfg.embedding == EmbeddingConfig(
        model="jinaai/jina-embeddings-v2-base-de",
        cpu_batch_size=8,
        gpu_batch_size=16,
        cache_dir=(tmp_path / "data/models").resolve(),
        device="cpu",
    )


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


def test_load_config_accepts_force_full(tmp_path):
    cfg = load_config(_write(tmp_path, "force-full"))
    assert cfg.crawl.recheck == "force-full"


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


def test_load_config_retry_transient_errors_defaults_true(tmp_path):
    cfg = load_config(_write(tmp_path, "all"))
    assert cfg.crawl.retry_transient_errors is True


def _write_raw(tmp_path, *, crawl="", extract="", dedup=""):
    """Write a minimal config with extra lines spliced into a section, so a test can state just the one key it is about."""
    (tmp_path / "config.toml").write_text(
        f"""
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
{crawl}
[extract]
{extract}
[dedup]
{dedup}
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )
    return tmp_path / "config.toml"


def test_load_config_dedup_section_is_optional(tmp_path):
    """Every [dedup] key defaults, so a config predating the section stays valid."""
    cfg = load_config(_write(tmp_path, "all"))
    assert cfg.dedup == DedupConfig(batch_size=500, vacuum=True)


def test_load_config_parses_dedup(tmp_path):
    cfg = load_config(_write_raw(tmp_path, dedup="batch_size = 250\nvacuum = false"))
    assert (cfg.dedup.batch_size, cfg.dedup.vacuum) == (250, False)


def test_load_config_parses_retry_transient_errors_false(tmp_path):
    cfg = load_config(_write_raw(tmp_path, crawl="retry_transient_errors = false"))
    assert cfg.crawl.retry_transient_errors is False


@pytest.mark.parametrize(
    "section,body,key",
    [
        ("crawl", "workers_per_host = 0", "crawl.workers_per_host"),
        ("crawl", "workers_per_host = -1", "crawl.workers_per_host"),
        ("crawl", "request_delay_seconds = -0.5", "crawl.request_delay_seconds"),
        ("crawl", "max_pages = -1", "crawl.max_pages"),
        ("crawl", "max_pages_per_host = -1", "crawl.max_pages_per_host"),
        ("extract", "workers = 0", "extract.workers"),
        ("extract", "min_words = -1", "extract.min_words"),
        ("dedup", "batch_size = 0", "dedup.batch_size"),
    ],
)
def test_load_config_rejects_out_of_range(tmp_path, section, body, key):
    """The message must name the offending key -- the whole point of validating here rather than letting the engine silently floor it."""
    with pytest.raises(ValueError, match=re.escape(key)):
        load_config(_write_raw(tmp_path, **{section: body}))


@pytest.mark.parametrize(
    "section,body",
    [
        ("crawl", "max_pages = 0"),
        ("crawl", "max_pages_per_host = 0"),
        ("crawl", "request_delay_seconds = 0.0"),
        ("crawl", "workers_per_host = 1"),
        ("extract", "min_words = 0"),
        ("dedup", "batch_size = 1"),
    ],
)
def test_load_config_accepts_boundary_values(tmp_path, section, body):
    """0 is meaningful for the budgets (= unlimited), so the floors must not be 1."""
    load_config(_write_raw(tmp_path, **{section: body}))


def test_load_config_parses_embedding_section(tmp_path):
    path = _write_raw(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            """
[embedding]
model = "custom/model"
device = "cuda"
cpu_batch_size = 3
gpu_batch_size = 24
cache_dir = "cache/models"
"""
        )

    cfg = load_config(path)

    assert cfg.embedding.model == "custom/model"
    assert cfg.embedding.device == "cuda"
    assert cfg.embedding.cpu_batch_size == 3
    assert cfg.embedding.gpu_batch_size == 24
    assert cfg.embedding.cache_dir == (tmp_path / "cache/models").resolve()


def test_load_config_rejects_unknown_embedding_device(tmp_path):
    path = _write_raw(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('\n[embedding]\ndevice = "amd"\n')

    with pytest.raises(ValueError, match="embedding.device"):
        load_config(path)


@pytest.mark.parametrize(
    "body,key",
    [
        ("cpu_batch_size = 0", "embedding.cpu_batch_size"),
        ("gpu_batch_size = 0", "embedding.gpu_batch_size"),
        ('model = "   "', "embedding.model"),
    ],
)
def test_load_config_rejects_invalid_embedding_values(tmp_path, body, key):
    path = _write_raw(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[embedding]\n{body}\n")

    with pytest.raises(ValueError, match=re.escape(key)):
        load_config(path)
