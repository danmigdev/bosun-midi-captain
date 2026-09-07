[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$HostName = 'bosun-hub',
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 6080,
    [ValidateRange(1024, 65535)]
    [int]$RemotePort = 6080,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ssh = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
$forward = "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
$url = "http://localhost:${LocalPort}/vnc.html?autoconnect=1&resize=scale&view_only=1&reconnect=1&reconnect_delay=2000"
$logDirectory = Join-Path $env:LOCALAPPDATA 'Bosun'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$errorLog = Join-Path $logDirectory "stage-vnc-${LocalPort}.log"

# Reuse only our exact SSH forward. Never stop or take over another service
# which happens to be listening on this local port.
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
$tunnel = $null
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if (-not $owner -or $owner.Name -ne 'ssh.exe' -or
        $owner.CommandLine -notmatch [regex]::Escape($forward) -or
        $owner.CommandLine -notmatch ('\s' + [regex]::Escape($HostName) + '\s*$')) {
        throw "La porta locale $LocalPort e' gia' utilizzata da un altro programma."
    }
    $tunnel = Get-Process -Id $owner.ProcessId
} else {
    $arguments = @('-N', '-L', $forward,
        '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3', $HostName)
    $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardError $errorLog
}

$watch = [Diagnostics.Stopwatch]::StartNew()
$ready = $false
while ($watch.Elapsed.TotalSeconds -lt 15) {
    if ($tunnel.HasExited) {
        $detail = if (Test-Path -LiteralPath $errorLog) { Get-Content -Raw -LiteralPath $errorLog } else { '' }
        throw "Connessione SSH al Raspberry Pi non riuscita. $detail"
    }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:${LocalPort}/vnc.html" -TimeoutSec 1
        if ($response.StatusCode -eq 200 -and $response.Content -match 'noVNC') {
            $ready = $true
            break
        }
    } catch { }
    Start-Sleep -Milliseconds 300
}
if (-not $ready) {
    throw "Il Raspberry Pi non rende ancora disponibile il display VNC. Riprova quando e' avviato."
}

if (-not $NoBrowser) { Start-Process $url }
Write-Host $url
