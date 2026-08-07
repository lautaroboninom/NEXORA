param(
  [string]$ComposeFile = "docker-compose.prod.yml",
  [string]$Branch = "main",
  [string]$HealthUrl = "http://localhost/api/ping/",
  [int]$HealthRetries = 30,
  [switch]$KeepLegacyContainers,
  [switch]$SkipBejermanSmoke
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

Write-Host "NEXORA deploy: $repo"

git fetch origin $Branch
git checkout $Branch
git pull --ff-only origin $Branch

if (-not $KeepLegacyContainers) {
  $legacyContainers = @(
    "sistemadereparaciones-web",
    "sistemadereparaciones-api",
    "sistemadereparaciones-bejerman-sync",
    "sistemadereparaciones-postgres"
  )
  foreach ($container in $legacyContainers) {
    $exists = docker ps -a --format "{{.Names}}" | Where-Object { $_ -eq $container }
    if ($exists) {
      docker stop $container | Out-Null
      docker rm $container | Out-Null
    }
  }
}

docker compose -f $ComposeFile pull
docker compose -f $ComposeFile up -d --build

# Los esquemas los aplica el propio arranque del contenedor (prepare_production_schema en el
# command de docker-compose.prod.yml). No repetir la lista acá: tenerla duplicada en varios
# archivos desincronizados es lo que hacía que un apply_ nuevo se olvidara en algún entorno.
if (-not $SkipBejermanSmoke) {
  docker compose -f $ComposeFile exec -T api python manage.py check_bejerman_sdk --skip-live
}

$ok = $false
for ($i = 1; $i -le $HealthRetries; $i++) {
  try {
    $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
      $ok = $true
      break
    }
  } catch {
    Start-Sleep -Seconds 2
  }
}

if (-not $ok) {
  docker compose -f $ComposeFile ps
  throw "NEXORA no respondió el smoke check: $HealthUrl"
}

Write-Host "NEXORA deploy OK"
