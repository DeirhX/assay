#!/usr/bin/env pwsh
# One command that mirrors CI, so "is this shippable?" is answerable before you
# push and eat a full CI round-trip. Runs the exact gating steps CI runs
# (.github/workflows/tests.yml + guard.yml), plus opt-in stages CI can't run
# (Playwright e2e; the private-data validators that need the `data` submodule).
#
# Usage:
#   pwsh tools/check.ps1                # all gating checks (ruff, mypy, tests, build, leak)
#   pwsh tools/check.ps1 -E2E           # also run the Playwright e2e suite
#   pwsh tools/check.ps1 -Data          # also run the private-data validators
#   pwsh tools/check.ps1 -SkipInstall   # skip npm + Python test dependency installs
#
# Exit code is non-zero iff a GATING step failed. Skipped stages never fail the run.

[CmdletBinding()]
param(
    [switch]$E2E,
    [switch]$Data,
    [switch]$SkipInstall
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$results = [System.Collections.Generic.List[object]]::new()

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Cmd,
        [switch]$NonGating
    )
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Cmd
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    $status = if ($code -eq 0) { "PASS" } elseif ($NonGating) { "WARN" } else { "FAIL" }
    $results.Add([pscustomobject]@{
        Step     = $Name
        Result   = $status
        Gating   = (-not $NonGating)
        ExitCode = $code
    })
}

function Add-Skip {
    param([string]$Name, [string]$Why)
    Write-Host ""
    Write-Host "=== $Name (skipped: $Why) ===" -ForegroundColor DarkGray
    $results.Add([pscustomobject]@{ Step = $Name; Result = "SKIP"; Gating = $false; ExitCode = $null })
}

# --- toolchain -------------------------------------------------------------
if (-not $SkipInstall) {
    Write-Host "=== npm ci ===" -ForegroundColor Cyan
    npm ci
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "=== Python test dependencies ===" -ForegroundColor Cyan
    py -3 -m pip install --quiet -r tools/requirements-test.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# --- gating: Python job ----------------------------------------------------
Invoke-Step "ruff (lint)"        { ruff check tools }
Invoke-Step "mypy (typecheck)"   { py -3 -m mypy tools }
Invoke-Step "pytest"             { py -3 -m pytest tools/tests -q }

# --- gating: Frontend job (CI order) ---------------------------------------
Invoke-Step "eslint"             { npm run lint }
Invoke-Step "tsc (typecheck)"    { npm run typecheck }
Invoke-Step "vitest"             { npm test }
Invoke-Step "vite build"         { npm run build }

# --- gating: leak backstop (guard.yml) -------------------------------------
$gitBash = Join-Path (Split-Path (Split-Path (Get-Command git).Source)) 'bin\bash.exe'
if (Test-Path $gitBash) {
    Invoke-Step "leak scan (site/)" { & $gitBash tools/hooks/leakcheck.sh tree site }
} else {
    Add-Skip "leak scan (site/)" "git bash not found"
}

# --- opt-in: Playwright e2e ------------------------------------------------
if ($E2E) {
    if (-not $env:E2E_PORT) { $env:E2E_PORT = "5199" }
    Invoke-Step "playwright e2e" { npm run e2e }
} else {
    Add-Skip "playwright e2e" "pass -E2E to run"
}

# --- opt-in: private-data validators (need the data submodule) -------------
# These can NEVER run in CI (no private submodule there), so a local runner is
# the only place they're systematically enforced.
if ($Data) {
    if (Test-Path (Join-Path $root "data/current-holdings.json")) {
        Invoke-Step "rebalance --check"  { py -3 tools/rebalance.py --check }
        Invoke-Step "verify_claims"      { py -3 tools/verify_claims.py }
        Invoke-Step "generate_site --check" { py -3 tools/generate_site.py --check }
    } else {
        Add-Skip "private-data validators" "data submodule not initialized"
    }
} else {
    Add-Skip "private-data validators" "pass -Data to run"
}

# --- summary ---------------------------------------------------------------
Write-Host ""
Write-Host "===== check summary =====" -ForegroundColor Cyan
$results | Format-Table -AutoSize Step, Result, Gating, ExitCode | Out-String | Write-Host

$failed = $results | Where-Object { $_.Gating -and $_.Result -eq "FAIL" }
if ($failed) {
    Write-Host ("SHIPPABLE: NO — {0} gating step(s) failed." -f $failed.Count) -ForegroundColor Red
    exit 1
}
$warns = $results | Where-Object { $_.Result -eq "WARN" }
if ($warns) {
    Write-Host "SHIPPABLE: YES (gating checks green; non-gating signals have findings)." -ForegroundColor Yellow
} else {
    Write-Host "SHIPPABLE: YES — all gating checks green." -ForegroundColor Green
}
exit 0
