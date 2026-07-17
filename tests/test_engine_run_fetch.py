"""End-to-end test of the Rust Phase-1 engine driven through the Python adapter.

Serves a tiny fixture site from a local ``ThreadingHTTPServer``, runs
``crawl.run_fetch`` against it (real HTTP via reqwest inside the extension), and
asserts the shared SQLite DB + content-addressed raw cache — the cross-language
contract Phase 2 relies on.

Skipped automatically if the ``_engine`` extension has not been built yet
(``maturin develop``).
"""

from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip(
    "scraper._engine",
    reason="Rust extension not built; run `maturin develop` first.",
)

from scraper import crawl  # noqa: E402
from scraper.config import (  # noqa: E402
    Config,
    CrawlConfig,
    ExtractConfig,
    Site,
    StorageConfig,
)

PAGES = {
    "/startseite": (
        "text/html",
        """<html><body>seed
          <a href="/a">a</a>
          <a href="/b">b</a>
          <a href="/a">dup</a>
          <a href="/calendar/view.php?view=month&time=1">trap</a>
          <a href="http://example.invalid/x">external</a>
        </body></html>""",
    ),
    "/a": ("text/html", '<html><body>page a <a href="/b">b</a><a href="/c">c</a></body></html>'),
    "/b": ("text/html", '<html><body>page b <a href="/a">a</a></body></html>'),
    "/c": ("text/html", "<html><body>page c leaf content here</body></html>"),
    "/from-sitemap": ("text/html", "<html><body>sitemap-only leaf page</body></html>"),
    "/sitemap.xml": (
        "application/xml",
        """<urlset>
          <url><loc>http://HOST/from-sitemap</loc></url>
          <url><loc>http://HOST/startseite</loc></url>
        </urlset>""",
    ),
}


def _make_handler(host: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence stderr noise
            pass

        def do_GET(self):
            path = self.path
            if path not in PAGES:
                self.send_response(404)
                self.end_headers()
                return
            content_type, body = PAGES[path]
            body = body.replace("HOST", host).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _config(tmp_path, host: str, recheck="all", max_pages=0) -> Config:
    return Config(
        root=tmp_path,
        sites=[Site("t", f"http://{host}/startseite", "127.0.0.1")],
        crawl=CrawlConfig(
            use_sitemap=True,
            max_pages=max_pages,
            request_delay_seconds=0.0,
            respect_robots=False,
            workers_per_host=4,
            recheck=recheck,
            user_agent="test-agent",
        ),
        extract=ExtractConfig(workers=1, min_words=3),
        storage=StorageConfig(
            db_file=tmp_path / "db.sqlite3", raw_dir=tmp_path / "raw"
        ),
    )


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler("PLACEHOLDER"))
    host = f"127.0.0.1:{srv.server_address[1]}"
    srv.RequestHandlerClass = _make_handler(host)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield host
    finally:
        srv.shutdown()


def test_run_fetch_crawls_and_writes_shared_db(tmp_path, server):
    host = server
    config = _config(tmp_path, host)

    counts = crawl.run_fetch(config, "run-it")

    c = counts["127.0.0.1"]
    assert c["fetched"] == 5, counts  # startseite, a, b, c, from-sitemap
    assert c["new"] == 5
    assert c["error"] == 0

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    conn.row_factory = sqlite3.Row

    done = conn.execute(
        "SELECT COUNT(*) c FROM queue WHERE work_state='done' AND present=1"
    ).fetchone()["c"]
    assert done == 5

    # Trap + external are recorded as edges but never enqueued.
    for absent in (f"http://{host}/calendar/view.php?view=month&time=1", "http://example.invalid/x"):
        n = conn.execute("SELECT COUNT(*) c FROM queue WHERE url=?", (absent,)).fetchone()["c"]
        assert n == 0, absent

    ext = conn.execute(
        "SELECT in_domain FROM links WHERE dst_url=?", ("http://example.invalid/x",)
    ).fetchone()
    assert ext is not None and ext["in_domain"] == 0

    raw_pending = conn.execute(
        "SELECT COUNT(*) c FROM raw_docs WHERE extract_state='pending'"
    ).fetchone()["c"]
    assert raw_pending == 5

    files = list((tmp_path / "raw").iterdir())
    assert len(files) == 5
    conn.close()


def test_new_only_rerun_fetches_nothing(tmp_path, server):
    host = server
    crawl.run_fetch(_config(tmp_path, host), "run-1")
    counts = crawl.run_fetch(_config(tmp_path, host, recheck="new-only"), "run-2")
    assert counts["127.0.0.1"]["fetched"] == 0


def test_backfill_links_rebuilds_edges_from_raw(tmp_path, server):
    """Crawl, wipe the link graph, then reconstruct it offline from the raw blobs
    on disk. The rebuilt edge set must match what the crawl originally wrote, with
    no network fetch (the fixture server would still answer, but backfill must not
    call it)."""
    host = server
    config = _config(tmp_path, host)
    crawl.run_fetch(config, "run-1")

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    before = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    assert before > 0
    original = set(
        conn.execute("SELECT src_url, dst_url, in_domain FROM links").fetchall()
    )
    # Also remember how many pages were fetched with a full body (all 5, all html).
    conn.execute("DELETE FROM links")
    conn.commit()
    conn.close()

    counts = crawl.backfill_links(config)

    # 5 present HTML pages re-parsed; every original edge re-inserted; none missing.
    assert counts["pages"] == 5, counts
    assert counts["raw_missing"] == 0, counts
    assert counts["edges"] == before, counts

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    rebuilt = set(
        conn.execute("SELECT src_url, dst_url, in_domain FROM links").fetchall()
    )
    conn.close()
    assert rebuilt == original, "backfill reconstructs the exact edge set"


def test_backfill_links_is_idempotent(tmp_path, server):
    host = server
    config = _config(tmp_path, host)
    crawl.run_fetch(config, "run-1")

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    total = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    conn.close()

    # Re-running over an already-populated graph inserts nothing new.
    counts = crawl.backfill_links(config)
    assert counts["edges"] == 0, counts

    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    after = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    conn.close()
    assert after == total
