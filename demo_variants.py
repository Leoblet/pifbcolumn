# -*- coding: utf-8 -*-
"""Варианты заготовленных ответов демо (TTS-кэш по каждому)."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

VAR_INDEX_FILE = Path('/tmp/vc_demo_var_idx.json')

# scenario → список фраз (озвучиваются из кэша demo_{scenario}_{i}.wav)
DEMO_VARIANTS: dict[str, list[str]] = {
    'greeting': [
        'Здравствуйте, Тимур Исланович. Я Дворецкий. Готов помочь.',
        'Доброе утро, Тимур Исланович. Дворецкий на связи.',
        'Здравствуйте. Я Дворецкий, голосовой помощник Freedom Station.',
    ],
    'weather': [
        'В Алматы сейчас 22 градуса, ясно. Завтра ожидается 24, без осадков.',
        'Сейчас в Алматы плюс 22, солнечно. Завтра около 24, дождя не будет.',
        'В Алматы 22 градуса и ясно. На завтра прогноз — 24, без осадков.',
    ],
    'holding': [
        'Freedom Holding Corporation — международная финансовая группа, основанная Тимуром Турловым. '
        'Компания торгуется на NASDAQ под тикером FRHC. '
        'Штаб-квартира в Алматы, ключевые рынки — Казахстан, Центральная Азия, США и Европа. '
        'В экосистему входят Freedom Broker, Freedom Bank, Freedom Pay и Freedom Media — '
        'единый финансовый суперапп для инвестиций, платежей и медиа.',
        'Freedom Holding — финансовая группа под тикером FRHC на NASDAQ, основана Тимуром Турловым. '
        'Штаб-квартира в Алматы. Экосистема: Freedom Broker, Freedom Bank, Freedom Pay и Freedom Media.',
    ],
    'portfolio': [
        'Ваш портфель в Freedom Broker: 12 позиций общей стоимостью 145 тысяч долларов, '
        'рост за день ноль целых восемь десятых процента, лидер — Tesla, плюс 2 целых три десятых процента.',
        'В Freedom Broker у вас 12 позиций на 145 тысяч долларов. За день плюс 0,8 процента, '
        'лучшая бумага — Tesla, плюс 2,3 процента.',
        'Портфель Freedom Broker: двенадцать позиций, сто сорок пять тысяч долларов, '
        'рост за день 0,8 процента. Лидирует Tesla.',
    ],
    'kazakh': [
        'Здравствуйте! Я понимаю казахский.',
        'Сәлеметсіз бе! Русский и казахский — без проблем.',
        'Здравствуйте. Можете говорить по-русски или по-казахски.',
    ],
    'miss': [
        'Не поняла команду демо. Повторите, пожалуйста.',
        'Не расслышала. Скажите команду ещё раз, пожалуйста.',
    ],
}


def demo_music_track_label() -> str:
    return os.environ.get('VC_DEMO_MUSIC_TRACK', 'Кино — Группа крови').strip()


def demo_music_play_query() -> str:
    return os.environ.get('VC_DEMO_MUSIC_QUERY', 'кино группа крови').strip()


def freedom_music_variants() -> list[str]:
    custom = os.environ.get('VC_DEMO_MUSIC_REPLY', '').strip()
    if custom:
        return [custom]
    return [
        'Включаю фридом мьюзик твой любимый плейлист.',
        'Включаю фридом мьюзик, твой любимый плейлист.',
        'Хорошо, включаю фридом мьюзик — твой любимый плейлист.',
    ]


def _scenario_variants(scenario: str) -> list[str]:
    if scenario == 'freedom_music':
        return freedom_music_variants()
    return DEMO_VARIANTS.get(scenario) or []


def reset_variant_rotation(*, log_fn=None) -> None:
    _save_var_index({})
    if log_fn:
        log_fn('[demo] варианты: счётчик сброшен')


def _load_var_index() -> dict[str, int]:
    if not VAR_INDEX_FILE.is_file():
        return {}
    try:
        data = json.loads(VAR_INDEX_FILE.read_text(encoding='utf-8'))
        return {k: int(v) for k, v in data.items() if isinstance(k, str)}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_var_index(data: dict[str, int]) -> None:
    try:
        VAR_INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def pick_variant(scenario: str, variants: list[str] | None = None) -> tuple[str, str]:
    """
    Выбор варианта ответа.
    Returns (tts_cache_key, reply_text).
    VC_DEMO_VARIANT=0|1|2 — фиксированный; rotate (default); random.
    """
    items = variants or _scenario_variants(scenario)
    if not items:
        return f'demo_{scenario}_0', ''

    mode = os.environ.get('VC_DEMO_VARIANT', 'rotate').lower()
    if mode.isdigit():
        idx = int(mode) % len(items)
    elif mode == 'random':
        idx = random.randint(0, len(items) - 1)
    else:
        counters = _load_var_index()
        idx = counters.get(scenario, 0) % len(items)
        counters[scenario] = idx + 1
        _save_var_index(counters)

    return f'demo_{scenario}_{idx}', items[idx]


def all_phrase_keys() -> dict[str, str]:
    """Все пары key→text для CACHED_PHRASES / прогрева TTS."""
    out: dict[str, str] = {}
    for scenario in list(DEMO_VARIANTS.keys()) + ['freedom_music']:
        texts = _scenario_variants(scenario)
        for i, text in enumerate(texts):
            out[f'demo_{scenario}_{i}'] = text
    return out


def warm_demo_cache(log_fn=None) -> None:
    if os.environ.get('VC_CMD_CACHE', '1').lower() in ('0', 'false', 'no'):
        return
    register_cached_phrases()
    try:
        from command_fast import warm_command_cache
        warm_command_cache(log_fn)
    except ImportError:
        pass
    if log_fn:
        n = len(all_phrase_keys())
        log_fn(f'[demo] прогрев {n} вариантов TTS')


def register_cached_phrases() -> None:
    try:
        from command_fast import CACHED_PHRASES
        CACHED_PHRASES.update(all_phrase_keys())
    except ImportError:
        pass
