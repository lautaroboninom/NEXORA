param(
  [string]$TaskName = "NEXORA WSL Docker",
  [string]$HeartbeatPath = "E:\Datos\NEXORA_Runtime\wsl-heartbeat.json",
  [ValidateRange(60, 900)]
  [int]$MaximumHeartbeatAgeSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$LogDirectory = "E:\Datos\Backups\NEXORA\Logs"
$LogPath = Join-Path $LogDirectory "wsl-watchdog.log"

function Write-WatchdogLog {
  param([string]$Message)

  New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
  Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value (
    "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Message
  )
}

if ([Environment]::MachineName -ne "SERVERDATA") {
  throw "Este watchdog solo puede ejecutarse en SERVERDATA."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$heartbeatAge = [double]::PositiveInfinity
if (Test-Path -LiteralPath $HeartbeatPath -PathType Leaf) {
  $heartbeatAge = ((Get-Date) - (Get-Item -LiteralPath $HeartbeatPath).LastWriteTime).TotalSeconds
}

if ($task.State -eq "Running" -and $heartbeatAge -le $MaximumHeartbeatAgeSeconds) {
  exit 0
}

if ($task.State -eq "Running") {
  Write-WatchdogLog (
    "El heartbeat quedó desactualizado ({0:N0} s). Se reiniciará el keepalive." -f $heartbeatAge
  )
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

Start-ScheduledTask -TaskName $TaskName
Write-WatchdogLog "Se inició la tarea '$TaskName'."
