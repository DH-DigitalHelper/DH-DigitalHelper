# `new-only` Recheck Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `recheck = "new-only"` crawl mode that fetches only queued URLs never fetched before (and everything they link to), never re-downloading a page already in the store.

**Architecture:** The whole feature is one claim-time predicate. "Never fetched" = `queue.last_checked_at IS NULL`. In `new-only` mode, `claim_pending_url` and `count_pending` append `AND last_checked_at IS NULL`, so already-downloaded pages — however they returned to `pending` — are simply un-claimable, while newly-discovered links (which `process_url` still enqueues) cascade normally.

**Tech Stack:** Python 3.11+ (stdlib `sqlite3`, `tomllib`, `argparse`, `concurrent.futures`), pytest, uv, ruff.

## Global Constraints

- Config dataclasses are `@dataclass(frozen=True)`; the CLI mutates loaded config via `object.__setattr__` (do not "fix" this).
- `CrawlConfig` is constructed with **positional** args in `tests/test_crawl.py::cfg`; do NOT reorder or insert fields in `CrawlConfig` — `recheck` stays the 6th field.
- Every storage function takes an explicit `conn`; workers each use their own connection. WAL mode; writers use `BEGIN IMMEDIATE`.
- Valid `recheck` values are exactly `{"all", "changed-only", "new-only"}`.
- Test timestamp constant in existing tests: `NOW = "2026-07-14T00:00:00"`.
- Run tests with `uv run pytest`.

---

### Task 1: Validate `recheck` and accept `new-only` in config loading

**Files:**
- Modify: `src/dhbw_scraper/config.py:78-93` (inside `load_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `load_config(path)` — existing.
- Produces: `load_config` raises `ValueError` for any `recheck` not in `{"all","changed-only","new-only"}`; accepts `"new-only"` and stores it on `CrawlConfig.recheck`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
import pytest


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_load_config_accepts_new_only tests/test_config.py::test_load_config_rejects_unknown_recheck -v`
Expected: `test_load_config_rejects_unknown_recheck` FAILS (no ValueError raised — `recheck` is currently unvalidated). `test_load_config_accepts_new_only` passes already but stays as a guard.

- [ ] **Step 3: Add validation in `load_config`**

In `src/dhbw_scraper/config.py`, replace the `crawl=CrawlConfig(...)` block's `recheck=str(crawl_raw.get("recheck", "all")),` line by first computing and validating the value just above the `return Config(` statement (after `storage_raw = data["storage"]`):

```python
    recheck = str(crawl_raw.get("recheck", "all"))
    if recheck not in {"all", "changed-only", "new-only"}:
        raise ValueError(
            "crawl.recheck must be one of 'all', 'changed-only', 'new-only'; "
            f"got {recheck!r}"
        )
```

Then change the `CrawlConfig(...)` argument from `recheck=str(crawl_raw.get("recheck", "all")),` to `recheck=recheck,`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/config.py tests/test_config.py
git commit -m "feat: validate crawl.recheck and accept new-only value"
```

---

### Task 2: Add `only_new` filter to `claim_pending_url` and `count_pending`

**Files:**
- Modify: `src/dhbw_scraper/storage.py:179-197` (`claim_pending_url`), `src/dhbw_scraper/storage.py:204-214` (`count_pending`)
- Test: `tests/test_storage_queue.py`

**Interfaces:**
- Consumes: `enqueue`, `claim_pending_url`, `count_pending`, `get_url_state` — existing.
- Produces:
  - `claim_pending_url(conn, site, only_new=False)` — when `only_new=True`, only returns pending rows with `last_checked_at IS NULL`.
  - `count_pending(conn, site=None, only_new=False)` — when `only_new=True`, counts only pending rows with `last_checked_at IS NULL`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage_queue.py`:

```python
def test_claim_only_new_skips_already_fetched_pending_rows():
    conn = mem()
    # `a` was fetched before and flipped back to pending (last_checked_at set).
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    # `b` has never been fetched.
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW)

    # only_new skips `a`, claims `b`.
    row = st.claim_pending_url(conn, "x", only_new=True)
    assert row["url"] == "https://x/b"
    # Nothing else new to claim; `a` is left untouched.
    assert st.claim_pending_url(conn, "x", only_new=True) is None
    assert st.get_url_state(conn, "https://x/a")["work_state"] == "pending"


def test_claim_default_still_claims_already_fetched_pending_rows():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    assert st.claim_pending_url(conn, "x")["url"] == "https://x/a"


def test_count_pending_only_new_counts_never_fetched():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW)
    assert st.count_pending(conn) == 2
    assert st.count_pending(conn, only_new=True) == 1
    assert st.count_pending(conn, "x", only_new=True) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage_queue.py -k "only_new or already_fetched" -v`
Expected: FAIL with `TypeError: claim_pending_url() got an unexpected keyword argument 'only_new'` (and the same for `count_pending`).

- [ ] **Step 3: Add the `only_new` parameter to both functions**

In `src/dhbw_scraper/storage.py`, replace `claim_pending_url`:

```python
def claim_pending_url(conn, site, only_new=False) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        extra = " AND last_checked_at IS NULL" if only_new else ""
        row = conn.execute(
            "SELECT * FROM queue WHERE site = ? AND work_state = 'pending'"
            + extra
            + " ORDER BY depth, url LIMIT 1",
            (site,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE queue SET work_state = 'in_progress' WHERE url = ?", (row["url"],)
        )
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

And replace `count_pending`:

```python
def count_pending(conn, site=None, only_new=False) -> int:
    extra = " AND last_checked_at IS NULL" if only_new else ""
    if site is None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending'" + extra
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending' AND site = ?"
            + extra,
            (site,),
        ).fetchone()
    return row["c"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage_queue.py -v`
Expected: PASS (all queue tests, including the three new ones).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/storage.py tests/test_storage_queue.py
git commit -m "feat: add only_new filter to claim_pending_url and count_pending"
```

---

### Task 3: Honor `new-only` in `crawl_site` (skip already-fetched, keep cascading)

**Files:**
- Modify: `src/dhbw_scraper/crawl.py:292-341` (the claim loop inside `crawl_site`)
- Test: `tests/test_crawl.py`

**Interfaces:**
- Consumes: `crawl.run_fetch(config, run_id, fetch_fn=..., clock=...)`, `storage.claim_pending_url(conn, site, only_new=...)`, `storage.count_pending(conn, site, only_new=...)` (Task 2), `storage.get_url_state`.
- Produces: no signature change. `crawl_site` derives `only_new = config.crawl.recheck == "new-only"` and threads it into its `claim_pending_url` and `count_pending` calls. `run_fetch` already gates `requeue_present_urls` on `recheck == "all" or force_full`, so `new-only` does not requeue — no change needed there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_crawl.py`:

```python
def test_run_fetch_new_only_skips_already_fetched_url(tmp_path):
    c = cfg(tmp_path, recheck="new-only")
    body = b"<html><body><p>" + b"content " * 60 + b"</p></body></html>"

    def fetch_fn(url, ua, etag=None, last_modified=None):
        return html_result(url, body)

    # First run downloads the seed.
    crawl.run_fetch(c, "run1", fetch_fn=fetch_fn, clock=lambda: NOW)

    # Simulate a change signal flipping the already-fetched seed back to pending.
    conn = st.connect(c.storage.db_file)
    conn.execute(
        "UPDATE queue SET work_state = 'pending' WHERE url = ?",
        (c.sites[0].seed_url,),
    )
    conn.commit()
    assert st.get_url_state(conn, c.sites[0].seed_url)["last_checked_at"] == NOW
    conn.close()

    # new-only must NOT re-fetch it (last_checked_at is set); it stays pending.
    counts = crawl.run_fetch(c, "run2", fetch_fn=fetch_fn, clock=lambda: NOW)
    assert counts[c.sites[0].name]["fetched"] == 0

    conn = st.connect(c.storage.db_file)
    assert st.get_url_state(conn, c.sites[0].seed_url)["work_state"] == "pending"
    conn.close()


def test_run_fetch_new_only_cascades_into_newly_discovered_links(tmp_path):
    c = cfg(tmp_path, recheck="new-only")
    body = (
        b"<html><body><p>"
        + b"real content " * 40
        + b'</p><a href="/studium">s</a></body></html>'
    )

    def fetch_fn(url, ua, etag=None, last_modified=None):
        return html_result(url, body)

    # Cold run: seed is never-fetched -> fetched, its link /studium is discovered,
    # is itself never-fetched, and cascades into a fetch in the same run.
    crawl.run_fetch(c, "run1", fetch_fn=fetch_fn, clock=lambda: NOW)

    conn = st.connect(c.storage.db_file)
    assert st.get_url_state(conn, "https://www.dhbw.de/studium")["work_state"] == "done"
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_crawl.py -k "new_only" -v`
Expected: `test_run_fetch_new_only_skips_already_fetched_url` FAILS — without the filter the flipped-back seed is re-claimed and `fetched == 1`, not `0`. (`...cascades...` may already pass since a cold run has no already-fetched rows; it guards the cascade path.)

- [ ] **Step 3: Thread `only_new` through the claim loop**

In `src/dhbw_scraper/crawl.py`, inside `crawl_site`, just after the `limiter` default block (right before `max_pages = config.crawl.max_pages`), add:

```python
    only_new = config.crawl.recheck == "new-only"
```

Then change the claim call from:

```python
            row = storage.claim_pending_url(conn, site.allowed_domain)
```

to:

```python
            row = storage.claim_pending_url(
                conn, site.allowed_domain, only_new=only_new
            )
```

And change the progress count from:

```python
                queued = storage.count_pending(conn, site.allowed_domain)
```

to:

```python
                queued = storage.count_pending(
                    conn, site.allowed_domain, only_new=only_new
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_crawl.py -v`
Expected: PASS (all crawl tests, including the two new ones — the existing `all`/`changed-only` run_fetch tests must remain green).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/crawl.py tests/test_crawl.py
git commit -m "feat: crawl_site honors new-only recheck (skip already-fetched, cascade new links)"
```

---

### Task 4: Add `--new-only` CLI flag to `fetch` and `run`

**Files:**
- Modify: `src/dhbw_scraper/cli.py:28-33` (`_cmd_fetch` mapping), `src/dhbw_scraper/cli.py:87-97` (`fetch` argparse group), `src/dhbw_scraper/cli.py:113-123` (`run` argparse group)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `cli.main(argv)`, `cli.crawl.run_fetch` (monkeypatched in tests), the existing mutually-exclusive recheck group.
- Produces: `--new-only` on both `fetch` and `run`; sets `config.crawl.recheck = "new-only"` with `force_full=False`; mutually exclusive with `--changed-only` and `--full`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
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
```

Note: the second test calls `cli.build_parser()` and expects `pytest` and `cli` already imported at the top of `tests/test_cli.py` (they are — see the existing `test ... p.parse_args(["run", "--changed-only", "--full"])` test). If the parser builder has a different name, match the name used by that existing mutually-exclusive test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "new_only" -v`
Expected: FAIL — argparse errors on the unknown `--new-only` flag (`SystemExit` in the first test before `run_fetch` is called), so `captured` is empty.

- [ ] **Step 3: Add the flag and mapping**

In `src/dhbw_scraper/cli.py`, in `_cmd_fetch`, extend the recheck mapping:

```python
    force_full = False
    if args.changed_only:
        object.__setattr__(config.crawl, "recheck", "changed-only")
    elif args.new_only:
        object.__setattr__(config.crawl, "recheck", "new-only")
    elif args.full:
        object.__setattr__(config.crawl, "recheck", "all")
        force_full = True
```

In the `fetch` subparser's `f_recheck` group, add (after the `--changed-only` argument, before `--full`):

```python
    f_recheck.add_argument(
        "--new-only",
        action="store_true",
        help="Only fetch queued URLs never fetched before; never re-download "
        "already-stored pages (recheck=new-only).",
    )
```

In the `run` subparser's `r_recheck` group, add the identical argument:

```python
    r_recheck.add_argument(
        "--new-only",
        action="store_true",
        help="Only fetch queued URLs never fetched before; never re-download "
        "already-stored pages (recheck=new-only).",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (all CLI tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/cli.py tests/test_cli.py
git commit -m "feat: add --new-only fetch/run flag"
```

---

### Task 5: Document `new-only` in `config.toml` and `README.md`

**Files:**
- Modify: `config.toml:69`, `README.md:110-116`, `README.md:132-134`
- Test: none (docs only; verified by inspection).

**Interfaces:**
- Consumes: nothing.
- Produces: user-facing documentation of the `new-only` mode and `--new-only` flag.

- [ ] **Step 1: Update the `config.toml` inline comment**

Change line 69 in `config.toml` from:

```toml
recheck = "changed-only"                       # "all" | "changed-only"
```

to:

```toml
recheck = "changed-only"                       # "all" | "changed-only" | "new-only"
```

- [ ] **Step 2: Update the CLI flags section in `README.md`**

Replace the `--full` bullet + mutual-exclusion sentence (lines 112-116) with:

```markdown
- `--full` — re-check every present URL and ignore stored `ETag`/`Last-Modified`
  validators, forcing a full re-download.
- `--new-only` — fetch only queued URLs never fetched before (and everything they link
  to); never re-download a page already in the store (same as `recheck = "new-only"`).

`--changed-only`, `--full`, and `--new-only` are mutually exclusive. With none of them,
the command falls back to `crawl.recheck` from `config.toml`.
```

- [ ] **Step 3: Update the Change-detection mode list in `README.md`**

After the `recheck = "changed-only"` bullet (line 134), add:

```markdown
- `recheck = "new-only"` — fetch only queued URLs never fetched before; a page already
  in the store is never re-downloaded even if a change signal fires. Newly-discovered
  links are still followed, so the crawl cascades forward into all new pages. Use this to
  drain remaining un-fetched work after a partial crawl without re-checking the corpus.
```

- [ ] **Step 4: Verify the docs render and no stale references remain**

Run: `grep -n "new-only" config.toml README.md`
Expected: matches in `config.toml` (the comment) and `README.md` (both new blocks).

- [ ] **Step 5: Commit**

```bash
git add config.toml README.md
git commit -m "docs: document new-only recheck mode and --new-only flag"
```

---

## Final verification

- [ ] Run the full suite: `uv run pytest`
  Expected: all tests pass.
- [ ] Lint: `uv run ruff check src tests && uv run ruff format --check src tests`
  Expected: clean (pre-commit runs ruff; keep it green).

## Self-Review notes

- **Spec coverage:** config validation + `new-only` (Task 1); claim/count filter core mechanism (Task 2); `crawl_site` threading + "no requeue" already satisfied by existing `all`/`force_full` gate (Task 3); `--new-only` CLI + mutual exclusion (Task 4); `config.toml` comment + README (Task 5). Spec's "Testing" items 1-2 → Task 2; item 3 → Task 1; item 4 → Task 3 skip test; item 5 → Task 3 cascade test.
- **"Never fetched" = `last_checked_at IS NULL`** is used identically across Tasks 2 and 3 (SQL predicate) and matches the spec's chosen definition.
- **Signature stability:** `CrawlConfig` field order unchanged (constraint honored); `claim_pending_url`/`count_pending` gain only a trailing defaulted `only_new` param, so all existing callers keep working.
- **No requeue special-casing:** confirmed `run_fetch` already gates `requeue_present_urls` on `recheck == "all" or force_full`, so `new-only` needs no change there — noted in Task 3 interfaces.
