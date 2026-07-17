import io
import threading

from scraper.progress import Progress


class Clock:
    """Manually-advanced monotonic clock so throttling is deterministic."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_tty_frame_shows_each_site_and_a_total():
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=0.0)
    p.update({"fetched": 2000, "new": 44}, "https://a/x", key="site-a.de")
    clk.t = 1.0
    p.update({"fetched": 50, "new": 3}, "https://b/y", key="site-b.de")
    out = buf.getvalue()
    assert "site-a.de" in out
    assert "site-b.de" in out
    assert "TOTAL" in out
    assert "2050" in out  # total fetched = 2000 + 50, never a flickered value


def test_total_reflects_latest_per_site_counts_not_a_flicker():
    """The historical bug: three sites stomping one status line made the number
    appear to jump 10 -> 2000 -> 50. Now each site keeps its own row and the
    total is their sum, so the last frame is stable and correct."""
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=0.0)
    p.update({"fetched": 10}, "u", key="a")
    clk.t = 1.0
    p.update({"fetched": 2000}, "u", key="b")
    clk.t = 2.0
    p.update({"fetched": 50}, "u", key="c")
    last_frame = buf.getvalue().split("\033[J")[-1]
    assert "2060" in last_frame  # 10 + 2000 + 50, all three rows still present
    assert "a" in last_frame and "b" in last_frame and "c" in last_frame


def test_row_shows_queued_remaining_when_provided():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=True, clock=Clock(), interval=0.0)
    p.update({"fetched": 5}, "u", key="site-a.de", queued=312)
    assert "312 queued" in buf.getvalue()


def test_total_sums_queued_across_sites():
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=0.0)
    p.update({"fetched": 5}, "u", key="a", queued=300)
    clk.t = 1.0
    p.update({"fetched": 5}, "u", key="b", queued=12)
    last_frame = buf.getvalue().split("\033[J")[-1]
    assert "312 queued" in last_frame  # 300 + 12, in the TOTAL row


def test_single_track_line_shows_processing_rate():
    """With one track (e.g. the extract phase, key=''), there is no TOTAL row,
    so the throughput (documents/second) must be shown on the sole line itself."""
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=0.0)
    p.update({"indexed": 4}, "sha", key="")
    clk.t = 1.0
    p.update({"indexed": 12}, "sha", key="")  # +8 items over 1s -> 8/s
    last_frame = buf.getvalue().split("\033[J")[-1]
    assert "8/s" in last_frame


def test_queued_omitted_when_not_provided():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=True, clock=Clock(), interval=0.0)
    p.update({"fetched": 5}, "u", key="a")  # no queued arg (e.g. extract phase)
    assert "queued" not in buf.getvalue()


def test_updates_within_one_interval_repaint_at_most_once():
    """A thousand workers hammering update() must not cause a thousand
    flush()es. Within one throttle interval only the first update paints."""
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=10.0)
    for i in range(20):
        p.update({"fetched": i}, "u", key="a")  # all at clk.t == 0.0
    assert buf.getvalue().count("\033[J") == 1  # one repaint, not twenty


def test_interval_elapsed_allows_a_new_repaint():
    buf = io.StringIO()
    clk = Clock()
    p = Progress(stream=buf, is_tty=True, clock=clk, interval=10.0)
    p.update({"fetched": 1}, "u", key="a")  # paints (t=0)
    p.update({"fetched": 2}, "u", key="a")  # throttled
    clk.t = 10.0
    p.update({"fetched": 3}, "u", key="a")  # interval elapsed -> paints again
    assert buf.getvalue().count("\033[J") == 2


def test_non_tty_never_emits_carriage_returns_or_cursor_moves():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=False, clock=Clock(), interval=0.0)
    p.header("Crawling x")
    p.update({"fetched": 3, "new": 1}, "u", key="a")
    p.note("dropped abc")
    p.summary("Done", {"fetched": 3})
    out = buf.getvalue()
    assert "\r" not in out
    assert "\033[" not in out  # no ANSI cursor control when piped
    assert "Crawling x" in out and "dropped abc" in out and "Done" in out


def test_summary_erases_live_block_and_prints_final_line():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=True, clock=Clock(), interval=0.0)
    p.update({"fetched": 5}, "u", key="site-a.de")
    p.summary("site-a.de", {"fetched": 5, "new": 2})
    assert "site-a.de: fetched 5 | new 2" in buf.getvalue()


def test_concurrent_updates_are_serialized_not_torn():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=False, clock=Clock(), interval=0.0)

    def worker(n):
        for i in range(100):
            p.update({"fetched": i}, f"u{i}", key=f"site-{n}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    val = buf.getvalue()
    assert val.endswith("\n")
    # Every emitted line is a complete, well-formed aggregate line -- a torn
    # write from an unserialized stream would produce a line without the marker.
    for line in val.splitlines():
        assert "fetched" in line
