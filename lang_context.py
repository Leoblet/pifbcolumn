# -*- coding: utf-8 -*-
"""Русский + казахский: определение языка, профили SpeechKit, подсказки LLM."""

from __future__ import annotations

import os
import re
import threading

KAZAKH_CHARS = re.compile(r'[әіңғүұқөһӘІҲҢҒҮҰҚӨҺ]')
KK_WORDS = re.compile(
    r'\b(?:'
    r'сәлем|салем|сәлеметсіз|салеметсиз|salametsiz|қалай|калай|qalay|маған|маган|керек|жақсы|жаксы|'
    r'рахмет|иә|иа|жоқ|жок|қай|кай|неге|бүгін|бугин|кеш|кеші|'
    r'не|қайда|кайда|қашан|кашан|болды|айт|кел|бер|ал|бар|сіз|сен|'
    r'мен|ол|біз|сіздер|олар|ма|ба|па|ме|бе|пе|ғой|гои|қазақша|казахша'
    r')\b',
    re.I,
)

# «Расскажи … на казахском» — вопрос по-русски, ответ на kk
REQUEST_REPLY_KK = re.compile(
    r'(?:'
    r'(?:на\s+)?(?:казахск(?:ом|ий|ом\s+языке?)|по-?казахски|қазақша|казахша|'
    r'қазақ\s+тіл(?:інде|де)|kazakh)'
    r'|(?:скажи|расскажи|ответь|объясни|говори|переведи|translate).{0,40}'
    r'(?:казахск|қазақша|казахша)'
    r')',
    re.I,
)
REQUEST_REPLY_RU = re.compile(
    r'(?:на\s+)?(?:русск(?:ом|ий|ом\s+языке?)|по-?русски|орысша|'
    r'орыс\s+тіл(?:інде|де))',
    re.I,
)

_turn = threading.local()
_session_lang = os.environ.get('VC_DEFAULT_LANG', 'ru').lower()


def lang_mode() -> str:
    """ru | kk | bilingual"""
    return os.environ.get('VC_LANG_MODE', 'bilingual').lower()


def default_lang() -> str:
    return os.environ.get('VC_DEFAULT_LANG', 'ru').lower()


def set_turn_lang(lang: str | None):
    if lang in ('ru', 'kk'):
        _turn.lang = lang
        global _session_lang
        _session_lang = lang


def get_turn_lang() -> str:
    lang = getattr(_turn, 'lang', None)
    if lang in ('ru', 'kk'):
        return lang
    return _session_lang if _session_lang in ('ru', 'kk') else 'ru'


def detect_lang(text: str) -> str:
    """ru или kk по тексту."""
    t = (text or '').strip()
    if not t:
        return default_lang()

    kk_chars = len(KAZAKH_CHARS.findall(t))
    if kk_chars >= 2:
        return 'kk'
    if KK_WORDS.search(t) and kk_chars >= 1:
        return 'kk'
    if kk_chars == 1:
        cyr_words = re.findall(r'[а-яёәіңғүұқөһ]+', t, re.I)
        if cyr_words and all(KAZAKH_CHARS.search(w) for w in cyr_words[:3]):
            return 'kk'

    if kk_chars == 1 and KK_WORDS.search(t):
        return 'kk'

    ratio = kk_chars / max(len(re.findall(r'[а-яёәіңғүұқөһ]', t, re.I)), 1)
    if ratio > 0.06 and KK_WORDS.search(t):
        return 'kk'

    return 'ru'


def wants_reply_in_kk(text: str) -> bool:
    return bool(REQUEST_REPLY_KK.search(text or ''))


def wants_reply_in_ru(text: str) -> bool:
    return bool(REQUEST_REPLY_RU.search(text or ''))


def resolve_reply_lang(text: str) -> str:
    """Язык ответа: учитывает «на казахском» при русском вопросе."""
    t = (text or '').strip()
    if not t:
        return default_lang()
    if wants_reply_in_kk(t):
        return 'kk'
    if wants_reply_in_ru(t):
        return 'ru'
    return detect_lang(t)


def apply_turn_language(text: str) -> str:
    """STT-текст → язык ответа; возвращает reply_lang."""
    input_lang = detect_lang(text)
    reply_lang = resolve_reply_lang(text)
    set_turn_lang(reply_lang)
    return reply_lang


def describe_lang_choice(text: str) -> str:
    input_lang = detect_lang(text)
    reply_lang = get_turn_lang()
    if wants_reply_in_kk(text) or wants_reply_in_ru(text):
        return f'Вопрос ({log_lang_label(input_lang)}) → ответ ({log_lang_label(reply_lang)})'
    return log_lang_label(reply_lang)


def stt_lang_code(lang: str) -> str:
    return 'kk-KZ' if lang == 'kk' else 'ru-RU'


def tts_profile(lang: str | None = None) -> dict:
    """Профиль TTS: голос Сауле ru/kk."""
    lang = lang or get_turn_lang()
    if lang == 'kk':
        return {
            'lang': 'kk-KZ',
            'voice': os.environ.get('VC_YANDEX_VOICE_KK', 'saule'),
            'role': os.environ.get('VC_YANDEX_ROLE_KK', 'neutral'),
        }
    return {
        'lang': 'ru-RU',
        'voice': os.environ.get('VC_YANDEX_VOICE_RU', 'saule_ru'),
        'role': os.environ.get('VC_YANDEX_ROLE_RU', 'neutral'),
    }


def _stt_score(text: str, expected: str) -> float:
    t = (text or '').strip()
    if not t:
        return 0.0
    kk = len(KAZAKH_CHARS.findall(t))
    cyr = len(re.findall(r'[а-яё]', t, re.I))
    if expected == 'kk':
        score = kk * 3.0 + (2.0 if KK_WORDS.search(t) else 0)
        score += len(t) * 0.01
        if kk == 0 and not KK_WORDS.search(t):
            score *= 0.3
        return score
    score = cyr * 0.5 + len(t) * 0.01
    score -= kk * 4.0
    return max(score, 0.0)


def pick_stt_result(ru_text: str, kk_text: str) -> tuple[str, str]:
    """Выбор лучшего STT из ru-RU и kk-KZ."""
    ru = (ru_text or '').strip()
    kk = (kk_text or '').strip()

    if ru and not kk:
        return ru, detect_lang(ru)
    if kk and not ru:
        return kk, detect_lang(kk)
    if not ru and not kk:
        return '', default_lang()

    ru_s = _stt_score(ru, 'ru')
    kk_s = _stt_score(kk, 'kk')

    if kk_s > ru_s * 1.15:
        return kk, 'kk'
    if ru_s > kk_s * 1.15:
        return ru, 'ru'

    preferred = detect_lang(kk if len(kk) >= len(ru) else ru)
    return (kk if preferred == 'kk' else ru), preferred


def llm_lang_instruction(lang: str | None = None, user_text: str = '') -> str:
    lang = lang or get_turn_lang()
    if lang == 'kk':
        extra = ''
        if user_text and wants_reply_in_kk(user_text):
            extra = (
                'Пользователь просит ответ НА КАЗАХСКОМ (кириллица). '
                'Ответь по теме вопроса полностью на казахском. '
                'НЕ отвечай по-русски. НЕ отказывай и не предлагай другой язык. '
            )
        return (
            extra
            + 'Жауапты ТЕК қазақ тілінде бер (кириллица). '
            '«Қазақша сөйлей алмаймын», «жеткілікті емес» — ДЕМЕ, ты умеешь. '
            'Формат: алдымен жауап, соңында міндетті түрде бір қысқа сұрақ. Markdown жоқ.'
        )
    return (
        'Пользователь говорит по-русски. Отвечай по-русски. '
        'Формат: ответ по сути, в конце один короткий уместный вопрос. Без markdown.'
    )


def log_lang_label(lang: str) -> str:
    return 'Қазақша' if lang == 'kk' else 'Русский'
