import pytest

from scraper import chromaDB, cli


def test_parser_has_all_subcommands():
    p = cli.build_parser()
    for cmd in (
        ["fetch"],
        ["extract"],
        ["extract-html"],
        ["extract-pdf"],
        ["run"],
        ["stats"],
        ["reset-site", "--site", "x"],
        ["dedup"],
        ["backfill"],
        ["chunk"],
        ["embedding-smoke", "--limit", "10"],
        ["index"],
        ["delta", "--since", "2026-01-01"],
        ["report"],
    ):
        ns = p.parse_args(cmd)
        assert ns.command == cmd[0]


@pytest.mark.parametrize(
    "command,expected_source_type",
    [("extract", None), ("extract-html", "html"), ("extract-pdf", "pdf")],
)
def test_extract_commands_dispatch_with_source_type(
    tmp_path, monkeypatch, command, expected_source_type
):
    _write_config(tmp_path)
    captured = {}

    def fake_run_extract(config, source_type=None, **kwargs):
        captured["source_type"] = source_type
        captured["workers"] = config.extract.workers
        return {"indexed": 0, "rejected": 0, "error": 0}

    monkeypatch.setattr(cli.extract, "run_extract", fake_run_extract)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), command])

    assert rc == 0
    assert captured["source_type"] == expected_source_type


def test_extract_reads_workers_from_config(tmp_path, monkeypatch):
    _write_config(tmp_path, extract_extra="workers = 13")
    captured = {}

    def fake_run_extract(config, source_type=None, **kwargs):
        captured["source_type"] = source_type
        captured["workers"] = config.extract.workers
        return {"indexed": 0, "rejected": 0, "error": 0}

    monkeypatch.setattr(cli.extract, "run_extract", fake_run_extract)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "extract-pdf"])

    assert rc == 0
    assert captured == {"source_type": "pdf", "workers": 13}


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "--max-pages", "5"],
        ["fetch", "--max-pages-per-host", "5"],
        ["fetch", "--workers-per-host", "4"],
        ["fetch", "--request-delay", "0.5"],
        ["fetch", "--changed-only"],
        ["fetch", "--new-only"],
        ["fetch", "--full"],
        ["run", "--max-pages", "5"],
        ["run", "--workers-per-host", "4"],
        ["run", "--workers", "4"],
        ["extract", "--workers", "4"],
        ["extract-html", "--workers", "4"],
        ["extract-pdf", "--workers", "4"],
        ["dedup", "--batch-size", "7"],
        ["dedup", "--no-vacuum"],
    ],
)
def test_removed_value_flags_are_rejected(argv):
    """config.toml is the sole source of tuning values."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def _write_config(
    tmp_path, recheck="all", crawl_extra="", extract_extra="", dedup_extra=""
):
    """The *_extra fragments splice extra keys into a section, so a test about one value states only that value."""
    (tmp_path / "config.toml").write_text(
        f"""
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
recheck = "{recheck}"
{crawl_extra}
[extract]
{extract_extra}
[dedup]
{dedup_extra}
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )


def test_cmd_fetch_uses_config_force_full(tmp_path, monkeypatch):
    """What --full used to do is now spelled in the file."""
    _write_config(tmp_path, recheck="force-full")
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        captured["recheck"] = config.crawl.recheck
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch"])

    assert rc == 0
    assert captured == {"recheck": "force-full"}


def test_cmd_fetch_passes_config_crawl_values_through_untouched(tmp_path, monkeypatch):
    """Every [crawl] value reaches the engine exactly as written, because nothing between the file and run_fetch may change one."""
    _write_config(
        tmp_path,
        recheck="new-only",
        crawl_extra="\n".join(
            [
                "max_pages = 7",
                "max_pages_per_host = 123",
                "workers_per_host = 5",
                "request_delay_seconds = 0.5",
                "use_sitemap = false",
            ]
        ),
    )
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        c = config.crawl
        captured.update(
            max_pages=c.max_pages,
            max_pages_per_host=c.max_pages_per_host,
            workers_per_host=c.workers_per_host,
            request_delay_seconds=c.request_delay_seconds,
            use_sitemap=c.use_sitemap,
            recheck=c.recheck,
        )
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch"])

    assert rc == 0
    assert captured == {
        "max_pages": 7,
        "max_pages_per_host": 123,
        "workers_per_host": 5,
        "request_delay_seconds": 0.5,
        "use_sitemap": False,
        "recheck": "new-only",
    }


def test_cmd_fetch_uses_config_recheck(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="changed-only")
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        captured["recheck"] = config.crawl.recheck
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch"])

    assert rc == 0
    assert captured == {"recheck": "changed-only"}


def test_dedup_command_uses_config_values(tmp_path, monkeypatch):
    _write_config(tmp_path, dedup_extra="batch_size = 7\nvacuum = false")
    captured = {}

    def fake_run_dedup(conn, batch_size=500, vacuum=True):
        captured["batch_size"] = batch_size
        captured["vacuum"] = vacuum
        return {"backfilled": 0, "groups": 0, "deleted": 0, "before": 0, "after": 0}

    monkeypatch.setattr(cli.storage, "run_dedup", fake_run_dedup)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "dedup"])

    assert rc == 0
    assert captured == {"batch_size": 7, "vacuum": False}


def test_backfill_command_dispatches_with_config_values(tmp_path, monkeypatch):
    _write_config(tmp_path, dedup_extra="batch_size = 9")
    captured = {}

    def fake_run_backfill(conn, raw_dir, batch_size=500):
        captured["raw_dir"] = str(raw_dir)
        captured["batch_size"] = batch_size
        return {"lang": 0, "final_url": 0, "titles": 0, "scanned": 0}

    monkeypatch.setattr(cli.storage, "run_backfill", fake_run_backfill)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "backfill"])

    assert rc == 0
    assert captured["batch_size"] == 9
    assert captured["raw_dir"].endswith("raw")


def _write_two_site_config(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
[[sites]]
name = "alpha"
seed_url = "https://alpha/"
allowed_domain = "alpha.de"
[[sites]]
name = "beta"
seed_url = "https://beta/"
allowed_domain = "beta.de"
[crawl]
user_agent = "ua"
recheck = "all"
[extract]
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )


def test_fetch_site_flag_is_repeatable():
    p = cli.build_parser()
    ns = p.parse_args(["fetch", "--site", "a", "--site", "b"])
    assert ns.site == ["a", "b"]


def test_run_accepts_site_flag():
    p = cli.build_parser()
    ns = p.parse_args(["run", "--site", "villingen_schwenningen"])
    assert ns.site == ["villingen_schwenningen"]


def test_cmd_fetch_site_filter_selects_only_named_site(tmp_path, monkeypatch):
    _write_two_site_config(tmp_path)
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        captured["sites"] = [s.name for s in config.sites]
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "fetch", "--site", "beta"]
    )
    assert rc == 0
    assert captured["sites"] == ["beta"]


def test_cmd_fetch_site_filter_matches_allowed_domain(tmp_path, monkeypatch):
    _write_two_site_config(tmp_path)
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        captured["sites"] = [s.name for s in config.sites]
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "fetch", "--site", "alpha.de"]
    )
    assert rc == 0
    assert captured["sites"] == ["alpha"]


def test_cmd_fetch_unknown_site_exits(tmp_path, monkeypatch):
    _write_two_site_config(tmp_path)
    monkeypatch.setattr(cli.crawl, "run_fetch", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        cli.main(["--config", str(tmp_path / "config.toml"), "fetch", "--site", "nope"])


def test_reset_site_requires_site():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["reset-site"])


def test_reset_site_command_dispatches_and_resolves_allowed_domain(
    tmp_path, monkeypatch, capsys
):
    _write_two_site_config(tmp_path)
    calls = []

    def fake_reset_site(conn, site):
        calls.append(site)
        return {"queue": 5, "crawl_log": 7, "documents": 2, "links": 1}

    monkeypatch.setattr(cli.storage, "reset_site", fake_reset_site)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "reset-site", "--site", "beta"]
    )
    assert rc == 0
    assert calls == ["beta.de"]
    assert "beta.de" in capsys.readouterr().out


def test_stats_command_prints_counts(tmp_path, capsys, monkeypatch):
    (tmp_path / "config.toml").write_text(
        """
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
[extract]
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "documents" in out


def test_embedding_smoke_uses_config_and_operational_flags(
    tmp_path, monkeypatch, capsys
):
    _write_config(tmp_path)
    captured = {}

    def fake_run_embedding_smoke(input_path, **kwargs):
        captured.update(input=input_path, **kwargs)
        return {
            "status": "ok",
            "tested_chunks": 3,
            "dimension": 768,
            "preview": {
                "chunk_id": "chunk-1",
                "text": "Beispieltext",
                "embedding_first_10": [0.1, 0.2],
            },
        }

    monkeypatch.setattr(cli.embedding, "run_embedding_smoke", fake_run_embedding_smoke)
    rc = cli.main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "embedding-smoke",
            "--device",
            "cpu",
            "--limit",
            "10",
        ]
    )

    assert rc == 0
    assert captured["input"] == (tmp_path / "db.sqlite3").resolve()
    assert captured["model_name"] == "jinaai/jina-embeddings-v2-base-de"
    assert captured["device"] == "cpu"
    assert captured["batch_size"] == 8
    assert captured["limit"] == 10
    output = capsys.readouterr().out
    assert "Embedding preview:" in output
    assert "chunk_id: chunk-1" in output
    assert "text: Beispieltext" in output
    assert "first 10 values: [0.1, 0.2]" in output
    assert '"embeddings"' not in output


def test_embedding_error_is_reported_without_traceback(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)

    def fail(*args, **kwargs):
        raise cli.embedding.EmbeddingError("model unavailable")

    monkeypatch.setattr(cli.embedding, "run_embedding_smoke", fail)
    rc = cli.main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "embedding-smoke",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err == "embedding failed: model unavailable\n"


def test_index_refreshes_chunks_and_synchronizes_configured_chroma(
    tmp_path, monkeypatch, capsys
):
    _write_config(tmp_path)
    captured = {}
    monkeypatch.setattr(
        cli.chunk,
        "run_chunking",
        lambda *args, **kwargs: {"documents": 1, "chunks": 2},
    )
    monkeypatch.setattr(
        chromaDB,
        "create_client",
        lambda **kwargs: captured.setdefault("client", kwargs),
    )
    monkeypatch.setattr(
        chromaDB,
        "get_collection",
        lambda client, name: captured.setdefault("collection", (client, name)),
    )

    def fake_index(collection, input_path, **kwargs):
        captured.update(input=input_path, index_kwargs=kwargs)
        return {"upserted": 2, "deleted": 1}

    monkeypatch.setattr(chromaDB, "index_chunks", fake_index)

    rc = cli.main(["--config", str(tmp_path / "config.toml"), "index"])

    assert rc == 0
    assert captured["client"]["mode"] == "persistent"
    assert captured["collection"][1] == "dhbw_corpus"
    assert captured["input"] == (tmp_path / "db.sqlite3").resolve()
    assert captured["index_kwargs"]["device"] == "cpu"
    assert '"deleted": 1' in capsys.readouterr().out


def test_index_reports_chroma_error_without_traceback(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    monkeypatch.setattr(
        cli.chunk,
        "run_chunking",
        lambda *args, **kwargs: {"documents": 0, "chunks": 0},
    )
    monkeypatch.setattr(
        chromaDB,
        "create_client",
        lambda **kwargs: (_ for _ in ()).throw(
            chromaDB.ChromaError("server unavailable")
        ),
    )

    rc = cli.main(["--config", str(tmp_path / "config.toml"), "index"])

    assert rc == 1
    assert capsys.readouterr().err == "indexing failed: server unavailable\n"
