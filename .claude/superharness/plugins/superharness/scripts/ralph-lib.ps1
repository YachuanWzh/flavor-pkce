# Ralph state mechanism — zero-dependency PowerShell state library.
#
# Manages the four runtime files of a resumable autonomous-task loop, all under
# <project>/.claude/superharness/ralph/ :
#   .current-task      one-line pointer to the active task (switch = rewrite the line)
#   task.json          task-list snapshot {status,phase,sprint,tasks[],updated_at}
#   trace.jsonl        append-only ledger, one {ts,phase,event,detail} JSON per line
#   .ralph-state.json  retry counter {retries,max,updated_at}, capped at 5
#
# Dot-source this file to use the functions. The trace hooks (hooks/stop.ps1,
# hooks/user-prompt-submit.ps1) dot-source it for go task tracking. Conventions:
# UTF-8 without BOM, atomic temp-then-move for JSON snapshots, ISO-8601 timestamps.

# ---------------------------------------------------------------- paths & helpers

function Get-RalphDir {
    param([Parameter(Mandatory)][string]$Root)
    Join-Path $Root '.claude\superharness\ralph'
}

function Get-RalphIso { (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz') }

function Get-RalphGoInvocation {
    # Parse a UserPromptSubmit prompt. If it is a `/superharness:go <goal>` invocation
    # (leading slash optional, must be at the start of the prompt), return
    # { Goal; Slug='YYYY-MM-DD-<kebab|task-HHmmss>' }; otherwise return $null. Pure.
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Prompt,
        [datetime]$Now = (Get-Date)
    )
    if ($null -eq $Prompt) { return $null }
    $m = [regex]::Match($Prompt, '^\s*/?superharness:go\b[ \t]*(?<goal>[\s\S]*)$')
    if (-not $m.Success) { return $null }
    $goal = $m.Groups['goal'].Value.Trim()
    $date = $Now.ToString('yyyy-MM-dd')
    $tokens = [regex]::Matches($goal.ToLower(), '[a-z0-9]+') | ForEach-Object { $_.Value }
    if ($tokens.Count -gt 0) {
        $kebab = (@($tokens) | Select-Object -First 6) -join '-'
    } else {
        $kebab = 'task-' + $Now.ToString('HHmmss')
    }
    [PSCustomObject]@{ Goal = $goal; Slug = "$date-$kebab" }
}

function New-RalphDir {
    param([string]$Root)
    $dir = Get-RalphDir $Root
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    return $dir
}

function Read-RalphJson {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try { return Get-Content $Path -Raw | ConvertFrom-Json } catch { return $null }
}

function Write-RalphText {
    # Atomic write: temp file then move-replace. UTF-8 without BOM.
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $enc = New-Object System.Text.UTF8Encoding($false)
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, $Text, $enc)
    Move-Item -Path $tmp -Destination $Path -Force
}

function Write-RalphJson {
    param([string]$Path, $Object)
    Write-RalphText $Path (ConvertTo-Json -InputObject $Object -Depth 12 -Compress)
}

# ---------------------------------------------------------------- .current-task

function Get-RalphCurrentTaskPath { param([string]$Root) Join-Path (Get-RalphDir $Root) '.current-task' }

function Set-RalphCurrentTask {
    # The pointer is a single line; switching a task rewrites only this line.
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$TaskId)
    Write-RalphText (Get-RalphCurrentTaskPath $Root) ($TaskId.Trim())
}

function Get-RalphCurrentTask {
    param([Parameter(Mandatory)][string]$Root)
    $p = Get-RalphCurrentTaskPath $Root
    if (-not (Test-Path $p)) { return $null }
    $raw = (Get-Content $p -Raw)
    if ($null -eq $raw) { return $null }
    $line = $raw.Trim()
    if ($line -eq '') { return $null }
    return $line
}

# ---------------------------------------------------------------- task.json

$script:RalphStatuses = @('pending', 'in_progress', 'done')

function Get-RalphTaskPath { param([string]$Root) Join-Path (Get-RalphDir $Root) 'task.json' }

function Initialize-RalphTasks {
    # Write the task-list snapshot. Each task defaults to status 'pending'.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Tasks,
        [string]$Status = 'planning',
        [string]$Phase = 'implement',
        [int]$SprintCurrent = 0,
        [int]$SprintTotal = 0
    )
    $norm = @()
    foreach ($t in $Tasks) {
        $st = if ($t.status) { [string]$t.status } else { 'pending' }
        if ($script:RalphStatuses -notcontains $st) { throw "Invalid task status '$st' (allowed: $($script:RalphStatuses -join ', '))" }
        $norm += [ordered]@{ id = $t.id; name = [string]$t.name; status = $st }
    }
    $snapshot = [ordered]@{
        status     = $Status
        phase      = $Phase
        sprint     = [ordered]@{ current = $SprintCurrent; total = $SprintTotal }
        tasks      = $norm
        updated_at = (Get-RalphIso)
    }
    Write-RalphJson (Get-RalphTaskPath $Root) $snapshot
}

function Get-RalphTasks {
    param([Parameter(Mandatory)][string]$Root)
    Read-RalphJson (Get-RalphTaskPath $Root)
}

function Get-RalphNextTask {
    # The first task whose status is not 'done' — the resume entry point.
    param([Parameter(Mandatory)][string]$Root)
    $snap = Get-RalphTasks $Root
    if (-not $snap) { return $null }
    foreach ($t in @($snap.tasks)) {
        if ($t.status -ne 'done') { return $t }
    }
    return $null
}

function Set-RalphTaskStatus {
    # Idempotently set one task's status and refresh updated_at. Order preserved.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)]$Id,
        [Parameter(Mandatory)][string]$Status
    )
    if ($script:RalphStatuses -notcontains $Status) { throw "Invalid task status '$Status' (allowed: $($script:RalphStatuses -join ', '))" }
    $snap = Get-RalphTasks $Root
    if (-not $snap) { throw "No task.json under $(Get-RalphDir $Root)" }
    $tasks = @()
    foreach ($t in @($snap.tasks)) {
        $st = if ("$($t.id)" -eq "$Id") { $Status } else { [string]$t.status }
        $tasks += [ordered]@{ id = $t.id; name = [string]$t.name; status = $st }
    }
    $snapshot = [ordered]@{
        status     = [string]$snap.status
        phase      = [string]$snap.phase
        sprint     = [ordered]@{ current = $snap.sprint.current; total = $snap.sprint.total }
        tasks      = $tasks
        updated_at = (Get-RalphIso)
    }
    Write-RalphJson (Get-RalphTaskPath $Root) $snapshot
}

# ---------------------------------------------------------------- trace.jsonl

function Get-RalphTracePath { param([string]$Root) Join-Path (Get-RalphDir $Root) 'trace.jsonl' }

function Add-RalphTrace {
    # Append a single minified {ts,phase,event,detail} line. Never rewrites earlier
    # lines — the worst a crash can corrupt is the final line.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Event,
        [string]$Detail = ''
    )
    New-RalphDir $Root | Out-Null
    $line = ConvertTo-Json -InputObject ([ordered]@{
        ts = (Get-RalphIso); phase = $Phase; event = $Event; detail = $Detail
    }) -Depth 12 -Compress
    $enc = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::AppendAllText((Get-RalphTracePath $Root), ($line + "`n"), $enc)
}

function Get-RalphTraceTail {
    # Return the last N events (parsed), oldest-first. Empty array if no ledger.
    param([Parameter(Mandatory)][string]$Root, [int]$Count = 1)
    $p = Get-RalphTracePath $Root
    if (-not (Test-Path $p)) { return @() }
    $lines = @(Get-Content $p -Tail $Count | Where-Object { $_.Trim() -ne '' })
    $out = @()
    foreach ($l in $lines) { try { $out += ($l | ConvertFrom-Json) } catch {} }
    return $out
}

# ---------------------------------------------------------------- .ralph-state.json (retry counter)

function Get-RalphRetryPath { param([string]$Root) Join-Path (Get-RalphDir $Root) '.ralph-state.json' }

function Get-RalphRetryState {
    # Defaults to {retries:0, max:5} when the file is absent or malformed.
    param([Parameter(Mandatory)][string]$Root)
    $s = Read-RalphJson (Get-RalphRetryPath $Root)
    $retries = 0; $max = 5; $upd = $null
    if ($s) {
        if ($null -ne $s.retries) { $retries = [int]$s.retries }
        if ($null -ne $s.max)     { $max = [int]$s.max }
        $upd = $s.updated_at
    }
    [PSCustomObject]@{ retries = $retries; max = $max; updated_at = $upd }
}

function Set-RalphRetryState {
    param([Parameter(Mandatory)][string]$Root, [int]$Retries, [int]$Max)
    $obj = [ordered]@{ retries = $Retries; max = $Max; updated_at = (Get-RalphIso) }
    Write-RalphJson (Get-RalphRetryPath $Root) $obj
    [PSCustomObject]@{ retries = $Retries; max = $Max; updated_at = $obj.updated_at }
}

function Add-RalphRetry {
    # Increment the retry counter, clamped at max. Returns the new state.
    param([Parameter(Mandatory)][string]$Root)
    $st = Get-RalphRetryState $Root
    $n = $st.retries + 1
    if ($n -gt $st.max) { $n = $st.max }
    Set-RalphRetryState -Root $Root -Retries $n -Max $st.max
}

function Test-RalphRetryExhausted {
    param([Parameter(Mandatory)][string]$Root)
    $st = Get-RalphRetryState $Root
    return ($st.retries -ge $st.max)
}

function Reset-RalphRetry {
    param([Parameter(Mandatory)][string]$Root)
    $st = Get-RalphRetryState $Root
    Set-RalphRetryState -Root $Root -Retries 0 -Max $st.max
}

# ---------------------------------------------------------------- task bootstrap

function Start-RalphTask {
    # Auto-bootstrap a fresh go task: point .current-task, seed an empty task.json
    # (planning/plan — the agent enriches the task list later), open the trace ledger
    # with a task:started event, and reset the retry counter. Idempotent-ish: calling
    # again repoints to a new TaskId and appends another task:started line.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$TaskId,
        [string]$Goal = ''
    )
    Set-RalphCurrentTask -Root $Root -TaskId $TaskId
    Initialize-RalphTasks -Root $Root -Tasks @() -Status 'planning' -Phase 'plan'
    Add-RalphTrace -Root $Root -Phase 'plan' -Event 'task:started' -Detail $Goal
    Reset-RalphRetry -Root $Root | Out-Null
}

# ---------------------------------------------------------------- cold-start recovery

function Get-RalphResumeContext {
    # Assemble the deterministic file-based facts a freshly-started agent needs to
    # resume: the active pointer, the task snapshot, the first not-done task, the
    # last ledger event, and the retry state. The agent then reconciles these
    # against `git diff` (code wins) and fixes task.json via Set-RalphTaskStatus.
    param([Parameter(Mandatory)][string]$Root)
    $snap = Get-RalphTasks $Root
    $next = Get-RalphNextTask $Root
    $tail = @(Get-RalphTraceTail -Root $Root -Count 1)
    $last = if ($tail.Count -gt 0) { $tail[0] } else { $null }
    [PSCustomObject]@{
        current_task = (Get-RalphCurrentTask $Root)
        tasks        = $snap
        next_task    = $next
        last_trace   = $last
        all_done     = [bool]($snap -and ($null -eq $next))
        retry        = (Get-RalphRetryState $Root)
    }
}
