#!/bin/bash
# wm8960: маршрут Speaker + ReSpeaker mic (эталон VBot + voice_column)
set -euo pipefail

CARD="${1:-0}"
ENV_FILE="${VC_ENV_FILE:-/home/pi/voice_column/.env}"

_read_env() {
  local key="$1" default="$2" val=""
  if [[ -f "$ENV_FILE" ]]; then
    val=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r')
  fi
  echo "${val:-$default}"
}

_amix() {
  amixer -c "$CARD" sset "$1" "$2" 2>/dev/null || true
}

_amix_off() {
  amixer -c "$CARD" sset "$1" off 2>/dev/null || true
}

_amix_on() {
  amixer -c "$CARD" sset "$1" on 2>/dev/null || true
}

PCT=$(_read_env VC_VOLUME_PERCENT 92)
SPEAKER=$(_read_env VC_SPEAKER_VOL $((127 * PCT / 100)))
PLAYBACK=$(_read_env VC_PLAYBACK_VOL $((255 * PCT / 100)))
HEADPHONE=$(_read_env VC_HEADPHONE_VOL "$SPEAKER")
CAPTURE=$(_read_env VC_CAPTURE_GAIN 39)
MIC_BOOST=$(_read_env VC_MIC_BOOST 3)
ADC_PCM=$(_read_env VC_ADC_PCM_GAIN 195)

# --- Воспроизведение → физический Speaker ---
_amix_on 'Left Output Mixer PCM'
_amix_on 'Right Output Mixer PCM'
_amix_on 'Mono Output Mixer Left'
_amix_on 'Mono Output Mixer Right'
_amix_off 'Left Output Mixer Boost Bypass'
_amix_off 'Right Output Mixer Boost Bypass'
_amix_off 'Left Output Mixer LINPUT3'
_amix_off 'Right Output Mixer RINPUT3'

_amix 'Speaker' "$SPEAKER"
_amix 'Headphone' "$HEADPHONE"
_amix 'Playback' "$PLAYBACK"
_amix_off 'PCM Playback -6dB'
_amix 'Speaker AC' 5
_amix 'Speaker DC' 5
_amix 'DAC Mono Mix' Mono

# --- Микрофоны ReSpeaker (LINPUT1 / RINPUT1) ---
_amix_on 'Left Boost Mixer LINPUT1'
_amix_on 'Right Boost Mixer RINPUT1'
_amix_off 'Left Boost Mixer LINPUT2'
_amix_off 'Left Boost Mixer LINPUT3'
_amix_off 'Right Boost Mixer RINPUT2'
_amix_off 'Right Boost Mixer RINPUT3'
_amix_on 'Left Input Mixer Boost'
_amix_on 'Right Input Mixer Boost'
_amix 'Left Input Boost Mixer LINPUT1' "$MIC_BOOST"
_amix 'Right Input Boost Mixer RINPUT1' "$MIC_BOOST"

_amix_on 'Capture'
_amix 'Capture' "$CAPTURE"
_amix 'ADC PCM' "$ADC_PCM"

# --- Отключить «улучшайзеры», ломающие STT ---
_amix 'ALC Function' Off
_amix_off '3D Switch'
_amix_off 'Noise Gate Switch'
_amix_off 'ADC High Pass Filter Switch'

# alsactl store — только при явном store (загрузка), не во время TTS
if [[ "${2:-}" == "store" && "${VC_MIXER_NOSTORE:-}" != "1" ]]; then
  if [[ -n "${SUDO_PASS:-}" ]]; then
    echo "$SUDO_PASS" | sudo -S alsactl store 2>/dev/null || true
  else
    sudo alsactl store 2>/dev/null || true
  fi
fi
