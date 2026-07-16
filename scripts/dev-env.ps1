# Load the MSVC build environment into the current PowerShell session.
#
# Building the Rust Phase-1 extension needs cl.exe/link.exe on PATH (for
# rusqlite's bundled SQLite). Rust's automatic MSVC detection is unreliable on
# some machines, so we source the VS "vcvars64" environment explicitly.
#
# Usage (dot-source it, then build in the same shell):
#     . .\scripts\dev-env.ps1
#     uv sync --extra dev
#     uv run maturin develop --release
#
# Alternatively, just open the "x64 Native Tools Command Prompt for VS 2022"
# (installed with the Build Tools) — it has this environment preloaded.

$ErrorActionPreference = 'Stop'

function Find-VcVars {
    # 1) Ask vswhere for the latest install path (any product/edition).
    $vswhereCandidates = @(
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\Installer\vswhere.exe"
    )
    $vswhere = $vswhereCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($vswhere) {
        $paths = & $vswhere -latest -prerelease -products * -property installationPath 2>$null
        foreach ($p in $paths) {
            $vc = Join-Path $p 'VC\Auxiliary\Build\vcvars64.bat'
            if (Test-Path $vc) { return $vc }
        }
    }
    # 2) Fall back to globbing the standard install roots (year\edition\...).
    $roots = @(
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio",
        "$env:ProgramFiles\Microsoft Visual Studio"
    )
    foreach ($root in $roots) {
        $hit = Get-ChildItem -Path (Join-Path $root '*\*\VC\Auxiliary\Build\vcvars64.bat') `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

$vcvars = Find-VcVars
if (-not $vcvars) {
    throw "vcvars64.bat not found. Install the Visual Studio Build Tools with the 'Desktop development with C++' workload."
}

cmd /c "`"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($matches[1])" -Value $matches[2] }
}

$cl = (Get-Command cl.exe -ErrorAction SilentlyContinue).Source
Write-Host "MSVC build environment loaded from: $vcvars"
Write-Host "cl.exe: $cl"

# Point pyo3 at the project venv, and put the interpreter's python3.dll directory
# on PATH so `cargo test` binaries (which link libpython) can load it on Windows.
# Not needed for `maturin develop` / `uv sync` (the host CPython provides the
# symbols there), only for running the Rust test binaries directly.
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    $env:PYO3_PYTHON = $venvPy
    $base = & $venvPy -c "import sys; print(sys.base_prefix)" 2>$null
    if ($base -and (Test-Path $base)) {
        $env:Path = "$base;$env:Path"
        Write-Host "cargo test ready: python3.dll on PATH ($base)"
    }
}
