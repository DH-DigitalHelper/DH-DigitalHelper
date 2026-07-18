# DHBW Corpus Audit — 2026-07-17

Read-only forensic audit of `data/scraper.sqlite3` (4.0 GB, 46,389 present
documents). Run as 16 agents (8 dimension finders + 8 adversarial verifiers)
opening the DB `mode=ro&immutable=1` — **no data was written or crawled**.

## Method & verification status

| Dimension | Independently verified by | Confidence |
|---|---|---|
| program-classification | 2nd agent (verdicts folded in) | high |
| standort-satellite | 2nd agent (verdicts folded in) | high |
| metadata-fields | 2nd agent (verdicts folded in) | high |
| department-classification | verifier killed by session limit → **key numbers re-run by hand** (3,185 FP, recoverable paths all matched exactly) | high |
| coverage-gaps | verifier killed → **re-run by hand** (queue states, VS trap, error buckets all matched) | high |
| integrity-reconciliation | verifier killed → **re-run by hand** (0 orphans, 9 missing, 4 rejected-backed, 7,380 ncat all matched) | high |
| links-freshness | verifier killed → **re-run by hand** (28 norm-mismatch, depth 198, 530 ADMCMD all matched) | high |

A handful of full-text-scan claims (U+FFFD counts, mojibake=0, the `normalize_text`
reconciliation tie-out, the 80k-edge `in_domain` sample) are **finder-measured,
not independently re-run here**. They are internally consistent with every number
that *was* re-verified and are flagged inline where they appear.

---

## TL;DR

**The corpus is fundamentally healthy.** No bulk data loss, exact-text dedup is
clean, the 15.1 M-edge link graph has zero referential orphans, encoding is clean
UTF-8 (the `�` you may see is a Windows-console artifact, **not** stored
corruption), the crawl is fresh (2026-07-14…07-16), and the revision machinery
works. **Do not** run a global "fix everything" pass — most of the surface issues
are expected corpus shape.

The real problems are **targeted and mostly cheap to fix**. In priority order:

1. **Villingen-Schwenningen is 94% missing** — a `buchen.*` spider trap consumed
   86% of the entire crawl, then transport timeouts killed the recovery run. This
   is the single highest-value "add data" action. (Karlsruhe & Stuttgart/Horb have
   the same recoverable-timeout problem, smaller.)
2. **Two classification defects worth fixing** — a keyword leak mis-tags **3,185**
   news-archive pages into a faculty, and the Friedrichshafen satellite is **75%
   false positives**. Both fixable by editing `taxonomy.py`/`classify.py` + a
   reclassify pass, **no crawl needed**.
3. **Two metadata fields are dead** — `lang` is NULL for 100% of docs (never
   implemented), and `final_url` is a verbatim copy of `url` (8,321 redirects
   lost). Both fixable in Phase-2, no crawl.
4. **The study-program catalog is tiny** (7 patterns, one of them unmatchable) —
   ~2,500–2,700 genuinely program-bearing docs are untagged.
5. **A pile of low-value noise** (7,380 near-duplicate news lists, ~7,200 thin
   directory cards, 25 auth-wall/404 pages) should be **filtered at the RAG layer**,
   not deleted from the corpus.

---

## Findings, severity-ranked

| # | Sev | Finding | Scope | Fix vehicle |
|---|-----|---------|-------|-------------|
| 1 | 🔴 crit | VS campus 94% missing (spider-trap + timeout) | 121 docs vs ~2–4k expected | polite re-crawl |
| 2 | 🟠 warn | Study-program catalog too small; 1 pattern unmatchable | ~2,500–2,700 taggable docs | taxonomy + reclassify |
| 3 | 🟠 warn | Dept keyword leak mis-tags news lists into faculties | 3,185 docs | classify.py + reclassify |
| 4 | 🟠 warn | Friedrichshafen satellite ~75% false positives | 44 of 59 docs | taxonomy + reclassify |
| 5 | 🟠 warn | `lang` NULL for 100% of corpus (never implemented) | 46,389 docs | extractor + backfill |
| 6 | 🟠 warn | `final_url` is a dead copy of `url` (8,321 redirects lost) | 46,389 docs | fetch→storage + backfill |
| 7 | 🟠 warn | ~1,686 faculty pages recoverable from dept=unknown | 1,686 docs | classify.py + reclassify |
| 8 | 🟠 warn | 7,380 ncat/`aktuelles/page-` near-duplicate news lists | 7,380 docs (~400 unique) | trap denylist + RAG filter |
| 9 | 🟠 warn | ~7,200 thin dual-partner/contact cards (<50 words) | ~7,200 docs | RAG filter |
| 10 | 🟠 warn | 54 PDFs fully lost to extract errors (34 retryable) | 54 docs | PyMuPDF upgrade + retry |
| 11 | 🟠 warn | 18 PDFs are U+FFFD garbage (2–95% of text) | 18 docs | re-extract or quarantine |
| 12 | 🟠 warn | ~164 image-only PDFs + 79 near-empty (no OCR) | ~243 docs | optional OCR fallback |
| 13 | 🟡 info | 1,097 docs missing a title (mostly PDFs) | 1,097 docs | PDF title fallback |
| 14 | 🟡 info | 9 fetched-OK pages silently absent from corpus | 9 docs | forced re-fetch + bug hunt |
| 15 | 🟡 info | Gesundheit RAG topic critically thin | 492 docs (1.06%) | re-crawl + program seed |
| 16 | 🟡 info | 28 docs invisible to link graph (space vs %20) | 28 docs | url normalization |
| 17 | 🟡 info | 4 present docs backed by a *rejected* raw row | 4 docs | retire + invariant |
| 18 | 🟡 info | ~150 Office docs (.docx/.pptx/.xlsx) silently dropped | ~150 docs | optional extractor branch |
| 19 | 🟡 info | 25 auth-wall/404 pages indexed with junk titles | ~25 docs | RAG filter |
| 20 | 🟡 info | ADMCMD_editIcons TYPO3 edit-mode trap in frontier/graph | 530 urls, 0 docs | trap denylist |

---

## A. Coverage gaps — "where to add data for the RAG"

This is the direct answer to *"identify spots where I could provide more data."*
There is **no uncrawled `pending` backlog** — every discovered URL was attempted
(queue: 106,648 done / 6,178 error / 0 pending). So "add data" = **recover the
6,178 errored URLs via a polite re-crawl**, not discover new sites. The errors are
~99% `http_status=0` transport timeouts/refusals caused by the aggressive default
crawl settings (`workers_per_host=16`, `request_delay_seconds=0.0`).

### A1 — Villingen-Schwenningen (🔴 highest value) — 121 docs

- **Present docs: 121**, vs a per-site median of ~3,377 (Loerrach 2,054, Heilbronn 2,372 are the nearest peers). ~94% missing.
- **Root cause, verified:** the first crawl drowned in a spider trap —
  `buchen.dhbw-vs.de` = **942,759 crawl_log rows (86% of the entire 1,092,011-row
  crawl_log)**, `moodle.dhbw-vs.de` another 12,053, while the real content host
  `www.dhbw-vs.de` got only **721** rows total. The trap is now denylisted
  (`links.rs` `TRAP_HOST_LABEL_PREFIXES=[buchen,moodle,elearning]`), so it will not
  re-explode.
- The 07-16 recovery run then left **414 errored / 216 done** in the VS queue; all
  414 errors are `http_status=0` (read-timeout / connection-refused / SSL-handshake
  timeout) — i.e. a throttling server, recoverable with politeness.
- **Expected yield:** 414 errored content URLs immediately, and because link
  discovery never expanded past the homepage, a full polite crawl should reach
  **~2,000–4,000 pages**. This is a whole DHBW campus currently absent from the RAG.

### A2 — Karlsruhe (🟠) — 2,599 docs, 1,660 errored

- 1,659 of 1,660 errors are transport timeouts on `www.karlsruhe.dhbw.de`.
- **~211 unique content pages + ~1,446 TYPO3 `cHash=` accordion variants**;
  realistically ~300–800 net-useful docs recoverable.
- Disproportionately valuable: Karlsruhe hosts the **Gesundheit/Pflege** faculty
  (`/bachelor/fachbereich-gesundheit/…/angewandte-gesundheits-und-pflegewissenschaften/`),
  the corpus's thinnest topic.

### A3 — Stuttgart / Campus Horb (🟠) — 4,004 errored (but mostly low value)

- 574 of the errors are **`/horb/` satellite content** — Horb has only 327 present
  docs, so recovering these ~triples it toward ~900.
- The other ~2,600 are **low/zero RAG value**: 1,644 `wwwlehre` legacy exam
  solutions, 669 `_processed_` image assets, 328 company-directory enumeration — most
  would be rejected by the extract gate anyway. Recommend **denylisting** these
  paths so they stop consuming crawl budget.

### A4 — Thin RAG topics

- **Gesundheit: 492 docs (1.06%)** — critically thin, and 0 are program-tagged (see
  §B1). Recovering the ~142 errored health URLs (Karlsruhe/Stuttgart) + seeding the
  program could lift it to ~700–900.
- **Sozialwesen: 1,615 docs** — thin but adequately sourced; lower priority.

### A5 — Do NOT over-invest here

- **Loerrach (2,054) and CAS (1,209) are complete crawls**, not defects
  (Loerrach 6,535 done / 15 error; CAS 1,456 done / 1 error). They are genuinely
  smaller institutions.
- The three satellites have **no own domain** — they are sub-sections of their
  parent campus, so low counts are structural (Horb is the only one with a
  large recoverable backlog, via Stuttgart).

---

## B. Classification defects

All classification fixes below share **one remediation vehicle**: edit
`src/scraper/taxonomy.py` (+ `classify.py`), bump `CLASSIFY_VERSION`, and run a
**reclassify pass over the existing `documents` rows** — every input
(`url`, `title`, `text`, `metadata`) is already stored, so **no crawl and no
re-extract are needed**. ⚠️ The one-time backfill script was removed (commit
`e117155`) and re-extract does **not** reclassify unchanged docs, so this needs a
small dedicated `reclassify` maintenance command (reads `documents`, calls
`classify.classify`, writes `standort_id`/`department_id`/`study_program_id`/`classify_meta`).

### B1 — Study-program catalog too small; one pattern is dead (🟠)

- Program tagging covers only **4,387 / 46,389 (9.5%)**. The 7-entry seed catalog
  misses 20+ real DHBW programs.
- **The "missing 7th `study_programs` row" is a symptom, not the bug.** Rows are
  interned on demand (a row exists only after ≥1 doc matches). The pattern
  `angewandte-gesundheitswissenschaften` matches **0 docs anywhere** — real paths
  use underscores (`Angewandte_Gesundheitswissenschaften`) or different program names
  (`angewandte-hebammenwissenschaft`). So the **entire Gesundheit program population
  (~250–400 docs) is untaggable**, and `classify()` never normalizes `_`→`-`.
- **Adding the missing programs ~doubles coverage numerically** (9.5% → ~19.7%),
  **but** the verifier flagged that ~44% of the newly-taggable docs are
  `liste-dualer-partner` employer stubs and ~35% are <50 words — the **genuinely
  content-bearing gain is ~2,500–2,700 docs (~15%)**, not 4,700.
- **Precision caveat (verifier):** do **not** add bare `informatik` / `medien`
  tokens — they conflate programs and catch company/event pages. Use
  path-anchored patterns.
- Top additions by yield: `informatik` (path-anchored), `bwl-*` umbrella,
  `wirtschaftsingenieurwesen`, `rsw-*`, `betriebswirtschaftslehre`, plus Technik
  programs the finder missed: `bauingenieurwesen` (108), `holztechnik` (62),
  `papiertechnik` (43), `food-management` (38). Place `informatik` **after**
  `wirtschaftsinformatik` (substring precedence).

### B2 — Department keyword leak mis-tags news lists into faculties (🟠, misclassification)

- **3,185 pages** (verified exactly) with title *"Aktuelles aus der DHBW Mannheim"*
  — a paginated news archive — are tagged into a faculty (wirtschaft 2,575, technik
  610) purely because one article *teaser* on the list page mentioned e.g. "bwl" or
  "maschinenbau". These are near-certain false positives.
- Fix: in `classify_department`, **suppress the keyword path** when the URL is a
  news-list/index (`ncat-`, `/aktuelles/page-`) or dual-partner index, **or** require
  the keyword to hit `title`/`metadata.description` rather than only body text.

### B3 — Friedrichshafen satellite ~75% false positives (🟠, misclassification)

- `ravensburg-friedrichshafen` (id=13): **44 of 59 docs are company-listing pages**
  (`.../detailansicht/zf-friedrichshafen-ag-…`) mis-tagged as campus, because the
  rule matches the **bare substring `friedrichshafen`** over the whole URL and
  Friedrichshafen is a large industrial city saturating dual-partner slugs.
- It retains a genuine core of ~10–13 FN-campus news pages beneath the FPs (verifier).
- Fix: anchor the rule like Horb/Mergentheim — use `/fn/`, `campus-friedrichshafen`,
  `technikcampus-friedrichshafen`, and exclude `detailansicht`/`unternehmen` paths.
  The 44 partner pages should revert to base `ravensburg`.
- **Also (verifier-found false negatives):** 4 base-ravensburg pages about the
  "Technikcampus" (= Campus FN) stay untagged; and 12 central `dhbw.de/…/SP/FN/…`
  study-profile PDFs are tagged `dhbw` because the satellite rule only fires when
  base==ravensburg. `bad-mergentheim` (133) and `horb` are otherwise clean.

### B4 — ~1,686 faculty pages recoverable from dept=unknown (🟠)

- **The 60% `unknown` rate is mostly correct** (~81% are structurally non-faculty:
  news 43%, dual-partner listings 24%, international/EN, contacts, admin). **Do not
  try to drive it to zero.**
- But **~1,686 unknown docs are genuine faculty pages** the narrow `DEPARTMENT_URL_RULES`
  miss. Verified recovery by path (unknown-only): `/bachelor-studienangebot/technik/`
  **+479**, `bwl-` **+490**, `/…/wirtschaft/` **+203**, `bauingenieurwesen` **+93**,
  `holztechnik` **+56**, `/…/gesundheit/` **+14**, etc.
- Fix: add these path-scoped rules to `DEPARTMENT_URL_RULES` (they run first, so they
  also override the false keyword ties from B2). Keep `bwl-` as a **URL** rule (not a
  bare `bwl` text keyword) for precision.
- Leave the `tie → unknown` rule as-is — the 3,051 tie docs are genuinely
  multi-faculty (graduation announcements, cross-faculty bulletins).

### B5 — Horb file directory tagging inconsistent (🟡)

- 15 docs under `/fileadmin/dateien-horb/` are Horb material but only 3 got the Horb
  tag (incidentally, via a trailing `Horb.pdf`); the other 12 stayed base Stuttgart.
- Fix: add `dateien-horb` to the `stuttgart-horb` satellite rules. (One mirror FP:
  a Stuttgart-wide `Organigramm…Campus_Horb.pdf` is wrongly tagged Horb.)

---

## C. Corruption, junk & lost content

### C1 — 54 PDFs fully lost to extract errors (🟠, missing-data)

- `raw_docs.extract_state='error'` = 54, all PDFs, **none have a present document**
  → complete content loss. Breakdown: **26 PyMuPDF `NoneType … h_lines` crashes**
  (a known bug), **20 password-encrypted**, **8 process-pool terminations**.
- The 34 crash/timeout PDFs are **high-value** (Modulhandbücher, Studienpläne from
  Heidenheim/Karlsruhe). Retry after upgrading PyMuPDF + more pool memory/timeout.
  The 20 encrypted are unrecoverable — record them so downstream isn't surprised.

### C2 — 18 PDFs are extraction garbage (🟠, corruption)

- 172 present docs contain the U+FFFD replacement char; **18 of them are 2–95%
  replacement chars** (two PDFs hold 54k and 166k of them) — non-standard font
  encodings PyMuPDF mangled. *(finder-measured; full-text scan not re-run here.)*
- Fix: flag docs where U+FFFD > ~2% of length → re-extract with a different engine
  or quarantine (`present=0`) so they don't inject pure noise into RAG. The ~135
  long-tail docs with <10 FFFD are benign ligature dropouts.

### C3 — ~164 image-only PDFs + 79 near-empty (🟠, missing-data)

- ~164 scanned/image PDFs were rejected as `empty` (font=0, dozens of image
  objects); ~79 present PDFs extracted almost no text. ~243 PDFs where content is
  trapped in images. *(finder-measured via magic bytes.)*
- Fix (optional): OCR fallback (ocrmypdf/Tesseract) for PDFs with image XObjects and
  no fonts. Prioritize Modulbeschreibungen & Amtliche Bekanntmachungen; posters can
  stay excluded.

### C4 — Low-value noise → filter at the RAG layer, don't delete (🟠/🟡)

- **7,380 `ncat-`/`aktuelles/page-` near-duplicate news lists** (verified) — every
  permutation hashes distinctly so exact-text dedup keeps them all; only ~400–450 are
  actually unique.
- **~7,200 thin dual-partner/contact cards** (<50 words) — real but near-duplicate
  boilerplate (company name + address, or a person's room/phone).
- **~25 auth-wall/404 pages** indexed with junk titles ("Login", "404", "Please Sign
  in") — the `final_url` bug (§D2) is why they're invisible.
- **Recommendation:** these are **not corrupt** and shouldn't be deleted at the
  extractor. Add a RAG-layer filter (URL denylist for `ncat-`/`liste-dualer-partner`/
  `/unternehmen/…-gmbh-<id>` + a `word_count` gate), and add the `ncat`/`ADMCMD`/
  enumeration patterns to the `links.rs` trap denylist so future crawls skip them.

### C5 — ~150 Office documents silently dropped (🟡)

- The `empty` rejects include ~151 OOXML files (107 `.docx`, 28 `.pptx`, 23 `.xlsx`,
  up to 17 MB) with real content — out of scope for the html+pdf-only pipeline. The
  rest of the 1,913 rejects are correctly-discarded junk formats (Citavi exports,
  vCards, PGP blocks). Add a docx/pptx/xlsx branch only if that content matters.

### C6 — What is NOT corrupt (do not "fix")

- **Encoding is clean.** `instr(text,'Ã¤')`, `'Ã¼'`, `'â€“'` … all **0**. `BWL – Industrie`
  stores a genuine U+2013 en-dash. **Do not run an "un-mojibake" find/replace** — it
  would damage correct UTF-8. When eyeballing, set `PYTHONIOENCODING=utf-8`.

---

## D. Missing / broken metadata fields

### D1 — `lang` NULL for 100% of the corpus (🟠) — never implemented

- 0 of 46,389 docs (and 0 of 100,285 raw_docs) have a language. Root cause is
  upstream, not a dropped write: `html_extract.py:41` and `pdf_extract.py:71` both
  return a hardcoded `"lang": None`; storage persists it faithfully.
- The corpus **is** mixed-language: ≥456 docs live under `/en/`. DE-only retrieval /
  language routing is impossible until this is populated.
- Fix in the **extractors** (not the write path, which is correct): trafilatura
  language detection for HTML, `langdetect` over extracted text for PDF. Phase-2
  only, no crawl — but needs a backfill pass (lang doesn't change `text_sha256`, so
  re-extract's change detection would skip it).

### D2 — `final_url` is a dead verbatim copy of `url` (🟠)

- `final_url = url` for all 46,389 docs; **0 redirects visible**, yet crawl_log
  records **8,321 distinct redirected URLs**. Root cause: `storage._upsert_document`
  binds `url` into *both* columns and the "changed" UPDATE never touches `final_url`.
- Consequence: **~23 auth-wall pages** and **2 `/404/` pages** are indexed under
  their original (misleading) URL with junk titles, and downstream has no way to
  detect redirect-to-login/404.
- Fix: thread the fetched `final_url` from the Rust fetch → `_upsert_document`;
  backfill from `crawl_log` by url.

### D3 — 1,097 docs missing a title (🟡) — mostly PDFs

- 1,085 PDFs + 12 HTML have NULL/empty title. PDFs derive title only from a leading
  `# ` heading in the pymupdf4llm markdown; `doc.metadata['title']` is never
  consulted. (4 of the 12 "html" are actually Office files mis-typed as `source_type=html`.)
- Fix: PDF title fallback = `doc.metadata['title']` else cleaned filename. Titles
  feed retrieval ranking, so blanks hurt recall.

### D4 — What works: `description` (42.5%, HTML), `date` (38,703), `sitename`
(35,666), and the revision machinery (rev 1 = 46,346, rev 2 = 43 genuine
re-indexes) are all functioning as designed. PDFs structurally carry no
`description` — expected, not a defect.

---

## E. Structural integrity

**The reconciliation is clean** — the 100,285 raw → 46,389 present collapse ties out
exactly (1,967 gate rejects/errors + 49,508 exact text-duplicates + 2,421
extracted-but-not-live stale cache). Verified: **0** documents with a
`content_sha256` absent from `raw_docs`, **0** documents sharing a `content_sha256`,
**0** NULL `text_sha256`. No silent bulk data loss.

Genuine (small) defects:

- **E1 (🟡) — 9 fetched-OK pages silently absent.** 9 `queue` rows are
  `work_state='done'`, `http_status=200`, with a `content_sha256` that has **no
  `raw_docs` row and no file on disk** (verified = 9). The Phase-1 change-detection
  path recorded `content_sha256` + `outcome='unchanged'` on first contact **without
  ever persisting the blob**. Fix: forced (non-conditional) re-fetch of the 9 URLs;
  investigate the `writer.rs`/`fetch.rs` first-contact path.
- **E2 (🟡) — 4 present docs backed by a *rejected* raw row** (verified = 4). A live
  corpus row contradicts its own raw row's sub-50-word rejection. Fix: retire
  (`present=0`) and add an invariant (retire `documents.present` when backing
  `raw_docs.quality_ok` flips to 0).
- **E3 (🟡) — 28 docs invisible to the link graph** (verified = 28). Their
  `documents.url` isn't in the `urls` dictionary (25 have a literal space where
  `urls` has `%20`; 3 trailing-slash/query cases), so they never resolve to a
  `links.src_id`. Text is fine; only graph features drop them. Fix: normalize
  `documents.url` (encode spaces, trailing slash) to match the frontier writer.
- **E4 (🟡) — ADMCMD_editIcons TYPO3 edit-mode trap** (verified: max depth 198, 530
  ADMCMD urls). A depth-198 pagination chain of backend edit-mode URLs pollutes the
  frontier (~530 urls, ~14,833 edges) but **0 corpus documents** (dedup caught them).
  Fix: add `ADMCMD_*` to the `links.rs` denylist + strip before frontier insertion.

**Link graph is otherwise sound:** 15,087,174 edges, **0 referential orphans**,
`in_domain` flag validated clean on an 80k-edge sample *(finder-measured)*, top
external targets are social media, no out-degree spider trap. The old sparse-links
bug is fully resolved (only 1 residual HTML page — a legacy `bookmarks.htm` — has
uncaptured links). Freshness is healthy: single 2026-07-14…07-16 window, negligible
sitemap staleness (≤4 URLs).

---

## Remediation runbook

Grouped by vehicle. **Nothing here has been executed** — live crawls and DB writes
are yours to run.

### Track 1 — Code + reclassify (no crawl, no re-extract)

Add a `reclassify` maintenance command (pure DB: read `documents`, call
`classify.classify` over stored `url`/`title`/`text`/`metadata`, update the 4
classification columns). Then, editing `src/scraper/taxonomy.py` + `classify.py`:

1. **Program catalog (§B1):** replace the dead `angewandte-gesundheitswissenschaften`
   pattern with underscore-tolerant Gesundheit patterns
   (`hebammenwissenschaft`, `pflegewissenschaft`, `angewandte_gesundheitswissenschaften`);
   add the ranked program additions with **path-anchored** patterns (not bare
   `informatik`/`medien`); place `informatik` after `wirtschaftsinformatik`.
2. **Department URL rules (§B4):** add `/bachelor-studienangebot/{technik,wirtschaft,gesundheit}/`,
   `/bachelor/{technik,wirtschaft}/`, `bwl-`, `bauingenieurwesen`, `holztechnik`,
   `angewandte-informatik`, `medizintechnik`, `wirtschaftsingenieur`→technik.
3. **Department keyword leak (§B2):** suppress the keyword path on news-list/index
   URLs (`ncat-`, `/aktuelles/page-`, dual-partner index), or require keyword hits in
   title/description.
4. **Friedrichshafen rule (§B3):** anchor to `/fn/`, `campus-friedrichshafen`,
   `technikcampus-friedrichshafen`; exclude `detailansicht`/`unternehmen`. Add
   `dateien-horb` to Horb rules (§B5).
5. Bump `CLASSIFY_VERSION`; run `reclassify`.

### Track 2 — Polite recovery re-crawl (operator-run; the pending VS re-crawl + 2 more)

Create `config.recover.toml` with `request_delay_seconds > 0`, low
`workers_per_host`, and a recheck mode that retries `error`-state URLs; then, in the
MSVC shell, in priority order:

```powershell
uv run dhbw-scraper --config config.recover.toml fetch --site villingen_schwenningen
uv run dhbw-scraper --config config.recover.toml fetch --site karlsruhe
uv run dhbw-scraper --config config.recover.toml fetch --site stuttgart
uv run dhbw-scraper extract        # then dedup / run tail
```

The `buchen/moodle` trap is already denylisted, so VS won't re-explode. Before
Stuttgart, consider denylisting `wwwlehre` exam paths, `/fileadmin/_processed_/`
assets, and `liste-dualer-partner/unternehmen/` enumeration to save budget.

### Track 3 — Phase-2 extractor fixes + backfill (no crawl)

6. **`lang` (§D1):** add language detection to both extractors; backfill pass.
7. **PDF titles (§D3):** `doc.metadata['title']` → filename fallback; backfill.
8. **`final_url` (§D2):** thread fetched final_url into `_upsert_document`; backfill
   from `crawl_log`.
9. **Lost PDFs (§C1):** upgrade PyMuPDF (fixes the 26 `h_lines` crashes), raise pool
   timeout/memory, retry the 34; record the 20 encrypted.
10. **Garbage PDFs (§C2):** re-extract or quarantine the 18 high-FFFD docs.
11. *(optional)* OCR fallback (§C3); Office-doc branch (§C5).

### Track 4 — Small data fixes

12. **9 missing pages (§E1):** forced re-fetch + investigate the writer first-contact bug.
13. **4 rejected-backed docs (§E2):** retire + add the invariant.
14. **28 url-normalization (§E3)** and **ADMCMD trap (§E4):** normalize + denylist.

### Track 5 — Downstream RAG hygiene (in the RAG stage, not this repo)

15. Filter `ncat-`/`liste-dualer-partner`/`/unternehmen/…-gmbh-<id>` URLs and apply a
    `word_count` gate so the ~14,500 near-dup/thin/auth-wall pages don't dominate
    retrieval. Use `host`, not the `in_domain` bit, for "off-university" filtering
    (21% of "external" edges stay within `*.dhbw.de`).

---

*Generated by a 16-agent read-only audit, 2026-07-17. Regenerate the operational
dashboard any time with `uv run dhbw-scraper report`.*
