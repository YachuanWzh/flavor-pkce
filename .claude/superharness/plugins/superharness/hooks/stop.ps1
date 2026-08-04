# Stop hook: when a go task is active (.claude/superharness/ralph/.current-task present),
# append a 'round' heartbeat to trace.jsonl so every user-facing round is recorded
# even if the go skill wrote no execution event this round. No-op otherwise.
# Always exits 0.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot '..\scripts\ralph-lib.ps1')
try {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) { exit 0 }
    $in = $raw | ConvertFrom-Json
    $cwd = $in.cwd
    if ([string]::IsNullOrWhiteSpace($cwd)) { exit 0 }

    $pendingPath = Join-Path (Get-RalphDir $cwd) '.pending-prompt.json'
    $ct = Get-RalphCurrentTask -Root $cwd
    if (-not $ct) {
        # Not tracking a go task — drop any stray pending prompt and bail.
        Remove-Item $pendingPath -Force -ErrorAction SilentlyContinue
        exit 0
    }

    $pending = Read-RalphJson $pendingPath
    $query = if ($pending -and $pending.query) { [string]$pending.query } else { '' }
    $tasks = Get-RalphTasks -Root $cwd
    $phase = if ($tasks -and $tasks.phase) { [string]$tasks.phase } else { 'go' }

    Add-RalphTrace -Root $cwd -Phase $phase -Event 'round' -Detail $query
    Remove-Item $pendingPath -Force -ErrorAction SilentlyContinue
} catch { }
exit 0
