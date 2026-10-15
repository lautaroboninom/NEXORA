$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    docker compose -f docker-compose.technical-dev.yml build api
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo construir la API de desarrollo.' }
    # PostgreSQL exige que la plantilla no tenga otras conexiones durante su clonación.
    docker compose -f docker-compose.technical-dev.yml stop api
    try {
        docker compose -f docker-compose.technical-dev.yml run --rm --no-deps api python manage.py test service.tests.test_technical_interventions --keepdb --noinput
        $testResult = $LASTEXITCODE
    } finally {
        docker compose -f docker-compose.technical-dev.yml up -d api
    }
    if ($testResult -ne 0) { throw 'Fallaron las pruebas de carga técnica.' }
} finally {
    Pop-Location
}
