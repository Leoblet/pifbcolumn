# -*- coding: utf-8 -*-
"""Формат ответа: суть + вопрос в конце (все пути: LLM, ZeroClaw, stream)."""

from __future__ import annotations

import os
import re


def ends_with_question(text: str) -> bool:
    t = (text or '').strip()
    return bool(t) and (t.endswith('?') or t.endswith('？'))


def count_sentences(text: str) -> int:
    """Число законченных предложений."""
    t = (text or '').strip()
    if not t:
        return 0
    parts = re.findall(r'[^.!?…]+[.!?…]+', t)
    if parts:
        return len(parts)
    return 1 if t else 0


_VOICE_BLOCK = re.compile(r'\[ГОЛОС\]\s*(.*?)\s*\[/ГОЛОС\]', re.DOTALL | re.I)
_TELEGRAM_BLOCK = re.compile(r'\[TELEGRAM\]\s*(.*?)\s*\[/TELEGRAM\]', re.DOTALL | re.I)


def parse_dual_reply(text: str) -> tuple[str, str | None]:
    """
    Разбор ответа с блоками [ГОЛОС] и [TELEGRAM].
    Returns (spoken_text, telegram_text_or_none).
    """
    raw = (text or '').strip()
    if not raw:
        return '', None

    voice_m = _VOICE_BLOCK.search(raw)
    tg_m = _TELEGRAM_BLOCK.search(raw)

    if voice_m and tg_m:
        return voice_m.group(1).strip(), tg_m.group(1).strip()

    if tg_m:
        before = raw[: tg_m.start()].strip()
        before = _VOICE_BLOCK.sub('', before).strip()
        voice = before or long_reply_spoken_notice(telegram_ok=True)
        return voice, tg_m.group(1).strip()

    if voice_m:
        return voice_m.group(1).strip(), None

    return raw, None


def has_telegram_block(text: str) -> bool:
    return bool(_TELEGRAM_BLOCK.search(text or ''))


def is_long_reply(text: str, max_sentences: int | None = None) -> bool:
    if max_sentences is None:
        max_sentences = int(os.environ.get('VC_LONG_REPLY_SENTENCES', '3'))
    return count_sentences(text) > max_sentences


def long_reply_spoken_notice(telegram_ok: bool = True) -> str:
    if telegram_ok:
        return os.environ.get(
            'VC_LONG_REPLY_SPOKEN',
            'Ответ большой — сейчас скину его в Telegram.',
        ).strip()
    return os.environ.get(
        'VC_LONG_REPLY_NO_TG',
        'Ответ длинный. Настрой Telegram, чтобы получать такие ответы.',
    ).strip()


def trim_voice_reply(text: str) -> str:
    """Обрезка для TTS — короче озвучка, быстрее ответ."""
    t = (text or '').strip()
    if not t:
        return t
    max_sent = int(os.environ.get('VC_REPLY_MAX_SENTENCES', '2'))
    max_chars = int(os.environ.get('VC_REPLY_MAX_CHARS', '100'))
    parts = re.findall(r'[^.!?…]+[.!?…]+', t)
    if parts:
        t = ' '.join(s.strip() for s in parts[:max_sent])
    if len(t) > max_chars:
        cut = t[:max_chars]
        sp = cut.rfind(' ')
        t = (cut[:sp] if sp > 40 else cut).rstrip(' ,;:') + '…'
    return t.strip()


def _reply_lang(lang: str | None = None) -> str:
    if lang in ('ru', 'kk'):
        return lang
    try:
        from lang_context import get_turn_lang
        return get_turn_lang()
    except ImportError:
        return 'ru'


def closing_question_suffix(answer: str, user_text: str = '', lang: str | None = None) -> str:
    """Только хвост-вопрос для договоривания (stream TTS). Пусто если вопрос уже есть."""
    if os.environ.get('VC_SPEAK_CLOSING', '1').lower() in ('0', 'false', 'no'):
        return ''
    answer = (answer or '').strip()
    if not answer or ends_with_question(answer):
        return ''
    lang = _reply_lang(lang)
    fast = os.environ.get('VC_FAST_CLOSING', '1').lower() not in ('0', 'false', 'no')
    if not fast:
        try:
            from fast_llm import generate_closing_question
            q = generate_closing_question(answer, user_text, lang)
            if q:
                q = q.strip()
                if not q.endswith('?') and not q.endswith('？'):
                    q = q.rstrip('.!') + '?'
                return f' {q}'
        except Exception:
            pass
    if lang == 'kk':
        return ' Тағы не білгіңіз келеді?'
    return ' Чем ещё помочь?'


def ensure_closing_question(
    reply: str,
    user_text: str = '',
    lang: str | None = None,
    log_fn=None,
) -> str:
    reply = (reply or '').strip()
    if not reply or ends_with_question(reply):
        return reply
    fast = os.environ.get('VC_FAST_CLOSING', '1').lower() not in ('0', 'false', 'no')
    if not fast:
        try:
            from fast_llm import generate_closing_question
            lang = _reply_lang(lang)
            q = generate_closing_question(reply, user_text, lang)
            if q:
                q = q.strip()
                if not q.endswith('?') and not q.endswith('？'):
                    q = q.rstrip('.!') + '?'
                suffix = f' {q}'
                if log_fn:
                    log_fn(f'+ вопрос:{suffix.strip()[:70]}')
                base = reply.rstrip()
                if base.endswith(('.', '!', '…')):
                    return base + suffix
                return base + '.' + suffix
        except Exception:
            pass
    suffix = closing_question_suffix(reply, user_text, lang)
    if not suffix:
        return reply
    if log_fn:
        log_fn(f'+ вопрос:{suffix.strip()[:70]}')
    base = reply.rstrip()
    if base.endswith(('.', '!', '…')):
        return base + suffix
    return base + '.' + suffix
