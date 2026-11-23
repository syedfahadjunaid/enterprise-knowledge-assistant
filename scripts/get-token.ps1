<#
.SYNOPSIS
    Signs in a demo user against the local Keycloak and puts the access token in $env:TOKEN.

.EXAMPLE
    . .\scripts\get-token.ps1 bob
    Invoke-RestMethod http://localhost:8300/api/me -Headers @{ Authorization = "Bearer $env:TOKEN" }

.NOTES
    LOCAL DEVELOPMENT ONLY. Uses the password grant on the eka-dev-cli client, which exists
    only in the local realm. The web app uses the authorization code flow + PKCE.
    Demo passwords are read from infra/keycloak/eka-realm.json. Tokens expire after 5 minutes.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('alice', 'bob', 'carol', 'dave')]
    [string]$User
)

. (Join-Path $PSScriptRoot 'lib.ps1')

$env:TOKEN = Get-DemoToken $User
Write-Host "Signed in as $User. Token stored in `$env:TOKEN (valid 5 minutes)."
