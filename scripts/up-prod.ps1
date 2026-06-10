$ErrorActionPreference = "Stop"

# Variables
# Usa .env.prod + docker-compose.prod.yml para despliegue LAN (sin internet).
# Para publicar en internet, combina docker-compose.prod.internet.yml con .env.prod.internet manualmente.
$compose = @("-f","docker-compose.yml","-f","docker-compose.prod.yml","--env-file",".env.prod")

Write-Host "Building and starting stack (prod LAN)" -ForegroundColor Cyan
docker compose @compose up -d --build

Write-Host "`nServices:" -ForegroundColor Cyan
docker compose @compose ps

Write-Host "`nTailing API logs (Ctrl+C to stop)" -ForegroundColor Cyan
docker compose @compose logs -f api

# Helpful commands:
# docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod logs -f api
# docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod logs -f frontend
# docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod logs -f nexora-postgres
# docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod exec nexora-postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT NOW();"'
# docker compose -f docker-compose.yml -f docker-compose.prod.internet.yml --env-file .env.prod.internet up -d --build
