<#
.SYNOPSIS
    Per-user uninstaller for Project Watashi.

.DESCRIPTION
    Removes exactly the files recorded by install.ps1, the Start-menu shortcut
    and the per-user uninstall entry. The user directory (config/user.json,
    corpora, fetched models) is kept unless -RemoveUserData is given.
#>
[CmdletBinding()]
param(
    [string]$Prefix = "$env:LOCALAPPDATA\ProjectWatashi",
    [string]$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectWatashi",
    [string]$ShortcutDir = "",
    [switch]$RemoveUserData
)

$ErrorActionPreference = "Stop"

try {
    if ([string]::IsNullOrWhiteSpace($Prefix)) {
        throw "the install prefix is empty"
    }
    $fullPrefix = [System.IO.Path]::GetFullPath($Prefix)

    $listPath = Join-Path $fullPrefix "installed_files.json"
    $files = @()
    if (Test-Path -LiteralPath $listPath) {
        $files = @((Get-Content -LiteralPath $listPath -Raw -Encoding UTF8 | ConvertFrom-Json).files)
    } else {
        # Fall back to the manifest; an interrupted install can leave only it.
        $manifestPath = Join-Path $fullPrefix "install_manifest.json"
        if (Test-Path -LiteralPath $manifestPath) {
            $files = @((Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).payload)
        } else {
            Write-Warning "no installed file list or manifest under $fullPrefix; removing known files only"
            $files = @(
                "watashi_ui.exe", "watashi_cli.exe", "watashi_fetch.exe",
                "onnxruntime.dll", "ctranslate2.dll", "dnnl.dll",
                "models.lock.json", "LICENSE", "THIRD_PARTY_LICENSES.md",
                "config/presentation.json", "config/settings_ui.json"
            )
        }
    }

    foreach ($relative in $files) {
        $destination = Join-Path $fullPrefix $relative
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Force
        }
    }

    foreach ($name in @("installed_files.json", "install_manifest.json")) {
        $path = Join-Path $fullPrefix $name
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    $script = Join-Path $fullPrefix "uninstall.ps1"
    if (Test-Path -LiteralPath $script) {
        Remove-Item -LiteralPath $script -Force
    }

    $shortcutDirectory = if ($ShortcutDir) {
        $ShortcutDir
    } else {
        Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Project Watashi"
    }
    $shortcut = Join-Path $shortcutDirectory "Project Watashi.lnk"
    if (Test-Path -LiteralPath $shortcut) {
        Remove-Item -LiteralPath $shortcut -Force
    }
    if ((Test-Path -LiteralPath $shortcutDirectory) -and
        -not (Get-ChildItem -LiteralPath $shortcutDirectory -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $shortcutDirectory -Force
    }

    # Only HKCU is ours to remove.
    if ($UninstallKey.StartsWith("HKCU:\", [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $UninstallKey)) {
        Remove-Item -LiteralPath $UninstallKey -Recurse -Force
    }

    $userDir = Join-Path $fullPrefix "user"
    if ($RemoveUserData) {
        if (Test-Path -LiteralPath $userDir) {
            Remove-Item -LiteralPath $userDir -Recurse -Force
            Write-Host "removed user data: $userDir"
        }
    } elseif (Test-Path -LiteralPath $userDir) {
        Write-Host "kept user data: $userDir"
    }

    $configDir = Join-Path $fullPrefix "config"
    if ((Test-Path -LiteralPath $configDir) -and
        -not (Get-ChildItem -LiteralPath $configDir -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $configDir -Force
    }

    # licenses/ is a payload directory like config/: remove it when every file
    # in it has been deleted, so -RemoveUserData can remove the whole prefix.
    $licensesDir = Join-Path $fullPrefix "licenses"
    if ((Test-Path -LiteralPath $licensesDir) -and
        -not (Get-ChildItem -LiteralPath $licensesDir -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $licensesDir -Force
    }

    if (Test-Path -LiteralPath $fullPrefix) {
        $remaining = @(Get-ChildItem -LiteralPath $fullPrefix -Force)
        if (-not $RemoveUserData -and (Test-Path -LiteralPath $userDir)) {
            Write-Host "Project Watashi files removed; $fullPrefix kept for user data"
        } elseif ($remaining.Count -eq 0) {
            Remove-Item -LiteralPath $fullPrefix -Force
            Write-Host "uninstalled Project Watashi; removed $fullPrefix"
        } else {
            Write-Host "Project Watashi files removed; other files remain in $fullPrefix"
        }
    } else {
        Write-Host "uninstalled Project Watashi"
    }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
