<#
  Avisa por mail cuando SERVERDATA se reinicia, y despues avisa si NEXORA
  levanto solo o si hay que intervenir.

  El problema que resuelve: cuando el server se reinicia, NEXORA vuelve solo en
  unos 10 minutos, pero nadie se entera. Sin ese aviso se asume que no arranco y
  se corre un rebuild a mano, que provoca un segundo corte que no existia.

  Instalar (una vez, PowerShell como administrador en SERVERDATA):
    .\nexora-alerta-arranque-serverdata.ps1 -Instalar -Para "alguien@sepid.com.ar"

  Probar sin esperar un reinicio:
    .\nexora-alerta-arranque-serverdata.ps1 -Para "alguien@sepid.com.ar" -Prueba

  Desinstalar:
    .\nexora-alerta-arranque-serverdata.ps1 -Desinstalar
#>
[CmdletBinding()]
param(
  [string[]]$Para,
  [switch]$Instalar,
  [switch]$Desinstalar,
  [switch]$Prueba,
  [ValidateRange(1, 60)]
  [int]$EsperaMaximaMinutos = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$NombreTarea = "NEXORA Alerta Arranque"
$Repo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$RutaEnv = Join-Path $Repo ".env.prod"
$RutaHeartbeat = "E:\Datos\NEXORA_Runtime\wsl-heartbeat.json"
$DirectorioLog = "E:\Datos\Backups\NEXORA\Logs"
$RutaLog = Join-Path $DirectorioLog "alerta-arranque.log"
$RutaScriptInstalado = "C:\ProgramData\NEXORA\nexora-alerta-arranque-serverdata.ps1"
$UrlSalud = "https://nexora.tail7bb880.ts.net/api/health/"

function Write-Log {
  param([string]$Mensaje)
  New-Item -ItemType Directory -Path $DirectorioLog -Force | Out-Null
  $linea = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Mensaje
  Add-Content -LiteralPath $RutaLog -Encoding UTF8 -Value $linea
  Write-Output $linea
}

function Read-EnvFile {
  param([string]$Ruta)
  $valores = @{}
  if (-not (Test-Path -LiteralPath $Ruta -PathType Leaf)) { return $valores }
  foreach ($linea in [System.IO.File]::ReadAllLines($Ruta)) {
    $t = $linea.Trim()
    if ($t.Length -eq 0 -or $t.StartsWith("#")) { continue }
    $i = $t.IndexOf("=")
    if ($i -lt 1) { continue }
    $clave = $t.Substring(0, $i).Trim()
    $valor = $t.Substring($i + 1).Trim()
    if ($valor.Length -ge 2) {
      if (($valor.StartsWith('"') -and $valor.EndsWith('"')) -or
          ($valor.StartsWith("'") -and $valor.EndsWith("'"))) {
        $valor = $valor.Substring(1, $valor.Length - 2)
      }
    }
    $valores[$clave] = $valor
  }
  return $valores
}

function Send-Aviso {
  param(
    [Parameter(Mandatory = $true)][string]$Asunto,
    [Parameter(Mandatory = $true)][string]$Cuerpo,
    [Parameter(Mandatory = $true)][string[]]$Destinatarios
  )

  $env = Read-EnvFile -Ruta $RutaEnv
  foreach ($requerido in @("EMAIL_HOST", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD")) {
    if (-not $env.ContainsKey($requerido) -or [string]::IsNullOrWhiteSpace($env[$requerido])) {
      Write-Log "No se pudo enviar el aviso: falta $requerido en $RutaEnv"
      return $false
    }
  }

  $puerto = 587
  if ($env.ContainsKey("EMAIL_PORT") -and $env["EMAIL_PORT"]) { $puerto = [int]$env["EMAIL_PORT"] }
  $remitente = if ($env.ContainsKey("DEFAULT_FROM_EMAIL") -and $env["DEFAULT_FROM_EMAIL"]) {
    $env["DEFAULT_FROM_EMAIL"]
  } else { $env["EMAIL_HOST_USER"] }

  $cliente = $null
  $mensaje = $null
  try {
    $cliente = New-Object System.Net.Mail.SmtpClient($env["EMAIL_HOST"], $puerto)
    $cliente.EnableSsl = ($env.ContainsKey("EMAIL_USE_TLS") -and $env["EMAIL_USE_TLS"] -eq "1")
    $cliente.Credentials = New-Object System.Net.NetworkCredential(
      $env["EMAIL_HOST_USER"], $env["EMAIL_HOST_PASSWORD"]
    )
    $cliente.Timeout = 30000

    $mensaje = New-Object System.Net.Mail.MailMessage
    $mensaje.From = New-Object System.Net.Mail.MailAddress($remitente, "NEXORA - SERVERDATA")
    foreach ($d in $Destinatarios) { $mensaje.To.Add($d.Trim()) }
    $mensaje.Subject = $Asunto
    $mensaje.Body = $Cuerpo
    $mensaje.IsBodyHtml = $false

    $cliente.Send($mensaje)
    Write-Log "Aviso enviado a $($Destinatarios -join ', '): $Asunto"
    return $true
  } catch {
    Write-Log "Fallo el envio del aviso: $($_.Exception.Message)"
    return $false
  } finally {
    if ($null -ne $mensaje) { $mensaje.Dispose() }
    if ($null -ne $cliente) { $cliente.Dispose() }
  }
}

function Get-Heartbeat {
  if (-not (Test-Path -LiteralPath $RutaHeartbeat -PathType Leaf)) { return $null }
  try {
    $archivo = Get-Item -LiteralPath $RutaHeartbeat
    $datos = Get-Content -LiteralPath $RutaHeartbeat -Raw -Encoding UTF8 | ConvertFrom-Json
    return [pscustomobject]@{
      Estado = [string]$datos.state
      Contenedores = "{0}/{1}" -f $datos.runningContainers, $datos.totalContainers
      TodosArriba = ([int]$datos.runningContainers -eq [int]$datos.totalContainers)
      ApiHttp = [int]$datos.apiHttp
      EdadSegundos = [Math]::Round(((Get-Date) - $archivo.LastWriteTime).TotalSeconds, 0)
    }
  } catch {
    return $null
  }
}

function Get-MotivoReinicio {
  try {
    $ev = Get-WinEvent -FilterHashtable @{
      LogName = 'System'; Id = 1074, 6008; StartTime = (Get-Date).AddHours(-2)
    } -MaxEvents 3 -ErrorAction SilentlyContinue
    if (-not $ev) { return "no registrado (posible corte de energia o reinicio forzado)" }
    return (($ev[0].Message -split "`r?`n" | Where-Object { $_.Trim() }) -join " | ")
  } catch {
    return "no se pudo determinar"
  }
}

# ---------------------------------------------------------------- instalacion

if ($Desinstalar) {
  Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "Tarea '$NombreTarea' eliminada."
  exit 0
}

if ($Instalar) {
  if ([Environment]::MachineName -ne "SERVERDATA") {
    throw "El instalador solo puede ejecutarse en SERVERDATA."
  }
  if (-not $Para -or $Para.Count -eq 0) {
    throw "Falta -Para con al menos un destinatario."
  }

  New-Item -ItemType Directory -Path (Split-Path -Parent $RutaScriptInstalado) -Force | Out-Null
  Copy-Item -LiteralPath $PSCommandPath -Destination $RutaScriptInstalado -Force

  $destinatarios = ($Para | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ","
  $argumento = (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-File `"$RutaScriptInstalado`" -Para $destinatarios"
  )
  $accion = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument $argumento
  # El retardo le da tiempo a la red y a Tailscale antes del primer intento de envio.
  $disparador = New-ScheduledTaskTrigger -AtStartup
  $disparador.Delay = "PT45S"
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $opciones = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ($EsperaMaximaMinutos + 10))

  Register-ScheduledTask `
    -TaskName $NombreTarea `
    -Action $accion `
    -Trigger $disparador `
    -Principal $principal `
    -Settings $opciones `
    -Force | Out-Null

  Write-Output "Tarea '$NombreTarea' registrada. Avisara a: $($Para -join ', ')"
  Write-Output "Probar ahora sin reiniciar:  Start-ScheduledTask -TaskName '$NombreTarea'"
  exit 0
}

# ------------------------------------------------------------------ ejecucion

if (-not $Para -or $Para.Count -eq 0) {
  throw "Falta -Para con al menos un destinatario."
}

$os = Get-WmiObject Win32_OperatingSystem
$arranque = $os.ConvertToDateTime($os.LastBootUpTime)
$motivo = Get-MotivoReinicio

Write-Log "Arranque detectado ($arranque). Motivo: $motivo"

$encabezado = @"
SERVERDATA se reinicio.

  Arranque : $arranque
  Motivo   : $motivo

"@

if ($Prueba) { $encabezado = "[PRUEBA - no hubo reinicio real]`r`n`r`n" + $encabezado }

$cuerpoInicial = $encabezado + @"
NEXORA levanta solo despues de un reinicio. Normalmente tarda entre 5 y 10
minutos desde que arranca Windows.

NO HACE FALTA CORRER EL REBUILD A MANO. Hacerlo mientras el sistema esta
levantando provoca un segundo corte.

Cuando termine de levantar (o si no lo logra) te llega un segundo mail con el
resultado.

Estado en vivo: $UrlSalud
"@

[void](Send-Aviso -Asunto "NEXORA: SERVERDATA se reinicio, levantando" -Cuerpo $cuerpoInicial -Destinatarios $Para)

$limite = (Get-Date).AddMinutes($EsperaMaximaMinutos)
$listo = $false
$ultimo = $null

while ((Get-Date) -lt $limite) {
  $hb = Get-Heartbeat
  if ($null -ne $hb) {
    $ultimo = $hb
    if ($hb.Estado -eq "healthy" -and $hb.TodosArriba -and $hb.EdadSegundos -le 90) {
      $listo = $true
      break
    }
  }
  Start-Sleep -Seconds 30
}

$demora = [Math]::Round(((Get-Date) - $arranque).TotalMinutes, 1)

if ($listo) {
  $detalle = "  Contenedores : $($ultimo.Contenedores)`r`n  API          : HTTP $($ultimo.ApiHttp)"
  Write-Log "NEXORA operativo tras $demora minutos. $($ultimo.Contenedores)"
  [void](Send-Aviso `
    -Asunto "NEXORA: operativo de nuevo ($demora min)" `
    -Cuerpo ($encabezado + "NEXORA volvio a estar operativo.`r`n`r`n  Demora total : $demora minutos`r`n$detalle`r`n`r`nNo hay nada que hacer.") `
    -Destinatarios $Para)
} else {
  $detalle = if ($null -ne $ultimo) {
    "  Ultimo estado : $($ultimo.Estado)`r`n  Contenedores  : $($ultimo.Contenedores)`r`n  API           : HTTP $($ultimo.ApiHttp)`r`n  Heartbeat     : hace $($ultimo.EdadSegundos) s"
  } else {
    "  No hay heartbeat: el keepalive de WSL no llego a arrancar."
  }
  Write-Log "NEXORA NO levanto tras $EsperaMaximaMinutos minutos."
  [void](Send-Aviso `
    -Asunto "NEXORA: NO LEVANTO tras el reinicio - hay que intervenir" `
    -Cuerpo ($encabezado + "Pasaron $EsperaMaximaMinutos minutos y NEXORA sigue sin estar operativo.`r`n`r`n$detalle`r`n`r`nQue mirar:`r`n  - Tarea 'NEXORA WSL Docker' en el Programador de tareas`r`n  - E:\Datos\Backups\NEXORA\Logs\wsl-keepalive.log`r`n  - E:\Datos\Backups\NEXORA\Logs\wsl-watchdog.log`r`n`r`nRecien ahi tiene sentido levantarlo a mano.") `
    -Destinatarios $Para)
}
