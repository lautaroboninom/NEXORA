<#
  Manda una senal de vida ("latido") a un servicio externo cada pocos minutos.

  El problema que resuelve: todo el monitoreo de NEXORA corre DENTRO de
  SERVERDATA, asi que si el server se muere el monitoreo se muere con el y nadie
  se entera. Nextcloud / el nodo `nube` tampoco sirven de testigo: son
  contenedores del mismo WSL.

  La solucion es al reves: el server avisa periodicamente "estoy vivo" a un
  servicio de afuera (healthchecks.io). Si el servicio deja de recibir la senal,
  manda el mail. Asi el que avisa esta afuera del sistema que puede fallar.

  Tres casos, tres comportamientos:
    - NEXORA sano          -> ping de exito. Nadie se entera de nada (bien).
    - NEXORA caido         -> ping a /fail. El mail sale al instante.
    - SERVERDATA muerto    -> no llega ningun ping. El servicio avisa por timeout.

  Instalar (una vez, PowerShell como administrador en SERVERDATA):
    .\nexora-latido-externo-serverdata.ps1 -Instalar -UrlLatido "https://hc-ping.com/<uuid-del-check>"

  Probar sin esperar:
    .\nexora-latido-externo-serverdata.ps1 -UrlLatido "https://hc-ping.com/<uuid>" -Verboso

  Desinstalar:
    .\nexora-latido-externo-serverdata.ps1 -Desinstalar
#>
[CmdletBinding()]
param(
  [string]$UrlLatido,
  [switch]$Instalar,
  [switch]$Desinstalar,
  [switch]$Verboso,
  [ValidateRange(1, 60)]
  [int]$CadaMinutos = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$NombreTarea = "NEXORA Latido Externo"
$RutaHeartbeat = "E:\Datos\NEXORA_Runtime\wsl-heartbeat.json"
$DirectorioLog = "E:\Datos\Backups\NEXORA\Logs"
$RutaLog = Join-Path $DirectorioLog "latido-externo.log"
$RutaScriptInstalado = "C:\ProgramData\NEXORA\nexora-latido-externo-serverdata.ps1"
$HostSalud = "nexora.tail7bb880.ts.net"
$UrlSalud = "https://$HostSalud/api/health/"
# Un heartbeat mas viejo que esto significa que el keepalive dejo de escribir.
$EdadMaximaHeartbeat = 120

function Write-Log {
  param([string]$Mensaje)
  New-Item -ItemType Directory -Path $DirectorioLog -Force | Out-Null
  $linea = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Mensaje
  Add-Content -LiteralPath $RutaLog -Encoding UTF8 -Value $linea
  if ($Verboso) { Write-Output $linea }
}

if ($Desinstalar) {
  Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "Tarea '$NombreTarea' eliminada."
  exit 0
}

if ($Instalar) {
  if ([Environment]::MachineName -ne "SERVERDATA") {
    throw "El instalador solo puede ejecutarse en SERVERDATA."
  }
  if ([string]::IsNullOrWhiteSpace($UrlLatido)) {
    throw "Falta -UrlLatido con la URL del check de healthchecks.io."
  }

  New-Item -ItemType Directory -Path (Split-Path -Parent $RutaScriptInstalado) -Force | Out-Null
  Copy-Item -LiteralPath $PSCommandPath -Destination $RutaScriptInstalado -Force

  $argumento = (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-File `"$RutaScriptInstalado`" -UrlLatido `"$UrlLatido`""
  )
  $accion = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument $argumento
  $arranque = New-ScheduledTaskTrigger -AtStartup
  $arranque.Delay = "PT3M"
  $periodico = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $CadaMinutos) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $opciones = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

  Register-ScheduledTask `
    -TaskName $NombreTarea `
    -Action $accion `
    -Trigger @($arranque, $periodico) `
    -Principal $principal `
    -Settings $opciones `
    -Force | Out-Null

  Write-Output "Tarea '$NombreTarea' registrada (cada $CadaMinutos minutos)."
  Write-Output "En healthchecks.io configurar: Period = $CadaMinutos min, Grace = 15 min."
  exit 0
}

# ------------------------------------------------------------------ ejecucion

if ([string]::IsNullOrWhiteSpace($UrlLatido)) {
  throw "Falta -UrlLatido."
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$problemas = New-Object System.Collections.ArrayList
$resumen = ""

# 1) El heartbeat que escribe el keepalive dentro de WSL.
try {
  if (-not (Test-Path -LiteralPath $RutaHeartbeat -PathType Leaf)) {
    [void]$problemas.Add("No existe el heartbeat de WSL.")
  } else {
    $archivo = Get-Item -LiteralPath $RutaHeartbeat
    $edad = [Math]::Round(((Get-Date) - $archivo.LastWriteTime).TotalSeconds, 0)
    $hb = Get-Content -LiteralPath $RutaHeartbeat -Raw -Encoding UTF8 | ConvertFrom-Json
    $resumen = "estado=$($hb.state) contenedores=$($hb.runningContainers)/$($hb.totalContainers) heartbeat=${edad}s"
    if ($edad -gt $EdadMaximaHeartbeat) {
      [void]$problemas.Add("El heartbeat de WSL tiene $edad s (el keepalive dejo de escribir).")
    }
    if ([int]$hb.runningContainers -lt [int]$hb.totalContainers) {
      [void]$problemas.Add("Contenedores caidos: $($hb.runningContainers) de $($hb.totalContainers).")
    }
  }
} catch {
  [void]$problemas.Add("No se pudo leer el heartbeat: $($_.Exception.Message)")
}

# 2) La API respondiendo de verdad, no sólo los contenedores "arriba".
try {
  $r = Invoke-WebRequest -Uri $UrlSalud -UseBasicParsing -TimeoutSec 15
  if ([int]$r.StatusCode -ne 200) {
    [void]$problemas.Add("La API respondio HTTP $($r.StatusCode).")
  }
} catch {
  [void]$problemas.Add("La API no responde: $($_.Exception.Message)")
}

# 3) Resultado al servicio externo.
$sano = ($problemas.Count -eq 0)
$destino = if ($sano) { $UrlLatido.TrimEnd('/') } else { $UrlLatido.TrimEnd('/') + "/fail" }
$cuerpo = if ($sano) { "OK $resumen" } else { (@("NEXORA con problemas: $resumen") + $problemas) -join "`n" }

try {
  Invoke-WebRequest -Uri $destino -Method Post -Body $cuerpo -UseBasicParsing -TimeoutSec 20 | Out-Null
  if (-not $sano) { Write-Log "Latido de FALLA enviado. $cuerpo" }
  elseif ($Verboso) { Write-Log "Latido OK enviado. $resumen" }
} catch {
  # Si no hay internet no se puede hacer nada mas que dejarlo registrado: el
  # servicio externo va a avisar igual por timeout.
  Write-Log "No se pudo enviar el latido: $($_.Exception.Message)"
}

if ($Verboso) {
  if ($sano) { Write-Output "NEXORA sano. $resumen" }
  else { Write-Output (("NEXORA con problemas:`n" + ($problemas -join "`n"))) }
}
