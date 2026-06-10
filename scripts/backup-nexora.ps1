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
docker compose -f $ComposeFile exec -T postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > $dbDump

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
