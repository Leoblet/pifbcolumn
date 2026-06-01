# -*- coding: utf-8 -*-
"""Deepgram STT — REST API, PCM/WAV 16 kHz."""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor

_API_KEY = None


def _load_api_key() -> str:
    global _API_KEY
    if _API_KEY is not None:
        return _API_KEY
    _API_KEY = os.environ.get('VC_DEEPGRAM_API_KEY', '').strip()
    return _API_KEY


def credentials_ok() -> bool:
    return bool(_load_api_key())


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _resolve_language() -> str:
    """Язык Deepgram: ru | kk | multi. При bilingual — multi или kk+ru параллельно."""
    explicit = os.environ.get('VC_DEEPGRAM_LANG', '').strip().lower()
    if explicit in ('ru', 'kk', 'multi', 'en'):
        return explicit
    try:
        from lang_context import lang_mode
        mode = lang_mode()
        if mode == 'kk':
            return 'kk'
        if mode == 'bilingual':
            return os.environ.get('VC_DEEPGRAM_BILINGUAL_LANG', 'multi').lower()
    except ImportError:
        pass
    return 'ru'


def _keyterms() -> list[str]:
    raw = os.environ.get(
        'VC_DEEPGRAM_KEYTERMS',
        'айдана,дворецкий,колонка,алло,слушай,погода,громкость,музыка,привет,как дела,'
        'включи,выключи,поставь,мэдкид,медкид,ассистент,родненький,родной,'
        'сәлеметсіз,салеметсиз,салем,сәлем,казахск,қазақ',
    )
    return [x.strip() for x in raw.split(',') if x.strip()]


def _request_deepgram(wav: bytes, lang: str, key: str, model: str, timeout: int) -> tuple[str, float]:
    params = [
        ('model', model),
        ('punctuate', 'true'),
        ('smart_format', 'true'),
    ]
    if lang == 'multi':
        params.append(('language', 'multi'))
    else:
        params.append(('language', lang))
    for term in _keyterms()[:10]:
        params.append(('keyterm', term))

    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f'https://api.deepgram.com/v1/listen?{qs}',
        data=wav,
        method='POST',
        headers={
            'Authorization': f'Token {key}',
            'Content-Type': 'audio/wav',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    alts = data['results']['channels'][0]['alternatives']
    alt = alts[0] if alts else {}
    text = (alt.get('transcript') or '').strip()
    conf = float(alt.get('confidence') or 0.0)
    return text, conf


def stt_recognize_pcm(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    language: str | None = None,
) -> str:
    text, _ = stt_recognize_pcm_detail(pcm_bytes, sample_rate, language)
    return text


def stt_recognize_pcm_detail(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    language: str | None = None,
) -> tuple[str, float]:
    key = _load_api_key()
    if not key:
        raise RuntimeError('Нужен VC_DEEPGRAM_API_KEY в .env')

    model = os.environ.get('VC_DEEPGRAM_MODEL', 'nova-3')
    lang = language or _resolve_language()
    timeout = int(os.environ.get('VC_DEEPGRAM_TIMEOUT', '15'))

    wav = _pcm_to_wav(pcm_bytes, sample_rate)

    try:
        from lang_context import lang_mode, pick_stt_result, set_turn_lang
        mode = lang_mode()
    except ImportError:
        mode = 'ru'
        pick_stt_result = None
        set_turn_lang = None

    # bilingual: параллельно ru + kk (лучше для «сәлеметсіз бе», чем один ru)
    if (
        language is None
        and mode == 'bilingual'
        and os.environ.get('VC_DEEPGRAM_BILINGUAL_DUAL', '1').lower() not in ('0', 'false', 'no')
    ):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_ru = pool.submit(_request_deepgram, wav, 'ru', key, model, timeout)
                f_kk = pool.submit(_request_deepgram, wav, 'kk', key, model, timeout)
                ru_text, ru_conf = f_ru.result()
                kk_text, kk_conf = f_kk.result()
            if pick_stt_result:
                text, detected = pick_stt_result(ru_text, kk_text)
                conf = kk_conf if detected == 'kk' else ru_conf
                if text and set_turn_lang:
                    set_turn_lang(detected)
                return text, conf
        except urllib.error.HTTPError:
            pass

    try:
        text, conf = _request_deepgram(wav, lang, key, model, timeout)
        if not text and lang == 'ru':
            if mode in ('bilingual', 'kk') and os.environ.get('VC_STT_FAST', '0').lower() not in ('1', 'true', 'yes'):
                text, conf = _request_deepgram(wav, 'multi', key, model, timeout)
        if not text and mode == 'kk' and lang != 'kk':
            text, conf = _request_deepgram(wav, 'kk', key, model, timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'Deepgram HTTP {e.code}: {body}') from e

    if text:
        try:
            from lang_context import detect_lang, set_turn_lang
            set_turn_lang(detect_lang(text))
        except ImportError:
            pass
    return text, conf
