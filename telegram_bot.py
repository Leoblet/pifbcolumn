#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот для голосовой колонки (long polling, без webhook)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VC_DIR = Path(os.environ.get('VC_DIR', '/home/pi/voice_column'))
ENV_FILE = Path(os.environ.get('VC_ENV_FILE', VC_DIR / '.env'))


def _load_env() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
if str(VC_DIR) not in sys.path:
    sys.path.insert(0, str(VC_DIR))

from column_bridge import process_message  # noqa: E402
from column_api import _apply_volume, _status  # noqa: E402

TOKEN = os.environ.get('VC_TELEGRAM_BOT_TOKEN', '').strip()
ALLOWED = {
    x.strip()
    for x in os.environ.get('VC_TELEGRAM_ALLOWED_IDS', '').split(',')
    if x.strip()
}
POLL_TIMEOUT = int(os.environ.get('VC_TELEGRAM_POLL_TIMEOUT', '25'))
API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''


def log(msg: str) -> None:
    print(f'[telegram] {msg}', flush=True)


def _api_post(method: str, data: dict) -> dict:
    url = f'{API}/{method}'
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 15) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'Telegram HTTP {exc.code}: {err}') from exc
    if not payload.get('ok'):
        raise RuntimeError(payload.get('description') or 'Telegram API error')
    return payload


def _api_get(path: str) -> dict:
    url = f'{API}/{path}'
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 15) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'Telegram HTTP {exc.code}: {err}') from exc
    if not payload.get('ok'):
        raise RuntimeError(payload.get('description') or 'Telegram API error')
    return payload


def _allowed(chat_id: int) -> bool:
    if not ALLOWED:
        return False
    return str(chat_id) in ALLOWED


def send_message(chat_id: int, text: str, *, reply_to: int | None = None) -> None:
    text = (text or '').strip()
    if not text:
        return
    if len(text) > 4000:
        text = text[:3990] + '…'
    data: dict = {'chat_id': chat_id, 'text': text}
    if reply_to:
        data['reply_to_message_id'] = reply_to
    _api_post('sendMessage', data)


def send_typing(chat_id: int) -> None:
    try:
        _api_post('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
    except Exception:
        pass


def _help_text(chat_id: int) -> str:
    return (
        'Колонка «Айдана» — Telegram-пульт.\n\n'
        'Пиши текстом — ответ озвучится на колонке и придёт сюда.\n\n'
        'Команды:\n'
        '/status — состояние\n'
        '/volume 80 — громкость 0–100\n'
        '/stop — стоп музыка\n'
        '/music джаз — включить музыку\n\n'
        'Примеры:\n'
        '• какая погода в Алматы\n'
        '• включи Kishlak\n'
        '• громче\n\n'
        f'Твой chat_id: `{chat_id}`\n'
        '(добавь в .env: VC_TELEGRAM_ALLOWED_IDS)'
    )


def _format_status() -> str:
    s = _status()
    vol = s.get('volume', '?')
    svc = s.get('service', '?')
    music = 'играет' if s.get('music') else 'нет'
    setup = s.get('setup') or {}
    ip = setup.get('ip') or '—'
    ssid = setup.get('ssid') or '—'
    return (
        f'Сервис wake: {svc}\n'
        f'Музыка: {music}\n'
        f'Громкость: {vol}%\n'
        f'Wi-Fi: {ssid}\n'
        f'IP: {ip}'
    )


def _handle_command(chat_id: int, text: str, msg_id: int | None) -> None:
    low = text.strip().lower()
    if low in ('/start', '/help'):
        send_message(chat_id, _help_text(chat_id), reply_to=msg_id)
        return
    if low == '/status':
        send_message(chat_id, _format_status(), reply_to=msg_id)
        return
    if low.startswith('/volume'):
        m = re.search(r'(\d{1,3})', text)
        if not m:
            send_message(chat_id, 'Пример: /volume 85', reply_to=msg_id)
            return
        pct = max(0, min(100, int(m.group(1))))
        _apply_volume(pct)
        send_message(chat_id, f'Громкость {pct}%', reply_to=msg_id)
        return
    if low in ('/stop', '/stopmusic', '/стоп'):
        ok, reply = process_message('выключи музыку', source='telegram')
        send_message(chat_id, reply or ('Ок' if ok else 'Не вышло'), reply_to=msg_id)
        return
    if low.startswith('/music'):
        query = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
        if not query:
            send_message(chat_id, 'Пример: /music джаз', reply_to=msg_id)
            return
        ok, reply = process_message(f'включи {query}', source='telegram')
        send_message(chat_id, reply or ('Ищу…' if ok else 'Не вышло'), reply_to=msg_id)
        return

    send_typing(chat_id)
    ok, reply = process_message(text, source='telegram')
    if not ok and not reply:
        send_message(chat_id, 'Не смогла ответить. Попробуй ещё раз.', reply_to=msg_id)
        return
    send_message(chat_id, reply or 'Готово.', reply_to=msg_id)


def _handle_update(update: dict) -> None:
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    if chat_id is None:
        return
    user = msg.get('from') or {}
    text = msg.get('text') or msg.get('caption') or ''
    if not text:
        return

    if not _allowed(chat_id):
        log(f'deny chat_id={chat_id} user={user.get("username") or user.get("id")}')
        send_message(
            chat_id,
            f'Нет доступа. Твой chat_id: `{chat_id}`\n'
            'Добавь в /home/pi/voice_column/.env:\n'
            f'VC_TELEGRAM_ALLOWED_IDS={chat_id}',
            reply_to=msg.get('message_id'),
        )
        return

    log(f'← {chat_id}: {text[:80]}')
    try:
        _handle_command(chat_id, text, msg.get('message_id'))
    except Exception as exc:
        log(f'handle: {exc}')
        send_message(chat_id, f'Ошибка: {exc}', reply_to=msg.get('message_id'))


def run_polling() -> None:
    if not TOKEN:
        log('Нужен VC_TELEGRAM_BOT_TOKEN в .env')
        sys.exit(1)
    log('polling… (Ctrl+C для выхода)')
    if ALLOWED:
        log(f'allowed ids: {", ".join(sorted(ALLOWED))}')
    else:
        log('VC_TELEGRAM_ALLOWED_IDS пуст — первый пишущий получит свой chat_id')

    offset = 0
    while True:
        try:
            qs = urllib.parse.urlencode({'timeout': POLL_TIMEOUT, 'offset': offset})
            payload = _api_get(f'getUpdates?{qs}')
        except Exception as exc:
            log(f'poll: {exc}')
            time.sleep(5)
            continue
        for upd in payload.get('result') or []:
            offset = max(offset, int(upd.get('update_id', 0)) + 1)
            try:
                _handle_update(upd)
            except Exception as exc:
                log(f'update: {exc}')


def main() -> None:
    run_polling()


if __name__ == '__main__':
    main()
