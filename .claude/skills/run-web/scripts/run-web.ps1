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
  pwsh .claude/skills/run-web/scripts/run-web.ps1
.EXAMPLE
  pwsh .claude/skills/run-web/scripts/run-web.ps1 -Mode prod
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

# --- Kill stale dev processes (a previous run's reload supervisor + child both
#     linger and hold port 6060 otherwise). Two passes: a command-line match
#     (catches the named python/node procs) and a port-owner sweep (catches any
#     zombie still bound to the API/Vite ports that the name match misses). ---
function Stop-PortOwners {
    param([int[]]$Ports)
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return }
    foreach ($port in $Ports) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                $procPid = $_
                if ($procPid -and $procPid -ne 0) {
                    $name = (Get-Process -Id $procPid -ErrorAction SilentlyContinue).ProcessName
                    Write-Host "Freeing port $port (PID $procPid$(if ($name) { " $name" }))" -ForegroundColor DarkGray
                    Stop-Process -Id $procPid -Force -ErrorAction SilentlyContinue
                }
            }
    }
}
function Stop-WebProcs {
    # A stale supervisor (a previous run of THIS script) sits blocked in
    # `npm run dev`; left alive it auto-restarts serve.py and fights the new
    # run for the port. Kill those pwsh/powershell supervisors first -- but
    # never the current process (or its parent shell), or we'd kill ourselves.
    $self = $PID
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$self" -ErrorAction SilentlyContinue).ParentProcessId
    foreach ($shell in @('pwsh.exe', 'powershell.exe')) {
        Get-CimInstance Win32_Process -Filter "Name='$shell'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*run-web*' -and $_.ProcessId -ne $self -and $_.ProcessId -ne $parent } |
            ForEach-Object {
                Write-Host "Stopping stale supervisor $shell (PID $($_.ProcessId))" -ForegroundColor DarkGray
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
    }
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
    Stop-PortOwners -Ports @($ApiPort, 5173)
}
Stop-WebProcs

# --- Ensure frontend deps ---
if (-not $NoInstall -and -not (Test-Path (Join-Path $root 'node_modules'))) {
    Write-Host "Installing npm dependencies..." -ForegroundColor Cyan
    npm install
}

# --- Polite SEC user-agent default if the caller has not set one ---
# SEC fair-access rejects (403) a UA without a contact email, so the default MUST
# contain an email-looking token. example.com is RFC2606-reserved; override with
# your real contact via $env:SEC_USER_AGENT.
if (-not $env:SEC_USER_AGENT) {
    $env:SEC_USER_AGENT = 'assay-research (local dev) admin@example.com'
}

# --- Resolve a python launcher (py -3 on Windows, else python) ---
$py = 'python'
$pyArgs = @('tools/serve.py')
if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = 'py'
    $pyArgs = @('-3', 'tools/serve.py')
}
$pyArgs += @('--port', "$ApiPort")

if ($Mode -eq 'prod') {
    Write-Host "Building SPA (prod)..." -ForegroundColor Cyan
    npm run build
    Write-Host "Serving on http://127.0.0.1:$ApiPort  (Ctrl+C to stop)" -ForegroundColor Green
    & $py @pyArgs
    return
}

# --- dev: backend in background, Vite in foreground; tear down backend on exit.
#     --reload runs serve.py under its supervisor: editing tools/*.py auto-
#     restarts the API in place (syntax-checked first), and asset edits trigger
#     a browser live-reload -- so the web picks up both halves automatically. ---
$devArgs = $pyArgs + '--reload'
Write-Host "Starting API backend (serve.py --reload) on :$ApiPort ..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $py -ArgumentList $devArgs -PassThru -NoNewWindow
Start-Sleep -Seconds 2
if ($backend.HasExited) {
    throw "serve.py exited immediately (exit $($backend.ExitCode)). Check the output above."
}

try {
    # Advertise the IPv4 loopback URL, not "localhost". On Windows localhost can
    # resolve to ::1 first; Vite is pinned to 127.0.0.1 (see vite.config.ts) to
    # match the IPv4-only backend, so 127.0.0.1 avoids a ~2s dual-stack stall.
    Write-Host "Vite dev server -> http://127.0.0.1:5173  (proxies /api to :$ApiPort)" -ForegroundColor Green
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
