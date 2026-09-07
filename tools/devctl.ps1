[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'wait-ready')]
    [string]$Command = 'status',
    [int]$BackendPort = 3018,
    [int]$FrontendPort = 3011,
    [string]$BindAddress = '127.0.0.1',
    [switch]$FullStack,
    [int]$TimeoutSec = 300,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$DataDir = Join-Path $Root 'data'
$EnvFile = Join-Path $Root '.env'
$BackendPidFile = Join-Path $DataDir 'devctl-backend.pid'
$FrontendPidFile = Join-Path $DataDir 'devctl-frontend.pid'
$BackendLog = Join-Path $DataDir 'devctl-stdout.log'
$BackendErrLog = Join-Path $DataDir 'devctl-stderr.log'
$FrontendLog = Join-Path $DataDir 'frontend.log'
$FrontendErrLog = Join-Path $DataDir 'frontend.err.log'
$BaseUrl = "http://${BindAddress}:${BackendPort}"

if (-not (Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

function Get-PidFromFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $line = Get-Content -LiteralPath $Path -ErrorAction Stop | Select-Object -First 1
        return [int]$line
    } catch { return 0 }
}

function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -le 0) { return $false }
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    } catch { return $false }
}

function Get-PortPids([int]$Port) {
    $items = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($items) { return @($items | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique) }
    $needle = ':' + [string]$Port
    $fallback = netstat.exe -ano | Select-String $needle | ForEach-Object {
        $parts = ($_.Line -split '\s+') | Where-Object { $_ }
        if ($parts.Count -ge 5 -and $parts[3] -eq 'LISTENING') { [int]$parts[4] }
    }
    return @($fallback | Sort-Object -Unique)
}

function Stop-ProcessTree([int]$ProcessId) {
    if ($ProcessId -le 0) { return }
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & taskkill.exe /F /T /PID $ProcessId 2>&1 | Out-Null
    $ErrorActionPreference = $previousErrorAction
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Wait-PortFree([int]$Port, [int]$Seconds) {
    $limit = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $limit) {
        if ((Get-PortPids $Port).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return ((Get-PortPids $Port | Where-Object { Test-ProcessAlive $_ }).Count -eq 0)
}

function Probe([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        return @{ Status = [int]$r.StatusCode; Body = [string]$r.Content }
    } catch {
        $status = 0
        if ($_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch {}
        }
        return @{ Status = $status; Body = '' }
    }
}

function Start-Backend {
    $python = Join-Path $BackendDir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Error "backend venv missing: $python"
        return $false
    }
    $health = Probe "$BaseUrl/health"
    if ($health.Status -eq 200) {
        Write-Host "[devctl] backend already responding on $BaseUrl"
        return $true
    }
    $pids = @(Get-PortPids $BackendPort | Where-Object { Test-ProcessAlive $_ })
    if ($pids.Count -gt 0) {
        if (-not $Force) {
            Write-Host "[devctl] backend already running on port $BackendPort (PID $($pids -join ', '))"
            return $true
        }
        foreach ($p in $pids) { Stop-ProcessTree $p }
        if (-not (Wait-PortFree $BackendPort 15)) { Write-Error 'backend port did not become free'; return $false }
    }
    $uvArgs = @('-m', 'uvicorn', 'app.main:app')
    if (Test-Path -LiteralPath $EnvFile) { $uvArgs += @('--env-file', $EnvFile) }
    $uvArgs += @('--host', $BindAddress, '--port', [string]$BackendPort)
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $argText = ($uvArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $commandLine = '"' + $python + '" ' + $argText + ' 1> "' + $BackendLog + '" 2> "' + $BackendErrLog + '"'
    $psi.Arguments = '/c "' + $commandLine + '"'
    $psi.WorkingDirectory = $BackendDir
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $proc = [System.Diagnostics.Process]::Start($psi)
    Set-Content -LiteralPath $BackendPidFile -Value ([string]$proc.Id) -Encoding ASCII
    Write-Host "[devctl] backend started PID $($proc.Id)"
    return $true
}

function Resolve-PnpmPath {
    # cmd.exe cannot execute the pnpm.ps1 shim that Get-Command may resolve first.
    foreach ($name in @('pnpm.cmd', 'pnpm.bat', 'pnpm.exe', 'pnpm')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $source = [string]$command.Source
        if ($source -and $source -match '\.(cmd|bat|exe)$') { return $source }
    }
    return $null
}

function Start-Frontend {
    $pnpm = Resolve-PnpmPath
    if (-not $pnpm) { Write-Error 'pnpm not found (cmd.exe needs pnpm.cmd/pnpm.bat/pnpm.exe)'; return $false }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $commandLine = '"' + $pnpm + '" dev --host ' + $BindAddress + ' --port ' + [string]$FrontendPort + ' 1> "' + $FrontendLog + '" 2> "' + $FrontendErrLog + '"'
    $psi.Arguments = '/c "' + $commandLine + '"'
    $psi.WorkingDirectory = $FrontendDir
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $proc = [System.Diagnostics.Process]::Start($psi)
    Set-Content -LiteralPath $FrontendPidFile -Value ([string]$proc.Id) -Encoding ASCII
    Write-Host "[devctl] frontend started PID $($proc.Id)"
    return $true
}

function Stop-All {
    $bp = Get-PidFromFile $BackendPidFile
    $fp = Get-PidFromFile $FrontendPidFile
    Stop-ProcessTree $bp
    Stop-ProcessTree $fp
    foreach ($p in @(Get-PortPids $BackendPort)) { Stop-ProcessTree $p }
    foreach ($p in @(Get-PortPids $FrontendPort)) { Stop-ProcessTree $p }
    Remove-Item -LiteralPath $BackendPidFile,$FrontendPidFile -Force -ErrorAction SilentlyContinue
    $health = Probe "$BaseUrl/health"
    $backendStopped = ($health.Status -ne 200)
    $ok = $backendStopped -and (Wait-PortFree $BackendPort 15) -and (Wait-PortFree $FrontendPort 5)
    if ($ok) { Write-Host '[devctl] stopped'; return $true }
    Write-Error 'ports remain occupied'
    return $false
}

function Wait-ReadyInternal {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $health = Probe "$BaseUrl/health"
        if ($health.Status -eq 200) { break }
        Start-Sleep -Milliseconds 700
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($health.Status -ne 200) { Write-Error '/health not ready'; return $false }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    do {
        $status = Probe "$BaseUrl/api/data/status"
        if ($status.Status -eq 401 -or $status.Status -eq 403) { Write-Error 'authentication required for /api/data/status'; return $false }
        if ($status.Status -eq 200) {
            try {
                $obj = $status.Body | ConvertFrom-Json
                if ($obj.indicators_ready -eq $true) { Write-Host '[devctl] ready'; return $true }
            } catch {}
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Error 'indicators not ready before timeout'
    return $false
}

switch ($Command) {
    'start' {
        if (-not (Start-Backend)) { exit 1 }
        if ($FullStack) { if (-not (Start-Frontend)) { exit 1 } }
        exit 0
    }
    'wait-ready' { if (Wait-ReadyInternal) { exit 0 } else { exit 1 } }
    'stop' { if (Stop-All) { exit 0 } else { exit 1 } }
    'restart' { Stop-All | Out-Null; if (Start-Backend) { exit 0 } else { exit 1 } }
    'status' {
        $health = Probe "$BaseUrl/health"
        if ($health.Status -eq 200) { Write-Host "[devctl] running on $BaseUrl"; exit 0 }
        $bp = @(Get-PortPids $BackendPort | Where-Object { Test-ProcessAlive $_ })
        if ($bp.Count -gt 0) { Write-Host "[devctl] running PID $($bp -join ', ') on $BaseUrl"; exit 0 }
        Write-Host '[devctl] not running'
        exit 1
    }
}
