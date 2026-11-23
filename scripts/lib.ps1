# Shared helpers for the scripts in this folder (dot-source it: . .\scripts\lib.ps1).
# LOCAL DEVELOPMENT ONLY: signs demo users in with the password grant of the eka-dev-cli client.

function Get-DemoToken([string]$Username) {
    $realmFile = Join-Path $PSScriptRoot '..\infra\keycloak\eka-realm.json'
    $realm = Get-Content $realmFile -Raw | ConvertFrom-Json
    $password = @($realm.users | Where-Object username -eq $Username)[0].credentials[0].value
    $response = Invoke-RestMethod -Method Post `
        -Uri 'http://localhost:8380/realms/eka/protocol/openid-connect/token' `
        -Body @{ grant_type = 'password'; client_id = 'eka-dev-cli'; username = $Username; password = $password }
    return $response.access_token
}

# GET a JSON list. Windows PowerShell 5.1 passes a JSON array down the pipeline as ONE object,
# so @(Invoke-RestMethod ...) would have Count 1. Assigning first and then enumerating fixes it.
function Get-ApiList([string]$Url, [string]$Token) {
    $result = Invoke-RestMethod $Url -Headers @{ Authorization = "Bearer $Token" }
    foreach ($item in $result) { $item }
}

# Asks a question and returns the parsed server-sent events as objects (Event, Data).
function Invoke-Ask([string]$ApiUrl, [string]$Token, [string]$Question) {
    $body = @{ question = $Question } | ConvertTo-Json -Compress
    $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$ApiUrl/api/ask" `
        -Headers @{ Authorization = "Bearer $Token" } `
        -ContentType 'application/json; charset=utf-8' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
    $text = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
    $events = @()
    foreach ($frame in ($text -split "`n`n")) {
        if (-not $frame.Trim()) { continue }
        $name = $null; $data = $null
        foreach ($line in ($frame -split "`n")) {
            if ($line.StartsWith('event: ')) { $name = $line.Substring(7) }
            elseif ($line.StartsWith('data: ')) { $data = $line.Substring(6) | ConvertFrom-Json }
        }
        $events += [pscustomobject]@{ Event = $name; Data = $data }
    }
    return $events
}
