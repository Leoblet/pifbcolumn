#!/bin/bash
# Сброс Wi-Fi для передачи колонки новому владельцу
set -euo pipefail

log() { echo "[wifi_reset] $*"; }

log "Удаление сохранённых Wi-Fi…"
while IFS= read -r con; do
  [ -z "$con" ] && continue
  log "  − $con"
  nmcli connection delete "$con" 2>/dev/null || true
done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep ':wifi' | cut -d: -f1)

rm -f /home/pi/voice_column/setup.url 2>/dev/null || true
echo "0.0.0.0" > /home/pi/ip.txt 2>/dev/null || true

log "Перезагрузка → точка доступа Kolonka-Setup"
sleep 2
reboot
