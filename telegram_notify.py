# -*- coding: utf-8 -*-
"""Отправка уведомлений в Telegram без polling-бота."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(os.environ.get('VC_ENV_FILE', '/home/pi/voice_column/.env'))


def _load_env_file() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())


def _token() -> str:
    _load_env_file()
    return os.environ.get('VC_TELEGRAM_BOT_TOKEN', '').strip()


def _allowed_ids() -> list[str]:
    _load_env_file()
    raw = os.environ.get('VC_TELEGRAM_ALLOWED_IDS', '')
    return [x.strip() for x in raw.split(',') if x.strip()]


def telegram_configured() -> bool:
    return bool(_token() and _allowed_ids())


def _send(chat_id: str, text: str) -> bool:
    token = _token()
    if not token:
        return False
    body = json.dumps({'chat_id': int(chat_id), 'text': text}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=body,
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return bool(data.get('ok'))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def send_long_answer(question: str, answer: str, *, log_fn=None) -> bool:
    """Полный ответ в Telegram всем разрешённым chat_id."""
    ids = _allowed_ids()
    if not ids:
        if log_fn:
            log_fn('Telegram: нет VC_TELEGRAM_ALLOWED_IDS')
        return False
    if not _token():
        if log_fn:
            log_fn('Telegram: нет VC_TELEGRAM_BOT_TOKEN')
        return False

    q = (question or '').strip()
    a = (answer or '').strip()
    if not a:
        return False

    header = os.environ.get('VC_LONG_REPLY_TG_HEADER', '🎙 Колонка')
    parts = [header]
    if q:
        parts.append(f'Вопрос: {q}')
    parts.append('')
    parts.append(a)
    msg = '\n'.join(parts)
    if len(msg) > 4000:
        msg = '\n'.join(parts[:2]) + '\n\n' + a[:3900] + '…'

    ok_any = False
    for cid in ids:
        if _send(cid, msg):
            ok_any = True
    if log_fn:
        log_fn(f'⚡ Telegram long ({len(ids)} chat): {"ok" if ok_any else "fail"}')
    return ok_any
