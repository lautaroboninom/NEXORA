<#
.SYNOPSIS
  Rehace en producción el trabajo de Bejerman que quedó trabado: PDFs que nunca
  llegaron y jobs de sincronización zombis.

.DESCRIPTION
  Corre en la consola de SERVERDATA, igual que el rebuild. No reconstruye las
  imágenes: copia al contenedor `nexora-api` los dos archivos que la recuperación
  necesita y ejecuta el comando en un proceso aparte. El API que está atendiendo
  no se toca (Python ya tiene sus módulos cargados; el archivo nuevo lo lee el
  proceso nuevo), así que se puede correr con gente trabajando.

  Primero muestra qué va a hacer sin tocar nada, y recién después de confirmar
  aplica los cambios.

  Los equipos de TERAPIAS MÉDICAS quedan excluidos por defecto: su problema de
  stock (artículo 1115001 rechazado en STC) es otro y sigue sin resolverse.

.PARAMETER Apply
  Aplica los cambios sin preguntar. Sin esto, pide confirmación después de mostrar
  el plan.

.PARAMETER DryRunOnly
  Sólo muestra el plan y termina.

.PARAMETER RepairSsh
  Además intenta levantar el servicio sshd de Windows, que viene cayéndose y deja
  al equipo sin acceso remoto al server.
#>
param(
  [switch]$Apply,
  [switch]$DryRunOnly,
  [switch]$RepairSsh,
  [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
try {
  [Console]::OutputEncoding = $Utf8Encoding
} catch {
  # Hosts sin consola estándar: seguir igual.
}

$ExpectedComputerName = "SERVERDATA"
$Distro = "Ubuntu-24.04"
$WslRepo = "/mnt/e/Datos/Servicio Tecnico/1_SISTEMA REPARACIONES/NEXORA"
$ApiContainer = "nexora-api"
$LogDirectory = "E:\Datos\NEXORA_Runtime\deploy_logs"
$SkipCustomers = @("TMD")

# Lo que la recuperación necesita que esté al día dentro del contenedor, relativo
# a la raíz del código. Son aditivos: funciones nuevas que nada de lo que ya corre
# importa, así que copiarlas no cambia el comportamiento del API en marcha.
$FilesToStage = @(
  "service/bejerman_sync.py",
  "service/management/commands/recover_stuck_bejerman_work.py"
)

# Prod tiene el código en /app y el dev viejo en /code: se detecta y no se asume.
$script:AppRoot = ""

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host $Message -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host $Message -ForegroundColor Yellow }

function Wait-ForUser {
  if (-not $NoPause) {
    Write-Host ""
    [void](Read-Host "Presione ENTER para cerrar")
  }
}

function Invoke-Wsl {
  param(
    [Parameter(Mandatory = $true)][string]$Command,
    [ValidateRange(1, 3600)][int]$TimeoutSeconds = 300,
    [switch]$IgnoreExitCode
  )

  # El comando viaja en base64: así no hay que pelear con el escapado entre
  # cmd.exe, wsl y bash, que es donde se rompen estas cosas.
  $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Command))
  $wslPath = Join-Path $env:SystemRoot "System32\wsl.exe"
  $linuxTimeout = [Math]::Max(1, $TimeoutSeconds - 5)
  $bootstrap = "printf '%s' '$encoded' | base64 -d | timeout --signal=TERM --kill-after=5s ${linuxTimeout}s bash"

  & $wslPath -d $Distro -u root -- bash -lc "$bootstrap"
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0 -and -not $IgnoreExitCode) {
    throw "El comando falló en WSL (código $exitCode)."
  }
  return $exitCode
}

function Assert-Environment {
  Write-Step "Verificando el entorno"
  if ($env:COMPUTERNAME -ne $ExpectedComputerName) {
    throw "Este script corre sólo en $ExpectedComputerName (esta máquina es $($env:COMPUTERNAME))."
  }
  Write-Ok "Equipo correcto: $ExpectedComputerName"

  Invoke-Wsl -Command "docker info >/dev/null 2>&1" -TimeoutSeconds 60 | Out-Null
  Write-Ok "Docker responde dentro de WSL"

  $running = & (Join-Path $env:SystemRoot "System32\wsl.exe") -d $Distro -u root -- `
    docker ps --filter "name=$ApiContainer" --format "{{.Names}}"
  if (-not ($running -match [regex]::Escape($ApiContainer))) {
    throw "El contenedor $ApiContainer no está corriendo. Levante producción antes de recuperar."
  }
  Write-Ok "Contenedor $ApiContainer en marcha"

  $wslPath = Join-Path $env:SystemRoot "System32\wsl.exe"
  $root = (& $wslPath -d $Distro -u root -- docker exec $ApiContainer sh -c "dirname `$(readlink -f manage.py)") `
    | Select-Object -First 1
  $root = "$root".Trim()
  if ([string]::IsNullOrWhiteSpace($root)) {
    throw "No se pudo ubicar el código dentro de $ApiContainer (no se encontró manage.py)."
  }
  $script:AppRoot = $root
  Write-Ok "Código del contenedor en $root"
}

function Copy-RecoveryFiles {
  Write-Step "Poniendo al día el código de recuperación dentro del contenedor"
  foreach ($relative in $FilesToStage) {
    $source = "$WslRepo/api/$relative"
    $target = "$($script:AppRoot)/$relative"
    $backup = "$target.pre-recover"
    # Copia de resguardo la primera vez, para poder volver atrás sin rebuild.
    Invoke-Wsl -Command @"
set -e
if ! docker exec $ApiContainer test -f '$backup'; then
  docker exec $ApiContainer sh -c "cp '$target' '$backup' 2>/dev/null || true"
fi
docker cp '$source' ${ApiContainer}:'$target'
"@ -TimeoutSeconds 120 | Out-Null
    Write-Ok "  $relative"
  }
}

function Invoke-Recovery {
  param([switch]$ApplyChanges)

  $skipArgs = ($SkipCustomers | ForEach-Object { "--skip-customer $_" }) -join " "
  $applyArg = if ($ApplyChanges) { "--apply" } else { "" }
  $command = "docker exec $ApiContainer python manage.py recover_stuck_bejerman_work $skipArgs $applyArg"
  Invoke-Wsl -Command $command -TimeoutSeconds 900 | Out-Null
}

function Repair-SshService {
  Write-Step "Intentando levantar el servicio SSH"
  try {
    $service = Get-Service -Name sshd -ErrorAction Stop
    if ($service.Status -eq "Running") {
      Write-Ok "sshd ya está corriendo"
      return
    }
    Start-Service -Name sshd -ErrorAction Stop
    Start-Sleep -Seconds 3
    $service.Refresh()
    if ($service.Status -eq "Running") {
      Write-Ok "sshd levantado"
    } else {
      Write-Warn "sshd no quedó corriendo (estado: $($service.Status))."
    }
  } catch {
    Write-Warn "No se pudo levantar sshd: $($_.Exception.Message)"
    Write-Warn "Workaround conocido: & C:\Windows\System32\OpenSSH\sshd.exe -d (atiende una sola conexión)."
  }
}

$transcriptStarted = $false
try {
  if (-not (Test-Path $LogDirectory)) {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
  }
  $logFile = Join-Path $LogDirectory ("recover-bejerman-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
  Start-Transcript -Path $logFile | Out-Null
  $transcriptStarted = $true

  Assert-Environment
  Copy-RecoveryFiles

  Write-Step "Qué se va a hacer (sin tocar nada todavía)"
  Invoke-Recovery

  if ($DryRunOnly) {
    Write-Warn "`nModo sólo simulación: no se aplicó nada."
    exit 0
  }

  if (-not $Apply) {
    Write-Host ""
    $answer = Read-Host "¿Aplicar esta recuperación en PRODUCCIÓN? (escriba SI para continuar)"
    if ($answer.Trim().ToUpperInvariant() -ne "SI") {
      Write-Warn "Cancelado por el operador. No se modificó nada."
      exit 0
    }
  }

  Write-Step "Aplicando"
  Invoke-Recovery -ApplyChanges
  Write-Ok "`nRecuperación aplicada."

  Write-Host ""
  Write-Warn "Falta lo que no puede hacer este script:"
  Write-Warn "  - Los remitos en revisión se destraban desde el panel de Bejerman"
  Write-Warn "    (Verificar; y si queda ambiguo, Resolver revisión)."
  Write-Warn "  - Para que ese botón exista hay que reconstruir con rebuild-prod-serverdata.ps1."
  Write-Warn "  - OXL: cargar la situación de IVA en la ficha del cliente en Bejerman."

  if ($RepairSsh) {
    Repair-SshService
  }
} catch {
  Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
  $exitCode = 1
} finally {
  if ($transcriptStarted) {
    try { Stop-Transcript | Out-Null } catch { }
  }
  Wait-ForUser
}

if (Test-Path variable:exitCode) { exit $exitCode }
exit 0
