# -*- coding: utf-8 -*-
"""Демо-сценарий Freedom Station / Дворецкий для презентации."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from demo_variants import (
    all_phrase_keys,
    pick_variant,
    register_cached_phrases,
    reset_variant_rotation,
    warm_demo_cache,
)

DEMO_STATE = Path('/tmp/vc_demo.json')
ENV_FILE = Path(os.environ.get('VC_ENV_FILE', '/home/pi/voice_column/.env'))

PORTFOLIO_JSON = {
    'broker': 'Freedom Broker',
    'positions': 12,
    'total_usd': 145_000,
    'day_change_pct': 0.8,
    'leader': {'symbol': 'TSLA', 'name': 'Tesla', 'change_pct': 2.3},
}


def _read_demo_env() -> str:
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('VC_DEMO_MODE='):
                return line.split('=', 1)[1].strip()
    return os.environ.get('VC_DEMO_MODE', '0')


def enabled() -> bool:
    val = _read_demo_env()
    return val.lower() not in ('0', 'false', 'no', '')


def set_demo_mode(on: bool) -> bool:
    """Вкл/выкл демо — сразу, без перезапуска wake."""
    val = '1' if on else '0'
    lines = ENV_FILE.read_text(encoding='utf-8').splitlines() if ENV_FILE.is_file() else []
    out, seen = [], False
    for line in lines:
        if line.strip().startswith('VC_DEMO_MODE='):
            out.append(f'VC_DEMO_MODE={val}')
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f'VC_DEMO_MODE={val}')
    ENV_FILE.write_text('\n'.join(out) + '\n', encoding='utf-8')
    os.environ['VC_DEMO_MODE'] = val
    if on:
        reset_variant_rotation()
        warm_demo_cache()
    else:
        _save_state({'pipeline': [], 'updated': time.time(), 'enabled': False})
    return True


def _norm(text: str) -> str:
    t = (text or '').lower().strip()
    for w in (
        'дворецкий', 'дворецкого', 'айдана', 'айдан', 'ok google', 'ок google',
        'ок колонка', 'эй колонка', 'слушай', 'hey', 'алло колонка', 'фридом',
    ):
        t = re.sub(rf'\b{re.escape(w)}\b', ' ', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip(' .,!?;:')


def _load_state() -> dict:
    if not DEMO_STATE.is_file():
        return {'pipeline': [], 'updated': 0}
    try:
        return json.loads(DEMO_STATE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'pipeline': [], 'updated': 0}


def _save_state(state: dict) -> None:
    state['updated'] = time.time()
    try:
        DEMO_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass


_STAGE_ICONS = {
    'WAKE': '┌─', 'LISTEN': '├─', 'RECORD': '├─',
    'STT': '├─', 'LLM': '├─', 'API': '├─', 'TTS': '└─',
}

def demo_stage(stage: str, detail: str = '', *, log_fn=None) -> None:
    """Структурированный лог для экрана: WAKE / LISTEN / STT / LLM / TTS / API."""
    stage = (stage or '').upper()
    detail = (detail or '').strip()
    now = time.time()
    state = _load_state()
    pipe = state.setdefault('pipeline', [])

    # Вычисляем elapsed от предыдущего шага
    elapsed = ''
    if pipe and stage != 'WAKE':
        dt = now - pipe[-1]['t']
        elapsed = f' +{dt:.2f}s'
    elif stage == 'WAKE':
        elapsed = ''

    icon = _STAGE_ICONS.get(stage, '├─')
    if log_fn:
        log_fn(f'[demo] {icon} {stage:<7}{elapsed}  {detail[:120]}')

    entry = {'t': now, 'stage': stage, 'detail': detail[:500]}
    if elapsed:
        entry['dt'] = round(now - pipe[-1]['t'], 3) if pipe else 0
    pipe.append(entry)
    state['pipeline'] = pipe[-24:]
    state['last_stage'] = stage
    state['last_detail'] = detail[:500]

    # Итоговое время для TTS (последний шаг) — от последнего LISTEN
    if stage == 'TTS' and len(pipe) >= 2:
        start_entry = next(
            (e for e in reversed(pipe) if e['stage'] == 'LISTEN'), pipe[0]
        )
        total = now - start_entry['t']
        if log_fn:
            log_fn(f'[demo]          итого {total:.2f}s')

    _save_state(state)


def demo_reset_pipeline(*, log_fn=None) -> None:
    _save_state({'pipeline': [], 'updated': time.time()})
    if log_fn:
        log_fn('[demo] pipeline reset')


def demo_wake_start(heard: str = '', *, log_fn=None) -> None:
    """Новая фраза — чистый pipeline на экране."""
    demo_reset_pipeline(log_fn=log_fn)
    demo_stage('WAKE', f'detected: {(heard or "дворецкий")[:80]}', log_fn=log_fn)


def _squash(text: str) -> str:
    return re.sub(r'[\s\-_«»"\'`]', '', (text or '').lower())


def _match_weather(n: str, raw: str) -> bool:
    if re.search(r'погод|температур|градус|тепло|холодно|осадк|дожд|снег|ясно|облачн|климат|прогноз', n, re.I):
        return True
    if re.search(r'weather|forecast', n, re.I):
        return True
    return False
def _match_my_playlist(n: str, raw: str) -> bool:
    """«Включи мой плейлист» + типичный мусор STT."""
    blob = _squash(n) + _squash(raw)
    if re.search(r'плейлист|playlist|pleylist|плейлис', blob, re.I):
        return True
    if re.search(r'мой|мо[йи]|моя', n, re.I) and re.search(
        r'плей|play|лист|list|музык|music|мьюз|юзик|трек|песн', n, re.I,
    ):
        return True
    if re.search(r'включ|ключ|запуст|постав|вруби|давай|хочу', n, re.I) and re.search(
        r'плей|play|лист|list|музык|music|песн|трек', n, re.I,
    ):
        return True
    if re.search(r'музык|music', n, re.I) and len(n.split()) <= 4:
        # не матчить команды остановки
        if re.search(r'выключ|стоп|останов|хватит|тихо', n, re.I):
            return False
        return True
    return False


def _match_freedom_music(n: str, raw: str) -> bool:
    blob = _squash(n) + _squash(raw)
    if re.search(r'freedomusic|freedommusic|freedommu', blob, re.I):
        return True
    if re.search(r'freedom|фриdom|фридom|фридом', n, re.I) and re.search(
        r'music|музык|юзик|мьюз|muzic', n, re.I,
    ):
        return True
    if re.search(r'включ|ключ', n) and re.search(r'freedom|фри|юзик|music|музык', n, re.I):
        return True
    return False


def _match_portfolio(n: str, raw: str) -> bool:
    if re.search(r'портфел|portfolio|портф', n, re.I):
        return True
    if re.search(r'покаж|открой|посмотр|узна|скажи', n, re.I) and re.search(r'портфел|broker|брокер|акци|позици|баланс|счёт|счет|инвест', n, re.I):
        return True
    if re.search(r'моих|мои|мой', n, re.I) and re.search(r'акци|позици|бумаг|вложени|инвест', n, re.I):
        return True
    if re.search(r'broker|брокер', n, re.I) and re.search(r'покаж|что|как|сколько', n, re.I):
        return True
    return False


def _match_kazakh(n: str, raw: str) -> bool:
    """Сәлеметсіз бе + типичный мусор STT (салимекс избе и т.п.)."""
    if re.search(r'с[әa]леметс[іi]з|салеметсиз|salemetsiz|сalemetsiz', raw, re.I):
        return True
    if re.search(r'с[әa]лем', n) and len(n.split()) <= 5:
        return True

    blob = _squash(n) + _squash(raw)
    # «салимекс избе», «salemetsiz be», «салам aleikum»…
    if re.search(
        r'salim|salam|salem|selim|салим|салем|'
        r'salimex|салимекс|salemets|салемет',
        blob, re.I,
    ):
        return True
    if re.search(r'избе|izbe|sizbe|сізбе|избe', blob, re.I):
        return True
    if re.search(r'салим|salim', blob, re.I) and re.search(r'изб|izb|sizb', blob, re.I):
        return True
    if re.search(r'alem', blob, re.I) and re.search(r'мет|met|mets', blob, re.I):
        return True
    # STT: «сольется сбер» ← «сәлеметсіз бе»
    if re.search(r'сольет|солит|soliet|solits|соль', blob, re.I) and re.search(
        r'сбер|sber|изб|izb|ibe', blob, re.I,
    ):
        return True
    # запасная фраза для ведущего
    if re.search(r'казахск|kazakh|на kk', n, re.I) and len(n.split()) <= 8:
        return True
    return False


def demo_miss_reply(text: str) -> tuple[str, str]:
    return pick_variant('miss')


def get_demo_state() -> dict:
    state = _load_state()
    state['enabled'] = enabled()
    from demo_variants import DEMO_VARIANTS, _scenario_variants
    state['variant_counts'] = {
        **{k: len(v) for k, v in DEMO_VARIANTS.items()},
        'freedom_music': len(_scenario_variants('freedom_music')),
    }
    return state


@dataclass
class DemoHit:
    key: str
    reply: str
    screen: str = ''
    stop_music: bool = False
    start_music: str | None = None
    api_json: dict | None = None
    tts_key: str | None = None


def _hit(scenario: str, screen: str, *, key: str | None = None, **extra) -> DemoHit:
    tts_key, reply = pick_variant(scenario)
    return DemoHit(
        key=key or scenario,
        reply=reply,
        screen=screen,
        tts_key=tts_key,
        **extra,
    )


def demo_music_play_enabled() -> bool:
    """Реальное воспроизведение в демо — по умолчанию выкл (только TTS)."""
    return os.environ.get('VC_DEMO_MUSIC_PLAY', '0').lower() in ('1', 'true', 'yes')


def _weather_variants() -> list[str] | None:
    custom = os.environ.get('VC_DEMO_WEATHER_TEXT', '').strip()
    if custom:
        return [custom]
    if os.environ.get('VC_DEMO_WEATHER_LIVE', '0').lower() in ('1', 'true', 'yes'):
        try:
            from demo_weather import fetch_almaty_weather
            live = fetch_almaty_weather()
            if live:
                return [live]
        except ImportError:
            pass
    return None



# Fuzzy fallback — эталонные фразы для каждого интента
_FUZZY_INTENTS = [
    ('greeting',  ['доброе утро', 'добрый день', 'здравствуй', 'привет']),
    ('weather',   ['какая погода', 'какая сегодня погода', 'погода в астане', 'температура на улице']),
    ('freedom_music', ['включи мой плейлист', 'включи музыку', 'поставь музыку', 'запусти плейлист']),
    ('holding',   ['расскажи про фридом холдинг', 'что такое фридом холдинг', 'расскажи о компании']),
    ('portfolio', ['покажи мой портфель', 'мой портфель', 'покажи акции', 'состояние портфеля']),
    ('kazakh',    ['салем калайсын', 'салеметсиз бе', 'сәлем']),
    ('grocery',   ['хочу приготовить борщ', 'закажи продукты', 'купи продукты', 'заказать еду']),
    ('transfer',  ['отправь маме 50 тысяч тенге', 'переведи маме деньги', 'перевод тенге']),
]

def _fuzzy_score(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()

def _fuzzy_intent(text: str, threshold: float = 0.55) -> str | None:
    n = _norm(text)
    best_score, best_intent = 0.0, None
    for intent, examples in _FUZZY_INTENTS:
        for ex in examples:
            score = _fuzzy_score(n, ex)
            if score > best_score:
                best_score, best_intent = score, intent
    if best_score >= threshold:
        return best_intent
    return None


def _match_grocery(n: str, raw: str) -> bool:
    if re.search(r'продукт|магазин|корзин|заказ[аи]|купи[тьл]|покуп', n, re.I):
        return True
    if re.search(r'приготов|сварить|сделать|хочу', n, re.I) and re.search(
        r'борщ|суп|обед|ужин|завтрак|еду|еда|блюд', n, re.I,
    ):
        return True
    if re.search(r'закажи|добавь', n, re.I) and re.search(r'продукт|еду|еда|товар', n, re.I):
        return True
    return False


def _match_transfer(n: str, raw: str) -> bool:
    if re.search(r'отправ|перевед|переведи|перевод|перечисл', n, re.I) and re.search(
        r'тенге|тысяч|руб|сом|денег|деньги|тыс', n, re.I,
    ):
        return True
    if re.search(r'отправ|перевед|переведи', n, re.I) and re.search(
        r'маме|папе|другу|жене|мужу|сестр|брат|родител', n, re.I,
    ):
        return True
    if re.search(r'\d+\s*(тыс|тысяч|тенге)', n, re.I) and re.search(
        r'отправ|перевед|перечисл', n, re.I,
    ):
        return True
    return False


def try_demo(text: str, *, log_fn=None) -> DemoHit | None:
    if not enabled():
        return None
    raw = (text or '').strip()
    n = _norm(raw)
    if not n:
        return None

    if re.search(r'(?:стоп|останов)', n) and re.search(
        r'freedom\s*holding|фриdom\s*holding|фридom\s*holding|холдинг', n, re.I,
    ):
        return _hit(
            'holding',
            'Claude API → длинный response',
            key='holding_after_stop',
            stop_music=True,
            api_json={'source': 'demo', 'topic': 'Freedom Holding', 'ticker': 'NASDAQ:FRHC'},
        )

    if (
        re.search(r'(?:доброе|добро)\s+утр|добрый\s+(?:день|вечер|ранок)', n)
        or re.search(r'здравств|приветств', n)
        or (re.search(r'привет|хай|хелло|hello|hi', n) and len(n.split()) <= 4)
        or (re.search(r'утр[оа]|morning', n) and len(n.split()) <= 4)
    ):
        return _hit('greeting', 'WAKE → STT → LLM → TTS ✓')

    if _match_weather(n, raw):
        wv = _weather_variants()
        if wv:
            tts_key, reply = pick_variant('weather', wv)
            return DemoHit(
                key='weather',
                reply=reply,
                screen='Weather API → OpenWeather',
                tts_key=tts_key,
            )
        return _hit('weather', 'Weather API → OpenWeather')

    if _match_my_playlist(n, raw) or _match_freedom_music(n, raw):
        from demo_variants import demo_music_play_query, demo_music_track_label

        extra = {}
        if demo_music_play_enabled():
            extra['start_music'] = demo_music_play_query()
        return _hit(
            'freedom_music',
            'Music API mock → playlist',
            api_json={
                'service': 'Freedom Music',
                'status': 'mock',
                'playlist': 'favorite',
                'track': demo_music_track_label(),
            },
            **extra,
        )

    if _match_portfolio(n, raw):
        return _hit(
            'portfolio',
            'Broker API mock → JSON',
            api_json=PORTFOLIO_JSON,
        )

    if _match_kazakh(n, raw):
        custom = os.environ.get('VC_DEMO_KAZAKH_REPLY', '').strip()
        if custom:
            return DemoHit(
                key='kazakh',
                reply=custom,
                screen='STT: KZ detected → LLM → TTS',
                tts_key='demo_kazakh_0',
            )
        return _hit('kazakh', 'STT: KZ detected → LLM → TTS')

    if (
        re.search(r'freedom\s*holding|фриdom\s*holding|фридom\s*holding', n, re.I)
        or re.search(r'расскаж|расскажи|поведай|объясни|что такое|кто такой', n, re.I) and re.search(r'холдинг|компани|frhc|nasdaq', n, re.I)
        or re.search(r'холдинг|holding|frhc|nasdaq', n, re.I)
    ):
        return _hit(
            'holding',
            'Claude API → длинный response',
            api_json={'source': 'demo', 'topic': 'Freedom Holding', 'ticker': 'NASDAQ:FRHC'},
        )

    # Fuzzy fallback — если ни один паттерн не сработал

    if _match_grocery(n, raw):
        return _hit(
            'grocery',
            'Freedom Market API → корзина',
            api_json={'service': 'Freedom Market', 'action': 'cart_created', 'status': 'pending_payment'},
        )

    if _match_transfer(n, raw):
        return _hit(
            'transfer',
            'Freedom Pay API → уведомление',
            api_json={'service': 'Freedom Pay', 'action': 'transfer_confirm', 'status': 'pending'},
        )
    fuzzy = _fuzzy_intent(raw)
    if fuzzy == 'greeting':
        return _hit('greeting', 'WAKE → STT → LLM → TTS ✓')
    if fuzzy == 'weather':
        wv = _weather_variants()
        if wv:
            tts_key, reply = pick_variant('weather', wv)
            return DemoHit(key='weather', reply=reply, screen='Weather API → OpenWeather', tts_key=tts_key)
        return _hit('weather', 'Weather API → OpenWeather')
    if fuzzy == 'freedom_music':
        from demo_variants import demo_music_play_query, demo_music_track_label
        extra = {}
        if demo_music_play_enabled():
            extra['start_music'] = demo_music_play_query()
        return _hit('freedom_music', 'Music API mock → playlist',
                    api_json={'service': 'Freedom Music', 'status': 'mock',
                              'playlist': 'favorite', 'track': demo_music_track_label()}, **extra)
    if fuzzy == 'holding':
        return _hit('holding', 'Claude API → длинный response',
                    api_json={'source': 'demo', 'topic': 'Freedom Holding', 'ticker': 'NASDAQ:FRHC'})
    if fuzzy == 'portfolio':
        return _hit('portfolio', 'Broker API mock → JSON', api_json=PORTFOLIO_JSON)
    if fuzzy == 'kazakh':
        return _hit('kazakh', 'STT: KZ detected → LLM → TTS')
    if fuzzy == 'grocery':
        return _hit('grocery', 'Freedom Market API → корзина',
                    api_json={'service': 'Freedom Market', 'action': 'cart_created', 'status': 'pending_payment'})
    if fuzzy == 'transfer':
        return _hit('transfer', 'Freedom Pay API → уведомление',
                    api_json={'service': 'Freedom Pay', 'action': 'transfer_confirm', 'status': 'pending'})

    return None


# re-export для совместимости
DEMO_PHRASES = all_phrase_keys()
