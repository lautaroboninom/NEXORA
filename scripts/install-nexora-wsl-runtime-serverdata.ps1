param(
  [switch]$ApplyWslConfiguration = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedComputerName = "SERVERDATA"
$Distro = "Ubuntu-24.04"
$WindowsRepo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$ProgramDataDirectory = "C:\ProgramData\NEXORA"
$MainTaskName = "NEXORA WSL Docker"
$WatchdogTaskName = "NEXORA WSL Docker Watchdog"
$LinuxSource = Join-Path $WindowsRepo "deploy\nexora-wsl-keepalive.sh"
$WatchdogSource = Join-Path $WindowsRepo "scripts\ensure-nexora-wsl-runtime-serverdata.ps1"
$WatchdogDestination = Join-Path $ProgramDataDirectory "ensure-nexora-wsl-runtime-serverdata.ps1"
$warnings = New-Object System.Collections.ArrayList

if ([Environment]::MachineName -ne $ExpectedComputerName) {
  throw "Este instalador solo puede ejecutarse en $ExpectedComputerName."
}

foreach ($requiredFile in @($LinuxSource, $WatchdogSource)) {
  if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
    throw "Falta el archivo requerido: $requiredFile"
  }
}

$mainTask = Get-ScheduledTask -TaskName $MainTaskName -ErrorAction Stop
if ($mainTask.Principal.UserId -notmatch 'Administrator$') {
  throw "La tarea '$MainTaskName' no usa el perfil de Administrator que contiene la distribución WSL."
}

New-Item -ItemType Directory -Path $ProgramDataDirectory -Force | Out-Null
Copy-Item -LiteralPath $WatchdogSource -Destination $WatchdogDestination -Force

$linuxBytes = [System.IO.File]::ReadAllBytes($LinuxSource)
$linuxBase64 = [Convert]::ToBase64String($linuxBytes)
$installLinuxCommand = @"
set -eu
printf '%s' '$linuxBase64' | base64 -d > /usr/local/sbin/nexora-wsl-keepalive.sh
sed -i 's/\r$//' /usr/local/sbin/nexora-wsl-keepalive.sh
chmod 750 /usr/local/sbin/nexora-wsl-keepalive.sh
"@
& "$env:SystemRoot\System32\wsl.exe" -d $Distro -u root -- bash -lc $installLinuxCommand
if ($LASTEXITCODE -ne 0) {
  throw "No se pudo instalar el keepalive dentro de $Distro."
}

if ($ApplyWslConfiguration) {
  New-Item -ItemType Directory -Path "E:\WSL" -Force | Out-Null
  $wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
  if (Test-Path -LiteralPath $wslConfigPath -PathType Leaf) {
    $backupPath = "$wslConfigPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $wslConfigPath -Destination $backupPath -Force
  }
  $wslConfig = @"
[wsl2]
memory=24GB
processors=4
swap=8GB
swapFile=E:\\WSL\\swap.vhdx
localhostForwarding=true

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"@
  [System.IO.File]::WriteAllText(
    $wslConfigPath,
    $wslConfig,
    (New-Object System.Text.UTF8Encoding($false))
  )
}

$compatibleMainAction = @($mainTask.Actions | Where-Object {
  [System.IO.Path]::GetFileName([string]$_.Execute) -ieq "wsl.exe" -and
  [string]$_.Arguments -match [regex]::Escape("/usr/local/sbin/nexora-wsl-keepalive.sh")
}).Count -gt 0
if (-not $compatibleMainAction) {
  throw (
    "La tarea '$MainTaskName' no apunta al keepalive esperado. " +
    "Se necesita la credencial de su usuario para reemplazar la acción con seguridad."
  )
}

if ([string]$mainTask.Principal.LogonType -eq "Password") {
  [void]$warnings.Add(
    "Se conservó la configuración de recuperación de la tarea principal porque Windows no permite modificar una tarea con LogonType Password sin volver a proporcionar la contraseña. El watchdog independiente realizará la recuperación por minuto."
  )
} else {
  $mainAction = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\wsl.exe" `
    -Argument "-d $Distro -u root -- /usr/local/sbin/nexora-wsl-keepalive.sh"
  $mainSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
  Set-ScheduledTask `
    -TaskName $MainTaskName `
    -Action $mainAction `
    -Settings $mainSettings | Out-Null
}

$watchdogAction = New-ScheduledTaskAction `
  -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$WatchdogDestination`""
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$repeatingTrigger = New-ScheduledTaskTrigger `
  -Once `
  -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
$watchdogPrincipal = New-ScheduledTaskPrincipal `
  -UserId "SYSTEM" `
  -LogonType ServiceAccount `
  -RunLevel Highest
$watchdogSettings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
  -TaskName $WatchdogTaskName `
  -Action $watchdogAction `
  -Trigger @($startupTrigger, $repeatingTrigger) `
  -Principal $watchdogPrincipal `
  -Settings $watchdogSettings `
  -Force | Out-Null

[pscustomobject]@{
  MainTask = (Get-ScheduledTask -TaskName $MainTaskName).State
  WatchdogTask = (Get-ScheduledTask -TaskName $WatchdogTaskName).State
  WslConfiguration = if ($ApplyWslConfiguration) { Join-Path $env:USERPROFILE ".wslconfig" } else { "Sin cambios" }
  Keepalive = "/usr/local/sbin/nexora-wsl-keepalive.sh"
  Warnings = @($warnings)
}
