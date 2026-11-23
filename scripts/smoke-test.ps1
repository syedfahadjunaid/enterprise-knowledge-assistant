<#
.SYNOPSIS
    End-to-end check of the running stack as real Keycloak users: sign in, list documents and
    ask questions, and verify that every answer respects the asker's permissions.

.EXAMPLE
    .\scripts\seed-demo.ps1      # once
    .\scripts\smoke-test.ps1

.NOTES
    Asks 7 questions in total, under the 10-per-minute rate limit per user.
#>
param(
    [string]$ApiUrl = 'http://localhost:8300'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')

$failures = 0
function Check([bool]$Condition, [string]$Message) {
    if ($Condition) { Write-Host "  PASS  $Message" }
    else { Write-Host "  FAIL  $Message" -ForegroundColor Red; $script:failures++ }
}

$tokens = @{}
foreach ($user in 'alice', 'bob', 'carol', 'dave') { $tokens[$user] = Get-DemoToken $user }

Write-Host "Documents each user can see:"
$expected = @{ alice = 4; bob = 5; carol = 5; dave = 4 }
foreach ($user in 'alice', 'bob', 'carol', 'dave') {
    $docs = @(Get-ApiList "$ApiUrl/api/documents" $tokens[$user])
    Check ($docs.Count -eq $expected[$user]) "$user sees $($docs.Count) documents (expected $($expected[$user])): $(($docs.title | Sort-Object) -join ', ')"
}

function Ask-As([string]$User, [string]$Question) {
    $events = Invoke-Ask $ApiUrl $tokens[$User] $Question
    $sources = @(($events | Where-Object Event -eq 'sources')[0].Data)
    $answer = (($events | Where-Object Event -eq 'delta') | ForEach-Object { $_.Data.text }) -join ''
    $citations = @($events | Where-Object Event -eq 'citation')
    return [pscustomobject]@{
        Titles    = @($sources | ForEach-Object { $_.document_title } | Select-Object -Unique)
        Answer    = $answer
        Citations = $citations.Count
        Done      = ($events | Where-Object Event -eq 'done')[0].Data
    }
}

Write-Host "`nQuestions:"
$q = 'What is the salary range for a senior engineer?'
$hr = Ask-As 'alice' $q
Check ($hr.Titles -contains 'Salary bands 2026' -and $hr.Citations -gt 0) "alice (HR) gets a cited answer from 'Salary bands 2026'"
foreach ($user in 'bob', 'dave') {
    $r = Ask-As $user $q
    Check (-not ($r.Titles -contains 'Salary bands 2026') -and $r.Answer -notmatch '78,000') "$user is never shown the salary bands (sources: $(if ($r.Titles) { $r.Titles -join ', ' } else { 'none' }))"
}

$q = 'How do I fail over the main Postgres database?'
$platform = Ask-As 'bob' $q
Check ($platform.Titles -contains 'Platform incident runbook') "bob (engineering/platform) gets the platform runbook"
$eng = Ask-As 'dave' $q
Check (-not ($eng.Titles -contains 'Platform incident runbook')) "dave (engineering, not platform) is not shown the platform runbook"

$common = Ask-As 'carol' 'How many days a week can I work from home?'
Check ($common.Titles[0] -eq 'Company handbook' -and $common.Citations -gt 0) "carol gets a cited answer from the company handbook"

# The retrieval eval shows which unrelated questions the cut-off catches; this is one of them.
$none = Ask-As 'dave' 'When is the company summer party?'
Check ($none.Done.no_llm_call -eq $true) "an unanswerable question returns 'not found' without calling the LLM"

Write-Host "`nAudit log (alice is kb-admin):"
$audit = @(Get-ApiList "$ApiUrl/api/audit?limit=20" $tokens['alice'])
$shown = @($audit | Where-Object action -eq 'ANSWER_SOURCES_SHOWN')
Check ($shown.Count -ge 7) "the audit log recorded the documents shown for each question ($($shown.Count) entries)"
try {
    Invoke-RestMethod "$ApiUrl/api/audit" -Headers @{ Authorization = "Bearer $($tokens['bob'])" } | Out-Null
    Check $false "bob (not an admin) is refused the audit log"
} catch {
    Check ($_.Exception.Response.StatusCode.value__ -eq 403) "bob (not an admin) is refused the audit log (403)"
}

if ($failures -gt 0) { Write-Host "`n$failures check(s) failed." -ForegroundColor Red; exit 1 }
Write-Host "`nAll checks passed."
