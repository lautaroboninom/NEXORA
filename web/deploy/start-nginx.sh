#!/bin/sh
set -eu

CERT_PATH="${TLS_CERT_PATH:-/etc/nginx/certs/sistemadereparaciones.tail7bb880.ts.net.crt}"
KEY_PATH="${TLS_KEY_PATH:-/etc/nginx/certs/sistemadereparaciones.tail7bb880.ts.net.key}"
FALLBACK_CERT="/etc/nginx/fallback-certs/default.crt"
FALLBACK_KEY="/etc/nginx/fallback-certs/default.key"
NGINX_CONF="/etc/nginx/conf.d/default.conf"

use_fallback_cert() {
  sed \
    -e "s|$CERT_PATH|$FALLBACK_CERT|g" \
    -e "s|$KEY_PATH|$FALLBACK_KEY|g" \
    "$NGINX_CONF" > /tmp/default.conf
  mv /tmp/default.conf "$NGINX_CONF"
}

if [ ! -s "$CERT_PATH" ] || [ ! -s "$KEY_PATH" ]; then
  echo "[WARN] TLS files missing in /etc/nginx/certs. Using fallback self-signed cert."
  use_fallback_cert
fi

if ! nginx -t; then
  echo "[WARN] nginx -t failed with current cert setup. Retrying with fallback cert."
  use_fallback_cert
  nginx -t
fi

exec nginx -g "daemon off;"
