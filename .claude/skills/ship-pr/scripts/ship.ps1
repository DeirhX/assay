#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Push the current branch, open/find its PR, enable squash auto-merge, then sync
  main once it lands. The deterministic tail of the ship-pr skill.

.DESCRIPTION
  Assumes you are on a feature branch with your work already committed. Does NOT
  stage or commit (that needs judgment: exclude the private data submodule and
  any secrets). It pushes, creates the PR if one doesn't exist for the branch,
  sets --squash --auto --delete-branch (branch protection requires CI, so the
  merge happens after the pipeline passes), then waits and fast-forwards main.

.EXAMPLE
  pwsh .claude/skills/ship-pr/scripts/ship.ps1 -Title "Fix flaky pull" -BodyFile PR_BODY.txt
.EXAMPLE
  pwsh .claude/skills/ship-pr/scripts/ship.ps1 -Title "Tidy console" -NoWait
#>
[CmdletBinding()]
param(
    [string]$Title,
    [string]$BodyFile,
    [string]$Base = 'main',
    [switch]$NoWait,
    [int]$TimeoutSec = 600
)
$ErrorActionPreference = 'Stop'

$root = (git rev-parse --show-toplevel 2>$null)
if (-not $root) { throw "Not inside a git repo." }
Set-Location $root

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -in @($Base, 'main', 'master', 'HEAD')) {
    throw "On '$branch' — create a feature branch with your commit first, then re-run."
}

# Guard: refuse if the private data submodule pointer is staged for this PR.
$staged = git diff --cached --name-only
if ($staged -contains 'data') {
    throw "The private 'data' submodule is staged. Unstage it (git restore --staged data) before shipping."
}

Write-Host "Pushing $branch ..." -ForegroundColor Cyan
git push -u origin HEAD | Out-Host

function Get-PrNumber {
    $n = gh pr view --json number -q .number 2>$null
    if ($LASTEXITCODE -eq 0 -and $n) { return "$n".Trim() }
    return $null
}

$num = Get-PrNumber
if (-not $num) {
    if (-not $Title) { throw "No PR exists for $branch and no -Title given to create one." }
    Write-Host "Creating PR ..." -ForegroundColor Cyan
    $createArgs = @('pr', 'create', '--base', $Base, '--title', $Title)
    if ($BodyFile -and (Test-Path $BodyFile)) { $createArgs += @('--body-file', $BodyFile) }
    else { $createArgs += @('--body', 'Automated PR (no body provided).') }
    gh @createArgs | Out-Host
    $num = Get-PrNumber
    if (-not $num) { throw "PR creation did not yield a number." }
}
else {
    Write-Host "Reusing existing PR #$num (push updated it)." -ForegroundColor DarkGray
}

$url = (gh pr view $num --json url -q .url 2>$null)
Write-Host "PR #$num -> $url" -ForegroundColor Green

Write-Host "Enabling squash auto-merge ..." -ForegroundColor Cyan
gh pr merge $num --squash --auto --delete-branch | Out-Host

if ($NoWait) {
    Write-Host "Auto-merge armed. It will land when CI passes. (-NoWait: not syncing main.)" -ForegroundColor Yellow
    return
}

Write-Host "Waiting for CI + merge (timeout ${TimeoutSec}s) ..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$state = ''
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $state = (gh pr view $num --json state -q .state 2>$null)
    Write-Host "  PR #$num state: $state" -ForegroundColor DarkGray
    if ($state -eq 'MERGED') { break }
    if ($state -eq 'CLOSED') { throw "PR #$num was CLOSED without merging. Check CI: gh pr checks $num" }
}

if ($state -ne 'MERGED') {
    Write-Host "Still pending after ${TimeoutSec}s; auto-merge stays armed. Check: gh pr checks $num" -ForegroundColor Yellow
    return
}

Write-Host "Merged. Syncing $Base ..." -ForegroundColor Cyan
git checkout $Base | Out-Host
git pull --ff-only | Out-Host
git branch -D $branch 2>$null | Out-Null
Write-Host "Done. $Base is up to date and PR #$num is merged." -ForegroundColor Green
