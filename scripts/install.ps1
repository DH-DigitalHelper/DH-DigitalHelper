<#
.SYNOPSIS
    One-shot dev install for dhbw-scraper on Windows.

.DESCRIPTION
    Builds the project into a local .venv, installing any missing prerequisites
    via winget along the way and handling the repo's #1 gotcha: the Rust Phase-1
    extension (scraper._engine) needs the MSVC C compiler on PATH, so a plain
    shell fails `uv sync` with "cl.exe not found".

    Steps (each is skipped when already satisfied, so the script is re-runnable):

        1. uv        -> winget install astral-sh.uv
        2. Rust      -> winget install Rustlang.Rustup
        3. MSVC C++  -> winget install Microsoft.VisualStudio.2022.BuildTools
                        (Desktop development with C++ / VCTools workload), then
                        import the "x64 Native Tools" environment into this session
        4. uv sync --extra dev            # deps + build scraper._engine
        5. uv run pre-commit install ...  # git hooks (unless -NoHooks)
        6. import scraper._engine         # smoke test

    winget must be present (ships as "App Installer" on Windows 10/11). Installing
    the VS Build Tools is a multi-GB download and needs administrator elevation --
    winget raises a UAC prompt; approve it when it appears. The script makes no
    changes outside the repo, its .venv, and whatever winget installs.

.PARAMETER NoHooks
    Skip installing the pre-commit git hooks.

.PARAMETER Quiet
    Suppress step progress messages (errors are still shown).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -NoHooks
#>
[CmdletBinding()]
param(
    [switch]$NoHooks,
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- helpers ---------------------------------------------------------------

function Write-Step([string]$Message) {
    if (-not $Quiet) { Write-Host "==> $Message" -ForegroundColor Cyan }
}

function Test-OnPath([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-IsAdmin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# Merge freshly-installed PATH entries from the registry (Machine + User) into the
# current session WITHOUT dropping anything already present. Appending (not
# replacing) preserves session-only entries such as an imported MSVC environment or
# a launching "x64 Native Tools" prompt.
function Update-SessionPath {
    $fromRegistry = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) -join ';'
    $existing  = $env:PATH -split ';'
    $additions = $fromRegistry -split ';' | Where-Object { $_ -and ($existing -notcontains $_) }
    if ($additions) {
        $env:PATH = ($env:PATH.TrimEnd(';'), ($additions -join ';')) -join ';'
    }
}

# Install a package by winget id, then refresh the session PATH so the new tool is
# callable in-process. Only called when the tool is confirmed absent, so any
# non-zero exit is a real failure.
function Install-ViaWinget([string]$Id, [string[]]$ExtraArgs = @()) {
    if (-not (Test-OnPath 'winget')) {
        throw ("winget not found. Install 'App Installer' from the Microsoft Store " +
               "(or install the missing tools manually), then re-run this script.")
    }
    Write-Step "Installing $Id via winget"
    $wingetArgs = @('install', '--id', $Id, '-e',
                    '--accept-package-agreements', '--accept-source-agreements') + $ExtraArgs
    & winget @wingetArgs
    if ($LASTEXITCODE -ne 0) {
        throw "winget install of $Id failed (exit code $LASTEXITCODE)."
    }
    Update-SessionPath
}

# Run a native command and abort if it returns a non-zero exit code. PowerShell's
# $ErrorActionPreference='Stop' does not catch native exit codes, so gate on
# $LASTEXITCODE explicitly.
function Invoke-Checked([scriptblock]$Command, [string]$What) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

# Locate vcvars64.bat: prefer vswhere, then fall back to probing the well-known
# install paths. (`vswhere -requires ...VC.Tools.x86.x64` returns nothing on some
# BuildTools installs, so the probe is the reliable path.)
function Find-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $installPath = & $vswhere -latest -products * -property installationPath 2>$null |
            Select-Object -First 1
        if ($installPath) {
            $candidate = Join-Path $installPath 'VC\Auxiliary\Build\vcvars64.bat'
            if (Test-Path $candidate) { return $candidate }
        }
    }

    $roots    = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    $years    = @('2022', '2019')
    $editions = @('BuildTools', 'Community', 'Professional', 'Enterprise')
    foreach ($root in $roots) {
        foreach ($year in $years) {
            foreach ($edition in $editions) {
                $candidate = Join-Path $root "Microsoft Visual Studio\$year\$edition\VC\Auxiliary\Build\vcvars64.bat"
                if (Test-Path $candidate) { return $candidate }
            }
        }
    }
    return $null
}

# Import the MSVC environment (PATH/INCLUDE/LIB/LIBPATH, ...) exported by
# vcvars64.bat into the current PowerShell session.
function Import-VcVars([string]$VcVarsPath) {
    $lines = & cmd.exe /c "`"$VcVarsPath`" >nul 2>&1 && set"
    if ($LASTEXITCODE -ne 0) {
        throw "Importing the MSVC environment from '$VcVarsPath' failed."
    }
    foreach ($line in $lines) {
        if ($line -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2])
        }
    }
}

# --- main ------------------------------------------------------------------

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Write-Step "Repo root: $repoRoot"

    # Pick up anything a prior run installed but that this session hasn't seen yet.
    Update-SessionPath

    # 1. uv (Python packaging + venv + build driver).
    if (Test-OnPath 'uv') {
        Write-Step 'uv already installed'
    }
    else {
        Install-ViaWinget 'astral-sh.uv'
        if (-not (Test-OnPath 'uv')) {
            throw 'Installed uv but it is not on PATH. Open a new shell and re-run.'
        }
    }

    # 2. Rust toolchain (cargo/rustc) for building the extension.
    if (Test-OnPath 'cargo') {
        Write-Step 'Rust (cargo) already installed'
    }
    else {
        # Rustlang.Rustup runs rustup-init, which installs the default
        # stable-x86_64-pc-windows-msvc toolchain.
        Install-ViaWinget 'Rustlang.Rustup'
        $cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
        if ((Test-Path $cargoBin) -and (($env:PATH -split ';') -notcontains $cargoBin)) {
            $env:PATH = "$env:PATH;$cargoBin"
        }
        if (-not (Test-OnPath 'cargo')) {
            throw 'Installed Rust but cargo is not on PATH. Open a new shell and re-run.'
        }
    }

    # 3. MSVC C compiler on PATH (rusqlite's bundled SQLite needs it).
    if (Test-OnPath 'cl.exe') {
        Write-Step 'MSVC environment already active (cl.exe on PATH)'
    }
    else {
        $vcvars = Find-VcVars
        if (-not $vcvars) {
            Write-Step 'VS Build Tools not found - installing via winget (multi-GB download)'
            if (-not (Test-IsAdmin)) {
                Write-Step 'This install needs elevation - approve the UAC prompt when it appears.'
            }
            # --override replaces the installer args entirely: install the C++ build
            # tools (VCTools) workload plus its recommended components (Windows SDK).
            Install-ViaWinget 'Microsoft.VisualStudio.2022.BuildTools' @(
                '--override',
                '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
            )
            $vcvars = Find-VcVars
            if (-not $vcvars) {
                throw 'Installed VS Build Tools but could not find vcvars64.bat. Open a new shell and re-run.'
            }
        }
        Write-Step "Importing MSVC environment from: $vcvars"
        Import-VcVars $vcvars
        if (-not (Test-OnPath 'cl.exe')) {
            throw 'Imported the VS environment but cl.exe is still not on PATH - is the C++ workload installed?'
        }
    }

    # 4. Install Python deps and build the Rust extension.
    Write-Step 'Running: uv sync --extra dev  (installs deps + builds scraper._engine)'
    Invoke-Checked { & uv sync --extra dev } 'uv sync --extra dev'

    # 5. Git hooks (opt out with -NoHooks).
    if ($NoHooks) {
        Write-Step 'Skipping pre-commit hook install (-NoHooks)'
    }
    else {
        Write-Step 'Installing pre-commit git hooks'
        Invoke-Checked { & uv run pre-commit install --install-hooks } 'pre-commit install'
    }

    # 6. Smoke test: confirm the extension actually built and imports.
    Write-Step 'Smoke test: import scraper._engine'
    Invoke-Checked { & uv run python -c "import scraper._engine" } 'Extension import smoke test'

    Write-Host ''
    Write-Host 'Install complete. Try:  uv run dhbw-scraper --help' -ForegroundColor Green
}
finally {
    Pop-Location
}
