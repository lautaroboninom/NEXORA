#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Ensure acme.json exists with correct permissions for Traefik
mkdir -p deploy/traefik
touch deploy/traefik/acme.json
chmod 600 deploy/traefik/acme.json || true

echo "> Starting stack (Traefik, DB, API, Web)"
docker compose -f docker-compose.prod.internet.yml up -d --build

echo "Stack is up."
