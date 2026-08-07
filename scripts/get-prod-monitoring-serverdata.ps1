param(
  [Parameter(Mandatory = $true)]
  [string]$OutputFile,
  [ValidateRange(1, 60)]
  [int]$TrafficMinutes = 10,
  [ValidateSet("Quick", "Full")]
  [string]$Mode = "Quick"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
$global:OutputEncoding = $Utf8Encoding
try {
  [Console]::InputEncoding = $Utf8Encoding
  [Console]::OutputEncoding = $Utf8Encoding
} catch {
  # El recolector también puede ejecutarse sin una consola visible.
}

$ExpectedComputerName = "SERVERDATA"
$Distro = "Ubuntu-24.04"
$WslPath = "$env:SystemRoot\System32\wsl.exe"
$LogDirectory = "E:\Datos\NEXORA_Runtime\deploy_logs"
$MonitoringHistoryDirectory = "E:\Datos\NEXORA_Runtime\monitoring"
$HeartbeatPath = "E:\Datos\NEXORA_Runtime\wsl-heartbeat.json"
$KeepaliveTaskName = "NEXORA WSL Docker"
$HealthHost = "nexora.tail7bb880.ts.net"
$HealthUrl = "https://${HealthHost}/api/health/"
$script:WslProbeAllowed = $false
$ExpectedContainers = @(
  "nexora-web",
  "nexora-api",
  "nexora-postgres",
  "nexora-bejerman-sync",
  "nexora-bejerman-articulos-sync",
  "nexora-bejerman-pdf",
  "nexora-email-outbox",
  "nexora-billing-postponements",
  "nexora-billing-summary"
)

function Protect-Text {
  param([AllowNull()][string]$Text, [int]$MaximumLength = 280)

  if ([string]::IsNullOrWhiteSpace($Text)) {
    return ""
  }

  $safe = $Text -replace '[\x00-\x08\x0B\x0C\x0E-\x1F]', ' '
  $safe = [regex]::Replace($safe, '(?i)(Bearer\s+)[A-Za-z0-9._~+\-/]+=*', '$1***')
  $safe = [regex]::Replace($safe, '(?i)(postgres(?:ql)?://[^:\s]+:)[^@\s]+@', '$1***@')
  $safe = [regex]::Replace(
    $safe,
    '(?i)((?:password|passwd|secret|token|authorization|api[_-]?key)\s*[:=]\s*)[^\s,;]+',
    '$1***'
  )
  $safe = [regex]::Replace($safe, '(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[correo]')
  $safe = [regex]::Replace($safe, '(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b', '[uuid]')
  $safe = [regex]::Replace($safe, '\b\d{6,}\b', '[id]')
  $safe = ($safe -replace '\s+', ' ').Trim()
  if ($safe.Length -gt $MaximumLength) {
    return $safe.Substring(0, $MaximumLength - 1) + "…"
  }
  return $safe
}

function Get-MonitorProperty {
  param(
    [AllowNull()]$Object,
    [Parameter(Mandatory = $true)][string]$Name,
    $DefaultValue = $null
  )

  if ($null -eq $Object) {
    return $DefaultValue
  }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return $DefaultValue
  }
  return $property.Value
}

function Stop-NativeProcessTree {
  param([Parameter(Mandatory = $true)][int]$TargetProcessId)

  $taskKillPath = "$env:SystemRoot\System32\taskkill.exe"
  $taskKillProcess = $null
  try {
    if (Test-Path -LiteralPath $taskKillPath -PathType Leaf) {
      $taskKillInfo = New-Object System.Diagnostics.ProcessStartInfo
      $taskKillInfo.FileName = $taskKillPath
      $taskKillInfo.Arguments = "/PID $TargetProcessId /T /F"
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
    # El fallback siguiente garantiza que al menos termine el proceso principal.
  } finally {
    if ($null -ne $taskKillProcess) {
      $taskKillProcess.Dispose()
    }
  }

  try {
    Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue |
      Stop-Process -Force -ErrorAction SilentlyContinue
  } catch {
    # El proceso puede haber terminado mientras se recorría su árbol.
  }
}

function Read-RedirectedOutput {
  param([Parameter(Mandatory = $true)][string]$Path)

  try {
    if ([System.IO.File]::Exists($Path)) {
      return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    }
  } catch {
    # Un timeout debe conservar su causa aunque Windows todavía esté liberando el archivo.
  }
  return ""
}

function ConvertTo-OutputLines {
  param([AllowNull()][string]$Text)

  if ([string]::IsNullOrEmpty($Text)) {
    return @()
  }

  # Algunas versiones de wsl.exe intercalan NUL al redirigir su salida UTF-16.
  $normalized = ($Text -replace [char]0, "").TrimEnd([char[]]"`r`n")
  if ([string]::IsNullOrEmpty($normalized)) {
    return @()
  }
  return @($normalized -split "`r?`n")
}

function Invoke-NativeProcessCapture {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string]$Arguments,
    [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds,
    [Parameter(Mandatory = $true)][string]$Label
  )

  $stdoutPath = Join-Path $env:TEMP ("nexora-monitor-{0}-{1}.out" -f $PID, [guid]::NewGuid().ToString("N"))
  $stderrPath = Join-Path $env:TEMP ("nexora-monitor-{0}-{1}.err" -f $PID, [guid]::NewGuid().ToString("N"))
  $process = $null
  try {
    $process = Start-Process `
      -FilePath $FilePath `
      -ArgumentList $Arguments `
      -PassThru `
      -WindowStyle Hidden `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath

    # Windows PowerShell 5.1 puede perder ExitCode si no se materializa el handle
    # antes de que termine un proceso iniciado mediante Start-Process -PassThru.
    [void]$process.Handle

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      Stop-NativeProcessTree -TargetProcessId $process.Id
      try { [void]$process.WaitForExit(5000) } catch { }
      $timeoutDetailParts = @(
        Read-RedirectedOutput -Path $stdoutPath
        Read-RedirectedOutput -Path $stderrPath
      )
      $timeoutDetail = Protect-Text -Text ($timeoutDetailParts -join " ")
      if ([string]::IsNullOrWhiteSpace($timeoutDetail)) {
        throw "$Label excedió el límite de ${TimeoutSeconds} segundos."
      }
      throw "$Label excedió el límite de ${TimeoutSeconds} segundos: $timeoutDetail"
    }

    # La segunda espera garantiza que las redirecciones hayan terminado de vaciarse.
    [void]$process.WaitForExit()
    $process.Refresh()
    if (-not $process.HasExited) {
      throw "$Label terminó la espera sin informar su finalización."
    }
    $rawExitCode = $process.ExitCode
    if ($null -eq $rawExitCode) {
      throw "$Label finalizó sin proporcionar un código de salida."
    }
    return [pscustomobject]@{
      ExitCode = [int]$rawExitCode
      StdOut = Read-RedirectedOutput -Path $stdoutPath
      StdErr = Read-RedirectedOutput -Path $stderrPath
    }
  } finally {
    if ($null -ne $process) {
      $process.Dispose()
    }
    foreach ($temporaryPath in @($stdoutPath, $stderrPath)) {
      if ([System.IO.File]::Exists($temporaryPath)) {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

function Invoke-WslCapture {
  param(
    [Parameter(Mandatory = $true)][string]$Command,
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 20
  )

  if (-not $script:WslProbeAllowed) {
    throw "El runtime permanente de WSL no está confirmado; se omite la sonda para no iniciar una instancia transitoria."
  }

  # El comando se codifica para que comillas, pipes y variables lleguen intactos a Bash.
  $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Command))
  $bootstrapCommand = "printf '%s' '$encodedCommand' | base64 -d | bash"
  # El nombre de esta distribución no contiene espacios; evitar comillas aquí
  # impide que Windows PowerShell 5.1 las entregue como parte del nombre a WSL.
  $arguments = "-d $Distro -u root -- bash -lc `"$bootstrapCommand`""
  $result = Invoke-NativeProcessCapture `
    -FilePath $WslPath `
    -Arguments $arguments `
    -TimeoutSeconds $TimeoutSeconds `
    -Label "El comando de monitoreo en WSL"
  $output = @(ConvertTo-OutputLines -Text $result.StdOut)
  $errorOutput = @(ConvertTo-OutputLines -Text $result.StdErr)
  if ($result.ExitCode -ne 0) {
    $detail = Protect-Text -Text ((@($output) + @($errorOutput) | Select-Object -Last 3) -join " ")
    if ([string]::IsNullOrWhiteSpace($detail)) {
      $detail = "sin detalle"
    }
    throw "El comando de monitoreo terminó con código $($result.ExitCode): $detail"
  }
  return @($output | ForEach-Object { [string]$_ })
}

function New-MetricPayload {
  return [ordered]@{
    schemaVersion = 2
    mode = $Mode
    detailsAvailable = $false
    collectedAt = (Get-Date).ToString("o")
    success = $false
    error = ""
    warnings = @()
    health = [ordered]@{
      available = $false
      httpCode = 0
      latencyMs = 0
      url = $HealthUrl
    }
    host = [ordered]@{
      cpuPercent = 0
      memoryPercent = 0
      memoryUsed = ""
      diskPercent = 0
      diskFree = ""
    }
    runtime = [ordered]@{
      keepaliveTaskState = "Desconocido"
      heartbeatAvailable = $false
      heartbeatAgeSeconds = -1
      state = "unknown"
      dockerAvailable = $false
      apiHttp = 0
      runningContainers = 0
      totalContainers = $ExpectedContainers.Count
      wslMemoryPercent = 0
      wslMemoryUsedKb = 0
      wslMemoryTotalKb = 0
      wslUptimeSeconds = 0
      bootId = ""
    }
    containers = @()
    traffic = [ordered]@{
      available = $false
      windowMinutes = $TrafficMinutes
      requests = 0
      healthChecks = 0
      requestsPerMinute = 0
      status2xx = 0
      status3xx = 0
      status4xx = 0
      status5xx = 0
      apiRequests = 0
      sampleLimited = $false
      topPaths = @()
      recentHttpErrors = @()
    }
    database = [ordered]@{
      available = $false
      sizeBytes = 0
      connections = 0
      active = 0
      idle = 0
      idleInTransaction = 0
      lockWaiters = 0
      blockedSessions = 0
      longestActiveSeconds = 0
      oldestTransactionSeconds = 0
      maxConnections = 0
      transactions = 0
      rollbacks = 0
      cacheHitPercent = 0
      deadlocks = 0
      temporaryBytes = 0
      uptimeSeconds = 0
    }
    recentErrors = @()
    recentErrorsAvailable = $false
    lastRebuild = [ordered]@{
      available = $false
      timestamp = ""
      result = "Sin registros"
      fileName = ""
    }
  }
}

$payload = New-MetricPayload
$exitCode = 0

try {
  if ([Environment]::MachineName -ne $ExpectedComputerName) {
    throw "Este recolector solo puede ejecutarse en $ExpectedComputerName."
  }

  $payload.detailsAvailable = $Mode -eq "Full"

  try {
    $operatingSystem = Get-WmiObject -Class Win32_OperatingSystem
    $processors = @(Get-WmiObject -Class Win32_Processor)
    $cpuValues = @($processors | Where-Object { $null -ne $_.LoadPercentage } | ForEach-Object { [double]$_.LoadPercentage })
    if ($cpuValues.Count -gt 0) {
      $payload.host.cpuPercent = [Math]::Round(($cpuValues | Measure-Object -Average).Average, 0)
    }

    $totalMemoryKb = [double]$operatingSystem.TotalVisibleMemorySize
    $freeMemoryKb = [double]$operatingSystem.FreePhysicalMemory
    if ($totalMemoryKb -gt 0) {
      $usedMemoryKb = $totalMemoryKb - $freeMemoryKb
      $payload.host.memoryPercent = [Math]::Round(($usedMemoryKb / $totalMemoryKb) * 100, 0)
      $payload.host.memoryUsed = "{0:N1} / {1:N1} GB" -f ($usedMemoryKb / 1MB), ($totalMemoryKb / 1MB)
    }

    $dataDisk = Get-WmiObject -Class Win32_LogicalDisk -Filter "DeviceID='E:'"
    if ($null -ne $dataDisk -and [double]$dataDisk.Size -gt 0) {
      $payload.host.diskPercent = [Math]::Round((1 - ([double]$dataDisk.FreeSpace / [double]$dataDisk.Size)) * 100, 0)
      $payload.host.diskFree = "{0:N1} GB libres" -f ([double]$dataDisk.FreeSpace / 1GB)
    }
  } catch {
    $payload.warnings += (Protect-Text -Text ("Recursos de SERVERDATA: " + $_.Exception.Message))
  }

  try {
    $keepaliveTask = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction Stop
    $payload.runtime.keepaliveTaskState = [string]$keepaliveTask.State

    if (Test-Path -LiteralPath $HeartbeatPath -PathType Leaf) {
      $heartbeatFile = Get-Item -LiteralPath $HeartbeatPath
      $heartbeatAge = [Math]::Max(0, ((Get-Date) - $heartbeatFile.LastWriteTime).TotalSeconds)
      $heartbeat = Get-Content -LiteralPath $HeartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json

      $payload.runtime.heartbeatAvailable = $true
      $payload.runtime.heartbeatAgeSeconds = [Math]::Round($heartbeatAge, 0)
      $payload.runtime.state = [string](Get-MonitorProperty -Object $heartbeat -Name "state" -DefaultValue "unknown")
      $payload.runtime.dockerAvailable = [bool](Get-MonitorProperty -Object $heartbeat -Name "dockerAvailable" -DefaultValue $false)
      $payload.runtime.apiHttp = [int](Get-MonitorProperty -Object $heartbeat -Name "apiHttp" -DefaultValue 0)
      $payload.runtime.runningContainers = [int](Get-MonitorProperty -Object $heartbeat -Name "runningContainers" -DefaultValue 0)
      $payload.runtime.totalContainers = [int](Get-MonitorProperty -Object $heartbeat -Name "totalContainers" -DefaultValue $ExpectedContainers.Count)
      $payload.runtime.wslMemoryPercent = [int](Get-MonitorProperty -Object $heartbeat -Name "wslMemoryPercent" -DefaultValue 0)
      $payload.runtime.wslMemoryUsedKb = [long](Get-MonitorProperty -Object $heartbeat -Name "wslMemoryUsedKb" -DefaultValue 0)
      $payload.runtime.wslMemoryTotalKb = [long](Get-MonitorProperty -Object $heartbeat -Name "wslMemoryTotalKb" -DefaultValue 0)
      $payload.runtime.wslUptimeSeconds = [long](Get-MonitorProperty -Object $heartbeat -Name "wslUptimeSeconds" -DefaultValue 0)
      $payload.runtime.bootId = [string](Get-MonitorProperty -Object $heartbeat -Name "bootId" -DefaultValue "")
    }

    $script:WslProbeAllowed = (
      $payload.runtime.keepaliveTaskState -eq "Running" -and
      $payload.runtime.heartbeatAvailable -and
      $payload.runtime.heartbeatAgeSeconds -le 90
    )

    if ($payload.runtime.keepaliveTaskState -ne "Running") {
      $payload.warnings += "El keepalive permanente de WSL no está activo."
    } elseif (-not $payload.runtime.heartbeatAvailable) {
      $payload.warnings += "El keepalive está activo, pero todavía no publicó su heartbeat."
    } elseif ($payload.runtime.heartbeatAgeSeconds -gt 90) {
      $payload.warnings += (
        "El heartbeat de WSL está desactualizado ({0} segundos)." -f
        $payload.runtime.heartbeatAgeSeconds
      )
    }
    if ($payload.runtime.wslMemoryPercent -ge 85) {
      $payload.warnings += (
        "WSL está utilizando {0}% de la memoria asignada." -f
        $payload.runtime.wslMemoryPercent
      )
    }
  } catch {
    $script:WslProbeAllowed = $false
    $payload.warnings += (Protect-Text -Text ("Runtime permanente de WSL: " + $_.Exception.Message))
  }

  $containerState = @{}
  $dockerAvailable = $false
  try {
    # `docker ps` valida WSL y Docker en una sola invocación. Evita una sonda
    # adicional antes de obtener el estado que necesita el resumen.
    $containerRows = @(Invoke-WslCapture -Command "timeout 12s docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}'")
    $dockerAvailable = $true
    foreach ($row in $containerRows) {
      $parts = $row -split '\|', 3
      if ($parts.Count -eq 3 -and $parts[0] -in $ExpectedContainers) {
        $containerState[$parts[0]] = [ordered]@{
          name = $parts[0]
          state = $parts[1]
          status = $parts[2]
          health = "none"
          restarts = 0
          startedAt = ""
          cpu = "—"
          memory = "—"
          memoryPercent = "—"
          network = "—"
          pids = "—"
        }
      }
    }
  } catch {
    $payload.warnings += (Protect-Text -Text ("Estado de Docker: " + $_.Exception.Message))
  }

  $safeNames = @($containerState.Keys | Where-Object { $_ -match '^nexora-[a-z0-9-]+$' } | Sort-Object)
  if ($safeNames.Count -gt 0) {
    try {
      $inspectCommand = "timeout 12s docker inspect --format '{{.Name}}|{{.RestartCount}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' " + ($safeNames -join " ")
      foreach ($row in @(Invoke-WslCapture -Command $inspectCommand)) {
        $parts = $row -split '\|', 4
        if ($parts.Count -eq 4) {
          $name = $parts[0].TrimStart('/')
          if ($containerState.ContainsKey($name)) {
            $containerState[$name].restarts = [int]$parts[1]
            $containerState[$name].startedAt = $parts[2]
            $containerState[$name].health = $parts[3]
          }
        }
      }
    } catch {
      $payload.warnings += (Protect-Text -Text ("Detalle de contenedores: " + $_.Exception.Message))
    }
  }

  if ($Mode -eq "Full" -and $dockerAvailable -and $safeNames.Count -gt 0) {
    try {
      $statsContainerNames = $safeNames -join " "
      $statsRows = @(Invoke-WslCapture -Command "timeout 15s docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.PIDs}}' $statsContainerNames")
      foreach ($row in $statsRows) {
        $parts = $row -split '\|', 6
        if ($parts.Count -eq 6 -and $containerState.ContainsKey($parts[0])) {
          $containerState[$parts[0]].cpu = $parts[1]
          $containerState[$parts[0]].memory = $parts[2]
          $containerState[$parts[0]].memoryPercent = $parts[3]
          $containerState[$parts[0]].network = $parts[4]
          $containerState[$parts[0]].pids = $parts[5]
        }
      }
    } catch {
      $payload.warnings += (Protect-Text -Text ("Consumo de contenedores: " + $_.Exception.Message))
    }
  }

  $containers = New-Object System.Collections.ArrayList
  $absentState = if ($dockerAvailable) { "missing" } else { "unknown" }
  $absentStatus = if ($dockerAvailable) { "No encontrado" } else { "Docker no disponible" }
  foreach ($name in $ExpectedContainers) {
    if ($containerState.ContainsKey($name)) {
      [void]$containers.Add($containerState[$name])
    } else {
      [void]$containers.Add([ordered]@{
        name = $name
        state = $absentState
        status = $absentStatus
        health = "none"
        restarts = 0
        startedAt = ""
        cpu = "—"
        memory = "—"
        memoryPercent = "—"
        network = "—"
        pids = "—"
      })
    }
  }
  $payload.containers = @($containers)

  try {
    $healthResult = (Invoke-WslCapture -Command "timeout 12s curl -sS --max-time 8 -o /dev/null -w '%{http_code}|%{time_total}' --resolve ${HealthHost}:443:127.0.0.1 '$HealthUrl'") -join ""
    if ($healthResult -notmatch '^(?<code>\d{3})\|(?<seconds>[0-9.]+)$') {
      throw "La sonda devolvió un formato inesperado."
    }
    $payload.health.httpCode = [int]$Matches.code
    $payload.health.latencyMs = [Math]::Round(([double]::Parse($Matches.seconds, [Globalization.CultureInfo]::InvariantCulture) * 1000), 0)
    $payload.health.available = $payload.health.httpCode -ge 200 -and $payload.health.httpCode -lt 300
  } catch {
    $payload.health.available = $false
    $payload.warnings += (Protect-Text -Text ("Sonda HTTPS: " + $_.Exception.Message))
  }

  if ($Mode -eq "Full" -and $dockerAvailable) {
    try {
      $webLogLines = @(Invoke-WslCapture -Command "timeout 12s docker logs --since ${TrafficMinutes}m --tail 2500 nexora-web 2>&1")
      $accessPattern = '^(?<ip>\S+)\s+\S+\s+\S+\s+\[(?<when>[^\]]+)\]\s+"(?<method>[A-Z]+)\s+(?<target>\S+)\s+[^"]+"\s+(?<status>\d{3})\s+'
      $trafficRows = New-Object System.Collections.ArrayList
      $healthChecks = 0
      foreach ($line in $webLogLines) {
        $match = [regex]::Match($line, $accessPattern)
        if (-not $match.Success) {
          continue
        }

        $path = ($match.Groups['target'].Value -split '[?#]', 2)[0]
        if ([string]::IsNullOrWhiteSpace($path)) {
          $path = "/"
        }
        $path = [regex]::Replace($path, '(?i)/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?=/|$)', '/:uuid')
        $path = [regex]::Replace($path, '/\d+(?=/|$)', '/:id')
        if ($path -eq "/api/health/") {
          $healthChecks++
          continue
        }

        [void]$trafficRows.Add([pscustomobject]@{
          when = $match.Groups['when'].Value
          method = $match.Groups['method'].Value
          path = Protect-Text -Text $path -MaximumLength 140
          status = [int]$match.Groups['status'].Value
        })
      }

      $payload.traffic.requests = $trafficRows.Count
      $payload.traffic.healthChecks = $healthChecks
      $payload.traffic.requestsPerMinute = [Math]::Round(($trafficRows.Count / [double]$TrafficMinutes), 1)
      $payload.traffic.status2xx = @($trafficRows | Where-Object { $_.status -ge 200 -and $_.status -lt 300 }).Count
      $payload.traffic.status3xx = @($trafficRows | Where-Object { $_.status -ge 300 -and $_.status -lt 400 }).Count
      $payload.traffic.status4xx = @($trafficRows | Where-Object { $_.status -ge 400 -and $_.status -lt 500 }).Count
      $payload.traffic.status5xx = @($trafficRows | Where-Object { $_.status -ge 500 }).Count
      $payload.traffic.apiRequests = @($trafficRows | Where-Object { $_.path -like '/api/*' }).Count
      $payload.traffic.sampleLimited = $webLogLines.Count -ge 2500
      $payload.traffic.topPaths = @(
        $trafficRows |
          Group-Object -Property path |
          Sort-Object -Property Count -Descending |
          Select-Object -First 12 |
          ForEach-Object { [ordered]@{ path = $_.Name; count = $_.Count } }
      )
      $payload.traffic.recentHttpErrors = @(
        $trafficRows |
          Where-Object { $_.status -ge 400 } |
          Select-Object -Last 20 |
          ForEach-Object {
            [ordered]@{
              when = $_.when
              status = $_.status
              method = $_.method
              path = $_.path
            }
          }
      )
      $payload.traffic.available = $true
    } catch {
      $payload.warnings += (Protect-Text -Text ("Tráfico HTTP: " + $_.Exception.Message))
    }
  }

  try {
    $databaseSql = @"
SELECT
  pg_database_size(current_database()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle' AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction' AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0 AND pid <> pg_backend_pid()),
  COALESCE((SELECT EXTRACT(EPOCH FROM max(now() - query_start))::bigint FROM pg_stat_activity WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()), 0),
  COALESCE((SELECT EXTRACT(EPOCH FROM max(now() - xact_start))::bigint FROM pg_stat_activity WHERE datname = current_database() AND xact_start IS NOT NULL AND pid <> pg_backend_pid()), 0),
  current_setting('max_connections')::int,
  (SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()),
  (SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()),
  (SELECT blks_hit FROM pg_stat_database WHERE datname = current_database()),
  (SELECT blks_read FROM pg_stat_database WHERE datname = current_database()),
  (SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()),
  (SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()),
  EXTRACT(EPOCH FROM now() - pg_postmaster_start_time())::bigint;
"@
    $databaseShell = @"
set -eu
export PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=500'
exec psql -X -qAt -w -v ON_ERROR_STOP=1 --username="`$POSTGRES_USER" --dbname="`$POSTGRES_DB" <<'SQL'
$databaseSql
SQL
"@
    $databaseShellBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($databaseShell))
    $databaseCommand = "printf '%s' '$databaseShellBase64' | base64 -d | timeout 12s docker exec -i nexora-postgres sh"
    $databaseResult = (Invoke-WslCapture -Command $databaseCommand | Select-Object -Last 1)
    $databaseParts = $databaseResult -split '\|', 17
    if ($databaseParts.Count -eq 17) {
      $payload.database.available = $true
      $payload.database.sizeBytes = [long]$databaseParts[0]
      $payload.database.connections = [int]$databaseParts[1]
      $payload.database.active = [int]$databaseParts[2]
      $payload.database.idle = [int]$databaseParts[3]
      $payload.database.idleInTransaction = [int]$databaseParts[4]
      $payload.database.lockWaiters = [int]$databaseParts[5]
      $payload.database.blockedSessions = [int]$databaseParts[6]
      $payload.database.longestActiveSeconds = [long]$databaseParts[7]
      $payload.database.oldestTransactionSeconds = [long]$databaseParts[8]
      $payload.database.maxConnections = [int]$databaseParts[9]
      $payload.database.transactions = [long]$databaseParts[10]
      $payload.database.rollbacks = [long]$databaseParts[11]
      $blockHits = [double]$databaseParts[12]
      $blockReads = [double]$databaseParts[13]
      $payload.database.deadlocks = [long]$databaseParts[14]
      $payload.database.temporaryBytes = [long]$databaseParts[15]
      $payload.database.uptimeSeconds = [long]$databaseParts[16]
      if (($blockHits + $blockReads) -gt 0) {
        $payload.database.cacheHitPercent = [Math]::Round(($blockHits / ($blockHits + $blockReads)) * 100, 1)
      }
    } else {
      throw "PostgreSQL devolvió un formato de métricas inesperado."
    }
  } catch {
    $payload.database.available = $false
    $payload.warnings += (Protect-Text -Text ("Métricas de PostgreSQL: " + $_.Exception.Message))
  }

  if ($Mode -eq "Full" -and $dockerAvailable) {
    try {
      $eventContainerNames = @(
        "nexora-api",
        "nexora-web",
        "nexora-bejerman-sync",
        "nexora-bejerman-articulos-sync",
        "nexora-bejerman-pdf",
        "nexora-email-outbox",
        "nexora-billing-postponements",
        "nexora-billing-summary"
      )
      $safeEventContainerNames = @(
        $eventContainerNames |
          Where-Object { $containerState.ContainsKey($_) -and $_ -match '^nexora-[a-z0-9-]+$' }
      )
      if ($safeEventContainerNames.Count -eq 0) {
        throw "No hay contenedores disponibles para consultar eventos."
      }

      $eventNamesShell = $safeEventContainerNames -join " "
      $eventCommand = @"
set -u
for container in $eventNamesShell; do
  while IFS= read -r line; do
    printf '%s\t%s\n' "`$container" "`$line"
  done < <(timeout 8s docker logs --since 15m --tail 120 "`$container" 2>&1)
done
"@
      $eventRows = @(Invoke-WslCapture -Command $eventCommand -TimeoutSeconds 30)
      $recentErrors = New-Object System.Collections.ArrayList
      foreach ($row in $eventRows) {
        $eventParts = $row -split "`t", 2
        if ($eventParts.Count -ne 2) {
          continue
        }
        $containerName = $eventParts[0]
        $line = $eventParts[1]
        $isError = $line -match '(?i)(\bWARNING\b|\bERROR\b|\bCRITICAL\b|Traceback|Exception|timed out|connection refused)'
        if (-not $isError -and $line -match '(?i)\bfailed\b' -and $line -notmatch '(?i)["'']?failed["'']?\s*[:=]\s*0\b') {
          $isError = $true
        }
        if (-not $isError) {
          continue
        }
        [void]$recentErrors.Add([ordered]@{
          container = $containerName
          message = Protect-Text -Text $line
        })
      }
      $payload.recentErrors = @($recentErrors | Select-Object -Last 30)
      $payload.recentErrorsAvailable = $true
    } catch {
      $payload.warnings += (Protect-Text -Text ("Eventos recientes: " + $_.Exception.Message))
    }
  }

  try {
    if (Test-Path -LiteralPath $LogDirectory -PathType Container) {
      $lastRebuild = Get-ChildItem -LiteralPath $LogDirectory -Filter "nexora-rebuild-*.log" -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTime -Descending |
        Select-Object -First 1
      if ($null -ne $lastRebuild) {
        $tail = @(Get-Content -LiteralPath $lastRebuild.FullName -Tail 100 -ErrorAction SilentlyContinue)
        $result = if (($tail -join "`n") -match 'REBUILD COMPLETADO') {
          "Correcto"
        } elseif (($tail -join "`n") -match 'ERROR:') {
          "Con errores"
        } else {
          "Resultado no determinado"
        }
        $payload.lastRebuild.available = $true
        $payload.lastRebuild.timestamp = $lastRebuild.LastWriteTime.ToString("o")
        $payload.lastRebuild.result = $result
        $payload.lastRebuild.fileName = $lastRebuild.Name
      }
    }
  } catch {
    $payload.warnings += (Protect-Text -Text ("Último rebuild: " + $_.Exception.Message))
  }

  $payload.success = $true
} catch {
  $exitCode = 1
  $payload.success = $false
  $payload.error = Protect-Text -Text $_.Exception.Message
} finally {
  $payload.collectedAt = (Get-Date).ToString("o")
  $outputDirectory = Split-Path -Parent $OutputFile
  if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
  }
  $temporaryOutput = "$OutputFile.tmp-$PID"
  $json = $payload | ConvertTo-Json -Depth 8
  [System.IO.File]::WriteAllText($temporaryOutput, $json, $Utf8Encoding)
  Move-Item -LiteralPath $temporaryOutput -Destination $OutputFile -Force

  try {
    New-Item -ItemType Directory -Path $MonitoringHistoryDirectory -Force | Out-Null
    $historyPath = Join-Path $MonitoringHistoryDirectory (
      "runtime-{0}.jsonl" -f (Get-Date -Format "yyyyMMdd")
    )
    $restartTotal = 0
    foreach ($container in @($payload.containers)) {
      $restartTotal += [int](Get-MonitorProperty -Object $container -Name "restarts" -DefaultValue 0)
    }
    $historyEntry = [ordered]@{
      timestamp = $payload.collectedAt
      success = $payload.success
      apiAvailable = $payload.health.available
      apiHttp = $payload.health.httpCode
      apiLatencyMs = $payload.health.latencyMs
      keepalive = $payload.runtime.keepaliveTaskState
      heartbeatAgeSeconds = $payload.runtime.heartbeatAgeSeconds
      runtimeState = $payload.runtime.state
      wslMemoryPercent = $payload.runtime.wslMemoryPercent
      wslMemoryUsedKb = $payload.runtime.wslMemoryUsedKb
      hostMemoryPercent = $payload.host.memoryPercent
      runningContainers = @($payload.containers | Where-Object { $_.state -eq "running" }).Count
      totalContainers = @($payload.containers).Count
      restartTotal = $restartTotal
      databaseAvailable = $payload.database.available
    }
    [System.IO.File]::AppendAllText(
      $historyPath,
      (($historyEntry | ConvertTo-Json -Compress) + [Environment]::NewLine),
      $Utf8Encoding
    )
  } catch {
    # El historial no debe impedir la entrega del estado actual.
  }
}

exit $exitCode
