#!/usr/bin/env bash
set -u
set -o pipefail

RUNTIME_DIR="/mnt/e/Datos/NEXORA_Runtime"
LOG_DIR="/mnt/e/Datos/Backups/NEXORA/Logs"
HEARTBEAT_FILE="$RUNTIME_DIR/wsl-heartbeat.json"
LOG_FILE="$LOG_DIR/wsl-keepalive.log"
PORTPROXY_SCRIPT='C:\ProgramData\NEXORA\refresh-nexora-portproxy.ps1'
EXPECTED_CONTAINERS=(
  "nexora-web"
  "nexora-api"
  "nexora-postgres"
  "nexora-bejerman-sync"
  "nexora-bejerman-articulos-sync"
  "nexora-bejerman-pdf"
  "nexora-email-outbox"
  "nexora-billing-postponements"
  "nexora-billing-summary"
)

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" >>"$LOG_FILE"
}

write_heartbeat() {
  local state="$1"
  local docker_available="$2"
  local api_http="$3"
  local running_containers="$4"
  local total_containers="$5"
  local memory_total_kb memory_available_kb memory_used_kb memory_percent uptime_seconds

  memory_total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  memory_available_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
  memory_used_kb=$((memory_total_kb - memory_available_kb))
  if [ "$memory_total_kb" -gt 0 ]; then
    memory_percent=$((memory_used_kb * 100 / memory_total_kb))
  else
    memory_percent=0
  fi
  uptime_seconds="$(cut -d. -f1 /proc/uptime)"

  cat >"${HEARTBEAT_FILE}.tmp" <<EOF
{"timestamp":"$(date --iso-8601=seconds)","state":"$state","dockerAvailable":$docker_available,"apiHttp":$api_http,"runningContainers":$running_containers,"totalContainers":$total_containers,"wslMemoryPercent":$memory_percent,"wslMemoryUsedKb":$memory_used_kb,"wslMemoryTotalKb":$memory_total_kb,"wslUptimeSeconds":$uptime_seconds,"bootId":"$(cat /proc/sys/kernel/random/boot_id)"}
EOF
  mv -f "${HEARTBEAT_FILE}.tmp" "$HEARTBEAT_FILE"
}

apply_container_limit() {
  local container="$1"
  local memory="$2"
  local cpu_shares="$3"
  local current_memory current_swap current_cpu_shares desired_memory memory_value memory_unit

  if ! timeout 8s docker inspect "$container" >/dev/null 2>&1; then
    return
  fi

  current_memory="$(timeout 8s docker inspect --format '{{.HostConfig.Memory}}' "$container" 2>/dev/null || true)"
  current_swap="$(timeout 8s docker inspect --format '{{.HostConfig.MemorySwap}}' "$container" 2>/dev/null || true)"
  current_cpu_shares="$(timeout 8s docker inspect --format '{{.HostConfig.CpuShares}}' "$container" 2>/dev/null || true)"
  memory_value="${memory%[gGmM]}"
  memory_unit="${memory: -1}"
  case "$memory_unit" in
    g|G) desired_memory=$((memory_value * 1024 * 1024 * 1024)) ;;
    m|M) desired_memory=$((memory_value * 1024 * 1024)) ;;
    *) desired_memory="$memory_value" ;;
  esac

  if [ "$current_memory" != "$desired_memory" ] ||
    [ "$current_swap" != "$desired_memory" ] ||
    [ "$current_cpu_shares" != "$cpu_shares" ]; then
    if timeout 20s docker update \
      --memory "$memory" \
      --memory-swap "$memory" \
      --cpu-shares "$cpu_shares" \
      "$container" >/dev/null 2>&1; then
      log "Límite aplicado a $container: memoria=$memory cpu_shares=$cpu_shares"
    else
      log "No se pudo aplicar el límite de recursos a $container."
    fi
  fi
}

refresh_portproxy() {
  local wsl_ip
  wsl_ip="$(hostname -I 2>/dev/null || true)"
  wsl_ip="${wsl_ip%% *}"
  if [ -z "$wsl_ip" ]; then
    log "No se pudo determinar la IPv4 de WSL para actualizar portproxy."
    return
  fi

  /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe \
    -NoProfile \
    -NonInteractive \
    -ExecutionPolicy Bypass \
    -File "$PORTPROXY_SCRIPT" \
    -ConnectAddress "$wsl_ip" \
    >"$LOG_DIR/portproxy.log" 2>&1 || log "Falló la actualización de portproxy."
}

total_containers="${#EXPECTED_CONTAINERS[@]}"
log "Iniciando keepalive reforzado de WSL/Docker."
write_heartbeat "starting" false 0 0 "$total_containers"

for _ in $(seq 1 90); do
  if systemctl is-system-running >/dev/null 2>&1; then
    break
  fi
  write_heartbeat "starting" false 0 0 "$total_containers"
  sleep 1
done

refresh_portproxy
last_resource_update=0

while true; do
  docker_available=false
  api_http=0
  running_containers=0
  state="degraded"

  if ! systemctl is-active --quiet docker; then
    log "Docker no está activo; se solicita su inicio."
    timeout 120s systemctl start docker >>"$LOG_FILE" 2>&1 || true
  fi

  if timeout 12s docker info >/dev/null 2>&1; then
    docker_available=true
    for container in "${EXPECTED_CONTAINERS[@]}"; do
      container_state="$(
        timeout 8s docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true
      )"
      if [ "$container_state" = "running" ]; then
        running_containers=$((running_containers + 1))
      fi
    done
    api_http="$(
      timeout 10s curl -k -sS --max-time 6 -o /dev/null -w '%{http_code}' \
        --resolve nexora.tail7bb880.ts.net:443:127.0.0.1 \
        https://nexora.tail7bb880.ts.net/api/health/ 2>/dev/null || true
    )"
    if [[ ! "$api_http" =~ ^[0-9]{3}$ ]]; then
      api_http=0
    else
      api_http=$((10#$api_http))
    fi

    now_epoch="$(date +%s)"
    if [ $((now_epoch - last_resource_update)) -ge 300 ]; then
      # NEXORA tiene prioridad. Los servicios auxiliares conservan recursos
      # suficientes, pero no pueden agotar la memoria completa de WSL.
      apply_container_limit "nextcloud-aio-nextcloud" "4g" "256"
      apply_container_limit "nextcloud-aio-database" "2g" "256"
      apply_container_limit "nextcloud-aio-apache" "1g" "192"
      apply_container_limit "nextcloud-aio-mastercontainer" "1g" "128"
      apply_container_limit "nextcloud-aio-borgbackup" "2g" "128"
      apply_container_limit "nextcloud-aio-redis" "512m" "128"
      apply_container_limit "nextcloud-aio-notify-push" "512m" "128"
      apply_container_limit "nube-tailscale" "512m" "128"
      last_resource_update="$now_epoch"
    fi

    if [ "$api_http" = "200" ] && [ "$running_containers" -eq "$total_containers" ]; then
      state="healthy"
    fi
  else
    log "Docker todavía no responde; se reintentará."
  fi

  write_heartbeat "$state" "$docker_available" "$api_http" "$running_containers" "$total_containers"
  sleep 20
done
