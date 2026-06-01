# -*- coding: utf-8 -*-
"""Мост для Telegram / веб — общая логика команд колонки."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

VC_DIR = Path(os.environ.get('VC_DIR', '/home/pi/voice_column'))
_BRIDGE_LOCK = threading.Lock()


def _log(msg: str) -> None:
    print(f'[column_bridge] {msg}', flush=True)


def _ensure_vc_path() -> None:
    p = str(VC_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    os.chdir(p)


def process_message(text: str, *, source: str = 'telegram') -> tuple[bool, str]:
    """
    Текст → колонка (TTS + LLM / музыка / громкость).
    Returns (ok, reply_for_user).
    """
    text = (text or '').strip()
    if not text:
        return False, ''

    _ensure_vc_path()
    from column_api import (
        _ensure_wake_running,
        _service_active,
        _set_web_busy,
        _systemctl,
        _try_fast_command,
    )

    handled, msg, action = _try_fast_command(text)
    if handled:
        _log(f'{source} fast {action}: {msg[:60]}')
        return True, msg

    speak = os.environ.get('VC_TELEGRAM_SPEAK', '1').lower() not in ('0', 'false', 'no')
    if source != 'telegram':
        speak = os.environ.get('VC_REMOTE_SPEAK', '1').lower() not in ('0', 'false', 'no')

    with _BRIDGE_LOCK:
        _set_web_busy(True)
        was_wake = _service_active()
        if was_wake:
            _systemctl('stop')
            time.sleep(0.8)
        try:
            import voice_column
            voice_column.handle_command(
                text, speak=speak, long_reply_telegram=False, prompt_mode='chat',
            )
            reply = voice_column.get_last_command_reply()
            _log(f'{source} ok: {(reply or "")[:80]}')
            return bool(reply), reply or 'Готово.'
        except Exception as exc:
            _log(f'{source} error: {exc}')
            return False, f'Ошибка: {exc}'
        finally:
            if was_wake:
                _ensure_wake_running()
            _set_web_busy(False)
