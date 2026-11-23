<#
.SYNOPSIS
    Uploads the demo documents (demo-docs/manifest.json) through the API, as the demo users who
    own them, and waits until the ingestion worker has made them searchable.

.EXAMPLE
    .\scripts\seed-demo.ps1

.NOTES
    Safe to run twice: documents whose title the uploader can already see are skipped.
    Uses curl.exe (built into Windows 10+) for multipart uploads; Windows PowerShell 5.1's
    Invoke-RestMethod has no -Form parameter.
#>
param(
    [string]$ApiUrl = 'http://localhost:8300'
)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib.ps1')

$demoDir = Join-Path $PSScriptRoot '..\demo-docs'
$manifest = Get-Content (Join-Path $demoDir 'manifest.json') -Raw | ConvertFrom-Json
$uploaded = @()

foreach ($doc in $manifest.documents) {
    $token = Get-DemoToken $doc.uploader
    $headers = @{ Authorization = "Bearer $token" }
    $existing = @(Get-ApiList "$ApiUrl/api/documents" $token)
    if (@($existing | Where-Object { $_.title -eq $doc.title -and $_.is_owner }).Count -gt 0) {
        Write-Host "skip    $($doc.title) (already uploaded by $($doc.uploader))"
        continue
    }

    $path = (Resolve-Path (Join-Path $demoDir $doc.file)).Path
    $curlArgs = @('-sS', '-f', '-X', 'POST', "$ApiUrl/api/documents",
        '-H', "Authorization: Bearer $token",
        '-F', "file=@$path",
        '--form-string', "title=$($doc.title)")
    foreach ($principal in $doc.share_with) { $curlArgs += @('--form-string', "share_with=$principal") }
    $json = & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "Upload of $($doc.file) failed (curl exit code $LASTEXITCODE)" }
    $created = $json | ConvertFrom-Json
    $uploaded += [pscustomobject]@{ Id = $created.id; Title = $doc.title; Uploader = $doc.uploader }
    Write-Host ("upload  {0,-30} by {1,-6} shared with {2}" -f $doc.title, $doc.uploader, ($doc.share_with -join ', '))
}

# Wait for the worker (it is woken by NOTIFY, so this is usually a few seconds).
$deadline = (Get-Date).AddMinutes(3)
foreach ($item in $uploaded) {
    $headers = @{ Authorization = "Bearer $(Get-DemoToken $item.Uploader)" }
    do {
        $status = (Invoke-RestMethod "$ApiUrl/api/documents/$($item.Id)" -Headers $headers).status
        if ($status -eq 'READY' -or $status -eq 'FAILED') { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    Write-Host ("{0,-7} {1}" -f $status.ToLower(), $item.Title)
    if ($status -ne 'READY') { throw "$($item.Title) did not become READY (status $status)" }
}
Write-Host "Done: $($uploaded.Count) document(s) uploaded."
