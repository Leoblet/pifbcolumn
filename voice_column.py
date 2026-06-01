#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Голосовая колонка: микрофон → STT → ZeroClaw → TTS → динамик
Без VBot. ZeroClaw уже должен работать (OpenRouter настроен).
"""

import argparse
import asyncio
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import contextmanager, nullcontext

# --- настройки (env или defaults) ---


def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, val = line.split('=', 1)
            os.environ.setdefault(key.strip(), val.strip())


_load_env_file()

ZEROCLAW_BIN = os.environ.get('ZEROCLAW_BIN', '/home/pi/zeroclaw/zeroclaw')
ZEROCLAW_TIMEOUT = int(os.environ.get('ZEROCLAW_TIMEOUT', '90'))
RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
RECORD_SEC = float(os.environ.get('VC_RECORD_SEC', '6'))
SILENCE_RMS = int(os.environ.get('VC_SILENCE_RMS', '100'))
SPEECH_RMS = int(os.environ.get('VC_SPEECH_RMS', '180'))
SPEECH_CONTINUE_RMS = int(os.environ.get('VC_SPEECH_CONTINUE_RMS', '130'))
SILENCE_TIMEOUT = float(os.environ.get('VC_SILENCE_TIMEOUT', '0.9'))
MIN_SPEECH_SEC = float(os.environ.get('VC_MIN_SPEECH_SEC', '0.4'))
END_SILENCE_CHUNKS = int(os.environ.get('VC_END_SILENCE_CHUNKS', '6'))
POST_ACK_GUARD = float(os.environ.get('VC_POST_ACK_GUARD', '0.25'))
POST_TTS_GUARD = float(os.environ.get('VC_POST_TTS_GUARD', '0.8'))
TTS_VOICE = os.environ.get('VC_TTS_VOICE', 'ru-RU-SvetlanaNeural')
TTS_ENGINE = os.environ.get('VC_TTS_ENGINE', 'edge').lower()  # edge | yandex | espeak
TTS_ESPEAK_SPEED = int(os.environ.get('VC_TTS_ESPEAK_SPEED', '145'))
STREAM_REPLY = os.environ.get('VC_STREAM', '1').lower() not in ('0', 'false', 'no')
STT_ENGINE = os.environ.get('VC_STT_ENGINE', 'google').lower()  # deepgram | deepgram_first | google | google_first | yandex | vosk
FAST_LLM = os.environ.get('VC_FAST_LLM', '1').lower() not in ('0', 'false', 'no')
ALSA_DEVICE = os.environ.get('VC_ALSA_DEVICE', '')  # plughw:1,0 после wm8960
MIC_DEVICE_INDEX = os.environ.get('VC_MIC_INDEX', '')  # PyAudio index
VOSK_MODEL = os.environ.get('VC_VOSK_MODEL', '/home/pi/voice_column/vosk-model-small-ru-0.22')
WAKE_PHRASES = [
    p.strip().lower()
    for p in os.environ.get(
        'VC_WAKE_PHRASES',
        'айдана,дворецкий',
    ).split(',')
    if p.strip()
]
WAKE_COOLDOWN = float(os.environ.get('VC_WAKE_COOLDOWN', '1.0'))
WAKE_DEBUG = os.environ.get('VC_WAKE_DEBUG', '').lower() in ('1', 'true', 'yes')
WAKE_RECORD_SEC = float(os.environ.get('VC_WAKE_RECORD_SEC', '6'))
WAKE_SILENCE_TIMEOUT = float(os.environ.get('VC_WAKE_SILENCE_TIMEOUT', '0.85'))
WAKE_STT_FALLBACK = os.environ.get('VC_WAKE_STT_FALLBACK', '0').lower() in ('1', 'true', 'yes')
WAKE_STT_FALLBACK_SEC = float(os.environ.get('VC_WAKE_STT_FALLBACK_SEC', '0.7'))

VC_DIR = os.path.dirname(os.path.abspath(__file__))
ACK_WAV = os.environ.get('VC_ACK_WAV', os.path.join(VC_DIR, 'assets', 'ack.wav'))

_MIC_INDEX = None

# Только русские слова (small-ru не знает ok/google)
WAKE_GRAMMAR_PHRASES = [
    'айдана',
    'дворецкий'
]


def log(msg):
    print(f'[voice_column] {msg}', flush=True)


def _led(state=None):
    """WS2812 / GPIO / ACT — индикация состояния колонки."""
    try:
        from led_status import led_idle_if_no_music, led_set
        if state:
            led_set(state)
        else:
            led_idle_if_no_music()
    except Exception:
        pass


def _led_init():
    try:
        from led_status import get_led
        get_led()
    except Exception:
        pass


@contextmanager
def _suppress_native_stderr():
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


def get_mic_index():
    global _MIC_INDEX
    if _MIC_INDEX is not None:
        return _MIC_INDEX
    if MIC_DEVICE_INDEX.isdigit():
        _MIC_INDEX = int(MIC_DEVICE_INDEX)
        return _MIC_INDEX
    _MIC_INDEX = find_wm8960_mic_index()
    return _MIC_INDEX


def find_wm8960_mic_index():
    try:
        import pyaudio
    except ImportError:
        return None
    pa = pyaudio.PyAudio()
    try:
        if MIC_DEVICE_INDEX.isdigit():
            return int(MIC_DEVICE_INDEX)
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = (info.get('name') or '').strip().lower()
            if info.get('maxInputChannels', 0) > 0 and name == 'default':
                return i
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = (info.get('name') or '').lower()
            if info.get('maxInputChannels', 0) > 0 and (
                'wm8960' in name or 'seeed' in name or 'voicecard' in name or 'soundcard' in name
            ):
                return i
        # fallback: первый вход с каналами
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                log(f'микрофон fallback: [{i}] {info.get("name")}')
                return i
    finally:
        pa.terminate()
    return None


def frame_rms(frame_bytes):
    n = len(frame_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack('<' + 'h' * n, frame_bytes)
    return (sum(s * s for s in samples) / n) ** 0.5


def _pcm_max_rms(pcm_bytes, chunk=2048):
    if not pcm_bytes:
        return 0.0
    peak = 0.0
    for i in range(0, len(pcm_bytes), chunk):
        peak = max(peak, frame_rms(pcm_bytes[i:i + chunk]))
    return peak


def _normalize_pcm_for_stt(pcm_bytes):
    """Подтянуть тихую запись перед STT (частая причина мусора в Google)."""
    if not pcm_bytes:
        return pcm_bytes
    import array

    samples = array.array('h')
    samples.frombytes(pcm_bytes)
    if not samples:
        return pcm_bytes
    peak = max(abs(s) for s in samples)
    if peak <= 100:
        return pcm_bytes
    target = int(os.environ.get('VC_STT_GAIN_PEAK', '14000'))
    quiet_rms = float(os.environ.get('VC_STT_GAIN_QUIET_RMS', '3500'))
    frame_rms = _pcm_max_rms(pcm_bytes)
    if peak < target or frame_rms < quiet_rms:
        gain = min(float(os.environ.get('VC_STT_GAIN_MAX', '4.5')), target / peak)
        boosted = array.array(
            'h',
            (max(-32767, min(32767, int(s * gain))) for s in samples),
        )
        log(f'STT усиление x{gain:.1f} (peak={peak}, rms={frame_rms:.0f})')
        return boosted.tobytes()
    return pcm_bytes


_STT_JUNK = frozenset(
    {'ты', 'да', 'нет', 'ок', 'э', 'а', 'ну', 'и', 'в', 'на', 'у', 'о', 'я', 'мы'},
)


def _stt_text_score(text: str) -> float:
    t = (text or '').strip()
    if not t:
        return 0.0
    words = t.split()
    score = len(t) + len(words) * 4.0
    if len(words) == 1 and words[0].lower() in _STT_JUNK:
        score -= 80.0
    if any(ch.isdigit() for ch in t):
        score += 6.0
    return score


_STT_VOICE_TERMS = (
    'chatgpt', 'chat gpt', 'gpt', 'google', 'гугл', 'джипити', 'джи пи ти',
    'openai', 'colonna', 'колонка', 'alexa', 'siri', 'john',
)


def _stt_has_voice_terms(text: str) -> bool:
    low = (text or '').lower()
    return any(k in low for k in _STT_VOICE_TERMS)


def _stt_suspicious_latin(text: str) -> bool:
    """Deepgram иногда выдаёт чистый английский на русской речи."""
    t = (text or '').strip()
    if not t:
        return False
    if _stt_has_voice_terms(t):
        return False
    cyr = len(re.findall(r'[а-яёәіңғүұқөһ]', t, re.I))
    lat = len(re.findall(r'[a-z]', t, re.I))
    if cyr >= 2:
        return False
    return lat >= 4 or (lat >= 2 and cyr == 0)


def _stt_suspicious_garbage(text: str) -> bool:
    """Повторы слов и типичный мусор Deepgram на wm8960."""
    words = [w.lower() for w in (text or '').split()]
    if len(words) >= 2:
        for i in range(len(words) - 1):
            if words[i] == words[i + 1] and len(words[i]) > 2:
                return True
        from collections import Counter
        top, count = Counter(words).most_common(1)[0]
        if count >= 3 and len(top) > 2:
            return True
        if len(words) >= 4 and count / len(words) >= 0.55:
            return True
    low = (text or '').lower()
    for junk in ('таким образом', 'мороки', 'морозки', 'не получается'):
        if junk in low:
            return True
    return False


def _stt_mixed_latin_noise(text: str) -> bool:
    """Смесь кириллицы с латинским мусором: «Включи prost нельзя»."""
    if not text or _stt_has_voice_terms(text):
        return False
    if not re.search(r'[а-яё]', text, re.I):
        return False
    return any(re.fullmatch(r'[a-z]{3,}', w, re.I) for w in text.split())


def _stt_never_accept(text: str, engine: str) -> bool:
    """Жёсткий отказ — не принимать даже через cloud fallback."""
    if _stt_suspicious_garbage(text):
        return True
    if engine in ('deepgram', 'google') and _stt_mixed_latin_noise(text):
        return True
    if engine == 'deepgram' and _stt_suspicious_latin(text):
        return True
    return False


def _stt_quality_ok(text: str, engine: str, max_rms: float, conf: float = 0.0) -> bool:
    t = (text or '').strip()
    if len(t) < 2:
        return False
    if engine == 'deepgram':
        min_conf = float(os.environ.get('VC_STT_DEEPGRAM_MIN_CONF', '0.55'))
        if conf > 0 and conf < min_conf:
            return False
    if engine == 'deepgram' and _stt_suspicious_latin(t):
        return False
    if engine == 'deepgram' and _stt_suspicious_garbage(t):
        return False
    if engine in ('deepgram', 'google') and _stt_mixed_latin_noise(t):
        return False
    words = t.split()
    if engine == 'vosk' and len(words) < 2 and len(t) < 8:
        return False
    if len(words) == 1 and words[0].lower() in _STT_JUNK:
        return False
    quiet = max_rms < float(os.environ.get('VC_STT_QUIET_RMS', '4500'))
    if quiet and len(t) < 12 and len(words) < 3:
        return False
    return True


def _yandex_stt_available() -> bool:
    try:
        from yandex_speechkit import credentials_ok
        return credentials_ok()
    except ImportError:
        return False


def _deepgram_stt_available() -> bool:
    try:
        from deepgram_stt import credentials_ok
        return credentials_ok()
    except ImportError:
        return False


def _stt_stream_enabled() -> bool:
    if os.environ.get('VC_STT_STREAM', '1').lower() in ('0', 'false', 'no'):
        return False
    if STT_ENGINE not in ('deepgram', 'deepgram_first', 'hybrid', 'google_first'):
        return False
    if not _deepgram_stt_available():
        return False
    try:
        from deepgram_live import live_available
        return live_available()
    except ImportError:
        return False


def _open_live_stt():
    if not _stt_stream_enabled():
        return None
    try:
        from deepgram_live import DeepgramLiveSTT
        live = DeepgramLiveSTT(log_fn=log)
        if live.open():
            return live
    except Exception as exc:
        log(f'Deepgram live: {exc}')
    return None


def trim_pcm_edges(pcm_bytes, chunk_samples=1024):
    """Убрать длинную тишину в начале/конце — меньше повторов в STT."""
    if not pcm_bytes:
        return pcm_bytes
    chunk = chunk_samples * SAMPLE_WIDTH
    n = len(pcm_bytes) // chunk
    if n == 0:
        return pcm_bytes
    start = 0
    end = n
    for i in range(n):
        off = i * chunk
        if frame_rms(pcm_bytes[off:off + chunk]) >= SPEECH_CONTINUE_RMS:
            start = max(0, i - 1)
            break
    for i in range(n - 1, -1, -1):
        off = i * chunk
        if frame_rms(pcm_bytes[off:off + chunk]) >= SILENCE_RMS:
            end = min(n, i + 2)
            break
    trimmed = pcm_bytes[start * chunk:end * chunk]
    return trimmed if len(trimmed) > chunk else pcm_bytes


def _drain_mic(stream, chunk, seconds):
    if seconds <= 0:
        return
    end = time.time() + seconds
    while time.time() < end:
        stream.read(chunk, exception_on_overflow=False)


def _record_dynamic_enabled(wake: bool) -> bool:
    if os.environ.get('VC_RECORD_DYNAMIC', '1').lower() in ('0', 'false', 'no'):
        return False
    if wake:
        return True
    return os.environ.get('VC_RECORD_DYNAMIC_ALL', '0').lower() in ('1', 'true', 'yes')


def _record_limits(wake: bool, max_sec, silence_timeout):
    """Динамические лимиты: короткие команды быстро, длинные — дольше."""
    prefix = 'VC_WAKE_' if wake else 'VC_'
    return {
        'listen_max': float(os.environ.get(
            f'{prefix}LISTEN_MAX',
            '3.0' if wake else str(max_sec),
        )),
        'speech_max': float(os.environ.get(
            f'{prefix}SPEECH_MAX',
            '4.5' if wake else str(max_sec),
        )),
        'silence_min': float(os.environ.get(
            f'{prefix}SILENCE_MIN',
            os.environ.get(f'{prefix}SILENCE_TIMEOUT', str(silence_timeout)),
        )),
        'silence_max': float(os.environ.get(f'{prefix}SILENCE_MAX', '0.48')),
        'silence_ramp': float(os.environ.get(f'{prefix}SILENCE_RAMP', '1.6')),
    }


def _dynamic_silence_timeout(speech_len: float, limits: dict, live_stt=None) -> float:
    lo = limits['silence_min']
    hi = limits['silence_max']
    ramp = limits['silence_ramp']
    t = lo + (hi - lo) * min(1.0, max(0.0, speech_len) / ramp)
    if live_stt is not None and hasattr(live_stt, 'phrase_looks_incomplete'):
        if live_stt.phrase_looks_incomplete():
            t = hi + float(os.environ.get('VC_WAKE_SILENCE_EXTEND', '0.12'))
    return t


def record_pcm(
    stream=None,
    pa=None,
    chunk=1024,
    max_sec=None,
    silence_timeout=None,
    quiet=False,
    discard_guard=0.0,
    live_stt=None,
):
    import pyaudio

    own = stream is None
    max_sec = max_sec if max_sec is not None else RECORD_SEC
    silence_timeout = silence_timeout if silence_timeout is not None else SILENCE_TIMEOUT
    wake = quiet
    dynamic = _record_dynamic_enabled(wake)
    limits = _record_limits(wake, max_sec, silence_timeout) if dynamic else None

    if own:
        idx = get_mic_index()
        if idx is None:
            raise RuntimeError('Микрофон не найден. Установите wm8960 (install_voice_column.sh)')
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=idx,
            frames_per_buffer=chunk,
        )
        if not quiet:
            log(f'Слушаю... (mic index={idx})')

    _drain_mic(stream, chunk, discard_guard)
    _led('listen')

    pre_roll = []
    frames = []
    start = time.time()
    speech_start = None
    last_voice = start
    voiced = False
    max_rms = 0.0
    silent_run = 0
    max_pre = max(2, int(RATE / chunk * 0.35))
    end_silence = silence_timeout
    end_reason = ''

    try:
        while True:
            now = time.time()
            if dynamic:
                if not voiced and now - start > limits['listen_max']:
                    end_reason = 'listen_timeout'
                    break
                if voiced and now - speech_start > limits['speech_max']:
                    end_reason = 'speech_max'
                    break
            elif now - start >= max_sec:
                end_reason = 'max_sec'
                break

            data = stream.read(chunk, exception_on_overflow=False)
            rms = frame_rms(data)
            max_rms = max(max_rms, rms)
            threshold = SPEECH_CONTINUE_RMS if voiced else SPEECH_RMS

            if rms >= threshold:
                if not voiced:
                    frames.extend(pre_roll)
                    if live_stt:
                        for pr in pre_roll:
                            live_stt.send_pcm(pr)
                    pre_roll = []
                    speech_start = time.time()
                voiced = True
                last_voice = time.time()
                silent_run = 0
                frames.append(data)
                if live_stt:
                    live_stt.send_pcm(data)
                continue

            if voiced:
                frames.append(data)
                if live_stt:
                    live_stt.send_pcm(data)
                if rms < SILENCE_RMS:
                    silent_run += 1
                else:
                    silent_run = 0
                    last_voice = time.time()

                speech_len = time.time() - (speech_start or start)
                if speech_len >= MIN_SPEECH_SEC:
                    pause = time.time() - last_voice
                    end_silence = (
                        _dynamic_silence_timeout(speech_len, limits, live_stt)
                        if dynamic and limits
                        else silence_timeout
                    )
                    if pause >= end_silence:
                        end_reason = 'silence'
                        break
            else:
                pre_roll.append(data)
                if len(pre_roll) > max_pre:
                    pre_roll.pop(0)
    finally:
        if own:
            stream.stop_stream()
            stream.close()
            pa.terminate()
        if live_stt and not voiced:
            live_stt.abort()

    dur = len(frames) * chunk / RATE if frames else 0
    if dynamic and voiced:
        log(
            f'Запись: {dur:.1f}s, max_rms={max_rms:.0f}, '
            f'dyn pause≥{end_silence:.2f}s ({end_reason or "silence"})'
        )
    else:
        log(f'Запись: {dur:.1f}s, max_rms={max_rms:.0f}')
    if not voiced:
        if not quiet and max_rms < SPEECH_RMS:
            log(f'Слишком тихо (нужно >={SPEECH_RMS}). Проверьте: bash /usr/local/bin/setup_wm8960_mixer.sh 0')
        return None
    return trim_pcm_edges(b''.join(frames), chunk)


_VOSK_STT_MODEL = None


def get_vosk_stt_model():
    global _VOSK_STT_MODEL
    if _VOSK_STT_MODEL is not None:
        return _VOSK_STT_MODEL
    if not os.path.isdir(VOSK_MODEL):
        return None
    try:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
    except ImportError:
        from vosk import Model
    _VOSK_STT_MODEL = Model(VOSK_MODEL)
    return _VOSK_STT_MODEL


def stt_vosk(pcm_bytes):
    model = get_vosk_stt_model()
    if not model:
        return ''
    from vosk import KaldiRecognizer

    # Zero 2 W: ограничить длину; брать начало фразы (после wake пользователь говорит сразу)
    max_bytes = int(RATE * SAMPLE_WIDTH * float(os.environ.get('VC_VOSK_MAX_SEC', '5.5')))
    if len(pcm_bytes) > max_bytes:
        pcm_bytes = pcm_bytes[:max_bytes]

    rec = KaldiRecognizer(model, RATE)
    step = 4000
    for i in range(0, len(pcm_bytes), step):
        rec.AcceptWaveform(pcm_bytes[i : i + step])
    text = json.loads(rec.FinalResult()).get('text') or ''
    return text.strip()


def stt_yandex(pcm_bytes):
    try:
        from yandex_speechkit import credentials_ok, stt_recognize_auto
        from lang_context import set_turn_lang
    except ImportError:
        return ''
    if not credentials_ok():
        return ''
    try:
        text, lang = stt_recognize_auto(pcm_bytes, RATE)
        if lang in ('ru', 'kk'):
            set_turn_lang(lang)
        return (text or '').strip()
    except (RuntimeError, OSError, TimeoutError) as e:
        log(f'Yandex STT: {e}')
        return ''


def stt_deepgram_detail(pcm_bytes):
    try:
        from deepgram_stt import credentials_ok, stt_recognize_pcm_detail
    except ImportError:
        return '', 0.0
    if not credentials_ok():
        return '', 0.0
    try:
        text, conf = stt_recognize_pcm_detail(pcm_bytes, RATE)
        return (text or '').strip(), conf
    except (RuntimeError, OSError, TimeoutError) as e:
        log(f'STT deepgram: {e}')
        return '', 0.0


def stt_deepgram(pcm_bytes):
    text, _ = stt_deepgram_detail(pcm_bytes)
    return text


def _yandex_stt_enabled() -> bool:
    if os.environ.get('VC_STT_YANDEX', '0').lower() in ('0', 'false', 'no', 'off'):
        return False
    return _yandex_stt_available()


def _stt_run_engine(engine: str, pcm_bytes: bytes) -> str:
    text, _ = _stt_run_engine_detail(engine, pcm_bytes)
    return text


def _stt_run_engine_detail(engine: str, pcm_bytes: bytes) -> tuple[str, float]:
    if engine == 'google':
        return stt_google_detail(pcm_bytes)
    if engine == 'yandex':
        return stt_yandex(pcm_bytes), 0.0
    if engine == 'deepgram':
        return stt_deepgram_detail(pcm_bytes)
    if engine == 'vosk':
        return stt_vosk(pcm_bytes), 0.0
    return '', 0.0


def _stt_candidate_score(eng: str, text: str, conf: float) -> float:
    """Выбор лучшего из параллельных STT — не слепой приоритет vosk."""
    score = _stt_text_score(text) + conf * 25.0
    low = text.lower()
    if re.search(r'[а-яё]', text, re.I):
        score += 8.0
    if any(k in low for k in (
        'привет', 'chatgpt', 'chat gpt', 'gpt', 'колонка', 'погода',
        'громкость', 'музыка', 'родненьк', 'ассистент', 'включи', 'выключи',
    )):
        score += 22.0
    if eng == 'vosk' and len(text.split()) <= 2 and len(text) <= 14:
        score -= 20.0
    if eng == 'deepgram' and conf >= 0.5 and len(text) >= 10:
        score += 8.0
    return score


def _stt_pick_parallel(
    results: list[tuple[str, str, float]],
    max_rms: float,
) -> tuple[str, str] | tuple[None, None]:
    """Выбрать лучший ответ по score (длина, confidence, ключевые слова)."""
    parts = []
    for eng, text, conf in results:
        if text:
            parts.append(f'{eng} ({conf:.2f}) «{text[:50]}»')
        else:
            parts.append(f'{eng}=пусто')
    if parts:
        log('STT parallel: ' + ' | '.join(parts))

    ok = [
        (eng, text, conf)
        for eng, text, conf in results
        if text and _stt_quality_ok(text, eng, max_rms, conf)
    ]
    if not ok:
        return None, None

    prefer_raw = os.environ.get('VC_STT_PREFER', 'deepgram,google,vosk')
    prefer_rank = {p: i for i, p in enumerate(p.strip() for p in prefer_raw.split(',') if p.strip())}

    def _rank(item):
        eng, text, conf = item
        return (
            _stt_candidate_score(eng, text, conf),
            conf,
            -prefer_rank.get(eng, 99),
        )

    eng, text, _ = max(ok, key=_rank)
    return eng, text


def _stt_cloud_fallback(
    results: list[tuple[str, str, float]],
    *,
    min_conf: float | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Если фильтры отбросили текст — взять cloud с высокой confidence."""
    if min_conf is None:
        min_conf = float(os.environ.get('VC_STT_MIN_CONF', '0.72'))
        if _stt_fast_mode():
            min_conf = float(os.environ.get('VC_STT_FAST_MIN_CONF', '0.48'))
    cloud = [
        (eng, text, conf)
        for eng, text, conf in results
        if eng in ('deepgram', 'google') and text and conf >= min_conf
        and not _stt_never_accept(text, eng)
    ]
    if not cloud:
        return None, None
    eng, text, conf = max(cloud, key=lambda x: (x[2], _stt_candidate_score(x[0], x[1], x[2])))
    log(f'STT fallback {eng} (conf {conf:.2f}): {text}')
    return eng, text


def _stt_log_deepgram_reject(text: str, conf: float, max_rms: float):
    """Почему deepgram не прошёл quality filter."""
    if not text:
        log('STT deepgram: пустой ответ')
        return
    reasons = []
    min_conf = float(os.environ.get('VC_STT_DEEPGRAM_MIN_CONF', '0.55'))
    if conf > 0 and conf < min_conf:
        reasons.append(f'conf={conf:.2f}<{min_conf}')
    if _stt_suspicious_latin(text):
        reasons.append('latin')
    if _stt_suspicious_garbage(text):
        reasons.append('garbage')
    if _stt_mixed_latin_noise(text):
        reasons.append('mixed-latin')
    words = text.split()
    if len(words) == 1 and words[0].lower() in _STT_JUNK:
        reasons.append('junk-word')
    quiet = max_rms < float(os.environ.get('VC_STT_QUIET_RMS', '4500'))
    if quiet and len(text) < 12 and len(words) < 3:
        reasons.append('quiet-short')
    tag = ', '.join(reasons) if reasons else 'filter'
    log(f'STT deepgram отклонён ({tag}): «{text[:80]}» conf={conf:.2f}')


def _stt_log_result(eng: str, text: str, t0: float):
    if eng == 'yandex':
        try:
            from lang_context import log_lang_label, get_turn_lang
            log(f'STT {eng} ({time.time() - t0:.1f}s, {log_lang_label(get_turn_lang())}): {text}')
            return
        except ImportError:
            pass
    log(f'STT {eng} ({time.time() - t0:.1f}s): {text}')


def _stt_engine_order():
    e = STT_ENGINE
    if e in ('deepgram', 'deepgram_first'):
        return ('deepgram', 'google', 'vosk')
    if e == 'yandex':
        return ('yandex', 'google', 'vosk')
    if e == 'vosk':
        return ('vosk',)
    if e in ('vosk_first',):
        return ('google', 'vosk')
    if e in ('hybrid', 'google_first'):
        order = ['google']
        if _deepgram_stt_available():
            order.append('deepgram')
        if _yandex_stt_enabled():
            order.append('yandex')
        order.append('vosk')
        return tuple(order)
    return ('google', 'vosk')


def _cap_pcm_for_stt(pcm_bytes: bytes) -> bytes:
    max_sec = float(os.environ.get('VC_STT_MAX_SEC', '3.5'))
    max_bytes = int(RATE * SAMPLE_WIDTH * max_sec)
    if len(pcm_bytes) > max_bytes:
        return pcm_bytes[:max_bytes]
    return pcm_bytes


def _stt_fast_mode() -> bool:
    """Быстрый STT — только явный VC_STT_FAST (не VC_SPEED_MODE)."""
    return os.environ.get('VC_STT_FAST', '0').lower() in ('1', 'true', 'yes')


def _stt_parallel_cloud(
    pcm_bytes: bytes,
    max_rms: float,
    t0: float,
) -> str:
    """Deepgram + Google параллельно, выбор лучшего."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    order = _stt_engine_order()
    cloud = [e for e in ('deepgram', 'google') if e in order]
    parallel = os.environ.get('VC_STT_PARALLEL', 'deepgram').lower()
    if parallel == 'deepgram':
        cloud = [e for e in cloud if e == 'deepgram']
    elif parallel == 'google':
        cloud = [e for e in cloud if e == 'google']
    if not cloud:
        return ''

    results: list[tuple[str, str, float]] = []
    timeout = int(os.environ.get('VC_STT_TIMEOUT', '12'))
    with ThreadPoolExecutor(max_workers=len(cloud)) as pool:
        futs = {pool.submit(_stt_run_engine_detail, eng, pcm_bytes): eng for eng in cloud}
        for fut in as_completed(futs, timeout=timeout):
            eng = futs[fut]
            try:
                text, conf = fut.result()
            except Exception as exc:
                log(f'STT {eng} ошибка: {exc}')
                continue
            results.append((eng, text, conf))
            text = (text or '').strip()
            early_conf = float(os.environ.get('VC_STT_EARLY_EXIT_CONF', '0.88'))
            if (
                os.environ.get('VC_STT_EARLY_EXIT', '1').lower() in ('1', 'true', 'yes')
                and text
                and conf >= early_conf
                and _stt_quality_ok(text, eng, max_rms, conf)
            ):
                _stt_log_result(eng, text, t0)
                log(f'⚡ STT early ({eng}, {time.time() - t0:.1f}s)')
                return text
            if (
                os.environ.get('VC_CMD_STT_FAST', '1').lower() in ('1', 'true', 'yes')
                and text
                and _stt_quality_ok(text, eng, max_rms, conf)
            ):
                try:
                    from command_fast import is_fast_command
                    if is_fast_command(text):
                        _stt_log_result(eng, text, t0)
                        log(f'⚡ STT команда ({eng}, {time.time() - t0:.1f}s)')
                        return text
                except ImportError:
                    pass

    eng, text = _stt_pick_parallel(results, max_rms)
    if eng and text:
        _stt_log_result(eng, text, t0)
        return text

    eng, text = _stt_cloud_fallback(results)
    if eng and text:
        _stt_log_result(eng, text, t0)
        return text

    dg = next(((e, t, c) for e, t, c in results if e == 'deepgram' and t), None)
    if dg:
        _stt_log_deepgram_reject(dg[1], dg[2], max_rms)
    elif not any(t for _, t, _ in results):
        tried = '+'.join(cloud)
        log(f'STT cloud: пусто ({tried})')
        if parallel == 'deepgram' and 'google' in order and max_rms >= SPEECH_RMS:
            try:
                g_text, g_conf = stt_google_detail(pcm_bytes)
                g_text = (g_text or '').strip()
                if g_text and _stt_quality_ok(g_text, 'google', max_rms, g_conf):
                    _stt_log_result('google', g_text, t0)
                    log(f'⚡ STT google rescue ({time.time() - t0:.1f}s)')
                    return g_text
            except Exception as exc:
                log(f'STT google rescue: {exc}')
    return ''


def _stt_vosk_fallback(pcm_bytes: bytes, max_rms: float, t0: float) -> str:
    """Локальный vosk — последний шанс, с таймаутом (Pi Zero медленный)."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

    timeout = float(os.environ.get('VC_STT_VOSK_TIMEOUT', '5'))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(stt_vosk, pcm_bytes)
            text = (fut.result(timeout=timeout) or '').strip()
    except FutTimeout:
        log(f'STT vosk timeout ({timeout:.0f}s)')
        return ''
    except Exception as exc:
        log(f'STT vosk ошибка: {exc}')
        return ''

    if text and _stt_quality_ok(text, 'vosk', max_rms):
        _stt_log_result('vosk', text, t0)
        return text
    if text:
        log(f'STT vosk отклонён: «{text[:80]}»')
    return ''


def _stt_bilingual_yandex_first() -> bool:
    return os.environ.get('VC_STT_YANDEX_BILINGUAL', '1').lower() not in ('0', 'false', 'no')


def _stt_lang_mode() -> str:
    try:
        from lang_context import lang_mode
        return lang_mode()
    except ImportError:
        return 'ru'


def _stt_try_yandex_bilingual(pcm_bytes: bytes, max_rms: float, t0: float) -> str:
    """Yandex SpeechKit ru+kk — лучший STT для казахского."""
    if not _yandex_stt_enabled() or not _stt_bilingual_yandex_first():
        return ''
    mode = _stt_lang_mode()
    if mode not in ('bilingual', 'kk'):
        return ''
    text = (stt_yandex(pcm_bytes) or '').strip()
    if text and _stt_quality_ok(text, 'yandex', max_rms):
        _stt_log_result('yandex', text, t0)
        try:
            from lang_context import describe_lang_choice
            log(describe_lang_choice(text))
        except ImportError:
            pass
        return text
    return ''


def stt_recognize(pcm_bytes):
    t0 = time.time()
    pcm_sec = len(pcm_bytes) / (RATE * SAMPLE_WIDTH) if pcm_bytes else 0.0
    max_rms = _pcm_max_rms(pcm_bytes)
    pcm_work = _cap_pcm_for_stt(_normalize_pcm_for_stt(pcm_bytes))
    pcm_sec = len(pcm_work) / (RATE * SAMPLE_WIDTH) if pcm_work else pcm_sec

    yx = _stt_try_yandex_bilingual(pcm_work, max_rms, t0)
    if yx:
        return yx

    use_cloud_parallel = (
        STT_ENGINE in ('deepgram_first', 'hybrid', 'google_first')
        or STT_ENGINE == 'deepgram'
        or _stt_fast_mode()
    )

    if use_cloud_parallel and (_deepgram_stt_available() or STT_ENGINE != 'deepgram'):
        text = _stt_parallel_cloud(pcm_work, max_rms, t0)
        if text:
            return text
        if 'vosk' in _stt_engine_order():
            text = _stt_vosk_fallback(pcm_work, max_rms, t0)
            if text:
                return text
        if pcm_sec >= 0.4:
            log(f'Не распознано ({pcm_sec:.1f}s, max_rms={max_rms:.0f})')
        return ''

    order = _stt_engine_order()

    if STT_ENGINE == 'deepgram':
        for eng in order:
            text = (_stt_run_engine(eng, pcm_work) or '').strip()
            if text and _stt_quality_ok(text, eng, max_rms):
                _stt_log_result(eng, text, t0)
                return text
        if pcm_sec >= 0.4:
            log(f'Не распознано ({pcm_sec:.1f}s, max_rms={max_rms:.0f})')
        return ''

    primary = [e for e in order if e in ('google', 'yandex', 'deepgram')]
    preferred = primary[0] if primary else ''
    candidates: list[tuple[str, str, float]] = []

    if len(primary) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run(engine):
            return engine, _stt_run_engine(engine, pcm_work)

        with ThreadPoolExecutor(max_workers=len(primary)) as pool:
            futures = {pool.submit(_run, eng): eng for eng in primary}
            for fut in as_completed(futures, timeout=int(os.environ.get('VC_STT_TIMEOUT', '22'))):
                try:
                    eng, text = fut.result()
                except Exception:
                    continue
                text = (text or '').strip()
                if text and _stt_quality_ok(text, eng, max_rms):
                    if eng == preferred:
                        _stt_log_result(eng, text, t0)
                        for f in futures:
                            f.cancel()
                        return text
                    candidates.append((eng, text, _stt_text_score(text)))
    else:
        for eng in primary:
            text = (_stt_run_engine(eng, pcm_work) or '').strip()
            if text and _stt_quality_ok(text, eng, max_rms):
                _stt_log_result(eng, text, t0)
                return text

    if candidates:
        candidates.sort(key=lambda x: x[2], reverse=True)
        eng, text, _ = candidates[0]
        _stt_log_result(eng, text, t0)
        return text

    for eng in order:
        if eng in primary:
            continue
        text = (_stt_run_engine(eng, pcm_bytes) or '').strip()
        if text and _stt_quality_ok(text, eng, max_rms):
            _stt_log_result(eng, text, t0)
            return text

    if pcm_sec >= 0.4:
        log(f'Не распознано ({pcm_sec:.1f}s, max_rms={max_rms:.0f})')
    return ''


def stt_google_detail(pcm_bytes):
    import speech_recognition as sr
    r = sr.Recognizer()
    r.dynamic_energy_threshold = False
    r.energy_threshold = 200
    audio = sr.AudioData(pcm_bytes, RATE, SAMPLE_WIDTH)
    try:
        result = r.recognize_google(audio, language='ru-RU', show_all=True)
        if isinstance(result, dict):
            alts = result.get('alternative') or []
            if alts:
                best = max(alts, key=lambda a: float(a.get('confidence') or 0))
                text = (best.get('transcript') or '').strip()
                conf = float(best.get('confidence') or 0.0)
                return text, conf
        if isinstance(result, str):
            return result.strip(), 0.0
        return '', 0.0
    except sr.UnknownValueError:
        return '', 0.0
    except sr.RequestError as e:
        log(f'STT google сеть: {e}')
        return '', 0.0


def stt_google(pcm_bytes):
    text, _ = stt_google_detail(pcm_bytes)
    return text


def ask_zeroclaw(prompt, *, lite=False):
    tag = 'ZeroClaw lite' if lite else 'ZeroClaw'
    log(f'→ {tag}: {prompt[:80]}')
    wrapped = (
        f'{prompt}\n\n'
        'Ответь кратко для голосовой колонки (2–3 предложения, до 50 слов). '
        'В самом конце обязательно задай один короткий уместный вопрос.'
    )
    if lite:
        wrapped = (
            f'{prompt}\n\n'
            'Голосовая колонка. Ответ: 2–3 коротких предложения, до 45 слов. '
            'Актуальные данные — через web_search_tool. '
            'В конце один короткий вопрос.'
        )
    t0 = time.time()
    cmd = [ZEROCLAW_BIN, 'agent', '-m', wrapped]
    timeout = ZEROCLAW_TIMEOUT
    if lite:
        provider = os.environ.get('VC_FAST_AGENT_PROVIDER', 'openrouter').strip()
        model = os.environ.get('VC_FAST_AGENT_MODEL', 'google/gemini-2.0-flash-001').strip()
        if provider:
            cmd.extend(['-p', provider])
        if model:
            cmd.extend(['--model', model])
        timeout = int(os.environ.get('VC_FAST_AGENT_TIMEOUT', '45'))
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
    except subprocess.TimeoutExpired:
        return 'Извините, ответ занял слишком много времени.'
    except subprocess.CalledProcessError as e:
        log(f'{tag} ошибка: {e.output[:200] if e.output else e}')
        return 'Не удалось получить ответ от ассистента.'

    lines = []
    for line in out.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not line or line.startswith('INFO ') or 'zeroclaw' in line.lower():
            continue
        if 'Cost tracking' in line or 'WARN' in line:
            continue
        lines.append(line)
    reply = lines[-1] if lines else out.strip()
    reply = reply.strip() or 'Пустой ответ.'
    try:
        from reply_format import ensure_closing_question
        reply = ensure_closing_question(reply, prompt, log_fn=log)
    except ImportError:
        pass
    log(f'← {tag} ({time.time() - t0:.1f}s): {reply[:120]}')
    return reply


def ask_assistant(prompt, mode='voice'):
    """Fast Butler: fast LLM / ZeroClaw lite / ZeroClaw full."""
    try:
        from session_memory import get_memory
        memory = get_memory()
    except ImportError:
        memory = None

    route = 'legacy'
    route_label = ''
    try:
        from command_router import classify
        route, route_label = classify(prompt)
        if route_label:
            log(f'[router] {route_label}')
            try:
                from demo_scenario import enabled as demo_on, demo_stage
                if demo_on():
                    demo_stage('LLM', route_label, log_fn=log)
            except ImportError:
                pass
    except ImportError:
        pass

    if route in ('legacy', 'fast_llm') and FAST_LLM:
        try:
            from fast_llm import ask_openrouter, needs_web_search
        except ImportError:
            ask_openrouter = None
            needs_web_search = lambda _: False

        use_fast = route == 'fast_llm' or (route == 'legacy' and not needs_web_search(prompt))
        if ask_openrouter and use_fast and not needs_web_search(prompt):
            t0 = time.time()
            reply = ask_openrouter(prompt, memory=memory, mode=mode)
            if reply:
                log(f'← fast LLM ({time.time() - t0:.1f}s): {reply[:120]}')
                try:
                    from reply_format import ensure_closing_question
                    reply = ensure_closing_question(reply, prompt, log_fn=log)
                except ImportError:
                    pass
                return reply
            log('fast LLM недоступен, fallback → ZeroClaw')

    if route == 'zeroclaw_lite':
        return ask_zeroclaw(prompt, lite=True)

    return ask_zeroclaw(prompt)


async def tts_to_file(text, path):
    import edge_tts
    comm = edge_tts.Communicate(text, TTS_VOICE)
    await comm.save(path)


def speak_espeak(text):
    if not text:
        return
    alsa = ALSA_DEVICE or 'default'
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav = f.name
    try:
        r = subprocess.run(
            [
                'espeak-ng', '-v', 'ru', '-s', str(TTS_ESPEAK_SPEED),
                '-w', wav, text,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            log(f'espeak-ng: {(r.stderr or r.stdout or "")[:120]}')
            return speak_edge(text)
        subprocess.run(
            ['aplay', '-q', '-D', alsa, wav],
            check=False,
            capture_output=True,
            timeout=30,
        )
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


def speak_edge_stream(text, t_question=None):
    from stream_pipeline import speak_edge_stream as _stream
    _stream(text, TTS_VOICE, _playback_device(), t_question=t_question, log_fn=log)


def speak_edge(text, t_question=None):
    if not text:
        return
    speak_edge_stream(text, t_question=t_question)


def speak_yandex(text, t_question=None):
    if not text:
        return
    from yandex_speechkit import credentials_ok, speak_yandex as _yandex_speak
    if not credentials_ok():
        log('Yandex TTS: нет ключей, fallback → edge')
        speak_edge(text, t_question=t_question)
        return
    try:
        _yandex_speak(
            text, alsa_device=_playback_device(),
            t_question=t_question, log_fn=log,
        )
    except (RuntimeError, OSError, TimeoutError) as e:
        log(f'Yandex TTS: {e}, fallback → edge')
        speak_edge(text, t_question=t_question)


def speak_text(text, t_question=None):
    if not text:
        return
    _ensure_speaker_route(force=True)
    _led('speak')
    t0 = time.time()
    if TTS_ENGINE == 'yandex':
        try:
            from lang_context import log_lang_label, get_turn_lang
            log(f'TTS yandex ({log_lang_label(get_turn_lang())})')
        except ImportError:
            pass
        speak_yandex(text, t_question=t_question)
    elif TTS_ENGINE == 'edge':
        speak_edge(text, t_question=t_question)
    elif TTS_ENGINE == 'espeak':
        speak_espeak(text)
    else:
        speak_edge(text, t_question=t_question)
    log(f'TTS ({TTS_ENGINE}, {time.time() - t0:.1f}s)')


def play_audio(path):
    alsa = ALSA_DEVICE or 'default'
    wav = path.replace('.mp3', '.wav')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-loglevel', 'error', '-i', path, '-ar', '44100', '-ac', '2', wav],
            check=True,
            capture_output=True,
        )
        r = subprocess.run(['aplay', '-q', '-D', alsa, wav], capture_output=True, text=True)
        if r.returncode == 0:
            return
        log(f'aplay: {(r.stderr or "")[:90]}')
    except FileNotFoundError:
        log('Нужен ffmpeg: sudo apt install ffmpeg')
    except subprocess.CalledProcessError as e:
        log(f'Ошибка воспроизведения: {e}')
    finally:
        try:
            if os.path.exists(wav):
                os.unlink(wav)
        except OSError:
            pass
    for cmd in (['mpg123', '-q', '-o', 'alsa', '-a', alsa, path], ['mpg123', '-q', path]):
        try:
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return
        except FileNotFoundError:
            break
    log(f'Не удалось воспроизвести ({alsa})')


def play_beep():
    _ensure_speaker_route(force=True)
    subprocess.run(['speaker-test', '-t', 'sine', '-f', '880', '-l', '1'], capture_output=True)


def _playback_device():
    d = (ALSA_DEVICE or '').strip()
    return d if d else 'speaker'


def _ensure_speaker_route(force=False):
    from audio_route import ensure_speaker_route
    ensure_speaker_route(force=force)


def _release_mic_stream(stream):
    if not stream:
        return
    try:
        if stream.is_active():
            stream.stop_stream()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _free_input_for_playback(pa_ref, stream_ref, pause=None):
    if pause is None:
        pause = 0.4
    if stream_ref:
        _release_mic_stream(stream_ref[0] if stream_ref else None)
        stream_ref[0] = None
    if pa_ref and pa_ref[0] is not None:
        try:
            pa_ref[0].terminate()
        except Exception:
            pass
        pa_ref[0] = None
    if pause > 0:
        time.sleep(pause)


def _reopen_mic_stream(pa, idx, chunk=2048):
    import pyaudio
    with _suppress_native_stderr():
        return pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=idx,
            frames_per_buffer=chunk,
        )


def _restore_input(pa_ref, stream_ref, mic_idx, chunk=2048, settle=None):
    import pyaudio
    if settle is None:
        settle = 0.2
    with _suppress_native_stderr():
        if pa_ref[0] is None:
            pa_ref[0] = pyaudio.PyAudio()
        stream_ref[0] = _reopen_mic_stream(pa_ref[0], mic_idx, chunk)
    if settle > 0:
        time.sleep(settle)
    return stream_ref[0]


@contextmanager
def _mic_released(pa_ref, stream_ref, mic_idx, chunk=2048, *, fast=False):
    pause = float(os.environ.get('VC_WAKE_MIC_PAUSE' if fast else 'VC_MIC_PAUSE', '0.08' if fast else '0.4'))
    settle = float(os.environ.get('VC_WAKE_MIC_RESTORE' if fast else 'VC_MIC_RESTORE', '0.12' if fast else '0.3'))
    _free_input_for_playback(pa_ref, stream_ref, pause=pause)
    _ensure_speaker_route(force=True)
    try:
        yield
    finally:
        _restore_input(pa_ref, stream_ref, mic_idx, chunk, settle=settle)


def _aplay_wav(path, log_errors=True, *, skip_route=False, wait=True):
    if not path or not os.path.isfile(path):
        if log_errors:
            log(f'aplay: файл не найден {path}')
        return False
    if not skip_route:
        _ensure_speaker_route(force=True)
    dev = _playback_device() or 'speaker'
    if not wait:
        subprocess.Popen(
            ['aplay', '-q', '-D', dev, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    fallbacks = []
    for d in (dev, 'speaker', 'default', 'plughw:0,0'):
        if d and d not in fallbacks:
            fallbacks.append(d)
    for d in fallbacks:
        r = subprocess.run(
            ['aplay', '-q', '-D', d, path],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True
        if log_errors:
            log(f'aplay {d} rc={r.returncode}: {(r.stderr or "")[:90]}')
    return False


def _ensure_ack_wav():
    """Кэш короткого бипа — без ffmpeg на каждый wake."""
    for path in (ACK_WAV, '/tmp/vc_ack.wav'):
        if os.path.isfile(path) and os.path.getsize(path) > 100:
            return path
    dst = ACK_WAV
    assets_dir = os.path.dirname(ACK_WAV)
    if assets_dir:
        try:
            os.makedirs(assets_dir, exist_ok=True)
        except OSError:
            dst = '/tmp/vc_ack.wav'
    else:
        dst = '/tmp/vc_ack.wav'
    subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
         'sine=frequency=880:duration=0.12', '-af', 'volume=0.85',
         '-ar', '44100', '-ac', '2', dst],
        capture_output=True,
        timeout=8,
    )
    return dst if os.path.isfile(dst) else ''


def play_wake_ack(*, skip_route=False):
    """Короткий локальный сигнал — без edge-tts."""
    if os.environ.get('VC_WAKE_ACK', '1').lower() in ('0', 'false', 'no'):
        return
    if not skip_route:
        _ensure_speaker_route(force=True)
    ack = _ensure_ack_wav()
    async_ack = os.environ.get('VC_WAKE_ACK_ASYNC', '1').lower() not in ('0', 'false', 'no')
    if ack and _aplay_wav(ack, skip_route=True, wait=not async_ack):
        return
    subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
         'sine=frequency=880:duration=0.12', '-af', 'volume=0.85',
         '-ar', '44100', '-ac', '2', '/tmp/vc_ack.wav'],
        capture_output=True,
        timeout=8,
    )
    if os.path.isfile('/tmp/vc_ack.wav'):
        _aplay_wav('/tmp/vc_ack.wav', skip_route=True, wait=not async_ack)


def _norm_text(text):
    return (text or '').lower().replace('ё', 'е').strip()


def strip_wake_phrase(text):
    t = _norm_text(text)
    for phrase in WAKE_PHRASES + WAKE_GRAMMAR_PHRASES:
        t = t.replace(_norm_text(phrase), ' ')
    for word in ('колонка', 'google', 'гугл', 'слушай', 'алло', 'эй', 'ок', 'ok', 'айдана', 'дворецкий'):
        t = re.sub(rf'\b{word}\b', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def wake_match(text):
    t = _norm_text(text)
    if not t or t in ('[unk]', 'unk'):
        return False
    return any(_norm_text(p) in t for p in WAKE_PHRASES)


def build_wake_grammar():
    # Только фразы из WAKE_GRAMMAR_PHRASES (русский словарь Vosk)
    phrases = list(dict.fromkeys(WAKE_GRAMMAR_PHRASES))
    quoted = [json.dumps(p, ensure_ascii=False) for p in phrases]
    quoted.append('"[unk]"')
    return '[' + ', '.join(quoted) + ']'


def new_wake_recognizer(model, quiet=False):
    from vosk import KaldiRecognizer

    grammar = build_wake_grammar()
    try:
        rec = KaldiRecognizer(model, RATE, grammar)
        rec.SetWords(True)
        if not quiet:
            log('Wake grammar mode')
        return rec
    except Exception as e:
        if not quiet:
            log(f'Wake grammar недоступен ({e}), полная модель')
        rec = KaldiRecognizer(model, RATE)
        rec.SetWords(True)
        return rec


def reset_wake_recognizer(rec, model):
    if hasattr(rec, 'Reset'):
        rec.Reset()
        return rec
    return new_wake_recognizer(model, quiet=True)


def _music_duck_context():
    try:
        from music_player import duck_while_voice, is_music_playing
    except ImportError:
        return nullcontext()
    return duck_while_voice() if is_music_playing() else nullcontext()


_LAST_CMD_REPLY = ''


def get_last_command_reply() -> str:
    return _LAST_CMD_REPLY


def _save_cmd_reply(msg: str) -> None:
    global _LAST_CMD_REPLY
    _LAST_CMD_REPLY = (msg or '').strip()


def _try_long_reply_delivery(
    text,
    full,
    *,
    speak,
    t_question,
    mem,
    log_fn,
) -> bool:
    """Длинный ответ: только фраза «скину в Telegram» + полный текст в TG."""
    from reply_format import count_sentences, is_long_reply, long_reply_spoken_notice
    from telegram_notify import send_long_answer

    if not is_long_reply(full):
        return False

    n = count_sentences(full)
    log_fn(f'⚡ длинный ответ ({n} предл.) → Telegram')
    tg_ok = send_long_answer(text, full, log_fn=log_fn)
    notice = long_reply_spoken_notice(tg_ok)
    if speak:
        speak_text(notice, t_question=t_question)
    _save_cmd_reply(full)
    if mem:
        mem.add_turn(text, full)
    return True


def handle_command(text, t_question=None, *, speak=True, long_reply_telegram=None, prompt_mode='voice'):
    global _LAST_CMD_REPLY
    _LAST_CMD_REPLY = ''
    t0 = time.time()
    if t_question is None:
        t_question = t0
    if long_reply_telegram is None:
        long_reply_telegram = (
            speak
            and os.environ.get('VC_LONG_REPLY_TELEGRAM', '1').lower() not in ('0', 'false', 'no')
        )
    _ensure_speaker_route(force=False)
    _led('think')

    try:
        from command_fast import is_fast_command, speak_command
    except ImportError:
        is_fast_command = lambda _: False
        speak_command = None

    fast_cmd = is_fast_command(text)

    if TTS_ENGINE == 'yandex' and not fast_cmd:
        try:
            from lang_context import apply_turn_language, describe_lang_choice
            apply_turn_language(text)
            log(describe_lang_choice(text))
        except ImportError:
            pass

    try:
        from session_memory import get_memory, is_memory_reset_command
        mem = get_memory()
    except ImportError:
        mem = None
        is_memory_reset_command = lambda _: False

    if mem and is_memory_reset_command(text):
        mem.clear_history()
        mem.clear_facts()
        log('⚡ команда: сброс памяти')
        msg = 'Хорошо, забыла.'
        if speak:
            if speak_command:
                speak_command('memory_clear', msg, alsa_device=_playback_device(), t_question=t_question, log_fn=log)
            else:
                speak_text(msg, t_question=t_question)
        _save_cmd_reply(msg)
        log(f'Готово за {time.time() - t0:.1f}s')
        _led()
        return True

    try:
        from volume_control import try_handle_volume
        vol_reply = try_handle_volume(text)
        if vol_reply is not None:
            log(f'⚡ команда: громкость — {vol_reply}')
            if speak:
                if speak_command:
                    speak_command('volume', vol_reply, alsa_device=_playback_device(), t_question=t_question, log_fn=log)
                else:
                    speak_text(vol_reply, t_question=t_question)
            _save_cmd_reply(vol_reply)
            log(f'Готово за {time.time() - t0:.1f}s')
            _led()
            return True
    except ImportError:
        pass

    try:
        from demo_scenario import enabled as demo_enabled, try_demo, demo_stage, register_cached_phrases, demo_miss_reply
        if demo_enabled():
            register_cached_phrases()
            demo_stage('STT', text, log_fn=log)
            hit = try_demo(text, log_fn=log)
            if hit:
                from music_player import stop_music as _demo_stop_music

                if hit.stop_music:
                    _demo_stop_music()
                reply = hit.reply
                demo_stage('LLM', reply[:200], log_fn=log)
                if hit.screen:
                    demo_stage('API', hit.screen, log_fn=log)
                if hit.api_json:
                    import json as _json
                    blob = _json.dumps(hit.api_json, ensure_ascii=False)
                    log(f'[demo] JSON: {blob}')
                    demo_stage('API', blob[:240], log_fn=log)
                if speak:
                    _demo_long = hit.key in ('holding', 'holding_after_stop', 'portfolio')
                    if _demo_long:
                        speak_text(reply, t_question=t_question)
                    elif speak_command and hit.tts_key:
                        speak_command(
                            hit.tts_key, reply,
                            alsa_device=_playback_device(), t_question=t_question, log_fn=log,
                        )
                    else:
                        speak_text(reply, t_question=t_question)
                    demo_stage('TTS', '✓', log_fn=log)
                if hit.start_music:
                    try:
                        from music_player import start_music_by_query as _demo_start_music
                        ok, mmsg = _demo_start_music(hit.start_music, ALSA_DEVICE or 'default')
                        if ok:
                            log(f'[demo] ▶ music: {mmsg}')
                            demo_stage('API', f'▶ {mmsg}', log_fn=log)
                        else:
                            log(f'[demo] music fail: {mmsg}')
                    except ImportError:
                        pass
                _save_cmd_reply(reply)
                if mem:
                    mem.add_turn(text, reply)
                log(f'⚡ demo: {hit.key} ({hit.tts_key or "tts"})')
                log(f'Готово за {time.time() - t0:.1f}s')
                _led()
                return True
            miss_key, miss = demo_miss_reply(text)
            demo_stage('LLM', f'нет в сценарии: {text[:80]}', log_fn=log)
            if speak:
                if speak_command and miss_key:
                    speak_command(
                        miss_key, miss,
                        alsa_device=_playback_device(), t_question=t_question, log_fn=log,
                    )
                else:
                    speak_text(miss, t_question=t_question)
            demo_stage('TTS', '✓', log_fn=log)
            _save_cmd_reply(miss)
            log(f'⚡ demo: miss «{text[:50]}»')
            log(f'Готово за {time.time() - t0:.1f}s')
            _led()
            return True
    except ImportError:
        pass

    if mem and not fast_cmd:
        import threading
        threading.Thread(target=lambda: mem.extract_facts(text), daemon=True).start()

    try:
        from music_player import (
            is_music_command,
            is_stop_music_command,
            start_music,
            stop_music,
        )
    except ImportError:
        is_music_command = lambda _: False
        is_stop_music_command = lambda _: False
        start_music = None
        stop_music = None

    if stop_music and is_stop_music_command(text):
        log('⚡ команда: стоп музыка')
        stop_music()
        msg = 'Остановила.'
        if speak:
            if speak_command:
                speak_command('stop_music', msg, alsa_device=_playback_device(), t_question=t_question, log_fn=log)
            else:
                speak_text(msg, t_question=t_question)
        _save_cmd_reply(msg)
        log(f'Готово за {time.time() - t0:.1f}s')
        _led()
        return True

    if start_music and is_music_command(text):
        log('⚡ команда: музыка')
        ok, msg = start_music(text, ALSA_DEVICE or 'default')
        if msg and speak:
            if speak_command:
                speak_command('music_start', msg, alsa_device=_playback_device(), t_question=t_question, log_fn=log)
            else:
                speak_text(msg, t_question=t_question)
        if msg:
            _save_cmd_reply(msg)
        log(f'Готово за {time.time() - t0:.1f}s')
        _led()
        return ok

    reply = None
    pre_collected = False
    try:
        if long_reply_telegram and STREAM_REPLY and FAST_LLM:
            try:
                from stream_pipeline import collect_llm_reply, speak_reply_text

                full = collect_llm_reply(text, memory=mem, log_fn=log, mode='long')
                if full and _try_long_reply_delivery(
                    text, full, speak=speak, t_question=t_question, mem=mem, log_fn=log,
                ):
                    log(f'Готово за {time.time() - t0:.1f}s')
                    _led()
                    return True
                if full:
                    reply = full
                    pre_collected = True
            except ImportError as exc:
                log(f'long→TG: {exc}')

        if not pre_collected and STREAM_REPLY and FAST_LLM:
            try:
                from stream_pipeline import try_batch_reply, try_streaming_reply
                mode = os.environ.get('VC_REPLY_MODE', 'stream').lower()
                if mode != 'batch':
                    reply = try_streaming_reply(
                        text, TTS_VOICE, _playback_device(), t_question, log, memory=mem,
                        mode=prompt_mode,
                    )
                if not reply and mode != 'stream':
                    reply = try_batch_reply(
                        text, _playback_device(), t_question, log, memory=mem,
                        mode=prompt_mode,
                    )
                if not reply and mode == 'stream':
                    reply = try_batch_reply(
                        text, _playback_device(), t_question, log, memory=mem,
                        mode=prompt_mode,
                    )
            except ImportError:
                reply = None
        elif pre_collected and reply and speak:
            from stream_pipeline import speak_reply_text
            speak_reply_text(reply, TTS_VOICE, _playback_device(), t_question, log)

        if not reply:
            reply = ask_assistant(text, mode='long' if long_reply_telegram else prompt_mode)
            if reply and long_reply_telegram:
                if _try_long_reply_delivery(
                    text, reply, speak=speak, t_question=t_question, mem=mem, log_fn=log,
                ):
                    log(f'Готово за {time.time() - t0:.1f}s')
                    _led()
                    return True
            if reply and speak:
                speak_text(reply, t_question=t_question)

        if reply and mem:
            mem.add_turn(text, reply)

        _save_cmd_reply(reply or '')
        log(f'Готово за {time.time() - t0:.1f}s')
        return bool(reply)
    except Exception as exc:
        log(f'Ошибка ответа: {exc}')
        _led('error')
        return False
    finally:
        _led()


def one_turn(strip_wake=False, stream=None, pa=None, chunk=1024, discard_guard=0.0,
              pa_ref=None, stream_ref=None, mic_idx=None, live_pre=None, **_ignored):
    with _music_duck_context():
        live = None
        if live_pre is not None:
            deadline = time.time() + float(os.environ.get('VC_LIVE_STT_WAIT', '0.6'))
            while live_pre[0] is None and time.time() < deadline:
                time.sleep(0.01)
            live = live_pre[0]
        if live is None:
            live = _open_live_stt()
        t_turn = time.time()
        # Deepgram WS уже открыт — параллельно с drain_guard
        pcm = record_pcm(
            stream=stream, pa=pa, chunk=chunk,
            max_sec=WAKE_RECORD_SEC if stream else RECORD_SEC,
            silence_timeout=WAKE_SILENCE_TIMEOUT if stream else SILENCE_TIMEOUT,
            quiet=bool(stream),
            discard_guard=discard_guard,
            live_stt=live,
        )
        if not pcm:
            if live:
                live.abort()
            log('Речь не услышана')
            _led()
            return False

        t_stt = time.time()
        text = ''
        stt_dur = 0.0
        max_rms = _pcm_max_rms(pcm)

        if live:
            dg_text, conf = live.finish()
            stt_dur = time.time() - t_stt
            text = ''
            if dg_text and _stt_quality_ok(dg_text, 'deepgram', max_rms, conf):
                text = dg_text
                _stt_log_result('deepgram', text, t_stt)
                log(f'⚡ STT live ({stt_dur:.1f}s, запись+STT {time.time() - t_turn:.1f}s)')
            else:
                if dg_text:
                    _stt_log_deepgram_reject(dg_text, conf, max_rms)
                elif live._err and log:
                    log(f'Deepgram live пусто: {live._err}')
                elif max_rms < float(os.environ.get('VC_STT_QUIET_RMS', '4500')):
                    log(f'Deepgram live пусто (тихо, rms={max_rms:.0f}) — говори громче')

            if _stt_bilingual_yandex_first() and _yandex_stt_enabled() and _stt_lang_mode() in ('bilingual', 'kk'):
                yx = (stt_yandex(pcm) or '').strip()
                if yx:
                    try:
                        from lang_context import pick_stt_result, set_turn_lang, describe_lang_choice, log_lang_label
                        merged, lang = pick_stt_result(text or '', yx)
                        if merged and (not text or lang == 'kk' or len(merged) >= len(text)):
                            text = merged
                            set_turn_lang(lang)
                            log(f'⚡ STT bilingual → {log_lang_label(lang)}: {text[:70]}')
                        elif not text:
                            text = yx
                            log(describe_lang_choice(text))
                    except ImportError:
                        if not text:
                            text = yx

        if not text:
            text = stt_recognize(pcm)
            stt_dur = time.time() - t_stt
        if not text:
            log('Не распознано')
            _led()
            try:
                from command_fast import speak_command
                speak_command(
                    'not_heard', 'Не расслышала. Повтори, пожалуйста.',
                    alsa_device=_playback_device(), log_fn=log,
                )
            except ImportError:
                speak_text('Не расслышала. Повтори, пожалуйста.')
            return False
        log(f'Вы сказали: {text} (STT {stt_dur:.1f}s)')

        if strip_wake:
            text = strip_wake_phrase(text)
            if not text:
                log('Команда после wake word пустая')
                return False

        t_question = time.time()

        if pa_ref is not None and stream_ref is not None and mic_idx is not None:
            with _mic_released(pa_ref, stream_ref, mic_idx, chunk, fast=True):
                ok = handle_command(text, t_question=t_question)
            if ok:
                log('Ответ OK')
            return bool(ok)

        if stream:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            time.sleep(0.2)
        try:
            return handle_command(text, t_question=t_question)
        finally:
            if stream:
                stream.start_stream()


def _drain_web_command():
    """Команды из веб-чата (/tmp/vc_web_cmd.txt) — музыка, стоп, громкость."""
    path = os.path.join('/tmp', 'vc_web_cmd.txt')
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read().strip()
        os.unlink(path)
    except OSError:
        return
    if not text or len(text) < 2:
        return
    log(f'Web: {text[:80]}')
    try:
        from command_fast import is_fast_command
        if is_fast_command(text):
            threading.Thread(
                target=lambda: handle_command(text, t_question=time.time()),
                daemon=True,
            ).start()
            return
    except ImportError:
        pass
    threading.Thread(
        target=lambda: handle_command(text, t_question=time.time()),
        daemon=True,
    ).start()


def wake_loop():
    """Wake word через Vosk grammar + fuzzy match."""
    if not os.path.isdir(VOSK_MODEL):
        raise RuntimeError(
            f'Модель Vosk не найдена: {VOSK_MODEL}\n'
            'Запустите: node tools/deploy_voice_column_full.mjs'
        )

    try:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
    except ImportError:
        from vosk import Model

    import pyaudio

    log(f'Wake word: {", ".join(WAKE_PHRASES[:5])}...')
    log('Скажите: «Айдана» / «Дворецкий» или «ок колонка»')
    _led_init()
    _led('idle')

    model = get_vosk_stt_model()
    if model is None:
        raise RuntimeError(f'Модель Vosk не найдена: {VOSK_MODEL}')

    def _warm(full=False):
        try:
            from stream_pipeline import warm_pipeline
            warm_pipeline(log, full_tts=full)
        except Exception as e:
            log(f'Прогрев: {e}')

    threading.Thread(target=lambda: _warm(full=True), daemon=True).start()
    try:
        _ensure_ack_wav()
        _ensure_speaker_route(force=True)
    except Exception as e:
        log(f'Ack preload: {e}')

    idx = get_mic_index()
    if idx is None:
        raise RuntimeError('Микрофон не найден')
    log('Колонка слушает…')

    pa_ref, stream_ref = [None], [None]
    chunk = int(os.environ.get('VC_WAKE_CHUNK', '1024'))
    rec = new_wake_recognizer(model)
    with _suppress_native_stderr():
        pa_ref[0] = pyaudio.PyAudio()
        stream = pa_ref[0].open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=idx,
            frames_per_buffer=chunk,
        )
    pa = pa_ref[0]
    stream_ref[0] = stream

    last_wake = 0.0
    last_debug = 0.0
    speech_since = 0.0
    speech_active = False
    speech_buf = []
    max_buf_chunks = int(RATE / chunk * 2)
    last_warm = time.time()
    try:
        while True:
            _drain_web_command()
            if time.time() - last_warm > 90:
                last_warm = time.time()
                threading.Thread(target=lambda: _warm(full=False), daemon=True).start()

            data = stream.read(chunk, exception_on_overflow=False)
            rms = frame_rms(data)
            now = time.time()

            if rms >= SPEECH_RMS:
                if not speech_active:
                    speech_active = True
                    speech_since = now
                    speech_buf = []
                speech_buf.append(data)
                if len(speech_buf) > max_buf_chunks:
                    speech_buf.pop(0)
            elif speech_active and speech_buf:
                speech_buf.append(data)

            if WAKE_DEBUG and rms >= SPEECH_RMS and now - last_debug > 2.0:
                last_debug = now
                partial = json.loads(rec.PartialResult()).get('partial', '')
                if partial:
                    log(f'[debug] partial={partial!r} rms={rms:.0f}')

            triggered = False
            heard = ''
            via_stt = False

            if rec.AcceptWaveform(data):
                heard = (json.loads(rec.Result()).get('text') or '').strip()
                if heard and wake_match(heard):
                    triggered = True
            else:
                pass  # partial results игнорируем — только финальный результат Vosk

            if (
                not triggered
                and WAKE_STT_FALLBACK
                and speech_active
                and speech_buf
                and now - speech_since > WAKE_STT_FALLBACK_SEC
                and rms < SILENCE_RMS
                and now - last_wake > WAKE_COOLDOWN
            ):
                heard = stt_google(b''.join(speech_buf))
                speech_buf = []
                speech_active = False
                if heard and wake_match(heard):
                    log(f'Wake STT: {heard}')
                    triggered = True
                    via_stt = True

            if not triggered:
                if rms < SILENCE_RMS and speech_active and now - speech_since > 2.5:
                    speech_active = False
                    speech_buf = []
                continue
            if now - last_wake < WAKE_COOLDOWN:
                continue

            last_wake = now
            speech_active = False
            speech_buf = []
            t_wake = time.time()
            log(f'Wake: {heard}')
            try:
                from demo_scenario import enabled as demo_enabled, demo_wake_start
                if demo_enabled():
                    demo_wake_start(heard, log_fn=log)
            except ImportError:
                pass
            _led('wake')
            cmd = strip_wake_phrase(heard)

            if cmd and len(cmd) >= 2 and not via_stt:
                with _music_duck_context():
                    try:
                        with _mic_released(pa_ref, stream_ref, idx, chunk, fast=True):
                            play_wake_ack(skip_route=True)
                            log(f'⚡ ack {time.time() - t_wake:.2f}s')
                            _led('listen')
                            handle_command(cmd, t_question=time.time())
                        log('Ответ OK')
                    except Exception as e:
                        log(f'Ошибка: {e}')
                        _led('error')
                        _led()
            else:
                with _music_duck_context():
                    try:
                        live_pre = [None]
                        if _stt_stream_enabled():
                            threading.Thread(
                                target=lambda: live_pre.__setitem__(0, _open_live_stt()),
                                daemon=True,
                            ).start()
                        with _mic_released(pa_ref, stream_ref, idx, chunk, fast=True):
                            play_wake_ack(skip_route=True)
                            log(f'⚡ ack {time.time() - t_wake:.2f}s')
                            _led('listen')
                        stream, pa = stream_ref[0], pa_ref[0]
                        one_turn(
                            strip_wake=True,
                            stream=stream,
                            pa=pa,
                            chunk=chunk,
                            discard_guard=POST_ACK_GUARD,
                            pa_ref=pa_ref,
                            stream_ref=stream_ref,
                            mic_idx=idx,
                            live_pre=live_pre if _stt_stream_enabled() else None,
                        )
                    except Exception as e:
                        log(f'Ошибка: {e}')
                        _led('error')
                        _led()

            stream, pa = stream_ref[0], pa_ref[0]
            if stream and POST_TTS_GUARD > 0:
                _drain_mic(stream, chunk, POST_TTS_GUARD)
            last_wake = time.time()

            rec = reset_wake_recognizer(rec, model)
            _led('idle')
    finally:
        _led()
        try:
            from led_status import get_led
            get_led().cleanup()
        except Exception:
            pass
        if stream_ref[0]:
            _release_mic_stream(stream_ref[0])
        if pa_ref[0]:
            try:
                pa_ref[0].terminate()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description='ZeroClaw voice column')
    parser.add_argument('--once', action='store_true', help='Один цикл и выход')
    parser.add_argument('--wake', action='store_true', help='Режим wake word (Vosk)')
    parser.add_argument('--text', help='Пропустить STT, сразу спросить ZeroClaw')
    parser.add_argument('--speak', help='Только озвучить текст (TTS)')
    args = parser.parse_args()

    if args.speak:
        speak_text(args.speak)
        return

    if args.text:
        handle_command(args.text)
        return

    if args.wake:
        try:
            wake_loop()
        except KeyboardInterrupt:
            print()
        return

    log('Голосовая колонка ZeroClaw')
    log('Enter — говорить, Ctrl+C — выход')

    while True:
        try:
            input('\n▶ Enter чтобы говорить...')
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            one_turn()
        except Exception as e:
            log(f'Ошибка: {e}')
        if args.once:
            break


if __name__ == '__main__':
    main()
