# -*- coding: utf-8 -*-
"""Быстрый путь: команды громкости/музыки без LLM и с кэш-TTS."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

VC_DIR = os.path.dirname(os.path.abspath(__file__))
CMD_DIR = Path(os.environ.get('VC_CMD_ASSETS', os.path.join(VC_DIR, 'assets', 'cmd')))

# Фразы с заранее синтезированным wav (одинаковые каждый раз)
CACHED_PHRASES: dict[str, str] = {
    'stop_music': 'Остановила.',
    'memory_clear': 'Хорошо, забыла.',
    'music_start': 'Ищу.',
    'not_heard': 'Не расслышала. Повтори, пожалуйста.',
}


def is_fast_command(text: str) -> bool:
    """Команда без LLM — громкость, музыка, стоп, сброс памяти."""
    t = (text or '').strip()
    if not t:
        return False
    try:
        from volume_control import is_volume_command
        if is_volume_command(t):
            return True
    except ImportError:
        pass
    try:
        from music_player import is_music_command, is_stop_music_command
        if is_stop_music_command(t) or is_music_command(t):
            return True
    except ImportError:
        pass
    try:
        from session_memory import is_memory_reset_command
        if is_memory_reset_command(t):
            return True
    except ImportError:
        pass
    return False


def _ogg_to_wav(ogg: bytes, wav_path: str) -> bool:
    try:
        p = subprocess.run(
            [
                'ffmpeg', '-y', '-loglevel', 'error',
                '-i', 'pipe:0', '-ar', '44100', '-ac', '2', wav_path,
            ],
            input=ogg,
            capture_output=True,
            timeout=30,
        )
        return p.returncode == 0 and os.path.isfile(wav_path)
    except (subprocess.TimeoutExpired, OSError):
        return False


def _synth_wav(key: str, text: str, log_fn=None) -> str | None:
    try:
        from yandex_speechkit import credentials_ok, synthesize_ogg
        from lang_context import get_turn_lang, tts_profile
    except ImportError:
        return None
    if not credentials_ok():
        return None
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = str(CMD_DIR / f'{key}.wav')
    prof = tts_profile(get_turn_lang())
    try:
        # Для длинных текстов используем gRPC стриминг
        if len(text) > 300:
            try:
                from yandex_tts_stream import iter_synthesize_ogg
                chunks = list(iter_synthesize_ogg(text, prof['voice'], prof['role'], prof['lang']))
                ogg = b''.join(chunks) if chunks else b''
            except Exception:
                ogg = synthesize_ogg(text, voice=prof['voice'], emotion=prof['role'], lang=prof['lang'])
        else:
            ogg = synthesize_ogg(text, voice=prof['voice'], emotion=prof['role'], lang=prof['lang'])
    except Exception as exc:
        if log_fn:
            log_fn(f'cmd cache {key}: {exc}')
        return None
    if not ogg or not _ogg_to_wav(ogg, wav_path):
        return None
    return wav_path


def warm_command_cache(log_fn=None) -> None:
    """Прогреть wav для типовых ответов на команды."""
    if os.environ.get('VC_CMD_CACHE', '1').lower() in ('0', 'false', 'no'):
        return
    for key, text in CACHED_PHRASES.items():
        path = CMD_DIR / f'{key}.wav'
        if path.is_file():
            continue
        if log_fn:
            log_fn(f'Кэш команды: {key}')
        _synth_wav(key, text, log_fn)
    if log_fn:
        log_fn('Прогрев команд OK')


def _aplay(wav_path: str, alsa: str, timeout: int | None = None) -> bool:
    if timeout is None:
        timeout = int(os.environ.get('VC_TTS_APLAY_TIMEOUT', '90'))
    for dev in (alsa, 'speaker' if alsa != 'speaker' else 'default'):
        r = subprocess.run(
            ['aplay', '-q', '-D', dev, wav_path],
            capture_output=True,
            timeout=timeout,
        )
        if r.returncode == 0:
            return True
    return False


def _speak_stream(text: str, *, alsa_device: str, t_question=None, log_fn=None) -> bool:
    try:
        from yandex_speechkit import credentials_ok, speak_yandex
        if credentials_ok():
            speak_yandex(text, alsa_device=alsa_device, t_question=t_question, log_fn=log_fn)
            return True
    except ImportError:
        pass
    return False


def speak_command(
    key: str,
    text: str,
    *,
    alsa_device: str = 'speaker',
    t_question=None,
    log_fn=None,
) -> None:
    """Ответ на команду: кэш-wav или короткий Yandex REST."""
    text = (text or '').strip()
    if not text:
        return
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=True)
    except ImportError:
        pass

    cached_text = CACHED_PHRASES.get(key)
    wav = CMD_DIR / f'{key}.wav'
    max_short = int(os.environ.get('VC_TTS_CMD_MAX_CHARS', '140'))
    if cached_text and text == cached_text and wav.is_file():
        if log_fn:
            log_fn(f'⚡ TTS команда (кэш): {text[:40]}')
        play_timeout = max(30, len(text) // 6 + 20)
        _aplay(str(wav), alsa_device, timeout=play_timeout)
        return

    if len(text) > max_short and _speak_stream(
        text, alsa_device=alsa_device, t_question=t_question, log_fn=log_fn,
    ):
        return

    try:
        from yandex_speechkit import credentials_ok, speak_yandex_short
        if credentials_ok():
            speak_yandex_short(
                text,
                alsa_device=alsa_device,
                t_question=t_question,
                log_fn=log_fn,
            )
            return
    except ImportError:
        pass

    if log_fn:
        log_fn(f'⚡ TTS команда (fallback): {text[:40]}')
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav = f.name
    try:
        subprocess.run(
            ['espeak-ng', '-v', 'ru', '-s', '160', '-w', wav, text],
            capture_output=True,
            timeout=15,
        )
        _aplay(wav, alsa_device)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
