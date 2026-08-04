# UserPromptSubmit hook. Two jobs, both best-effort (always exits 0):
#  1. Auto-trigger ralph tracking: if the submitted prompt is a `/superharness:go`
#     invocation, bootstrap the ralph state (.current-task + task.json + trace.jsonl)
#     under .claude/superharness/ralph/ so the files appear automatically the moment
#     a go task starts — no agent-run bootstrap required. A distinct go goal repoints
#     to a new task; re-submitting the same task is a no-op.
#  2. Stash the pending round's user query + timestamp so the Stop hook can record a
#     `round` heartbeat even if the go skill wrote no execution event this round.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot '..\scripts\ralph-lib.ps1')
try {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) { exit 0 }
    $in = $raw | ConvertFrom-Json
    $cwd = $in.cwd
    if ([string]::IsNullOrWhiteSpace($cwd)) { exit 0 }
    $prompt = [string]$in.prompt

    # 1. Auto-trigger on a go invocation (start/repoint a task automatically).
    $inv = Get-RalphGoInvocation -Prompt $prompt
    if ($inv -and $inv.Slug -ne (Get-RalphCurrentTask -Root $cwd)) {
        Start-RalphTask -Root $cwd -TaskId $inv.Slug -Goal $inv.Goal
    }

    # 2. Stash the pending round.
    $pending = [ordered]@{ ts = (Get-RalphIso); query = $prompt }
    Write-RalphJson (Join-Path (Get-RalphDir $cwd) '.pending-prompt.json') $pending
} catch { }
exit 0
