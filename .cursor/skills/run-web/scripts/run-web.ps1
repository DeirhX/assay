#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Launch the rebalancing research web app (Python API + Vite frontend).

.DESCRIPTION
  dev  (default): starts tools/serve.py on :6060 and the Vite dev server on
                  :5173 (HMR, proxies /api to the backend). Ctrl+C stops both.
  prod         : builds the SPA and serves everything from :6060 via serve.py.

  Stale serve.py / vite processes are killed first to avoid the recurring
  port-in-use zombie. npm deps are installed if node_modules is missing.

.EXAMPLE
  pwsh .cursor/skills/run-web/scripts/run-web.ps1
.EXAMPLE
  pwsh .cursor/skills/run-web/scripts/run-web.ps1 -Mode prod
#>
[CmdletBinding()]
param(
    [ValidateSet('dev', 'prod')] [string]$Mode = 'dev',
    [int]$ApiPort = 6060,
    [switch]$NoInstall
)
$ErrorActionPreference = 'Stop'

# --- Locate repo root by walking up to the package.json ---
$root = $PSScriptRoot
while ($root -and -not (Test-Path (Join-Path $root 'package.json'))) {
    $root = Split-Path $root -Parent
}
if (-not $root) { throw "Could not find repo root (package.json) above $PSScriptRoot" }
Set-Location $root
Write-Host "Repo root: $root" -ForegroundColor DarkGray

# --- Kill stale dev processes (the serve.py hot-reload does not rebind the port) ---
function Stop-WebProcs {
    foreach ($p in @(
            @{ Name = 'python.exe'; Match = '*serve.py*' },
            @{ Name = 'node.exe'; Match = '*vite*' }
        )) {
        Get-CimInstance Win32_Process -Filter "Name='$($p.Name)'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like $p.Match } |
            ForEach-Object {
                Write-Host "Stopping stale $($p.Name) (PID $($_.ProcessId))" -ForegroundColor DarkGray
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
    }
}
Stop-WebProcs

# --- Ensure frontend deps ---
if (-not $NoInstall -and -not (Test-Path (Join-Path $root 'node_modules'))) {
    Write-Host "Installing npm dependencies..." -ForegroundColor Cyan
    npm install
}

# --- Polite SEC user-agent default if the caller has not set one ---
if (-not $env:SEC_USER_AGENT) {
    $env:SEC_USER_AGENT = 'assay research (local-dev)'
}

# --- Resolve a python launcher (py -3 on Windows, else python) ---
$py = 'python'
$pyArgs = @('tools/serve.py')
if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = 'py'
    $pyArgs = @('-3', 'tools/serve.py')
}

if ($Mode -eq 'prod') {
    Write-Host "Building SPA (prod)..." -ForegroundColor Cyan
    npm run build
    Write-Host "Serving on http://127.0.0.1:$ApiPort  (Ctrl+C to stop)" -ForegroundColor Green
    & $py @pyArgs
    return
}

# --- dev: backend in background, Vite in foreground; tear down backend on exit ---
Write-Host "Starting API backend (serve.py) on :$ApiPort ..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $py -ArgumentList $pyArgs -PassThru -NoNewWindow
Start-Sleep -Seconds 2
if ($backend.HasExited) {
    throw "serve.py exited immediately (exit $($backend.ExitCode)). Check the output above."
}

try {
    Write-Host "Vite dev server -> http://localhost:5173  (proxies /api to :$ApiPort)" -ForegroundColor Green
    Write-Host "Press Ctrl+C to stop both." -ForegroundColor DarkGray
    npm run dev
}
finally {
    if ($backend -and -not $backend.HasExited) {
        Write-Host "`nStopping API backend (PID $($backend.Id))..." -ForegroundColor DarkGray
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-WebProcs
}
