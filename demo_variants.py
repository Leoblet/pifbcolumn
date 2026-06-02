# -*- coding: utf-8 -*-
from __future__ import annotations
import json, os, random
from pathlib import Path

VAR_INDEX_FILE = Path('/tmp/vc_demo_var_idx.json')

DEMO_VARIANTS: dict[str, list[str]] = {
    'greeting': [
        'Доброе утро. Я Дворецкий. Готов помочь.',
    ],
    'weather': [
        'В Астане сейчас 22 градуса, ясно. Завтра ожидается 24, без осадков.',
    ],
    'holding': [
        'Freedom Holding Corporation — международная финансовая группа, основанная Тимуром Турловым. '
        'Компания торгуется на бирже NASDAQ под тикером FRHC. '
        'Штаб-квартира расположена в Алматы, Казахстан. '
        'Сегодня Freedom Holding присутствует более чем в пятнадцати странах, '
        'обслуживая свыше четырёх миллионов клиентов. '
        'В экосистему группы входят: Freedom Broker — один из крупнейших брокеров в СНГ; '
        'Freedom Bank — цифровой банк с полным спектром услуг; '
        'Freedom Pay — платёжная система; '
        'Freedom Media — медиаплатформа и агрегатор контента. '
        'Компания демонстрирует стабильный рост выручки год к году '
        'и занимает ведущие позиции на рынке финансовых услуг Центральной Азии.',
    ],
    'portfolio': [
        'Ваш портфель в Freedom Broker: двенадцать позиций общей стоимостью '
        'сто сорок пять тысяч долларов. '
        'Рост за день — ноль целых восемь процента. '
        'Лидер — Tesla, плюс два целых три процента.',
    ],
    'kazakh': [
        'Сәлем! Жақсымын, рахмет. Сізге қалай көмектесе аламын?',
    ],
    'grocery': [
        'Отлично, я сформировал корзину во Фридом, направил уведомление для оплаты.',
    ],
    'transfer': [
        'Хорошо, направил уведомление во Фридом Код для подтверждения.',
    ],
    'miss': [
        'Не расслышала команду. Повторите, пожалуйста.',
    ],
}


def demo_music_track_label() -> str:
    counters = _load_var_index()
    idx = max(0, counters.get("music_track", 1) - 1) % len(DEMO_MUSIC_TRACKS)
    return DEMO_MUSIC_TRACKS[idx][0]

DEMO_MUSIC_TRACKS = [
    ("Miles Davis", "Включаю джаз. Miles Davis, Blue in Green."),
    ("Comfortably Numb", "Включаю рок. David Gilmour, Comfortably Numb."),
    ("Mozart Alla turca", "Включаю классику. Моцарт, Турецкий марш."),
]

def demo_music_play_query() -> str:
    custom = os.environ.get("VC_DEMO_MUSIC_QUERY", "").strip()
    if custom:
        return custom
    counters = _load_var_index()
    idx = counters.get("music_track", 0) % len(DEMO_MUSIC_TRACKS)
    counters["music_track"] = idx + 1
    _save_var_index(counters)
    return DEMO_MUSIC_TRACKS[idx][0]

def freedom_music_variants() -> list[str]:
    custom = os.environ.get('VC_DEMO_MUSIC_REPLY', '').strip()
    if custom:
        return [custom]
    counters = _load_var_index()
    idx = max(0, counters.get("music_track", 1) - 1) % len(DEMO_MUSIC_TRACKS)
    return ["Включаю Freedom Music."]

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
