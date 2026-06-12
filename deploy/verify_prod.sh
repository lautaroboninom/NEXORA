#!/usr/bin/env bash
set -euo pipefail

DOMAIN=${1:-nexora.sepid.com.ar}

echo "== Verify DNS resolves =="
getent ahosts ${DOMAIN} || (echo "DNS failed" && exit 1)

echo "== Verify 80 redirects to 443 =="
code=$(curl -s -o /dev/null -w "%{http_code}" http://${DOMAIN}/ || true)
[[ "$code" =~ ^30 ]] || { echo "Expected redirect from 80, got $code"; exit 1; }

echo "== Verify TLS valid (HTTP/2 ok) =="
curl -fsS https://${DOMAIN}/ >/dev/null

echo "== Verify frontend root =="
curl -fsS https://${DOMAIN}/ | grep -qi "<html" || { echo "Index missing"; exit 1; }

echo "== Verify /api/health =="
curl -fsS https://${DOMAIN}/api/health/ | grep -q '"ok"\s*:\s*true'

echo "== Verify security headers =="
curl -fsSI https://${DOMAIN}/ | grep -E "Strict-Transport-Security|X-Frame-Options|X-Content-Type-Options|Referrer-Policy" || {
  echo "Missing some security headers"; exit 1; }

echo "All checks green."
