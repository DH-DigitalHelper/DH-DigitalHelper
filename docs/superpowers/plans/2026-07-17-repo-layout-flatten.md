# Repo Layout — One Source Root: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the `rust/` and `src/dhbw_scraper/` trees into one source root — `src/scraper/` (Python) and `src/scrape-engine/` (Rust) — with `Cargo.toml` at the repo root.

**Architecture:** A pure move-and-rename. Rust becomes a *sibling* of the Python package under `src/`, never nested inside it (the package dir is the wheel payload — nesting ships `.rs` into site-packages; verified). `Cargo.toml` moves to the root and absorbs the cost via a custom `[lib] path` plus explicit `[[test]]` entries. The Python package renames to `scraper` and the extension to `_engine`.

**Tech Stack:** Rust (PyO3 0.29 / maturin), Python 3.14, uv, pytest, cargo, pre-commit.

**Spec:** [`docs/superpowers/specs/2026-07-17-repo-layout-flatten-design.md`](../specs/2026-07-17-repo-layout-flatten-design.md)

## Global Constraints

- **No behavior change.** No logic, schema, dependency, or config-value changes. Every `.rs`/`.py` file keeps its contents except import lines, module-name renames, and docstrings. Any behavior delta is a bug in the move.
- **No new tests.** This refactor is correct exactly when the existing suites pass unchanged. Do not add tests to "cover" the move — the existing suites *are* the test. Each task's verify step runs the real gate.
- **`git mv` only** — never delete + re-add. Rename detection is what keeps `git log --follow` working.
- **Conventional Commits** are enforced by a commit-msg hook. Types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`.
- **Every `cargo` / `uv` / `maturin` command needs MSVC.** `cl.exe` is not on the default PATH. Bootstrap it (verified working):

  ```powershell
  $vc   = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
  $repo = "C:\Users\manue\Documents\GitHub\DH-DigitalHelper"
  cmd /c "`"$vc`" >nul 2>&1 && cd /d `"$repo`" && <COMMAND> 2>&1"
  ```

  This plan refers to that wrapper as **`msvc <COMMAND>`**. A bare `cargo test` fails with a `link.exe` error; that is the missing MSVC env, not a code fault.
- **Do not touch** `[project] name = "dhbw-scraper"` (the distribution name) or the `dhbw-scraper` console-script name. Only the script's *target* changes. A dist name that differs from the import name is normal and intended.
- **Do not rewrite historical specs.** `docs/superpowers/specs/2026-07-14-*` and `2026-07-16-*` reference `rust/src/*.rs` and are records of past decisions. Leave them.

---

### Task 1: Move the Rust crate under `src/`, `Cargo.toml` to the root

Ends green with the package still named `dhbw_scraper` and the extension still `_native`. Renames come later — keep this task purely about location.

**Files:**
- Move: `rust/Cargo.toml` → `Cargo.toml`; `rust/Cargo.lock` → `Cargo.lock`
- Move: `rust/src/*.rs` (11 files) → `src/scrape-engine/`
- Move: `rust/tests/*.rs` (4 files) → `tests/scrape-engine/`
- Modify: `Cargo.toml`, `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.gitignore`
- Delete from index: `src/dhbw_scraper/_native.pdb`

**Interfaces:**
- Consumes: nothing.
- Produces: root `Cargo.toml` with `[lib] name = "_native"`, `path = "src/scrape-engine/lib.rs"`. Rust sources at `src/scrape-engine/`, Rust tests at `tests/scrape-engine/`. Task 3 renames `_native` → `_engine` in this manifest.

- [ ] **Step 1: Move the files**

```bash
cd "C:/Users/manue/Documents/GitHub/DH-DigitalHelper"
mkdir -p src/scrape-engine tests/scrape-engine
git mv rust/Cargo.toml Cargo.toml
git mv rust/Cargo.lock Cargo.lock
for f in rust/src/*.rs;   do git mv "$f" "src/scrape-engine/$(basename "$f")";   done
for f in rust/tests/*.rs; do git mv "$f" "tests/scrape-engine/$(basename "$f")"; done
rm -rf rust/target
rmdir rust/src rust/tests rust 2>/dev/null
```

- [ ] **Step 2: Verify `rust/` is gone and 15 files moved**

Run: `ls src/scrape-engine tests/scrape-engine && ls rust 2>&1`
Expected: 11 `.rs` in `src/scrape-engine`, 4 `.rs` in `tests/scrape-engine`, and `ls: cannot access 'rust': No such file or directory`.

- [ ] **Step 3: Point `Cargo.toml` at the new paths**

In `Cargo.toml`, add `autotests = false` to `[package]` and replace the `[lib]` block. `autotests = false` + explicit `[[test]]` entries is deliberate: it preserves `cargo test --test orchestration` and confines the cost to this file.

```toml
[package]
name = "dhbw_scraper_native"
version = "0.3.0"
edition = "2021"
publish = false
# Integration tests live in tests/scrape-engine/, which Cargo's autodiscovery
# (tests/*.rs, tests/*/main.rs) does not match — they are declared explicitly
# below. tests/ is shared with pytest; cargo ignores the .py files.
autotests = false

[lib]
name = "_native"
path = "src/scrape-engine/lib.rs"
crate-type = ["cdylib", "rlib"]

[[test]]
name = "backfill"
path = "tests/scrape-engine/backfill.rs"

[[test]]
name = "links_parity"
path = "tests/scrape-engine/links_parity.rs"

[[test]]
name = "orchestration"
path = "tests/scrape-engine/orchestration.rs"

[[test]]
name = "sitemap_parity"
path = "tests/scrape-engine/sitemap_parity.rs"
```

Leave `[dependencies]`, `[dev-dependencies]`, and `[profile.release]` exactly as they are.

- [ ] **Step 4: Drop `manifest-path` from `pyproject.toml`**

maturin defaults to the `Cargo.toml` beside `pyproject.toml`, so the key is now redundant. Replace the `[tool.maturin]` block:

```toml
[tool.maturin]
# Mixed Rust/Python layout, one source root: the pure-Python package tree stays
# authoritative under src/dhbw_scraper/, and the Rust crate at src/scrape-engine/
# is injected as the single submodule dhbw_scraper._native. The Rust is a SIBLING
# of the package, not nested inside it — anything under the package dir is the
# wheel payload and would ship .rs files into site-packages.
python-source = "src"
module-name = "dhbw_scraper._native"
features = ["pyo3/extension-module"]
```

- [ ] **Step 5: Drop `--manifest-path` from the three pre-commit hooks**

In `.pre-commit-config.yaml`, change each `entry:` line (cargo now finds the root manifest on its own):

```yaml
      - id: rustfmt
        name: rustfmt
        entry: cargo fmt --
```
```yaml
      - id: clippy
        name: clippy
        entry: cargo clippy --all-targets -- -D warnings
```
```yaml
      - id: cargo-test
        name: cargo test
        entry: cargo test
```

Leave every other key on those hooks (`language`, `types`, `pass_filenames`, `stages`) untouched.

- [ ] **Step 6: Drop `--manifest-path` and `workspaces: rust` from CI**

In `.github/workflows/ci.yml`:

Line ~19 comment — `see rust/Cargo.toml` → `see Cargo.toml`.

The cargo cache step loses its `with:` block (the crate is now at the repo root, which is the default):
```yaml
      - name: Cache cargo build
        uses: Swatinem/rust-cache@v2
```

The three cargo steps:
```yaml
      - name: rustfmt check
        run: cargo fmt --check
      - name: Clippy
        run: cargo clippy --all-targets -- -D warnings
```
```yaml
      - name: Cargo test
        shell: bash
        run: |
          # Expose the interpreter's shared libpython so the test binaries load it.
          PYLIB="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')"
          LD_LIBRARY_PATH="$PYLIB:${LD_LIBRARY_PATH:-}" \
            cargo test
```

- [ ] **Step 7: Repoint dependabot at the root crate**

In `.github/dependabot.yml`, the cargo ecosystem block:

```yaml
  # ── Rust crate (Cargo.toml + Cargo.lock at the repo root) ──
  - package-ecosystem: "cargo"
    directory: "/"
```

Leave `schedule`, `open-pull-requests-limit`, `commit-message`, and `groups` untouched.

- [ ] **Step 8: Fix `.gitignore` and untrack the stray `.pdb`**

`src/dhbw_scraper/_native.pdb` is a **tracked** Windows debug-symbol artifact, committed by accident. It sits in the directory Task 2 moves, so remove it now rather than carrying a stale copy across two renames. Its sibling `_native.pyd` needs no rule — `*.py[cod]` already catches it (the `d` in `[cod]`).

In `.gitignore`, replace `rust/target/` with `/target/`:
```
# Rust
/target/
# (Cargo.lock IS committed — this is a binary/app, not a library)
```

Add `*.pdb` under the Python section, next to the existing bytecode rules:
```
# Python
__pycache__/
*.py[cod]
*.pdb
.venv/
```

Then untrack the file and delete it from disk:
```bash
git rm --cached src/dhbw_scraper/_native.pdb
rm -f src/dhbw_scraper/_native.pdb
```

- [ ] **Step 9: Verify the Rust build at the new paths**

Run: `msvc "cargo test"`
Expected: PASS. The lib target reports as `Running unittests src\scrape-engine\lib.rs`, then four integration binaries from `tests\scrape-engine\`: `backfill`, `links_parity`, `orchestration`, `sitemap_parity`. Zero failures.

If this errors with `link.exe returned an unexpected error`, the MSVC wrapper was not used — re-run via `msvc`.

- [ ] **Step 10: Verify lint, format, and the Python side**

Run each; all must pass:
```
msvc "cargo fmt --check"
msvc "cargo clippy --all-targets -- -D warnings"
msvc "uv sync --extra dev"
msvc "uv run pytest"
```
Expected: `cargo fmt --check` silent (exit 0); clippy clean; `uv sync` rebuilds the extension and writes `src/dhbw_scraper/_native.pyd`; pytest all-pass with **no skips** in `test_native_run_fetch.py` (a skip there means the extension did not build — investigate, do not proceed).

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: move the rust crate to src/scrape-engine, manifest to the root

The rust/ tree is gone: sources are at src/scrape-engine/, integration tests
at tests/scrape-engine/, and Cargo.toml sits at the repo root next to
pyproject.toml. Cargo reaches them via an explicit [lib] path plus [[test]]
entries, so cargo test --test <name> keeps working.

maturin's manifest-path is now redundant and dropped; the pre-commit hooks,
CI steps, and dependabot lose --manifest-path/rust/ along with it.

Also untracks src/dhbw_scraper/_native.pdb, a debug-symbol artifact committed
by accident, and ignores *.pdb."
```

---

### Task 2: Rename the Python package `dhbw_scraper` → `scraper`

Ends green. The extension is still `_native` — Task 3 handles it.

**Files:**
- Move: `src/dhbw_scraper/` → `src/scraper/`
- Modify: `pyproject.toml`, `src/scraper/crawl.py:1-11` (docstring), and 19 import sites across `tests/`

**Interfaces:**
- Consumes: the root `Cargo.toml` and `src/scrape-engine/` layout from Task 1.
- Produces: importable `scraper` package; `pyproject` `module-name = "scraper._native"`, script target `scraper.cli:main`. Task 3 changes `_native` → `_engine` in `pyproject.toml` and `crawl.py`.

- [ ] **Step 1: Move the package and purge stale artifacts**

Stale bytecode and a stale `.pyd` at the old path can keep a ghost `dhbw_scraper` importable and mask a broken rename.

```bash
cd "C:/Users/manue/Documents/GitHub/DH-DigitalHelper"
git mv src/dhbw_scraper src/scraper
rm -f src/scraper/_native.pyd
find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
```

- [ ] **Step 2: Repoint `pyproject.toml` at the new package**

Two edits. The console-script **name** stays `dhbw-scraper` — only its target moves:

```toml
[project.scripts]
dhbw-scraper = "scraper.cli:main"
```

```toml
[tool.maturin]
# Mixed Rust/Python layout, one source root: the pure-Python package tree stays
# authoritative under src/scraper/, and the Rust crate at src/scrape-engine/ is
# injected as the single submodule scraper._native. The Rust is a SIBLING of the
# package, not nested inside it — anything under the package dir is the wheel
# payload and would ship .rs files into site-packages.
python-source = "src"
module-name = "scraper._native"
features = ["pyo3/extension-module"]
```

- [ ] **Step 3: Rewrite the 19 test import sites**

Every occurrence is a bare module path swap. Apply:

```bash
cd "C:/Users/manue/Documents/GitHub/DH-DigitalHelper"
grep -rl "dhbw_scraper" tests --include=*.py | xargs sed -i 's/\bdhbw_scraper\b/scraper/g'
```

That covers, exactly:
- `tests/test_cli.py:3` — `from scraper import cli`
- `tests/test_config.py:5` — `from scraper.config import load_config`
- `tests/test_dashboard.py:3-4` — `from scraper import dashboard` / `from scraper import storage as st`
- `tests/test_dedup.py:9`, `tests/test_storage_docs.py:3`, `tests/test_storage_queue.py:5` — `from scraper import storage as st`
- `tests/test_extract.py:5-7` — `from scraper import extract, storage as st`; `from scraper.config import ...`; `from scraper.progress import Progress`
- `tests/test_fetch.py:1` — `from scraper import fetch as f`
- `tests/test_html_extract.py:3` — `from scraper.html_extract import _markdown_to_text, extract_html`
- `tests/test_native_run_fetch.py:21,25,26` — `pytest.importorskip("scraper._native", ...)`; `from scraper import crawl`; `from scraper.config import (...)`
- `tests/test_pdf_extract.py:1-2`, `tests/test_progress.py:4`, `tests/test_quality.py:1`

- [ ] **Step 4: Verify no `dhbw_scraper` remains in code**

Run: `grep -rn "dhbw_scraper" src tests --include=*.py | grep -v __pycache__`
Expected: **two** hits, both in `src/scraper/crawl.py` (lines 3 and 7) — prose in the module docstring, fixed in the next step. Any hit outside `crawl.py` means the sed missed a file.

- [ ] **Step 5: Fix the two stale docstrings in `crawl.py`**

`src/scraper/crawl.py` has two — the module docstring names the old package and the
now-gone `rust/` directory, and `run_fetch`'s docstring points at `rust/tests`.

Lines 1-11:
```python
"""Phase 1: queue-driven crawl with conditional-GET change detection.

The crawl engine itself lives in Rust (`scraper._native`, built from
``src/scrape-engine/``): a tokio async crawler with a single dedicated SQLite
writer task and an in-memory frontier, which owns all Phase-1 writes to the same
SQLite database Phase 2 reads. This module is now a thin adapter that maps the
parsed (and CLI-overridden) :class:`~scraper.config.Config` into the plain dict
the extension expects and forwards the call.

Phase 2 (extraction) stays in Python and is untouched.
"""
```

Line 63 — `rust/tests` moved to `tests/scrape-engine/` in Task 1:
```python
    ``fetch_fn`` and ``clock`` are accepted for source compatibility with the
    old signature but ignored — the Rust engine owns fetching and time. Testing
    uses the engine's own injectable HTTP client (see ``tests/scrape-engine``)
    plus the end-to-end fixture-server test in ``tests/test_native_run_fetch.py``.
```

(The `test_native_run_fetch.py` reference on the next line is still correct — that
file is renamed in Task 3, not here.)

- [ ] **Step 6: Reinstall under the new name and confirm the old one is gone**

The venv still has a `dhbw_scraper` dist installed. If it lingers, tests pass for the wrong reason.

Run: `msvc "uv sync --extra dev"`
Then: `msvc "uv run python -c \"import scraper; print(scraper.__file__)\""`
Expected: a path under `src\scraper\__init__.py` or site-packages — no ImportError.

Then: `msvc "uv run python -c \"import dhbw_scraper\""`
Expected: **`ModuleNotFoundError: No module named 'dhbw_scraper'`**. If this *succeeds*, a stale dist is shadowing the rename — remove it before continuing.

- [ ] **Step 7: Run the suites**

Run: `msvc "uv run pytest"`
Expected: all pass, no skips in `test_native_run_fetch.py`.

Run: `msvc "uv run ruff check . && uv run ruff format --check ."`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: rename the python package dhbw_scraper to scraper

src/<pkg>/ is the import name, not just a folder name, so the directory rename
carries the import with it: import scraper, entry point scraper.cli:main.

The dhbw-scraper console-script name is unchanged — only its target moves. The
generic top-level name is safe here: this venv only ever holds this application
plus trafilatura and pymupdf."
```

---

### Task 3: Rename the extension `_native` → `_engine`

`scraper._native` disagrees with `src/scrape-engine/`, and `native` is the name this refactor set out to remove. Ends green.

**Files:**
- Modify: `Cargo.toml` (`[package] name`, `[lib] name`), `pyproject.toml` (`module-name`), `src/scrape-engine/lib.rs:1-2,87-92`, `src/scraper/crawl.py:3,15,19,63-64,68,87`
- Modify: `use _native::` in all 4 files under `tests/scrape-engine/`
- Move: `tests/test_native_run_fetch.py` → `tests/test_engine_run_fetch.py`

**Interfaces:**
- Consumes: Task 2's `scraper` package; Task 1's root manifest.
- Produces: `scraper._engine` as the extension module; Rust crate `scrape_engine` with `[lib] name = "_engine"`, imported in Rust tests as `use _engine::`.

> **Note for the reviewer:** two steps here **extend spec D3**, which named only the Python package and the extension module. Both are included because they are otherwise the last `native` / `dhbw_scraper` strings left in the tree, and "remove the name `native`" is what this refactor set out to do. Each is independently rejectable — nothing else depends on either.
>
> - **Step 1**, the `[package] name` rename (`dhbw_scraper_native` → `scrape_engine`). Surfaces in cargo output and the wheel's SBOM (`scraper_native.cyclonedx.json`). Internal to Cargo: the wheel and dist names are unaffected.
> - **Step 6**, renaming `tests/test_native_run_fetch.py` → `tests/test_engine_run_fetch.py`. Pytest discovers by the `test_*.py` glob, so the name is free to change.

- [ ] **Step 1: Rename the crate and the lib in `Cargo.toml`**

```toml
[package]
name = "scrape_engine"
version = "0.3.0"
edition = "2021"
publish = false
# Integration tests live in tests/scrape-engine/, which Cargo's autodiscovery
# (tests/*.rs, tests/*/main.rs) does not match — they are declared explicitly
# below. tests/ is shared with pytest; cargo ignores the .py files.
autotests = false

[lib]
name = "_engine"
path = "src/scrape-engine/lib.rs"
crate-type = ["cdylib", "rlib"]
```

Leave the four `[[test]]` blocks and all dependency sections untouched.

- [ ] **Step 2: Rename the PyO3 module in `lib.rs`**

`src/scrape-engine/lib.rs` line 2, the module docstring:
```rust
//! Phase-1 crawler for `dhbw-scraper`, implemented in Rust and exposed to Python
//! as the `scraper._engine` extension module.
```

And lines 87-92, the `#[pymodule]` fn — its **name must match `[lib] name`**:
```rust
#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_fetch, m)?)?;
    m.add_function(wrap_pyfunction!(backfill_links, m)?)?;
    Ok(())
}
```

- [ ] **Step 3: Repoint the Rust integration tests**

All four files under `tests/scrape-engine/` import the lib by its crate name:

```bash
cd "C:/Users/manue/Documents/GitHub/DH-DigitalHelper"
sed -i 's/\b_native::/_engine::/g' tests/scrape-engine/*.rs
```

That rewrites 16 sites: `backfill.rs:5-8`, `links_parity.rs:4`, `orchestration.rs:9-12,215,878,879,880`, `sitemap_parity.rs:7-8`.

- [ ] **Step 4: Repoint `pyproject.toml`**

```toml
module-name = "scraper._engine"
```

Also update the `[tool.maturin]` comment's `scraper._native` → `scraper._engine`.

- [ ] **Step 5: Repoint the Python adapter**

`src/scraper/crawl.py` — the docstring (line 3), the import (line 15), and the two call sites (lines 68, 87). Note `_native_config` is a *local helper*, not part of the extension: rename it to `_engine_config` for consistency, including its call sites.

```python
"""Phase 1: queue-driven crawl with conditional-GET change detection.

The crawl engine itself lives in Rust (`scraper._engine`, built from
``src/scrape-engine/``): a tokio async crawler with a single dedicated SQLite
writer task and an in-memory frontier, which owns all Phase-1 writes to the same
SQLite database Phase 2 reads. This module is now a thin adapter that maps the
parsed (and CLI-overridden) :class:`~scraper.config.Config` into the plain dict
the extension expects and forwards the call.

Phase 2 (extraction) stays in Python and is untouched.
"""

from __future__ import annotations

from . import _engine
from .progress import Progress


def _engine_config(config) -> dict:
```

Line 64's reference to `tests/test_native_run_fetch.py` is updated in Step 6, where that file is renamed. Lines 68 and 87 become:
```python
    return _engine.run_fetch(_engine_config(config), run_id, force_full, progress)
```
```python
    return _engine.backfill_links(_engine_config(config), progress)
```

- [ ] **Step 6: Rename the e2e test file and repoint its importorskip guard**

The last `native` in the tree is this file's own name. Pytest discovers by the
`test_*.py` glob, so renaming it is free:

```bash
cd "C:/Users/manue/Documents/GitHub/DH-DigitalHelper"
git mv tests/test_native_run_fetch.py tests/test_engine_run_fetch.py
```

Then, in `tests/test_engine_run_fetch.py`, line 8 (docstring) and line 21:

```python
Skipped automatically if the ``_engine`` extension has not been built yet
(``maturin develop``).
```
```python
pytest.importorskip(
    "scraper._engine",
    reason="Rust extension not built; run `maturin develop` first.",
)
```

Finally, `src/scraper/crawl.py` line 64 names this file — update it too:
```python
    plus the end-to-end fixture-server test in ``tests/test_engine_run_fetch.py``.
```

- [ ] **Step 7: Verify no `_native` or `native` remains**

Run: `grep -rni "native" src tests Cargo.toml pyproject.toml | grep -v __pycache__`
Expected: **no output**. This is case-insensitive and unfiltered on purpose — after Step 6 there is no legitimate `native` anywhere in code, including filenames-in-docstrings.

Note: `rustls-tls-native-roots` in `Cargo.toml`'s reqwest features is an upstream
feature name and **must not** be renamed. If the grep shows only that line, the step
passes — adjust with `| grep -v "native-roots"` and confirm nothing else remains.

- [ ] **Step 8: Delete the stale `_native.pyd` and rebuild**

The old extension is still on disk at the old module name and would satisfy a stale import.

```bash
rm -f src/scraper/_native.pyd
```

Run: `msvc "uv sync --extra dev"`
Expected: builds `src/scraper/_engine.pyd`.

Run: `ls src/scraper/*.pyd`
Expected: `src/scraper/_engine.pyd` and nothing else.

- [ ] **Step 9: Run the full gate**

```
msvc "cargo test"
msvc "cargo fmt --check"
msvc "cargo clippy --all-targets -- -D warnings"
msvc "uv run pytest"
msvc "uv run ruff check . && uv run ruff format --check ."
```
Expected: all pass. `cargo test` now reports `Running unittests src\scrape-engine\lib.rs (target\debug\deps\_engine-<hash>.exe)`. pytest shows **no skip** in `test_native_run_fetch.py`.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: rename the extension module _native to _engine

scraper._native disagreed with src/scrape-engine/, and 'native' says nothing
about what the crate does. The PyO3 module fn, the [lib] name, and the Rust
tests' use _native:: all move together, since PyO3 requires the #[pymodule] fn
name to match the lib name.

Also renames the cargo package dhbw_scraper_native -> scrape_engine, the last
dhbw_scraper* string left. Internal to cargo: the wheel and dist names are
unaffected."
```

---

### Task 4: Update the docs and the knowledge graph

Prose only — no code. Ends green.

**Files:**
- Modify: `README.md:58-59,63,113,173,368-389`, `CLAUDE.md:19-32,69,73-74,81,85`, `config.toml:14,70`, `src/scraper/storage.py:117`

**Interfaces:**
- Consumes: the final layout from Tasks 1-3.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Rewrite the README project-layout tree**

`README.md` — replace the fenced block under `## Project layout` (lines ~368-389):

```
src/scraper/         Phase 2 + CLI (pure Python)
  config.py        load + validate config.toml
  storage.py       SQLite schema (incl. links), Phase-2 claims/upserts/delta, raw-file cache
  fetch.py         content-type classification + ext_for (used by Phase 2 extraction)
  crawl.py         phase 1 adapter -> scraper._engine.run_fetch (Rust engine)
  html_extract.py  trafilatura -> markdown + metadata
  pdf_extract.py   PyMuPDF4LLM -> markdown/text (lazy import, lightweight)
  quality.py       moderate quality gate (min words, nav ratio, login/cookie/error filters)
  extract.py       phase 2: extract, quality-gate, materialize documents
  progress.py      stderr progress reporting (TTY status line / plain log lines)
  cli.py           `fetch` / `extract` / `run` / `stats` / `delta` entrypoints

src/scrape-engine/   Phase-1 crawler (compiled to scraper._engine via maturin/PyO3)
  crawl.rs          orchestrator: frontier, per-host workers, rate limit, termination
  writer.rs         single SQLite writer + in-memory frontier (no write contention)
  fetch.rs          reqwest conditional-GET HttpClient + content-type classify
  links.rs          <a href> discovery, in-domain filter, crawler-trap rules
  sitemap.rs        sitemap + nested sitemap-index discovery
  storage.rs        SQLite schema + write ops + content-addressed raw cache
  {config,outcome,progress,lib}.rs  config mapping, change detection, progress, PyO3

Cargo.toml           root manifest: [lib] path -> src/scrape-engine/lib.rs
tests/               pytest suites + fixtures/
  scrape-engine/     links/sitemap parity + end-to-end orchestration (cargo)
```

- [ ] **Step 2: Fix the four other README references**

Lines ~58-59:
```
**Phase 1 (fetch/crawl) is implemented in Rust** (`src/scrape-engine/`, exposed to Python as the
`scraper._engine` extension via [PyO3](https://pyo3.rs) + built with
```

Line ~63:
```
It owns every Phase-1 write to the SQLite DB; `src/scraper/crawl.py` is now a thin
```

Line ~113 — drop the flag:
```powershell
cargo test
```

Line ~173:
```
denylisted outright in `src/scrape-engine/links.rs`; the per-host cap only catches *unknown* ones.
```

- [ ] **Step 3: Update CLAUDE.md**

Lines 19-32 — the two path references in the architecture bullets:
```
- **Phase 1 — fetch/crawl — is Rust** (`src/scrape-engine/`), compiled to the Python extension
  `scraper._engine` via [PyO3](https://pyo3.rs) + [maturin](https://www.maturin.rs).
```
and, in the same bullet, `[`crawl.py`](src/dhbw_scraper/crawl.py)` → `[`crawl.py`](src/scraper/crawl.py)`.

The Rust-modules paragraph ending line 32:
```
`backfill.rs`, `lib.rs` (PyO3 boundary). `tests/scrape-engine/` holds `links_parity`,
`sitemap_parity`, `backfill`, and end-to-end `orchestration` tests.
```

Lines 69-74 — the commands lose the flag:
```powershell
cargo test                                      # Rust tests (needs python3.dll on PATH; see README "Windows")
```
```powershell
cargo clippy --all-targets -- -D warnings
cargo fmt
```

Lines 81 and 85 — two markdown links in the CLI section still point at the old path:
```
analysis via [`dashboard.py`](src/scraper/dashboard.py)) · `delta --since <ts>`
```
```
cache). All accept `--config PATH`. See [`cli.py`](src/scraper/cli.py) and README
```

- [ ] **Step 4: Update `config.toml` comments**

Line 14:
```toml
# in_domain() matches both the bare host and any subdomain of it (src/scrape-engine/links.rs),
```
Line 70:
```toml
# denylisted outright in src/scrape-engine/links.rs; this only catches unknown ones.
```

- [ ] **Step 5: Update the `storage.py` docstring**

`src/scraper/storage.py:117`:
```
-- external alike. Written by the Rust Phase-1 crawler (see src/scrape-engine/storage.rs,
```

- [ ] **Step 6: Verify no stale path references survive**

Run:
```bash
grep -rn "dhbw_scraper\|rust/src\|rust/tests\|manifest-path\|_native\|workspaces: rust" \
  README.md CLAUDE.md config.toml pyproject.toml .pre-commit-config.yaml .gitignore src tests .github \
  | grep -v __pycache__
```
Expected: **no output**.

`docs/superpowers/` is deliberately outside the searched paths — the historical specs
keep their `rust/` references (spec D5), and this plan and its own spec describe the
migration and necessarily name the old paths.

- [ ] **Step 7: Final full gate**

```
msvc "cargo test"
msvc "cargo fmt --check"
msvc "cargo clippy --all-targets -- -D warnings"
msvc "uv run pytest"
msvc "uv run ruff check . && uv run ruff format --check ."
msvc "uv run pre-commit run --all-files"
```
Expected: all pass.

- [ ] **Step 8: Rebuild the knowledge graph**

Run: `graphify update .`
Expected: completes; `graphify-out/graph.json` re-points at `src/scraper/` and `src/scrape-engine/`. `graphify-out/` is gitignored, so nothing to commit.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "docs: repoint the docs at the one-source-root layout

README project-layout tree, CLAUDE.md architecture + commands, config.toml
comments, and the storage.py docstring now name src/scraper/ and
src/scrape-engine/. The cargo commands lose --manifest-path.

Historical specs under docs/superpowers/specs/ keep their rust/ references —
they record past decisions and are not live docs."
```

---

## Done when

- `rust/` does not exist; `git log --follow src/scrape-engine/crawl.rs` reaches its pre-move history.
- `import dhbw_scraper` raises `ModuleNotFoundError`; `import scraper` works.
- `src/scraper/_engine.pyd` is the only `.pyd`; no `.pdb` is tracked.
- `cargo test`, `cargo clippy -D warnings`, `cargo fmt --check`, `pytest`, `ruff check`, `ruff format --check`, and `pre-commit run --all-files` all pass.
- `grep -rn "dhbw_scraper" README.md CLAUDE.md config.toml src tests` returns nothing.
