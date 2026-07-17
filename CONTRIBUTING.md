# Contributing

Thanks for working on the DHBW scraper. This repo enforces a small, cross-platform
dev-tooling setup so the same checks run on Windows, macOS, Linux, and Nix.

## One-time setup

The git hooks are driven by [pre-commit](https://pre-commit.com), declared in
[`.pre-commit-config.yaml`](./.pre-commit-config.yaml) (the single source of truth). After
cloning and installing deps, install the hooks once:

```powershell
# Windows: run from the "x64 Native Tools Command Prompt for VS 2022" so the MSVC env
# needed to build the Rust extension is loaded (see README "Setup → Windows").
uv sync --extra dev            # installs deps incl. pre-commit, builds the extension
uv run pre-commit install --install-hooks
```

```sh
# NixOS: `nix develop` installs the hooks for you via the flake's shellHook.
# Otherwise, the same two commands as above:
uv sync --extra dev
uv run pre-commit install --install-hooks
```

## What runs, and when

| Stage | Hooks | Notes |
| --- | --- | --- |
| **on commit** (`pre-commit`) | trailing-whitespace, EOF, check-yaml/toml, merge-conflict, large-files; **ruff** (lint `--fix`) + **ruff-format**; **rustfmt** | Fast, no compilation. |
| **commit message** (`commit-msg`) | **conventional-pre-commit** | Rejects messages that aren't Conventional Commits. |
| **on push** (`pre-push`) | **pytest** (always); **clippy** + **cargo test** (when Rust files changed) | The Rust hooks compile — see the Windows note below. |

Run everything manually any time:

```sh
uv run pre-commit run --all-files                       # commit-stage hooks
uv run pre-commit run --hook-stage pre-push --all-files  # push-stage tests
```

Bump the pinned hook versions with `uv run pre-commit autoupdate`.

## Commit messages: Conventional Commits

Every commit subject must follow [Conventional Commits](https://www.conventionalcommits.org):

```
<type>(<optional scope>)<optional !>: <description>
```

Allowed types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`,
`revert`, `style`, `test`. A trailing `!` (or a `BREAKING CHANGE:` footer) marks a breaking
change.

Examples:

```
feat(crawl): add per-host worker override flag
fix(extract): reject nav-dominated pages under the word gate
docs: document the change-detection flow
chore(deps): bump trafilatura to 2.1
```

This is validated locally by the `commit-msg` hook and again in CI on pull requests.

## Windows: Rust hooks need the MSVC environment

`clippy` and `cargo test` compile `rusqlite`'s bundled SQLite, which needs
`cl.exe`/`link.exe` on `PATH`. Run `git push` (and `pre-commit run --hook-stage pre-push`)
from the *x64 Native Tools Command Prompt for VS 2022*, which has that environment
preloaded:

```powershell
git push
```

If you push from a plain shell, the Rust hooks fail with `cl.exe not found` — CI still runs
them on a Linux runner, so nothing broken slips through, but a clean local push needs the
MSVC env. (`pytest` on push does **not** need it: the extension is already built.)

## CI

CI is split by concern across three Linux workflows; green CI is required before merge:

- [`ci.yml`](./.github/workflows/ci.yml) — lint & test, on pull requests and pushes
  to `main`: ruff lint + format check, `rustfmt --check`, clippy (`-D warnings`),
  `pytest`, and `cargo test`.
- [`commit-lint.yml`](./.github/workflows/commit-lint.yml) — validates every PR
  commit subject against Conventional Commits.
- [`secret-scan.yml`](./.github/workflows/secret-scan.yml) — TruffleHog scan of the
  PR's diff, the server-side backstop to the local `trufflehog` hook.

The two PR-only workflows run on pull requests; a push to a PR branch triggers only
the pull_request event, so `ci.yml` runs once per push rather than twice.

## Dependency updates

[`.github/dependabot.yml`](./.github/dependabot.yml) opens weekly Dependabot PRs for the
Python deps (uv), the Rust crate (cargo), and the GitHub Actions. Minor + patch bumps are
grouped into one PR per ecosystem; majors open individually. The config sets a
`commit-message.prefix` (`chore(deps):` / `ci(deps):`) so Dependabot's PRs pass the
Conventional-Commit check above.
