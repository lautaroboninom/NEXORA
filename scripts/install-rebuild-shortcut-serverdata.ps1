param(
  [switch]$NoPause
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
$WindowsRepo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$ConsoleScript = Join-Path $WindowsRepo "scripts\nexora-prod-console-serverdata.ps1"
$MonitorScript = Join-Path $WindowsRepo "scripts\get-prod-monitoring-serverdata.ps1"
$IconPath = Join-Path $WindowsRepo "scripts\assets\nexora-rebuild.ico"
$DesktopDirectory = "C:\Users\Administrator\Desktop"
$ShortcutPath = Join-Path $DesktopDirectory "Rebuild NEXORA Producción.lnk"
$PowerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$ShortcutArguments = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ConsoleScript`""

function Wait-ForUser {
  if (-not $NoPause) {
    Write-Host ""
    [void](Read-Host "Presione ENTER para cerrar")
  }
}

$exitCode = 0
try {
  try {
    $Host.UI.RawUI.WindowTitle = "Instalar acceso directo de NEXORA"
  } catch {
    # Algunos hosts de PowerShell no permiten cambiar el título.
  }

  if ([Environment]::MachineName -ne $ExpectedComputerName) {
    throw "Este instalador solo puede ejecutarse en $ExpectedComputerName. Equipo actual: $([Environment]::MachineName)."
  }

  foreach ($requiredFile in @($ConsoleScript, $MonitorScript, $IconPath, $PowerShellPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "Falta el archivo requerido: $requiredFile"
    }
  }

  if (-not (Test-Path -LiteralPath $DesktopDirectory -PathType Container)) {
    throw "No existe el escritorio esperado: $DesktopDirectory"
  }

  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($ShortcutPath)
  $shortcut.TargetPath = $PowerShellPath
  $shortcut.Arguments = $ShortcutArguments
  $shortcut.WorkingDirectory = $WindowsRepo
  $shortcut.Description = "Monitorear y reconstruir NEXORA Producción en SERVERDATA"
  $shortcut.IconLocation = "$IconPath,0"
  $shortcut.WindowStyle = 1
  $shortcut.Save()

  $installedShortcut = $shell.CreateShortcut($ShortcutPath)
  if ($installedShortcut.TargetPath -ne $PowerShellPath) {
    throw "El destino del acceso directo no coincide con PowerShell."
  }
  if ($installedShortcut.Arguments -ne $ShortcutArguments) {
    throw "Los argumentos del acceso directo no coinciden con el script de rebuild."
  }
  if ($installedShortcut.WorkingDirectory -ne $WindowsRepo) {
    throw "La carpeta de trabajo del acceso directo no coincide con el repositorio."
  }
  if ($installedShortcut.IconLocation -ne "$IconPath,0") {
    throw "El acceso directo no quedó asociado al ícono de NEXORA."
  }

  Write-Host "Acceso directo instalado correctamente:" -ForegroundColor Green
  Write-Host $ShortcutPath
  Write-Host "Destino: $PowerShellPath"
  Write-Host "Consola: $ConsoleScript"
  Write-Host "Monitoreo: $MonitorScript"
  Write-Host "Ícono: $IconPath"
} catch {
  $exitCode = 1
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
}

Wait-ForUser
exit $exitCode
