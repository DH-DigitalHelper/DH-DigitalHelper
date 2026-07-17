import pytest

from scraper import cli


def test_parser_has_all_subcommands():
    p = cli.build_parser()
    # argparse exits on unknown; parse each known command
    for cmd in (
        ["fetch"],
        ["extract"],
        ["extract-html"],
        ["extract-pdf"],
        ["run"],
        ["stats"],
        ["reset-site", "--site", "x"],
        ["dedup"],
        ["delta", "--since", "2026-01-01"],
        ["backfill-links"],
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


def test_extract_html_honors_workers_override(tmp_path, monkeypatch):
    _write_config(tmp_path)
    captured = {}

    def fake_run_extract(config, source_type=None, **kwargs):
        captured["source_type"] = source_type
        captured["workers"] = config.extract.workers
        return {"indexed": 0, "rejected": 0, "error": 0}

    monkeypatch.setattr(cli.extract, "run_extract", fake_run_extract)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "extract-pdf", "--workers", "13"]
    )

    assert rc == 0
    assert captured == {"source_type": "pdf", "workers": 13}


def test_fetch_accepts_changed_only_flag():
    p = cli.build_parser()
    ns = p.parse_args(["fetch", "--changed-only"])
    assert ns.changed_only is True
    assert ns.full is False


def test_fetch_accepts_full_flag():
    p = cli.build_parser()
    ns = p.parse_args(["fetch", "--full"])
    assert ns.full is True
    assert ns.changed_only is False


def test_fetch_rejects_changed_only_and_full_together():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["fetch", "--changed-only", "--full"])


def test_fetch_accepts_workers_per_host_flag():
    p = cli.build_parser()
    ns = p.parse_args(["fetch", "--workers-per-host", "4"])
    assert ns.workers_per_host == 4


def test_fetch_workers_per_host_defaults_to_none():
    p = cli.build_parser()
    ns = p.parse_args(["fetch"])
    assert ns.workers_per_host is None


def test_run_accepts_workers_per_host_flag():
    p = cli.build_parser()
    ns = p.parse_args(["run", "--workers-per-host", "4"])
    assert ns.workers_per_host == 4


def test_run_rejects_changed_only_and_full_together():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--changed-only", "--full"])


def _write_config(tmp_path, recheck="all"):
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


def test_cmd_fetch_maps_full_to_recheck_force_full(tmp_path, monkeypatch):
    """``--full`` is carried entirely by the recheck value now. It used to set
    recheck="all" plus a separate force_full=True argument; the engine derives the
    validator-dropping from the one enum value instead."""
    _write_config(tmp_path, recheck="changed-only")
    captured = {}

    def fake_run_fetch(config, run_id, **kwargs):
        captured["recheck"] = config.crawl.recheck
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch", "--full"])

    assert rc == 0
    assert captured == {"recheck": "force-full"}


def test_cmd_fetch_maps_changed_only_to_recheck_changed_only(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["recheck"] = config.crawl.recheck
        captured["force_full"] = force_full
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "fetch", "--changed-only"]
    )

    assert rc == 0
    assert captured == {"recheck": "changed-only", "force_full": False}


def test_cmd_fetch_no_flag_leaves_config_recheck_as_loaded(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="changed-only")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["recheck"] = config.crawl.recheck
        captured["force_full"] = force_full
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch"])

    assert rc == 0
    assert captured == {"recheck": "changed-only", "force_full": False}


def test_cmd_fetch_maps_workers_per_host_to_config(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["workers_per_host"] = config.crawl.workers_per_host
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "fetch", "--workers-per-host", "5"]
    )

    assert rc == 0
    assert captured["workers_per_host"] == 5


def test_cmd_fetch_no_workers_per_host_flag_leaves_config_default(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["workers_per_host"] = config.crawl.workers_per_host
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch"])

    assert rc == 0
    assert captured["workers_per_host"] == 1  # config.toml default


def test_cmd_fetch_maps_new_only_to_recheck_new_only(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["recheck"] = config.crawl.recheck
        captured["force_full"] = force_full
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "fetch", "--new-only"])

    assert rc == 0
    assert captured == {"recheck": "new-only", "force_full": False}


def test_new_only_mutually_exclusive_with_full():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["fetch", "--new-only", "--full"])


def test_dedup_command_dispatches_with_flags(tmp_path, monkeypatch):
    _write_config(tmp_path)
    captured = {}

    def fake_run_dedup(conn, batch_size=500, vacuum=True):
        captured["batch_size"] = batch_size
        captured["vacuum"] = vacuum
        return {"backfilled": 0, "groups": 0, "deleted": 0, "before": 0, "after": 0}

    monkeypatch.setattr(cli.storage, "run_dedup", fake_run_dedup)
    rc = cli.main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "dedup",
            "--batch-size",
            "7",
            "--no-vacuum",
        ]
    )

    assert rc == 0
    assert captured == {"batch_size": 7, "vacuum": False}


def test_backfill_links_command_invokes_adapter(tmp_path, monkeypatch):
    _write_config(tmp_path)
    captured = {}

    def fake_backfill_links(config, **kwargs):
        captured["db_file"] = str(config.storage.db_file)
        return {"pages": 3, "edges": 7, "raw_missing": 0}

    monkeypatch.setattr(cli.crawl, "backfill_links", fake_backfill_links)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "backfill-links"])

    assert rc == 0
    assert captured["db_file"].endswith("db.sqlite3")


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

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
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

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
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
        cli.main(
            ["--config", str(tmp_path / "config.toml"), "fetch", "--site", "nope"]
        )


def test_cmd_fetch_maps_max_pages_per_host(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["mpph"] = config.crawl.max_pages_per_host
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "fetch",
            "--max-pages-per-host",
            "123",
        ]
    )
    assert rc == 0
    assert captured["mpph"] == 123


def test_cmd_fetch_maps_request_delay(tmp_path, monkeypatch):
    _write_config(tmp_path, recheck="all")
    captured = {}

    def fake_run_fetch(config, run_id, force_full=False, **kwargs):
        captured["delay"] = config.crawl.request_delay_seconds
        return {}

    monkeypatch.setattr(cli.crawl, "run_fetch", fake_run_fetch)
    rc = cli.main(
        ["--config", str(tmp_path / "config.toml"), "fetch", "--request-delay", "0.5"]
    )
    assert rc == 0
    assert captured["delay"] == 0.5


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
    assert calls == ["beta.de"]  # resolved from config name to allowed_domain
    assert "beta.de" in capsys.readouterr().out


def test_stats_command_prints_counts(tmp_path, capsys, monkeypatch):
    # minimal config on disk
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
