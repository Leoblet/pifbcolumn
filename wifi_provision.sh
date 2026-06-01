#!/bin/bash
# Voice Column — настройка Wi-Fi при первом включении (balena wifi-connect)
set -euo pipefail
sleep 20
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:$PATH"

VC_DIR="${VC_DIR:-/home/pi/voice_column}"
ENV_FILE="${VC_ENV_FILE:-$VC_DIR/.env}"
SSID="${VC_WIFI_AP_SSID:-Kolonka-Setup}"
AP_GATEWAY="${VC_WIFI_AP_GW:-192.168.4.1}"

log() { echo "[wifi_provision] $*"; }

read_env() {
  grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true
}

has_wifi_ssid() {
  local ssid
  ssid="$(iwgetid -r 2>/dev/null || true)"
  [ -n "$ssid" ]
}

has_gateway() {
  ip route | grep -q '^default '
}

has_internet() {
  ping -c 2 -W 4 1.1.1.1 >/dev/null 2>&1 || ping -c 2 -W 4 8.8.8.8 >/dev/null 2>&1
}

write_setup_url() {
  local ip token port url
  ip="$(hostname -I | awk '{print $1}')"
  token="$(read_env VC_UI_TOKEN)"
  port="$(read_env VC_UI_PORT)"
  port="${port:-8765}"
  url="http://${ip}:${port}/"
  [ -n "$token" ] && url="${url}?token=${token}"
  mkdir -p "$VC_DIR"
  {
    echo "$url"
    if [ -n "$token" ]; then
      echo "http://kolonka.local:${port}/?token=${token}"
    else
      echo "http://kolonka.local:${port}/"
    fi
  } > "$VC_DIR/setup.url"
  log "Панель: $url"
}

# Уже в домашней сети — только сохранить URL
if has_wifi_ssid && has_gateway; then
  write_setup_url
  if has_internet; then
    log "Wi-Fi OK"
    exit 0
  fi
  log "Wi-Fi есть, интернет слабый — продолжаем работу"
  exit 0
fi

log "Wi-Fi не настроен — точка доступа «${SSID}»"

systemctl stop voice-column-wake.service 2>/dev/null || true
systemctl stop voice-column-ui.service 2>/dev/null || true

if ! command -v wifi-connect >/dev/null 2>&1; then
  log "Установка wifi-connect…"
  curl -fsSL https://github.com/balena-io/wifi-connect/raw/master/scripts/raspbian-install.sh | bash -s -- -y || true
  [ -x /usr/local/sbin/wifi-connect ] && ln -sf /usr/local/sbin/wifi-connect /usr/bin/wifi-connect 2>/dev/null || sudo ln -sf /usr/local/sbin/wifi-connect /usr/bin/wifi-connect
fi

log "Подключите телефон к Wi-Fi «${SSID}», откроется страница настройки"
log "Выберите домашний Wi-Fi → колонка перезагрузится"

wifi-connect -s "$SSID" -g "$AP_GATEWAY" -d 192.168.4.2,192.168.4.5

log "Wi-Fi сохранён — перезагрузка"
sleep 2
reboot
