<#
.SYNOPSIS
    Sign the shipped executables, or state plainly that they are unsigned.

.DESCRIPTION
    The certificate path comes from WATASHI_SIGN_CERT and its password from
    WATASHI_SIGN_PASSWORD. Neither is stored in the repository. Without a
    certificate (or without signtool.exe) the script prints UNSIGNED and exits
    0, which is what CI does; -RequireSignature turns that into exit 2.

.EXAMPLE
    $env:WATASHI_SIGN_CERT = "C:\certs\watashi.pfx"
    $env:WATASHI_SIGN_PASSWORD = "..."
    powershell -ExecutionPolicy Bypass -File sign.ps1 -Path .\watashi_ui.exe, .\watashi_cli.exe
#>
[CmdletBinding()]
param(
    [string[]]$Path = @(),
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"

if ($Path.Count -eq 0) {
    Write-Error "sign.ps1: pass at least one file with -Path"
    exit 2
}
foreach ($item in $Path) {
    if (-not (Test-Path -LiteralPath $item)) {
        Write-Error "sign.ps1: no such file: $item"
        exit 1
    }
}

$certificate = $env:WATASHI_SIGN_CERT
$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue

if ([string]::IsNullOrWhiteSpace($certificate) -or -not (Test-Path -LiteralPath $certificate)) {
    Write-Warning "UNSIGNED: WATASHI_SIGN_CERT is not set or does not name a certificate"
    Write-Output "UNSIGNED"
    if ($RequireSignature) { exit 2 }
    exit 0
}
if ($null -eq $signtool) {
    Write-Warning "UNSIGNED: signtool.exe is not on PATH; cannot sign with $certificate"
    Write-Output "UNSIGNED"
    if ($RequireSignature) { exit 2 }
    exit 0
}

$arguments = @("sign", "/f", $certificate, "/fd", "sha256", "/td", "sha256", "/tr", $TimestampUrl)
if (-not [string]::IsNullOrWhiteSpace($env:WATASHI_SIGN_PASSWORD)) {
    $arguments += @("/p", $env:WATASHI_SIGN_PASSWORD)
}
foreach ($item in $Path) {
    & $signtool.Source @arguments $item
    if ($LASTEXITCODE -ne 0) {
        Write-Error "sign.ps1: signtool failed for $item"
        exit 1
    }
}

foreach ($item in $Path) {
    & $signtool.Source verify /pa $item
    if ($LASTEXITCODE -ne 0) {
        Write-Error "sign.ps1: signature verification failed for $item"
        exit 1
    }
}
Write-Output "SIGNED: $($Path.Count) file(s) signed and verified"
exit 0
