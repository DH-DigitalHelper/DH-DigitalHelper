{
  description = "DHBW Heidenheim scraper — RAG data pipeline (scrape stage)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python314
            pkgs.uv
            pkgs.git
            pkgs.git-lfs
            # Phase-1 crawler is a Rust/PyO3 extension built with maturin. rustc,
            # cargo and a C toolchain (for rusqlite's bundled SQLite) are needed
            # to build the wheel; reqwest uses rustls so no system OpenSSL.
            pkgs.rustc
            pkgs.cargo
            pkgs.maturin
            pkgs.clippy
            pkgs.rustfmt
            # Cross-platform git hooks (lint/format on commit, Conventional-Commit
            # validation, tests on push) are declared in the committed
            # .pre-commit-config.yaml — the single source of truth shared with the
            # Windows/uv workflow. Nix just provides the runner here.
            pkgs.pre-commit
            # Secret scanning is a local pre-commit hook (language: system) that
            # shells out to this binary — provide it so Nix contributors get the
            # hook for free. Windows installs it separately (see
            # .pre-commit-config.yaml).
            pkgs.trufflehog
          ];

          # uv-managed venv; tell uv to use the Nix Python and not download its own.
          env = {
            UV_PYTHON = "${pkgs.python314}/bin/python3";
            UV_PYTHON_DOWNLOADS = "never";
          };

          shellHook = ''
            # Install the git hooks declared in .pre-commit-config.yaml
            # (pre-commit + commit-msg + pre-push stages). Needs network the first
            # time to fetch hook repos; tolerate offline / non-repo checkouts.
            if [ -d .git ]; then
              pre-commit install --install-hooks >/dev/null 2>&1 || true
            fi

            # Install Git LFS's filter hooks (pre-push/post-checkout/…) for this
            # repo, in case any large binary assets are ever tracked via LFS.
            # Scraped data itself lives in the (gitignored) SQLite database
            # under data/, not in git — see README.md's "Data layout" section.
            if [ -d .git ]; then
              git lfs install --local >/dev/null 2>&1 || true
            fi

            echo "dhbw-scraper devShell — python: $(python3 --version), uv: $(uv --version)"
            echo "Run 'uv sync' to install dependencies, then 'uv run dhbw-scraper --help'."
          '';
        };
      });
}
