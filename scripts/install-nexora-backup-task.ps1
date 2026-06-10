param(
  [string]$TaskName = "NEXORA Backup",
  [string]$At = "02:30",
  [string]$BackupRoot = "C:\NEXORA\backups"
)

$ErrorActionPreference = "Stop"

$script = Resolve-Path (Join-Path $PSScriptRoot "backup-nexora.ps1")
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -BackupRoot `"$BackupRoot`"" `
  -WorkingDirectory "$repo"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

Write-Host "Tarea programada creada: $TaskName a las $At"
