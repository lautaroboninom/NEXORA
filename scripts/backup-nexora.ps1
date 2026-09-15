param(
  [string]$BackupRoot = "C:\NEXORA\backups",
  [string]$ComposeFile = "docker-compose.prod.yml"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $BackupRoot $stamp
New-Item -ItemType Directory -Force -Path $target | Out-Null

$dbDump = Join-Path $target "postgres.dump"
$partialDump = Join-Path $target "postgres.dump.partial"
$remoteDump = "/tmp/nexora-backup-$([guid]::NewGuid().ToString('N')).dump"
$containerIds = @(docker compose -f $ComposeFile ps -q postgres)
if ($LASTEXITCODE -ne 0 -or $containerIds.Count -ne 1 -or [string]::IsNullOrWhiteSpace($containerIds[0])) {
  throw "No se encontró un único contenedor PostgreSQL activo; no se generó el respaldo."
}
$containerId = $containerIds[0].Trim()
try {
  # PowerShell 5.1 convierte la salida nativa en texto: redirigir pg_dump -Fc
  # corrompe sus bytes. Crear el archivo dentro del contenedor y copiarlo intacto.
  docker compose -f $ComposeFile exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$1" && pg_restore --list "$1" >/dev/null' sh $remoteDump
  if ($LASTEXITCODE -ne 0) {
    throw "Falló la generación o la lectura del respaldo PostgreSQL."
  }
  docker cp "${containerId}:$remoteDump" $partialDump
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $partialDump)) {
    throw "No se pudo copiar el respaldo PostgreSQL."
  }
  if ((Get-Item -LiteralPath $partialDump).Length -eq 0) {
    throw "El respaldo PostgreSQL está vacío."
  }
  Move-Item -LiteralPath $partialDump -Destination $dbDump
} finally {
  docker exec $containerId rm -f -- $remoteDump
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "No se pudo retirar el archivo temporal del contenedor: $remoteDump"
  }
}

$paths = @(
  @{ Source = ".env.prod"; Name = "env.prod" },
  @{ Source = ".env.prod.internet"; Name = "env.prod.internet" },
  @{ Source = $ComposeFile; Name = $ComposeFile },
  @{ Source = "quotes"; Name = "quotes" },
  @{ Source = "api\service\media"; Name = "api-service-media" }
)

foreach ($item in $paths) {
  if (Test-Path $item.Source) {
    $dest = Join-Path $target $item.Name
    Copy-Item -LiteralPath $item.Source -Destination $dest -Recurse -Force
  }
}

Write-Host "Backup NEXORA OK: $target"
