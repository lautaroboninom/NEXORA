param(
  [string]$Domain = "sistemadereparaciones.tail7bb880.ts.net",
  [string]$CertDir = "C:\sepid_certs",
  [string]$ContainerName = "sistemadereparaciones-web",
  [string]$MinValidity = "720h"
)

$ErrorActionPreference = "Stop"

function Write-Info {
  param([string]$Message)
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
}

function Get-CertificateNotAfter {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return $null
  }
  return (Get-PfxCertificate -FilePath $Path).NotAfter
}

if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
  throw "No se encontró el comando tailscale en PATH."
}

New-Item -ItemType Directory -Force -Path $CertDir | Out-Null

$certPath = Join-Path $CertDir "$Domain.crt"
$keyPath = Join-Path $CertDir "$Domain.key"
$backupDir = Join-Path $CertDir "backup"
$tmpDir = Join-Path $CertDir "tmp-renew"

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

$tmpCert = Join-Path $tmpDir "$Domain.crt"
$tmpKey = Join-Path $tmpDir "$Domain.key"

Remove-Item -LiteralPath $tmpCert, $tmpKey -Force -ErrorAction SilentlyContinue

Write-Info "Solicitando certificado Tailscale para $Domain con validez mínima $MinValidity."
tailscale cert --min-validity $MinValidity --cert-file $tmpCert --key-file $tmpKey $Domain

if (-not (Test-Path -LiteralPath $tmpCert) -or -not (Test-Path -LiteralPath $tmpKey)) {
  throw "Tailscale no generó los archivos esperados."
}

$newNotAfter = Get-CertificateNotAfter -Path $tmpCert
if (-not $newNotAfter) {
  throw "No se pudo validar la fecha de vencimiento del certificado nuevo."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($path in @($certPath, $keyPath)) {
  if (Test-Path -LiteralPath $path) {
    $name = Split-Path -Path $path -Leaf
    Copy-Item -LiteralPath $path -Destination (Join-Path $backupDir "$stamp-$name") -Force
  }
}

Move-Item -LiteralPath $tmpCert -Destination $certPath -Force
Move-Item -LiteralPath $tmpKey -Destination $keyPath -Force

Write-Info "Certificado actualizado. Vence: $($newNotAfter.ToString('yyyy-MM-dd HH:mm:ss'))."

if (Get-Command docker -ErrorAction SilentlyContinue) {
  $running = docker ps --format "{{.Names}}" | Where-Object { $_ -eq $ContainerName }
  if ($running) {
    Write-Info "Validando configuración de Nginx en $ContainerName."
    docker exec $ContainerName nginx -t
    Write-Info "Recargando Nginx en $ContainerName."
    docker exec $ContainerName nginx -s reload
  } else {
    Write-Info "El contenedor $ContainerName no está corriendo; no se recargó Nginx."
  }
} else {
  Write-Info "Docker no está disponible; no se recargó Nginx."
}
