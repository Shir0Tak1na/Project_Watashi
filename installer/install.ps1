<#
.SYNOPSIS
    Per-user installer for Project Watashi. No administrator rights are used.

.DESCRIPTION
    Copies the payload listed in install_manifest.json into
    %LOCALAPPDATA%\ProjectWatashi (or -Prefix), creates the user data directory,
    a Start-menu shortcut and a per-user uninstall entry. Model weights are not
    part of the payload; run watashi_fetch.exe --fetch afterwards.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Prefix "$env:TEMP\watashi-test"
#>
[CmdletBinding()]
param(
    [string]$Prefix = "$env:LOCALAPPDATA\ProjectWatashi",
    [string]$SourceDir = "",
    [string]$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectWatashi",
    [string]$ShortcutDir = "",
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

# Resolve SourceDir in the body: when the script is started with -File,
# $PSScriptRoot is not reliably bound while parameter defaults are evaluated.
if ([string]::IsNullOrWhiteSpace($SourceDir)) {
    $SourceDir = $PSScriptRoot
}

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

try {
    if ([string]::IsNullOrWhiteSpace($Prefix)) {
        throw "the install prefix is empty"
    }
    $fullPrefix = Get-FullPath $Prefix
    # Never write into machine-wide locations: this is a per-user installer and
    # must work without UAC.
    $forbidden = @(
        [Environment]::GetFolderPath("ProgramFiles"),
        [Environment]::GetFolderPath("ProgramFilesX86"),
        $env:WINDIR
    )
    foreach ($root in $forbidden) {
        if (-not [string]::IsNullOrWhiteSpace($root) -and
            $fullPrefix.StartsWith((Get-FullPath $root), [StringComparison]::OrdinalIgnoreCase)) {
            throw "refusing to install under $root; the prefix must be per-user"
        }
    }

    $manifestPath = Join-Path $SourceDir "install_manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "missing install manifest: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $payload = @($manifest.payload)
    if ($payload.Count -eq 0) {
        throw "install manifest has an empty payload"
    }

    New-Item -ItemType Directory -Force -Path $fullPrefix | Out-Null
    $copied = @()
    foreach ($relative in $payload) {
        $source = Join-Path $SourceDir $relative
        if (-not (Test-Path -LiteralPath $source)) {
            throw "manifest lists a missing file: $relative"
        }
        $destination = Join-Path $fullPrefix $relative
        $parent = Split-Path -Parent $destination
        if ($parent) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $copied += $relative.Replace('\', '/')
    }

    # The MSVC/UCRT DLLs are not in the static manifest (their names depend on
    # the toolchain). Copy the remaining root DLLs and remember them so uninstall
    # can remove exactly what was installed.
    Get-ChildItem -LiteralPath $SourceDir -Filter "*.dll" -File | ForEach-Object {
        if ($copied -notcontains $_.Name) {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $fullPrefix $_.Name) -Force
            $copied += $_.Name
        }
    }

    # User data survives upgrades and uninstall.
    $userDir = Join-Path $fullPrefix ([string]$manifest.user_dir)
    if (-not (Test-Path -LiteralPath $userDir)) {
        New-Item -ItemType Directory -Force -Path $userDir | Out-Null
    }

    Copy-Item -LiteralPath (Join-Path $SourceDir "uninstall.ps1") -Destination (Join-Path $fullPrefix "uninstall.ps1") -Force
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $fullPrefix "install_manifest.json") -Force

    $installed = [ordered]@{ files = $copied }
    $installed | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fullPrefix "installed_files.json") -Encoding UTF8

    # Per-user uninstall entry. No HKLM, no elevation.
    #
    # The three diagnostics lines below exist because a restricted test token
    # cannot open its own HKCU subkeys; without them a host restriction looks
    # exactly like a bug in this script.
    Write-Host "uninstall key: $UninstallKey"
    $current_user = (& whoami 2>&1 | Out-String).Trim()
    Write-Host "user: $current_user"
    $group_list = (& whoami /groups 2>&1 | Out-String)
    if ($group_list -match 'S-1-5-12' -or $group_list -match 'NT AUTHORITY\\RESTRICTED') {
        Write-Host "restricted token detected: the host denies writes to HKCU"
    }
    if (-not (Test-Path -LiteralPath $UninstallKey)) {
        New-Item -Path $UninstallKey -Force | Out-Null
    }
    $version = if ($manifest.version) { [string]$manifest.version } else { "0.0.0" }
    $uninstallString = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}\uninstall.ps1" -Prefix "{0}" -UninstallKey "{1}"' -f $fullPrefix, $UninstallKey
    Set-ItemProperty -LiteralPath $UninstallKey -Name "DisplayName" -Value "Project Watashi"
    Set-ItemProperty -LiteralPath $UninstallKey -Name "DisplayVersion" -Value $version
    Set-ItemProperty -LiteralPath $UninstallKey -Name "Publisher" -Value "Shir0Tak1na"
    Set-ItemProperty -LiteralPath $UninstallKey -Name "InstallLocation" -Value $fullPrefix
    Set-ItemProperty -LiteralPath $UninstallKey -Name "DisplayIcon" -Value (Join-Path $fullPrefix "watashi_ui.exe")
    Set-ItemProperty -LiteralPath $UninstallKey -Name "UninstallString" -Value $uninstallString
    Set-ItemProperty -LiteralPath $UninstallKey -Name "QuietUninstallString" -Value $uninstallString
    New-ItemProperty -LiteralPath $UninstallKey -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -LiteralPath $UninstallKey -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

    if (-not $NoShortcut) {
        $shortcutDirectory = if ($ShortcutDir) {
            $ShortcutDir
        } else {
            Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Project Watashi"
        }
        New-Item -ItemType Directory -Force -Path $shortcutDirectory | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut((Join-Path $shortcutDirectory "Project Watashi.lnk"))
        $shortcut.TargetPath = Join-Path $fullPrefix "watashi_ui.exe"
        $shortcut.WorkingDirectory = $fullPrefix
        $shortcut.IconLocation = (Join-Path $fullPrefix "watashi_ui.exe")
        $shortcut.Description = "Project Watashi subtitle overlay"
        $shortcut.Save()
    }

    Write-Host "installed Project Watashi $version to $fullPrefix"
    Write-Host "user data: $userDir (kept on uninstall)"
    Write-Host "model weights are not included; run watashi_fetch.exe --fetch --dest `"$(Join-Path $userDir 'models')`""
    exit 0
} catch {
    Write-Error $_
    exit 1
}
