param(
  [string]$ComposeFile = "docker-compose.prod.yml",
  [string]$Branch = "main",
  [string]$HealthUrl = "http://localhost/api/ping/",
  [int]$HealthRetries = 30,
  [switch]$KeepLegacyContainers
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

docker compose -f $ComposeFile exec -T api python manage.py apply_ticket_sale_states_schema
docker compose -f $ComposeFile exec -T api python manage.py apply_delivery_orders_schema
docker compose -f $ComposeFile exec -T api python manage.py apply_bejerman_sync_schema
docker compose -f $ComposeFile exec -T api python manage.py apply_bejerman_ris_schema

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
