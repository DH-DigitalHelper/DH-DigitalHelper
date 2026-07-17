# Repo Layout — One Source Root: Design

**Status:** approved · **Date:** 2026-07-17 · **Branch:** `feat/create-dh-scraper`

## Why

The tree splits the two languages across two top-level directories: `rust/` for Phase 1
and `src/dhbw_scraper/` for Phase 2. Nothing is broken by it — it is a normal mixed
layout — but it reads as two projects sharing a repo rather than one pipeline in two
languages. This spec collapses both under a single `src/` root.

This is a **pure move-and-rename**. No behavior, no logic, no schema, no dependency
changes. Every `.rs` and `.py` file keeps its contents except for import lines and the
module-name rename in D3.

## Target layout

```
Cargo.toml                 <- rust/Cargo.toml
Cargo.lock                 <- rust/Cargo.lock
pyproject.toml
src/
  scraper/                 <- src/dhbw_scraper/      (import scraper)
    __init__.py cli.py config.py crawl.py dashboard.py
    extract.py fetch.py html_extract.py markdown.py
    pdf_extract.py progress.py quality.py storage.py
    _engine.pyd                                       (build output, gitignored)
  scrape-engine/           <- rust/src/
    lib.rs backfill.rs config.rs crawl.rs fetch.rs links.rs
    outcome.rs progress.rs sitemap.rs storage.rs writer.rs
tests/
  test_*.py  fixtures/                                (unchanged)
  scrape-engine/           <- rust/tests/
    backfill.rs links_parity.rs orchestration.rs sitemap_parity.rs
```

`rust/` ceases to exist. All moves use `git mv` so rename detection preserves history.

## Decisions

### D1 · Rust is a *sibling* of the Python package, never nested inside it

`src/scrape-engine/` sits beside `src/scraper/`, not within it.

This was tested, not assumed. An earlier iteration of this design nested the Rust at
`src/scraper/engine/`. A real maturin build of that layout put the Rust source
**inside the wheel**:

```
scraper/__init__.py
scraper/_native.pyd
scraper/cli.py
scraper/engine/crawl.rs      <- shipped to site-packages
scraper/engine/lib.rs        <- shipped to site-packages
```

The package directory is not a source folder — it is the wheel payload, copied
recursively into site-packages. Rust source has no business being installed at runtime.
The sibling layout was then built and verified to produce a **clean** wheel — only
`scraper/__init__.py`, `scraper/cli.py`, and the compiled extension, no `.rs` — because
maturin only copies `<python-source>/<module>/**`. A sibling under `python-source` is
ignored, so **no `[tool.maturin] exclude` is required**.

(Both probe builds predate D3 and so named the extension `_native.pyd`. The module name
is orthogonal to what maturin copies; the finding holds under `_engine`.)

This also matches ecosystem practice: maturin's own documented mixed layout puts Rust and
Python side by side (`src/` + `python/<pkg>/`) — never nested. Other mixed projects are
recalled to use the two-subtrees-under-`src/` shape adopted here (`cryptography`), but
that recollection is unverified and is not what the decision rests on: the wheel probe
above is.

### D2 · `Cargo.toml` moves to the repo root and absorbs the config cost

The root manifest is where this layout is paid for:

```toml
[package]
autotests = false

[lib]
name = "_engine"
path = "src/scrape-engine/lib.rs"
crate-type = ["cdylib", "rlib"]

[[test]]
name = "backfill"
path = "tests/scrape-engine/backfill.rs"
# ... one entry each for links_parity, orchestration, sitemap_parity
```

`autotests = false` plus four explicit `[[test]]` entries is ~20 lines of non-default
Cargo config. **Accepted deliberately**: it preserves the existing test structure
exactly, keeps `cargo test --test orchestration` working, and confines the cost to one
file. The zero-config alternative — collapsing the four integration tests into a single
auto-discovered `tests/scrape-engine/main.rs` — was rejected because it merges four test
binaries into one and changes how they are invoked.

Verified by build: Cargo resolves the custom `[lib] path` three levels deep, `mod crawl;`
resolves relative to the lib root's directory (`src/scrape-engine/crawl.rs`), the
hyphenated directory name is inert (it is a path, not an identifier), the explicit
`[[test]]` paths work, and `.rs` and `.py` files coexisting under `tests/` confuse
neither cargo nor pytest.

### D3 · The package is renamed `dhbw_scraper` → `scraper`, the extension `_native` → `_engine`

`src/<pkg>/` is the import name, not merely a folder name. Two renames:

- **Package:** `dhbw_scraper` → `scraper`. Import becomes `import scraper`; entry point
  becomes `scraper.cli:main`. 21 reference sites across `src/` and `tests/` (only 2 in
  `src/` — the package uses relative imports internally).
- **Extension module:** `_native` → `_engine`, so `scraper._engine` agrees with
  `src/scrape-engine/`. Touches `[lib] name`, `[tool.maturin] module-name`, the
  `#[pymodule]` fn in `lib.rs`, `use _native::` in the four Rust tests, and `crawl.py`'s
  import.

The name-collision risk of a generic top-level `scraper` import is **accepted**: this
venv only ever holds this application plus trafilatura and pymupdf, so nothing competes
for the name.

### D4 · The tracked `_native.pdb` is deleted, not moved

`src/dhbw_scraper/_native.pdb` is **tracked** — a Windows debug-symbol artifact committed
by accident. It sits inside the directory this refactor moves, so a blind `git mv` would
carry it to `src/scraper/_native.pdb`, stale under both renames in D3 and still tracked.

It is `git rm --cached`'d and `*.pdb` added to `.gitignore` as part of this work. This is
in scope because the file is in the moved path; unrelated hygiene is not pursued.

The sibling `_native.pyd` needs no such handling — it is already ignored, incidentally, by
`.gitignore`'s `*.py[cod]` rule (the `d` in `[cod]` matches Windows extension modules).

### D5 · Historical specs are not rewritten

`docs/superpowers/specs/2026-07-14-*` and `2026-07-16-*` reference `rust/src/*.rs`. They
are records of past decisions, not live documentation, and are left untouched. Only
current docs (README, CLAUDE.md, `config.toml` comments, docstrings) are updated.

## Change inventory

| File | Change |
|---|---|
| `Cargo.toml` | Moved to root; add `autotests`, `[lib] path`/`name`, 4 `[[test]]` entries |
| `Cargo.lock` | Moved to root |
| `pyproject.toml` | **Delete** `manifest-path` (now the default); `module-name = "scraper._engine"`; script → `scraper.cli:main`. `python-source = "src"` unchanged |
| `.pre-commit-config.yaml` | Drop `--manifest-path rust/Cargo.toml` from rustfmt, clippy, cargo-test |
| `.github/workflows/ci.yml` | Drop `--manifest-path` ×3; drop `workspaces: rust`; fix the `rust/Cargo.toml` comment |
| `.github/dependabot.yml` | Cargo `directory: /rust` → `/` |
| `.gitignore` | `rust/target/` → `/target/`; add `*.pdb` (D4) |
| `src/dhbw_scraper/_native.pdb` | `git rm --cached` — tracked build artifact (D4) |
| `src/**`, `tests/**` | 21 import sites for the package rename; `use _engine::` in 4 Rust tests |
| `README.md`, `CLAUDE.md` | Layout tree, commands, prose |
| `config.toml` | Comments at the `in_domain()` / denylist references |
| `crawl.py`, `storage.py` | Docstrings referencing `rust/` |

`.cargo/config.toml` needs no change — it already sits at the repo root and is
discovered by walking up from the working directory. The move makes it *more* correct,
since it now sits beside the manifest it configures.

## Testing

No new tests. This refactor is correct exactly when the existing suites pass unchanged —
any behavior delta is a bug in the move. The full gate must pass in an MSVC-enabled
environment:

```
uv sync --extra dev          # rebuilds the extension at the new paths
uv run pytest                # Phase 2 + CLI + native e2e
cargo test                   # lib + 4 integration tests
cargo clippy --all-targets -- -D warnings
cargo fmt --check
uv run ruff check . && uv run ruff format --check .
```

`cl.exe` is not on the default PATH, but MSVC bootstraps via
`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"`,
which is confirmed working — verification does not need to be handed off.

Finish with `graphify update .` to re-point the knowledge graph at the new paths.

## Risks

- **Stale build artifacts.** `src/dhbw_scraper/_native.pyd` (ignored) and `_native.pdb`
  (tracked — see D4) are build output at the old path, orphaned by the move. Delete both,
  plus `rust/target/` and the `__pycache__` trees, or a stale `dhbw_scraper` may keep
  importing and mask a broken rename.
- **The venv holds the old dist.** `uv sync` must reinstall under the new name;
  a lingering `dhbw_scraper` in site-packages would let tests pass for the wrong reason.
  Confirm `import dhbw_scraper` **fails** after the migration.
- **Rename detection.** Using `git mv` (not delete + add) keeps `git log --follow`
  working across the move.
