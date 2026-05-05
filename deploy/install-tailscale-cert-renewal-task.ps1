param(
  [string]$TaskName = "SistemaReparaciones-RenovarCertificadoTailscale",
  [string]$ScriptPath = "",
  [string]$At = "03:15"
)

$ErrorActionPreference = "Stop"

if (-not $ScriptPath) {
  $ScriptPath = Join-Path $PSScriptRoot "renew-tailscale-cert.ps1"
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
  throw "No existe el script de renovación: $ScriptPath"
}

$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Renueva el certificado HTTPS Tailscale del Sistema de Reparaciones y recarga Nginx." `
  -Force | Out-Null

Write-Host "Tarea programada instalada: $TaskName"
