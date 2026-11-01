<#
.SYNOPSIS
    Long-run soak harness for the synthetic/fake path. No model, no network.

.DESCRIPTION
    Starts watashi_ui or watashi_cli with synthetic capture and fake OCR, samples
    handle count and working set, and writes a JSON report with exit code,
    crash/timeout flags and pass/fail. The default run is a 10-minute smoke;
    --duration 28800 is the 8-hour soak (not run by default).

    The harness never downloads a model and never touches the network. It only
    reads the local ring log (%LOCALAPPDATA%\ProjectWatashi\logs) for [error]
    entries.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\soak.ps1 --duration 600 --mode ui --report build-w4\soak-ui-10m.json
    powershell -ExecutionPolicy Bypass -File tools\soak.ps1 --duration 28800 --mode ui --report build-w4\soak-ui-8h.json
#>

$ErrorActionPreference = "Stop"

function Show-Usage {
    Write-Host @"
usage: soak.ps1 [--duration SECONDS] [--mode ui|cli] [--report PATH]
                [--build-dir DIR] [--config NAME] [--work-dir DIR]
                [--exe PATH] [--interval SECONDS]

  --duration   run length, default 600 (10-minute smoke); 28800 = 8 hours
  --mode       ui (watashi_ui) or cli (watashi_cli), default ui
  --report     JSON report path, default soak-report.json
  --build-dir  build tree holding the executables, default build-w4
  --config     configuration directory, default Release
  --work-dir   working directory for the child, default repository root
  --exe        explicit executable override (must exist)
  --interval   sampling interval in seconds, default 5

Exit codes: 0 pass, 1 fail (crash/timeout/errors), 2 bad arguments.
"@
}

$durationText = "600"
$mode = "ui"
$report = "soak-report.json"
$buildDir = ""
$config = "Release"
$workDir = ""
$explicitExe = ""
$intervalText = "5"

for ($index = 0; $index -lt $args.Count; $index++) {
    $argument = [string]$args[$index]
    switch ($argument) {
        "--duration" { $index++; $durationText = [string]$args[$index]; continue }
        "--mode" { $index++; $mode = [string]$args[$index]; continue }
        "--report" { $index++; $report = [string]$args[$index]; continue }
        "--build-dir" { $index++; $buildDir = [string]$args[$index]; continue }
        "--config" { $index++; $config = [string]$args[$index]; continue }
        "--work-dir" { $index++; $workDir = [string]$args[$index]; continue }
        "--exe" { $index++; $explicitExe = [string]$args[$index]; continue }
        "--interval" { $index++; $intervalText = [string]$args[$index]; continue }
        "--help" { Show-Usage; exit 0 }
        default {
            Write-Host "soak: unknown argument $argument"
            Show-Usage
            exit 2
        }
    }
}

$duration = 0
$interval = 0
if (-not [int]::TryParse($durationText, [ref]$duration)) {
    Write-Host "soak: --duration must be an integer number of seconds"
    exit 2
}
if (-not [int]::TryParse($intervalText, [ref]$interval)) {
    Write-Host "soak: --interval must be an integer number of seconds"
    exit 2
}
if ($duration -lt 1 -or $duration -gt 86400) {
    Write-Host "soak: --duration must be 1..86400 seconds"
    exit 2
}
if ($mode -notin @("ui", "cli")) {
    Write-Host "soak: --mode must be ui or cli"
    exit 2
}
if ($interval -lt 1 -or $interval -gt 60) {
    Write-Host "soak: --interval must be 1..60 seconds"
    exit 2
}
if ([string]::IsNullOrWhiteSpace($report)) {
    Write-Host "soak: --report cannot be empty"
    exit 2
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $buildDir) { $buildDir = Join-Path $repoRoot "build-w4" }
if (-not $workDir) { $workDir = $repoRoot }

$name = if ($mode -eq "ui") { "watashi_ui.exe" } else { "watashi_cli.exe" }
if ($explicitExe) {
    $executable = $explicitExe
} else {
    $candidates = @(
        (Join-Path $buildDir (Join-Path $config $name)),
        (Join-Path $buildDir ("bin\" + $config + "\" + $name)),
        (Join-Path $buildDir $name)
    )
    $executable = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

$reportPath = [System.IO.Path]::GetFullPath($report)
$reportDir = Split-Path -Parent $reportPath
if ($reportDir) { New-Item -ItemType Directory -Force -Path $reportDir | Out-Null }

function Write-Report([hashtable]$values) {
    $values | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}

if (-not $executable -or -not (Test-Path -LiteralPath $executable)) {
    Write-Host "soak: no executable found (mode=$mode build-dir=$buildDir config=$config)"
    Write-Report @{
        mode = $mode
        requested_duration_seconds = $duration
        elapsed_seconds = 0
        exit_code = $null
        crashed = $true
        pass = $false
        failure_reason = "executable not found"
        executable = [string]$executable
        report = $reportPath
    }
    exit 1
}

$childArguments = if ($mode -eq "ui") {
    @("--capture", "synthetic", "--ocr", "fake", "--duration", "$duration", "--linger", "0")
} else {
    # --synthetic (the shortcut) drains its fixed frames instantly; the capture
    # path honours --duration and is what a long soak needs.
    @("--capture", "synthetic", "--ocr", "fake", "--duration", "$duration", "--json-lines")
}

$started = Get-Date
$stamp = $started.ToString("yyyyMMdd-HHmmss")
$stdoutPath = "$reportPath.$stamp.stdout.log"
$stderrPath = "$reportPath.$stamp.stderr.log"

# Start-Process -PassThru in Windows PowerShell 5.1 loses ExitCode when the
# child's streams are redirected, so use the .NET process API and drain both
# streams asynchronously (the CLI can emit a lot of NDJSON).
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $executable
$startInfo.Arguments = ($childArguments | ForEach-Object {
    if ($_ -match '^\s|\s$|"') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
}) -join ' '
$startInfo.WorkingDirectory = $workDir
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
$process.Start() | Out-Null
$processId = $process.Id
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()

Start-Sleep -Milliseconds 800
$snapshot = Get-Process -Id $processId -ErrorAction SilentlyContinue
$handlesStart = if ($snapshot) { $snapshot.HandleCount } else { 0 }
$memoryStart = if ($snapshot) { [math]::Round($snapshot.WorkingSet64 / 1MB, 1) } else { 0.0 }
$memoryPeak = $memoryStart
$lastHandles = $handlesStart
$lastMemory = $memoryStart

$timeoutSeconds = $duration + 60
$timedOut = $false
while (-not $process.HasExited) {
    $elapsed = ((Get-Date) - $started).TotalSeconds
    if ($elapsed -ge $timeoutSeconds) {
        $timedOut = $true
        break
    }
    Start-Sleep -Seconds $interval
    $snapshot = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($snapshot) {
        $lastHandles = $snapshot.HandleCount
        $lastMemory = [math]::Round($snapshot.WorkingSet64 / 1MB, 1)
        if ($lastMemory -gt $memoryPeak) { $memoryPeak = $lastMemory }
    }
}

if ($timedOut -or -not $process.HasExited) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    $process.WaitForExit(5000) | Out-Null
}
$elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

# Process.ExitCode is valid after the exit has been observed.
if ($process.HasExited) {
    $process.WaitForExit()
    $process.WaitForExit()
}
$handlesEnd = $lastHandles
$memoryEnd = $lastMemory

$stdoutText = ""
$stderrText = ""
try { $stdoutText = $stdoutTask.Result } catch { }
try { $stderrText = $stderrTask.Result } catch { }
Set-Content -LiteralPath $stdoutPath -Value $stdoutText -Encoding UTF8
Set-Content -LiteralPath $stderrPath -Value $stderrText -Encoding UTF8

$exitCode = $null
$crashed = $false
if ($timedOut) {
    $crashed = $true
} elseif ($process.HasExited) {
    $exitCode = $process.ExitCode
    $crashed = $exitCode -ne 0
} else {
    $crashed = true
}

# Log errors: only lines written after this run started. No network is involved.
$logPath = Join-Path $env:LOCALAPPDATA "ProjectWatashi\logs\watashi.log"
$logErrorCount = 0
$logStatus = "missing"
if (Test-Path -LiteralPath $logPath) {
    $logStatus = "available"
    foreach ($line in Get-Content -LiteralPath $logPath) {
        if ($line -match '^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) \[error\]') {
            $stampText = [datetime]::ParseExact($matches[1], "yyyy-MM-ddTHH:mm:ss", $null)
            if ($stampText -ge $started) { $logErrorCount++ }
        }
    }
}
# Normal diagnostics say "error 5" for an unavailable tray; count only an
# exception or an engine failure line.
$stderrErrorCount = @($stderrText -split "\r?\n" |
    Where-Object { $_ -match 'exception|fatal|^\s*watashi_(ui|cli): ' }).Count

$reasons = @()
$tolerance = [math]::Min(10, [math]::Max(2, [math]::Round($duration / 20)))
if ($timedOut) { $reasons += "timeout after $timeoutSeconds seconds" }
if ($crashed -and -not $timedOut) { $reasons += "non-zero exit code $exitCode" }
if (-not $timedOut -and $elapsed -lt ($duration - $tolerance)) {
    $reasons += "exited early after $elapsed seconds (requested $duration)"
}
if ($logErrorCount -gt 0) { $reasons += "$logErrorCount log error line(s)" }
if ($stderrErrorCount -gt 0) { $reasons += "$stderrErrorCount stderr error line(s)" }

$passed = $reasons.Count -eq 0
Write-Report @{
    mode = $mode
    requested_duration_seconds = $duration
    elapsed_seconds = $elapsed
    started_at = $started.ToString("o")
    finished_at = (Get-Date).ToString("o")
    executable = $executable
    exit_code = $exitCode
    crashed = $crashed
    timed_out = $timedOut
    handles_start = $handlesStart
    handles_end = $handlesEnd
    memory_mib_start = $memoryStart
    memory_mib_end = $memoryEnd
    memory_mib_peak = $memoryPeak
    log_path = $logPath
    log_status = $logStatus
    log_errors = $logErrorCount
    stderr_errors = $stderrErrorCount
    pass = $passed
    failure_reason = if ($passed) { "" } else { ($reasons -join "; ") }
    stdout = $stdoutPath
    stderr = $stderrPath
}

Write-Host ("soak: mode={0} duration={1}s elapsed={2}s handles={3}->{4} memory={5}..{6}MiB exit={7} pass={8}" -f `
    $mode, $duration, $elapsed, $handlesStart, $handlesEnd, $memoryStart, $memoryEnd, $exitCode, $passed)
if (-not $passed) { Write-Host ("soak: " + ($reasons -join "; ")) }
if ($passed) { exit 0 } else { exit 1 }
