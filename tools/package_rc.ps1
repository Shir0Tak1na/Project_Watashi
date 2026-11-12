<#
.SYNOPSIS
    Copy an RC ZIP into dist/ and write a sha256sum-compatible checksum file.

.DESCRIPTION
    dist/SHA256SUMS.txt must be UTF-8 without BOM, LF-only, and use the
    "<hash>  <filename>" form (two spaces) so `sha256sum -c` accepts it. Windows
    PowerShell's Set-Content defaults to CRLF, so this script writes bytes with
    [IO.File]::WriteAllText and a UTF8Encoding that does not emit a BOM.

    The ZIP itself is never modified: it is copied (or left in place) and hashed.

    Future releases:
      powershell -ExecutionPolicy Bypass -File tools\package_rc.ps1 `
        -Zip build-final\package\ProjectWatashi-1.0.1-win64.zip -OutputDirectory dist

    Exit codes: 0 checksum written and verified (when sha256sum is available),
    1 missing ZIP / write failure.
#>
[CmdletBinding()]
param(
    [string]$Zip = "",
    [string]$OutputDirectory = "dist",
    [string]$PackageDirectory = "build-rc/package",
    [string]$Sha256Sum = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Zip)) {
    $directory = Join-Path $repoRoot $PackageDirectory
    if (-not (Test-Path -LiteralPath $directory)) {
        Write-Error "package_rc: no package directory at $directory; pass -Zip"
        exit 1
    }
    $found = Get-ChildItem -LiteralPath $directory -Filter "ProjectWatashi-*-win64.zip" `
        -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $found) {
        Write-Error "package_rc: no ProjectWatashi-*-win64.zip under $directory"
        exit 1
    }
    $Zip = $found.FullName
}

$zipPath = [System.IO.Path]::GetFullPath($Zip)
if (-not (Test-Path -LiteralPath $zipPath)) {
    Write-Error "package_rc: no such ZIP: $zipPath"
    exit 1
}
$outputPath = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
} else {
    Join-Path $repoRoot $OutputDirectory
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

$name = Split-Path -Leaf $zipPath
$target = Join-Path $outputPath $name
if ([System.IO.Path]::GetFullPath($target) -ne $zipPath) {
    Copy-Item -LiteralPath $zipPath -Destination $target -Force
}

$hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = Join-Path $outputPath "SHA256SUMS.txt"
$text = "$hash  $name`n"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($checksumPath, $text, $utf8)

Write-Host "package_rc: wrote $checksumPath (UTF-8 no BOM, LF)"
Write-Host "package_rc: $hash  $name"

if ([string]::IsNullOrWhiteSpace($Sha256Sum)) {
    $command = Get-Command sha256sum -ErrorAction SilentlyContinue
    if ($command) {
        $Sha256Sum = $command.Source
    }
}
if (-not [string]::IsNullOrWhiteSpace($Sha256Sum) -and (Test-Path -LiteralPath $Sha256Sum)) {
    Push-Location $outputPath
    try {
        # sha256sum writes to stderr in some Git-for-Windows builds; capture it
        # with EAP=Continue so it cannot become a terminating error.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $check = & $Sha256Sum -c "SHA256SUMS.txt" 2>&1 | Out-String
            $checkCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($check.Trim()) { Write-Host $check.Trim() }
        if ($checkCode -ne 0) {
            Write-Error "package_rc: sha256sum -c failed"
            exit 1
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "package_rc: sha256sum not found; checksum file written but not verified here"
}
exit 0
