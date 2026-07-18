# `new-only` Recheck Mode — Design

**Date:** 2026-07-14
**Status:** Approved design, pending implementation plan
**Builds on:** [2026-07-14-dhbw-dual-site-scraper-design.md](2026-07-14-dhbw-dual-site-scraper-design.md)

## Purpose

Add a third `[crawl] recheck` mode, `new-only`, that fetches only URLs which are
**currently queued and have never been fetched**, and never re-downloads a page
already in the store. Link extraction continues normally for every page that *is*
fetched, so the crawl cascades forward into all newly-discovered territory — it just
doesn't waste requests re-downloading what's already been captured.

Motivation: after an interrupted or partial crawl, the operator wants to drain the
remaining un-fetched work (and everything it links to) without paying to re-check the
tens of thousands of pages already downloaded.

## The three recheck modes

| Mode | Requeues already-fetched pages? | What gets fetched |
|------|-------------------------------|-------------------|
| `all` | Yes (full GET, ignores validators) | Every present URL + all pending |
| `changed-only` | No (but sitemap `<lastmod>` advances flip pages to pending → conditional GET) | Pending URLs, including change-signalled ones |
| **`new-only`** (new) | No | **Only pending URLs never fetched before** |

## Definition of "never fetched"

The signal is `queue.last_checked_at IS NULL`. Every fetch path in `process_url`
(`mark_url_checked`, `mark_url_error`, `mark_url_removed`) stamps `last_checked_at`, so
any URL that has ever been through a fetch has a non-null value — even a URL that later
flipped back to `pending` via a sitemap advance or a requeue.

Note this counts a page fetched-but-not-stored (e.g. a `kind == "other"` binary skipped
as a non-document) as **fetched** — it will not be re-fetched under `new-only`. That is
intentional: "wasn't fetched" means no request has been made for the URL, not "has no
stored content".

## Core mechanism — a claim-time filter

The entire feature is one conditional predicate on the claim query. In `new-only` mode,
the worker's `claim_pending_url` gets `AND last_checked_at IS NULL` appended to its
`WHERE`. Consequences, all of which fall out for free:

- **Never-fetched pending URLs** (the seed on a cold run, newly-discovered links, new
  sitemap entries) → **claimed and fetched**.
- **Already-downloaded pages**, however they returned to `pending`, have
  `last_checked_at` set → **never claimed**; they stay `pending` untouched, available to
  a later `all` / `changed-only` run.
- **No special-casing of requeue logic** is needed — a requeued page is simply
  un-claimable, so whether or not it was requeued is irrelevant.
- **Link extraction and cascade are unchanged** — `process_url` still discovers links
  from every HTML page it fetches and enqueues them; those new URLs are themselves
  never-fetched and thus claimable, so the crawl keeps going into new territory.

## Touch points

1. **`config.py`** — validate `recheck` against `{"all", "changed-only", "new-only"}`
   and raise `ValueError` on anything else. (Currently unvalidated; add the guard so a
   typo fails loudly rather than silently behaving like `changed-only`.)
2. **`storage.claim_pending_url(conn, site, only_new=False)`** — new keyword-only-style
   param; when true, append `AND last_checked_at IS NULL` to the claim `WHERE`.
3. **`storage.count_pending(conn, site=None, only_new=False)`** — same optional filter,
   so the live progress "queued" count reflects actually-claimable work under `new-only`
   rather than pages that will never be claimed.
4. **`crawl.crawl_site` / `crawl.run_fetch`** — derive
   `only_new = (config.crawl.recheck == "new-only")` and thread it into both the
   `claim_pending_url` call and the `count_pending` call. `run_fetch` must NOT call
   `requeue_present_urls` in `new-only` mode (it already only does so for `all` /
   `force_full`, so no change beyond leaving that branch untouched).
5. **`cli.py`** — add a `--new-only` flag to the `fetch` and `run` subparsers'
   mutually-exclusive recheck group, setting `recheck = "new-only"` (no `force_full`).
6. **`config.toml`** — update the inline comment to list the third mode.
7. **`README.md`** — document `new-only` alongside the existing modes.

## Interaction with `force_full`

`force_full` (CLI `--full`) forces `recheck = "all"`. It is mutually exclusive with
`--new-only` at the argparse level, so the two never combine. `new-only` never sets
`force_full`.

## Testing

Test-first, mirroring existing `tests/test_crawl.py` / storage-level tests:

1. **`claim_pending_url(only_new=True)`** skips a pending row with `last_checked_at` set
   and returns one with `last_checked_at IS NULL`.
2. **`count_pending(only_new=True)`** counts only never-fetched pending rows.
3. **`config` validation** rejects an unknown `recheck` value and accepts `new-only`.
4. **End-to-end `run_fetch` in `new-only` mode**: given a DB with one already-fetched
   page (flipped back to `pending`, e.g. via a sitemap advance) and one never-fetched
   pending URL, only the never-fetched URL is fetched; the already-downloaded page is
   left `pending` and untouched.
5. **Cascade**: a never-fetched HTML page whose fetched body links to a new URL causes
   that newly-discovered URL to also be fetched in the same `new-only` run.

## Out of scope

- Site-level "only crawl new config sites" selection (a different feature).
- Re-discovering links from already-downloaded pages (those links were captured on first
  download; not re-fetching them loses nothing).
