#requires -Version 7.0
<#
.SYNOPSIS
  Encrypted backup + restore drill for the private `data/` submodule.

.DESCRIPTION
  The `data/` submodule is now irreplaceable: it holds the decision journal,
  target-model provenance, and research history that the process-attribution and
  tax-calendar features make load-bearing. Losing it would be the most expensive
  bug this project could have. This script makes an AES-256 encrypted archive of
  it (via 7-Zip, which encrypts file *contents and names* with `-mhe=on`) and can
  verify/restore one -- so "do one restore drill" is a single command, not a
  someday.

  What is backed up: everything under `data/` EXCEPT `data/cache/` (regenerable
  and holds live session auth). Secrets (`secrets.env`, tokens) never live under
  `data/`, so they are out of scope by construction.

  Encryption uses 7-Zip AES-256. If you prefer `age` or `gpg`, the doc
  (`docs/backup-restore.md`) shows the equivalent one-liners; this script targets
  7-Zip because it is what is installed here and encrypts headers too.

.PARAMETER Dest
  Directory to write the encrypted archive into. Defaults to
  $env:ASSAY_BACKUP_DIR, else "$HOME\assay-backups".

.PARAMETER Passphrase
  Backup passphrase. Defaults to $env:ASSAY_BACKUP_PASSPHRASE; if neither is set
  you are prompted (hidden input). Keep it in your password manager, NOT in the
  repo.

.PARAMETER Verify
  After creating (or with -Archive), test archive integrity and extract to a temp
  dir to confirm the key files decrypt and parse. This is the restore drill.

.PARAMETER Archive
  Verify/restore an existing archive instead of creating a new one.

.PARAMETER RestoreTo
  Decrypt + extract an archive into this directory (does not touch the live repo).

.PARAMETER SelfTest
  Run a full create -> verify -> restore cycle against a synthetic throwaway
  dataset (its own random passphrase, temp dirs, no real data). Proves the
  mechanism end-to-end in CI-safe fashion.

.EXAMPLE
  pwsh tools/backup-data.ps1 -Verify
  # create an encrypted backup and immediately drill the restore

.EXAMPLE
  pwsh tools/backup-data.ps1 -Archive C:\backups\assay-data-...7z -RestoreTo C:\tmp\restore
#>
[CmdletBinding()]
param(
    [string]$Dest,
    [string]$Passphrase,
    [switch]$Verify,
    [string]$Archive,
    [string]$RestoreTo,
    [switch]$SelfTest
)

# --- UTF-8 console hygiene (per the project's Windows rules) ------------------
try { chcp 65001 > $null } catch { }
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$DataDir  = Join-Path $RepoRoot 'data'

function Find-SevenZip {
    foreach ($name in '7z', '7za') {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    foreach ($p in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$SevenZip = Find-SevenZip
if (-not $SevenZip) {
    throw "7-Zip not found. Install it (e.g. 'scoop install 7zip' or 'winget install 7zip.7zip'), " +
          "or see docs/backup-restore.md for the age/gpg alternatives."
}

function Resolve-Passphrase {
    if ($Passphrase) { return $Passphrase }
    if ($env:ASSAY_BACKUP_PASSPHRASE) { return $env:ASSAY_BACKUP_PASSPHRASE }
    $secure = Read-Host -AsSecureString "Backup passphrase"
    return [System.Net.NetworkCredential]::new('', $secure).Password
}

# 7-Zip returns 0 = OK, 1 = warning (e.g. a file was in use). Treat >1 as fatal.
function Invoke-SevenZip {
    param([string[]]$SevenZipArgs)
    & $SevenZip @SevenZipArgs
    if ($LASTEXITCODE -gt 1) { throw "7-Zip failed (exit $LASTEXITCODE): $($SevenZipArgs[0])" }
}

function New-Backup {
    param([string]$Source, [string]$OutDir, [string]$Pass, [string]$Stem = 'assay-data')
    if (-not (Test-Path $Source)) { throw "source '$Source' does not exist" }
    if (-not (Get-ChildItem -Force -Path $Source)) {
        throw "source '$Source' is empty — is the data submodule initialized? (git submodule update --init)"
    }
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $ts = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $archivePath = Join-Path $OutDir "$Stem-$ts.7z"
    Push-Location $Source
    try {
        # -mhe=on encrypts headers (file names) too; -xr!cache drops the
        # regenerable cache; '.' archives the tree with relative paths.
        Invoke-SevenZip @('a', '-t7z', '-mhe=on', '-mx=5', "-p$Pass", '-xr!cache',
                          '-bso0', '-bsp0', '--', $archivePath, '.')
    } finally { Pop-Location }
    return $archivePath
}

function Test-Backup {
    param([string]$ArchivePath, [string]$Pass)
    Write-Host "  integrity: testing archive..."
    Invoke-SevenZip @('t', "-p$Pass", '-bso0', '-bsp0', '--', $ArchivePath)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("assay-verify-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Invoke-SevenZip @('x', "-p$Pass", "-o$tmp", '-y', '-bso0', '-bsp0', '--', $ArchivePath)
        $ok = $true
        foreach ($f in 'target-model.json', 'journal.json') {
            $path = Join-Path $tmp $f
            if (Test-Path $path) {
                try { Get-Content -Raw -Encoding utf8 $path | ConvertFrom-Json | Out-Null; Write-Host "  parsed OK: $f" }
                catch { Write-Warning "  FAILED to parse restored ${f}: $_"; $ok = $false }
            } else {
                Write-Host "  (absent, skipped): $f"
            }
        }
        if (Test-Path (Join-Path $tmp 'cache')) { Write-Warning "  cache/ leaked into the archive (should be excluded)"; $ok = $false }
        return $ok
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

function Invoke-Restore {
    param([string]$ArchivePath, [string]$Target, [string]$Pass)
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    Invoke-SevenZip @('x', "-p$Pass", "-o$Target", '-y', '-bso0', '-bsp0', '--', $ArchivePath)
    Write-Host "Restored '$ArchivePath' -> '$Target'"
}

function Invoke-SelfTest {
    Write-Host "== Self-test: create -> verify -> restore against a synthetic dataset =="
    $pass = [guid]::NewGuid().ToString('N')
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("assay-selftest-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $src = Join-Path $root 'data'; $out = Join-Path $root 'backups'; $restore = Join-Path $root 'restore'
    New-Item -ItemType Directory -Force -Path $src, (Join-Path $src 'cache'), (Join-Path $src 'research') | Out-Null
    try {
        '{"as_of":"2026-01-01","targets":{"X":{"low":1,"high":3,"rule":"hold"}}}' | Set-Content -Encoding utf8 (Join-Path $src 'target-model.json')
        '{"entries":[{"id":"abc","action":"buy","symbol":"X"}]}' | Set-Content -Encoding utf8 (Join-Path $src 'journal.json')
        '{"at":"2026-01-01","key":"X","source":"strategy"}' | Set-Content -Encoding utf8 (Join-Path $src 'provenance-log.jsonl')
        'SHOULD NOT BE BACKED UP' | Set-Content -Encoding utf8 (Join-Path $src 'cache\live-secret.json')

        $archive = New-Backup -Source $src -OutDir $out -Pass $pass -Stem 'assay-selftest'
        Write-Host "  created: $archive"
        $verified = Test-Backup -ArchivePath $archive -Pass $pass
        Invoke-Restore -ArchivePath $archive -Target $restore -Pass $pass

        $tmOk   = (Get-Content -Raw (Join-Path $restore 'target-model.json') | ConvertFrom-Json).targets.X.rule -eq 'hold'
        $provOk = Test-Path (Join-Path $restore 'provenance-log.jsonl')
        $noCache = -not (Test-Path (Join-Path $restore 'cache'))
        $wrongPass = $false
        try { & $SevenZip @('t', '-pWRONG', '-bso0', '-bsp0', '--', $archive) 2>$null; $wrongPass = ($LASTEXITCODE -le 1) }
        catch { $wrongPass = $false }

        Write-Host ""
        Write-Host ("  verify passed .......... {0}" -f $verified)
        Write-Host ("  target-model restored .. {0}" -f $tmOk)
        Write-Host ("  provenance restored .... {0}" -f $provOk)
        Write-Host ("  cache/ excluded ........ {0}" -f $noCache)
        Write-Host ("  wrong passphrase fails . {0}" -f (-not $wrongPass))
        if ($verified -and $tmOk -and $provOk -and $noCache -and (-not $wrongPass)) {
            Write-Host "`nSELF-TEST PASSED" -ForegroundColor Green
        } else {
            throw "SELF-TEST FAILED"
        }
    } finally { Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue }
}

# --- main --------------------------------------------------------------------
if ($SelfTest) { Invoke-SelfTest; return }

$pass = Resolve-Passphrase
if (-not $pass) { throw "no passphrase provided" }

if ($Archive) {
    if (-not (Test-Path $Archive)) { throw "archive '$Archive' not found" }
    if ($RestoreTo) { Invoke-Restore -ArchivePath $Archive -Target $RestoreTo -Pass $pass }
    if ($Verify -or -not $RestoreTo) {
        if (Test-Backup -ArchivePath $Archive -Pass $pass) { Write-Host "`nRESTORE DRILL PASSED" -ForegroundColor Green }
        else { throw "RESTORE DRILL FAILED — the backup did not verify" }
    }
    return
}

if (-not $Dest) { $Dest = if ($env:ASSAY_BACKUP_DIR) { $env:ASSAY_BACKUP_DIR } else { Join-Path $HOME 'assay-backups' } }
$archive = New-Backup -Source $DataDir -OutDir $Dest -Pass $pass
Write-Host "Encrypted backup written: $archive"

if ($Verify) {
    if (Test-Backup -ArchivePath $archive -Pass $pass) { Write-Host "`nRESTORE DRILL PASSED" -ForegroundColor Green }
    else { throw "RESTORE DRILL FAILED — the fresh backup did not verify" }
}

Write-Host "`nReminder: a backup that only exists on this machine isn't one. Copy the .7z" -ForegroundColor Yellow
Write-Host "off-box (cloud/USB) and keep the passphrase in your password manager." -ForegroundColor Yellow
