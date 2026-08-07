param(
  [switch]$ValidateOnly,
  [switch]$NoPause,
  [switch]$SkipConfirmation,
  [string]$StatusFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
$global:OutputEncoding = $Utf8Encoding
try {
  [Console]::InputEncoding = $Utf8Encoding
  [Console]::OutputEncoding = $Utf8Encoding
} catch {
  # Mantener compatibilidad con hosts que no exponen una consola estándar.
}

$ExpectedComputerName = "SERVERDATA"
$Distro = "Ubuntu-24.04"
$WindowsRepo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$WslRepo = "/mnt/e/Datos/Servicio Tecnico/1_SISTEMA REPARACIONES/NEXORA"
$LogDirectory = "E:\Datos\NEXORA_Runtime\deploy_logs"
$HealthHost = "nexora.tail7bb880.ts.net"
$HealthUrl = "https://${HealthHost}/api/health/"
$ComposeCommand = "docker compose -f docker-compose.prod.yml -f docker-compose.serverdata.yml --env-file .env.prod"
$KeepaliveTaskName = "NEXORA WSL Docker"
$HeartbeatPath = "E:\Datos\NEXORA_Runtime\wsl-heartbeat.json"
$MutexName = "NEXORA_PROD_REBUILD_SERVERDATA"
$WslStartupTimeoutSeconds = 120
$ComposeTimeoutSeconds = 120
$DiagnosticTimeoutSeconds = 15
$HealthAttemptTimeoutSeconds = 12
$HealthTotalTimeoutSeconds = 180
$BuildTimeoutSeconds = 3600
$BuildLinuxTimeoutSeconds = 3570
$HealthDelaySeconds = 3
$DurabilityDelaySeconds = 45

$script:TranscriptStarted = $false
$script:DockerAvailable = $false
$script:Mutex = $null
$script:CurrentStage = "preflight"
$script:ExitCode = 0
$script:TerminalLevel = "info"
$script:TerminalMessage = "Proceso finalizado."
$script:TerminalPublished = $false
$script:LastHealthDetail = ""

function Publish-Status {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Stage,
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 100)]
    [int]$Percent,
    [ValidateSet("info", "working", "success", "error", "cancelled")]
    [string]$Level = "info",
    [switch]$Terminal,
    [int]$ResultCode = 0
  )

  $script:CurrentStage = $Stage
  if ([string]::IsNullOrWhiteSpace($StatusFile)) {
    return
  }

  try {
    $entry = [ordered]@{
      timestamp = (Get-Date).ToString("o")
      stage = $Stage
      message = $Message
      percent = $Percent
      level = $Level
    }
    if ($Terminal) {
      $entry["terminal"] = $true
      $entry["exitCode"] = $ResultCode
    }
    $line = ($entry | ConvertTo-Json -Compress) + [Environment]::NewLine
    [System.IO.File]::AppendAllText($StatusFile, $line, $Utf8Encoding)
  } catch {
    Write-Host "No se pudo actualizar el estado gráfico: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

function Publish-TerminalStatus {
  if ($script:TerminalPublished) {
    return
  }

  Publish-Status -Stage "terminal" -Message $script:TerminalMessage -Percent 100 `
    -Level $script:TerminalLevel -Terminal -ResultCode $script:ExitCode
  $script:TerminalPublished = $true
}

function Write-Step {
  param([string]$Message)
  Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
  param([string]$Message)
  Write-Host $Message -ForegroundColor Green
}

function Wait-ForUser {
  if (-not $NoPause) {
    Write-Host ""
    [void](Read-Host "Presione ENTER para cerrar")
  }
}

function Confirm-Rebuild {
  try {
    Add-Type -AssemblyName System.Windows.Forms
    $message = "Se reconstruirá NEXORA PRODUCCIÓN en SERVERDATA.`n`n" +
      "Se utilizarán los archivos actuales del repositorio y no se ejecutará git pull.`n`n" +
      "¿Desea continuar?"
    $result = [System.Windows.Forms.MessageBox]::Show(
      $message,
      "Rebuild NEXORA Producción",
      [System.Windows.Forms.MessageBoxButtons]::YesNo,
      [System.Windows.Forms.MessageBoxIcon]::Warning,
      [System.Windows.Forms.MessageBoxDefaultButton]::Button2
    )
    return $result -eq [System.Windows.Forms.DialogResult]::Yes
  } catch {
    $answer = Read-Host "¿Reconstruir NEXORA PRODUCCIÓN? Escriba SI para continuar"
    return $answer.Trim().ToUpperInvariant() -eq "SI"
  }
}

function Stop-ProcessTree {
  param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

  try {
    if ($Process.HasExited) {
      return
    }
  } catch {
    return
  }

  $taskKillProcess = $null
  try {
    $taskKillPath = Join-Path $env:SystemRoot "System32\taskkill.exe"
    if (Test-Path -LiteralPath $taskKillPath -PathType Leaf) {
      $taskKillInfo = New-Object System.Diagnostics.ProcessStartInfo
      $taskKillInfo.FileName = $taskKillPath
      $taskKillInfo.Arguments = "/PID $($Process.Id) /T /F"
      $taskKillInfo.UseShellExecute = $false
      $taskKillInfo.CreateNoWindow = $true
      $taskKillInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
      $taskKillProcess = New-Object System.Diagnostics.Process
      $taskKillProcess.StartInfo = $taskKillInfo
      if ($taskKillProcess.Start() -and -not $taskKillProcess.WaitForExit(5000)) {
        try { $taskKillProcess.Kill() } catch { }
      }
    }
  } catch {
    # El cierre directo del proceso queda como respaldo.
  } finally {
    if ($null -ne $taskKillProcess) {
      $taskKillProcess.Dispose()
    }
  }

  try {
    if (-not $Process.HasExited) {
      $Process.Kill()
    }
  } catch {
    # El proceso puede haber terminado mientras se cerraba el árbol.
  }

  try {
    [void]$Process.WaitForExit(5000)
  } catch {
    # No bloquear la salida por un proceso que ya fue cancelado.
  }
}

function Invoke-BoundedProcess {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string]$Arguments,
    [Parameter(Mandatory = $true)][ValidateRange(1, 7200)][int]$TimeoutSeconds,
    [Parameter(Mandatory = $true)][string]$Context
  )

  $stdoutPath = [System.IO.Path]::GetTempFileName()
  $stderrPath = [System.IO.Path]::GetTempFileName()
  $process = $null
  $timedOut = $false
  $exitCode = -1
  $stdout = ""
  $stderr = ""
  $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

  try {
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -NoNewWindow `
      -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

    # Windows PowerShell 5.1 puede perder ExitCode si no se materializa el handle
    # antes de que termine un proceso iniciado mediante Start-Process -PassThru.
    [void]$process.Handle

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      $timedOut = $true
      Stop-ProcessTree -Process $process
    } else {
      # Garantiza que se vacíen completamente las redirecciones antes de leerlas.
      [void]$process.WaitForExit()
    }

    try {
      if ($process.HasExited) {
        $process.Refresh()
        $rawExitCode = $process.ExitCode
        if ($null -eq $rawExitCode) {
          throw "El proceso para $Context finalizó sin proporcionar un código de salida."
        }
        $exitCode = [int]$rawExitCode
      }
    } catch {
      $exitCode = -1
    }

    try { $stdout = [System.IO.File]::ReadAllText($stdoutPath, $Utf8Encoding) } catch { $stdout = "" }
    try { $stderr = [System.IO.File]::ReadAllText($stderrPath, $Utf8Encoding) } catch { $stderr = "" }

    return [pscustomobject]@{
      ExitCode = $exitCode
      TimedOut = $timedOut
      StdOut = $stdout
      StdErr = $stderr
      ElapsedSeconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 1)
      Context = $Context
    }
  } catch {
    if ($null -ne $process) {
      Stop-ProcessTree -Process $process
    }
    throw "No se pudo iniciar el proceso para $Context. $($_.Exception.Message)"
  } finally {
    $stopwatch.Stop()
    if ($null -ne $process) {
      $process.Dispose()
    }
    foreach ($temporaryPath in @($stdoutPath, $stderrPath)) {
      try { Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue } catch { }
    }
  }
}

function Get-ProcessDetail {
  param([Parameter(Mandatory = $true)]$Result)

  $lines = @(($Result.StdErr + "`n" + $Result.StdOut) -split "`r?`n" | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_)
  } | Select-Object -Last 8)
  if ($lines.Count -eq 0) {
    return "sin detalle adicional"
  }

  $detail = ($lines -join " | ").Trim()
  $detail = [regex]::Replace(
    $detail,
    '(?i)((?:password|passwd|secret|token|authorization|api[_-]?key)\s*[:=]\s*)[^\s,;]+',
    '$1***'
  )
  if ($detail.Length -gt 1200) {
    $detail = $detail.Substring(0, 1200) + "..."
  }
  return $detail
}

function Write-ProcessOutput {
  param([Parameter(Mandatory = $true)]$Result)

  foreach ($text in @([string]$Result.StdOut, [string]$Result.StdErr)) {
    if (-not [string]::IsNullOrWhiteSpace($text)) {
      Write-Host ($text.TrimEnd())
    }
  }
}

function Invoke-WslCommand {
  param(
    [Parameter(Mandatory = $true)][string]$Command,
    [Parameter(Mandatory = $true)][ValidateRange(1, 7200)][int]$TimeoutSeconds,
    [ValidateRange(0, 7200)][int]$LinuxTimeoutSeconds = 0,
    [string]$Context = "ejecutar un comando en WSL",
    [switch]$IgnoreExitCode,
    [switch]$SuppressOutput,
    [switch]$PassThru
  )

  # El comando real viaja codificado: no se interpola dentro de la línea de comandos de Windows.
  $commandBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Command))
  $linuxRunner = "bash"
  if ($LinuxTimeoutSeconds -gt 0) {
    $linuxRunner = "timeout --signal=TERM --kill-after=5s ${LinuxTimeoutSeconds}s bash"
  }
  $bootstrap = "printf '%s' '$commandBase64' | base64 -d | $linuxRunner"
  $arguments = "-d $Distro -u root -- bash -lc `"$bootstrap`""
  $wslPath = Join-Path $env:SystemRoot "System32\wsl.exe"
  $result = Invoke-BoundedProcess -FilePath $wslPath -Arguments $arguments `
    -TimeoutSeconds $TimeoutSeconds -Context $Context

  if (-not $SuppressOutput) {
    Write-ProcessOutput -Result $result
  }

  $detail = Get-ProcessDetail -Result $result
  if ($result.TimedOut) {
    $timeoutMessage = (
      "WSL no respondió al intentar $Context dentro de $TimeoutSeconds segundos. " +
      "El proceso y sus hijos fueron cancelados. Detalle: $detail"
    )
    $exception = New-Object System.TimeoutException -ArgumentList $timeoutMessage
    $exception.Data["TimedOut"] = $true
    throw $exception
  }
  if ($result.ExitCode -ne 0 -and -not $IgnoreExitCode) {
    if ($result.ExitCode -eq 124 -and $LinuxTimeoutSeconds -gt 0) {
      throw "Se agotó el límite Linux de $LinuxTimeoutSeconds segundos al intentar $Context. Detalle: $detail"
    }
    throw "No se pudo $Context (código WSL $($result.ExitCode)). Detalle: $detail"
  }

  if ($PassThru) {
    return $result
  }
}

function Test-DockerEngine {
  param([ValidateRange(1, 120)][int]$TimeoutSeconds = $WslStartupTimeoutSeconds)

  $linuxTimeout = [Math]::Max(1, $TimeoutSeconds - 2)
  $result = Invoke-WslCommand -Command "docker info >/dev/null 2>&1" `
    -TimeoutSeconds $TimeoutSeconds -LinuxTimeoutSeconds $linuxTimeout `
    -Context "comprobar el motor Docker" -IgnoreExitCode -SuppressOutput -PassThru
  return $result.ExitCode -eq 0
}

function Start-DockerEngine {
  Write-Step "Iniciando Docker dentro de $Distro"
  Publish-Status -Stage "preflight" -Message "Iniciando Docker dentro de WSL..." -Percent 10 -Level "working"
  Invoke-WslCommand -Command "systemctl start docker" -TimeoutSeconds $WslStartupTimeoutSeconds `
    -LinuxTimeoutSeconds 110 -Context "iniciar Docker dentro de WSL"

  $deadline = [DateTime]::UtcNow.AddSeconds($WslStartupTimeoutSeconds)
  while ([DateTime]::UtcNow -lt $deadline) {
    $remaining = [Math]::Max(1, [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalSeconds))
    if (Test-DockerEngine -TimeoutSeconds ([Math]::Min(12, $remaining))) {
      Write-Ok "Docker está disponible."
      Publish-Status -Stage "preflight" -Message "Docker está disponible." -Percent 18 -Level "success"
      return
    }
    Start-Sleep -Seconds ([Math]::Min(2, $remaining))
  }

  throw "Docker no respondió después de $WslStartupTimeoutSeconds segundos."
}

function Ensure-WslKeepaliveTask {
  param([switch]$StartIfNeeded)

  $task = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction Stop
  if ($task.State -eq "Running") {
    Write-Ok "Keepalive de WSL activo."
    return
  }

  if (-not $StartIfNeeded) {
    throw "La tarea '$KeepaliveTaskName' no está activa. Estado actual: $($task.State)."
  }

  Write-Step "Iniciando el keepalive permanente de WSL"
  Publish-Status -Stage "preflight" -Message "Iniciando el runtime permanente de WSL..." -Percent 8 -Level "working"
  Start-ScheduledTask -TaskName $KeepaliveTaskName

  $deadline = [DateTime]::UtcNow.AddSeconds(60)
  while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 2
    $task = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction Stop
    if ($task.State -eq "Running") {
      Write-Ok "Keepalive de WSL activo."
      return
    }
  }

  throw "La tarea '$KeepaliveTaskName' no quedó activa dentro de 60 segundos."
}

function Wait-PostgresHealthy {
  Write-Step "Esperando que PostgreSQL termine su arranque"
  Publish-Status -Stage "database" -Message "Esperando que PostgreSQL acepte conexiones..." -Percent 58 -Level "working"
  $waitCommand = @"
set -u
deadline=`$((SECONDS + 180))
while [ "`$SECONDS" -lt "`$deadline" ]; do
  state="`$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' nexora-postgres 2>/dev/null || true)"
  if [ "`$state" = "healthy" ]; then
    exit 0
  fi
  sleep 2
done
docker logs --tail 100 nexora-postgres >&2 || true
exit 1
"@
  Invoke-WslCommand -Command $waitCommand -TimeoutSeconds 195 -LinuxTimeoutSeconds 190 `
    -Context "esperar el arranque saludable de PostgreSQL"
  Write-Ok "PostgreSQL está saludable."
  Publish-Status -Stage "database" -Message "PostgreSQL está disponible." -Percent 62 -Level "success"
}

function Test-NexoraHealth {
  param([ValidateRange(1, 12)][int]$TimeoutSeconds = $HealthAttemptTimeoutSeconds)

  $curlCommand = "curl -kfsS --max-time 8 --resolve ${HealthHost}:443:127.0.0.1 '$HealthUrl'"
  try {
    $linuxTimeout = [Math]::Max(1, $TimeoutSeconds - 2)
    $result = Invoke-WslCommand -Command $curlCommand -TimeoutSeconds $TimeoutSeconds `
      -LinuxTimeoutSeconds $linuxTimeout -Context "comprobar la salud de la API" `
      -IgnoreExitCode -SuppressOutput -PassThru
  } catch {
    $script:LastHealthDetail = $_.Exception.Message
    return $false
  }

  if ($result.ExitCode -ne 0) {
    $script:LastHealthDetail = Get-ProcessDetail -Result $result
    return $false
  }

  try {
    $payload = $result.StdOut | ConvertFrom-Json
    if ($payload.ok -eq $true) {
      $script:LastHealthDetail = ""
      return $true
    }
  } catch {
    $script:LastHealthDetail = "La API devolvió una respuesta que no es JSON válido."
  }
  return $false
}

function Wait-NexoraHealth {
  Write-Step "Esperando que la API responda en $HealthUrl"
  Publish-Status -Stage "health" -Message "Esperando la respuesta de la API..." -Percent 82 -Level "working"
  $deadline = [DateTime]::UtcNow.AddSeconds($HealthTotalTimeoutSeconds)
  $attempt = 0

  while ([DateTime]::UtcNow -lt $deadline) {
    $attempt++
    $remaining = [Math]::Max(1, [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalSeconds))
    if (Test-NexoraHealth -TimeoutSeconds ([Math]::Min($HealthAttemptTimeoutSeconds, $remaining))) {
      Write-Ok "API disponible: $HealthUrl"
      Publish-Status -Stage "health" -Message "La API respondió correctamente." -Percent 95 -Level "success"
      return
    }

    if (($attempt % 5) -eq 0) {
      Write-Host "La API todavía no está disponible (intento $attempt)..." -ForegroundColor Yellow
      Publish-Status -Stage "health" -Message "La API todavía está iniciando (intento $attempt)..." `
        -Percent ([Math]::Min(94, 82 + [Math]::Floor($attempt / 2))) -Level "working"
    }
    if ([DateTime]::UtcNow -lt $deadline) {
      $remaining = [Math]::Max(1, [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalSeconds))
      Start-Sleep -Seconds ([Math]::Min($HealthDelaySeconds, $remaining))
    }
  }

  $detail = if ([string]::IsNullOrWhiteSpace($script:LastHealthDetail)) {
    "sin detalle adicional"
  } else {
    $script:LastHealthDetail
  }
  throw "La API no respondió correctamente dentro de $HealthTotalTimeoutSeconds segundos. Detalle: $detail"
}

function Test-NexoraHealthFromWindows {
  $curlPath = Join-Path $env:SystemRoot "System32\curl.exe"
  if (-not (Test-Path -LiteralPath $curlPath -PathType Leaf)) {
    $curlPath = "curl.exe"
  }

  try {
    $arguments = "-kfsS --max-time 10 --resolve ${HealthHost}:443:127.0.0.1 `"$HealthUrl`""
    $result = Invoke-BoundedProcess -FilePath $curlPath -Arguments $arguments `
      -TimeoutSeconds 15 -Context "comprobar NEXORA desde Windows"
    if ($result.TimedOut -or $result.ExitCode -ne 0) {
      $script:LastHealthDetail = Get-ProcessDetail -Result $result
      return $false
    }
    $payload = $result.StdOut | ConvertFrom-Json
    return $payload.ok -eq $true
  } catch {
    $script:LastHealthDetail = $_.Exception.Message
    return $false
  }
}

function Confirm-DurableRuntime {
  Write-Step "Comprobando que NEXORA permanezca activa después del rebuild"
  Publish-Status -Stage "durability" -Message "Verificando la permanencia del runtime..." -Percent 96 -Level "working"

  $task = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction Stop
  if ($task.State -ne "Running") {
    throw "El rebuild terminó, pero la tarea '$KeepaliveTaskName' no permanece activa."
  }

  # Esta pausa ocurre en Windows y no sostiene WSL mediante el proceso del
  # rebuild. Detecta el fallo histórico en el que NEXORA caía al terminar.
  Start-Sleep -Seconds $DurabilityDelaySeconds

  $task = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction Stop
  if ($task.State -ne "Running") {
    throw "El keepalive de WSL se detuvo después del rebuild."
  }
  if (-not (Test-Path -LiteralPath $HeartbeatPath -PathType Leaf)) {
    throw "No se encontró el heartbeat permanente de WSL: $HeartbeatPath"
  }

  $heartbeatAge = ((Get-Date) - (Get-Item -LiteralPath $HeartbeatPath).LastWriteTime).TotalSeconds
  if ($heartbeatAge -gt 60) {
    throw "El heartbeat de WSL está desactualizado ($([Math]::Round($heartbeatAge, 0)) segundos)."
  }
  if (-not (Test-NexoraHealthFromWindows)) {
    throw "NEXORA dejó de responder desde Windows después de finalizar los comandos WSL. Detalle: $script:LastHealthDetail"
  }

  Write-Ok "Runtime permanente verificado: keepalive activo, heartbeat vigente y API disponible."
  Publish-Status -Stage "durability" -Message "NEXORA permanece activa fuera del proceso de rebuild." -Percent 99 -Level "success"
}

function Invoke-DiagnosticCommand {
  param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Command
  )

  Write-Host "`n${Title}:" -ForegroundColor Yellow
  try {
    Invoke-WslCommand -Command $Command -TimeoutSeconds $DiagnosticTimeoutSeconds `
      -LinuxTimeoutSeconds 12 -Context "obtener el diagnóstico '$Title'" -IgnoreExitCode
    return $true
  } catch {
    Write-Host "Diagnóstico no disponible: $($_.Exception.Message)" -ForegroundColor Yellow
    return -not ($_.Exception.Data["TimedOut"] -eq $true)
  }
}

function Show-Diagnostics {
  if (-not $script:DockerAvailable) {
    return
  }

  try {
    if (-not (Invoke-DiagnosticCommand -Title "Estado actual de los servicios" `
      -Command "cd '$WslRepo' && $ComposeCommand ps")) {
      Write-Host "Se omiten los demás diagnósticos porque WSL no respondió." -ForegroundColor Yellow
      return
    }
    if (-not (Invoke-DiagnosticCommand -Title "Últimas líneas de nexora-api" `
      -Command "docker logs --tail 100 nexora-api")) {
      Write-Host "Se omite el diagnóstico restante porque WSL no respondió." -ForegroundColor Yellow
      return
    }
    [void](Invoke-DiagnosticCommand -Title "Últimas líneas de nexora-web" `
      -Command "docker logs --tail 60 nexora-web")
  } catch {
    Write-Host "No se pudieron completar los diagnósticos: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

try {
  try {
    $Host.UI.RawUI.WindowTitle = if ($ValidateOnly) {
      "NEXORA - Validación de producción"
    } else {
      "NEXORA - Rebuild de producción"
    }
  } catch {
    # Algunos hosts de PowerShell no permiten cambiar el título.
  }

  if ([Environment]::MachineName -ne $ExpectedComputerName) {
    throw "Este script solo puede ejecutarse en $ExpectedComputerName. Equipo actual: $([Environment]::MachineName)."
  }

  $requiredFiles = @(
    (Join-Path $WindowsRepo ".env.prod"),
    (Join-Path $WindowsRepo "docker-compose.prod.yml"),
    (Join-Path $WindowsRepo "docker-compose.serverdata.yml")
  )
  foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "Falta el archivo requerido: $requiredFile"
    }
  }

  $createdNew = $false
  $script:Mutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$createdNew)
  if (-not $createdNew) {
    throw "Ya hay otra ejecución de rebuild o validación de NEXORA en curso."
  }

  if (-not $ValidateOnly -and -not $SkipConfirmation -and -not (Confirm-Rebuild)) {
    $script:TerminalLevel = "cancelled"
    $script:TerminalMessage = "Operación cancelada. No se realizaron cambios."
    Write-Host "Operación cancelada. No se realizaron cambios." -ForegroundColor Yellow
    Publish-Status -Stage "cancelled" -Message "Operación cancelada. No se realizaron cambios." -Percent 0 -Level "cancelled"
    Publish-TerminalStatus
    Wait-ForUser
    exit 0
  }

  Ensure-WslKeepaliveTask -StartIfNeeded:(-not $ValidateOnly)

  # Validar la distribución ejecutándola evita depender del formato UTF-16 de
  # `wsl --list` y comprueba al mismo tiempo que el subsistema puede iniciar.
  Invoke-WslCommand -Command "true" -TimeoutSeconds $WslStartupTimeoutSeconds `
    -LinuxTimeoutSeconds 110 -Context "iniciar la distribución WSL $Distro"

  New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
  $mode = if ($ValidateOnly) { "validate" } else { "rebuild" }
  $logPath = Join-Path $LogDirectory ("nexora-{0}-{1}.log" -f $mode, (Get-Date -Format "yyyyMMdd-HHmmss"))
  try {
    Start-Transcript -LiteralPath $logPath -Append | Out-Null
    $script:TranscriptStarted = $true
  } catch {
    Write-Host "No se pudo iniciar el registro de PowerShell: $($_.Exception.Message)" -ForegroundColor Yellow
  }

  Write-Host "NEXORA PRODUCCIÓN - SERVERDATA" -ForegroundColor Cyan
  Write-Host "Repositorio: $WindowsRepo"
  Write-Host "Modo: $mode"
  Write-Host "Registro: $logPath"
  Publish-Status -Stage "preflight" -Message "Preparando el entorno productivo..." -Percent 5 -Level "working"
  Publish-Status -Stage "log" -Message "Registro técnico: $logPath" -Percent 5 -Level "info"

  Write-Step "Comprobando Docker"
  Publish-Status -Stage "preflight" -Message "Comprobando WSL y Docker..." -Percent 10 -Level "working"
  $script:DockerAvailable = Test-DockerEngine
  if (-not $script:DockerAvailable) {
    if ($ValidateOnly) {
      throw "Docker no está disponible dentro de $Distro. La validación no inicia servicios."
    }
    Start-DockerEngine
    $script:DockerAvailable = $true
  } else {
    Write-Ok "Docker está disponible."
    Publish-Status -Stage "preflight" -Message "WSL y Docker están disponibles." -Percent 18 -Level "success"
  }

  Write-Step "Validando la configuración productiva de Docker Compose"
  Publish-Status -Stage "preflight" -Message "Validando la configuración productiva..." -Percent 22 -Level "working"
  Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand config --quiet" `
    -TimeoutSeconds $ComposeTimeoutSeconds -LinuxTimeoutSeconds 110 `
    -Context "validar la configuración productiva de Docker Compose"
  Write-Ok "Configuración productiva válida."
  Publish-Status -Stage "preflight" -Message "Entorno productivo validado." -Percent 25 -Level "success"

  if ($ValidateOnly) {
    Write-Step "Comprobando el estado actual de NEXORA"
    Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand ps" `
      -TimeoutSeconds 60 -LinuxTimeoutSeconds 55 -Context "consultar los servicios productivos"
    if (-not (Test-NexoraHealth)) {
      throw "El endpoint productivo no está saludable: $HealthUrl"
    }
    Write-Ok "VALIDACIÓN OK: NEXORA está preparada para usar el acceso directo."
    Publish-Status -Stage "success" -Message "Validación completada correctamente." -Percent 100 -Level "success"
    $script:TerminalLevel = "success"
    $script:TerminalMessage = "Validación completada correctamente."
  } else {
    Write-Step "Construyendo las imágenes productivas"
    Publish-Status -Stage "build" -Message "Construyendo backend y frontend..." -Percent 30 -Level "working"
    Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand build api frontend" `
      -TimeoutSeconds $BuildTimeoutSeconds -LinuxTimeoutSeconds $BuildLinuxTimeoutSeconds `
      -Context "construir las imágenes productivas"
    Publish-Status -Stage "build" -Message "Imágenes productivas construidas." -Percent 50 -Level "success"

    Write-Step "Levantando PostgreSQL de forma aislada"
    Publish-Status -Stage "database" -Message "Iniciando PostgreSQL..." -Percent 54 -Level "working"
    Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand up -d --no-build postgres" `
      -TimeoutSeconds $ComposeTimeoutSeconds -LinuxTimeoutSeconds 110 `
      -Context "iniciar PostgreSQL"
    Wait-PostgresHealthy

    Write-Step "Preparando el esquema productivo de forma serializada"
    Publish-Status -Stage "database" -Message "Aplicando migraciones y esquemas en orden..." -Percent 64 -Level "working"
    $prepareSchemaCommand = (
      "cd '$WslRepo' && " +
      "$ComposeCommand run --rm --no-deps api " +
      "bash -lc 'python manage.py wait_for_database --timeout 180 && python manage.py prepare_production_schema'"
    )
    Invoke-WslCommand -Command $prepareSchemaCommand `
      -TimeoutSeconds 900 -LinuxTimeoutSeconds 870 `
      -Context "preparar el esquema productivo de forma serializada"
    Publish-Status -Stage "database" -Message "Esquema productivo preparado." -Percent 72 -Level "success"

    Write-Step "Levantando todos los servicios productivos"
    Publish-Status -Stage "services" -Message "Iniciando los servicios de NEXORA..." -Percent 75 -Level "working"
    Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand up -d --no-build --remove-orphans" `
      -TimeoutSeconds 600 -LinuxTimeoutSeconds 570 `
      -Context "levantar los servicios productivos"
    Publish-Status -Stage "services" -Message "Servicios productivos iniciados." -Percent 82 -Level "success"

    Wait-NexoraHealth

    Write-Step "Estado final de los servicios"
    Publish-Status -Stage "services" -Message "Comprobando el estado final de los servicios..." -Percent 95 -Level "working"
    Invoke-WslCommand -Command "cd '$WslRepo' && $ComposeCommand ps" `
      -TimeoutSeconds 60 -LinuxTimeoutSeconds 55 -Context "consultar el estado final de los servicios"
    Confirm-DurableRuntime
    Write-Host ""
    Write-Ok "REBUILD COMPLETADO: NEXORA PRODUCCIÓN está disponible."
    Publish-Status -Stage "success" -Message "NEXORA PRODUCCIÓN está disponible." -Percent 100 -Level "success"
    $script:TerminalLevel = "success"
    $script:TerminalMessage = "NEXORA PRODUCCIÓN está disponible."
  }
} catch {
  $script:ExitCode = 1
  $failedStage = $script:CurrentStage
  $errorMessage = $_.Exception.Message
  $script:TerminalLevel = "error"
  $script:TerminalMessage = $errorMessage
  Write-Host "`nERROR: $errorMessage" -ForegroundColor Red
  Publish-Status -Stage $failedStage -Message $errorMessage -Percent 100 -Level "error"
  try {
    Show-Diagnostics
  } catch {
    Write-Host "No se pudo completar el diagnóstico posterior: $($_.Exception.Message)" -ForegroundColor Yellow
  }
} finally {
  Publish-TerminalStatus

  if ($script:TranscriptStarted) {
    try {
      Stop-Transcript | Out-Null
    } catch {
      # No ocultar el resultado principal por un error al cerrar el registro.
    }
  }

  if ($null -ne $script:Mutex) {
    try {
      $script:Mutex.ReleaseMutex()
    } catch {
      # El mutex puede no haberse adquirido si la validación falló antes.
    }
    $script:Mutex.Dispose()
  }
}

Wait-ForUser
exit $script:ExitCode
