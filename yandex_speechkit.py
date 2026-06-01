# -*- coding: utf-8 -*-
"""Yandex SpeechKit — STT/TTS, ru+kk, голоса Сауле (TTS v3)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from lang_context import (
    default_lang,
    detect_lang,
    get_turn_lang,
    lang_mode,
    pick_stt_result,
    stt_lang_code,
    tts_profile,
)

STT_URL = 'https://stt.api.cloud.yandex.net/speech/v1/stt:recognize'
TTS_URL_V1 = 'https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize'
TTS_URL_V3 = 'https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis'

_HTTP = None

_YANDEX_KEY = None
_YANDEX_FOLDER = None


def _load_api_key() -> str:
    global _YANDEX_KEY
    if _YANDEX_KEY is not None:
        return _YANDEX_KEY
    key = (
        os.environ.get('VC_YANDEX_API_KEY', '').strip()
        or os.environ.get('YANDEX_API_KEY', '').strip()
    )
    if not key:
        env_path = os.environ.get('ZEROCLAW_ENV', '/home/pi/zeroclaw/.env')
        if os.path.isfile(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('YANDEX_API_KEY=') or line.startswith('VC_YANDEX_API_KEY='):
                        key = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
    _YANDEX_KEY = key
    return key


def _load_iam_token() -> str:
    return os.environ.get('VC_YANDEX_IAM_TOKEN', '').strip() or os.environ.get(
        'YANDEX_IAM_TOKEN', ''
    ).strip()


def _load_folder_id() -> str:
    global _YANDEX_FOLDER
    if _YANDEX_FOLDER is not None:
        return _YANDEX_FOLDER
    folder = (
        os.environ.get('VC_YANDEX_FOLDER_ID', '').strip()
        or os.environ.get('YANDEX_FOLDER_ID', '').strip()
    )
    if not folder:
        env_path = os.environ.get('ZEROCLAW_ENV', '/home/pi/zeroclaw/.env')
        if os.path.isfile(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('YANDEX_FOLDER_ID=') or line.startswith('VC_YANDEX_FOLDER_ID='):
                        folder = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
    _YANDEX_FOLDER = folder
    return folder


def credentials_ok() -> bool:
    return bool(_load_folder_id()) and bool(_load_api_key() or _load_iam_token())


def _http_opener():
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib.request.build_opener(urllib.request.HTTPSHandler())
    return _HTTP


def _urlopen(req, timeout):
    return _http_opener().open(req, timeout=timeout)


def _auth_header() -> dict:
    iam = _load_iam_token()
    if iam:
        return {'Authorization': f'Bearer {iam}'}
    key = _load_api_key()
    if key:
        return {'Authorization': f'Api-Key {key}'}
    return {}


def default_voice() -> str:
    return tts_profile('ru')['voice']


def default_emotion() -> str:
    return tts_profile('ru')['role']


def stt_recognize_pcm(pcm_bytes: bytes, sample_rate: int = 16000, lang: str = 'ru-RU') -> str:
    folder = _load_folder_id()
    if not folder:
        raise RuntimeError('Нужен VC_YANDEX_FOLDER_ID в .env')
    auth = _auth_header()
    if not auth:
        raise RuntimeError('Нужен VC_YANDEX_API_KEY или VC_YANDEX_IAM_TOKEN')

    qs = urllib.parse.urlencode(
        {
            'folderId': folder,
            'lang': lang,
            'format': 'lpcm',
            'sampleRateHertz': sample_rate,
            'topic': os.environ.get('VC_YANDEX_STT_TOPIC', 'general'),
        },
    )
    req = urllib.request.Request(
        f'{STT_URL}?{qs}',
        data=pcm_bytes,
        method='POST',
        headers={**auth, 'Content-Type': 'application/octet-stream'},
    )
    timeout = int(os.environ.get('VC_YANDEX_STT_TIMEOUT', '20'))
    try:
        with _urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return (data.get('result') or '').strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:200]
        raise RuntimeError(f'SpeechKit STT HTTP {e.code}: {body}') from e


def stt_recognize_auto(pcm_bytes: bytes, sample_rate: int = 16000) -> tuple[str, str]:
    """STT с авто-языком: bilingual → ru+kk параллельно."""
    mode = lang_mode()
    if mode == 'ru':
        text = stt_recognize_pcm(pcm_bytes, sample_rate, 'ru-RU')
        lang = detect_lang(text) if text else 'ru'
        return text, lang
    if mode == 'kk':
        text = stt_recognize_pcm(pcm_bytes, sample_rate, 'kk-KZ')
        lang = detect_lang(text) if text else 'kk'
        return text, lang

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ru = pool.submit(stt_recognize_pcm, pcm_bytes, sample_rate, 'ru-RU')
        f_kk = pool.submit(stt_recognize_pcm, pcm_bytes, sample_rate, 'kk-KZ')
        ru_text = f_ru.result()
        kk_text = f_kk.result()

    return pick_stt_result(ru_text, kk_text)


def _synthesize_v1(
    text: str,
    voice: str,
    role: str,
    lang: str,
) -> bytes:
    folder = _load_folder_id()
    auth = _auth_header()
    speed = os.environ.get('VC_YANDEX_TTS_SPEED', '1.0')
    form = {
        'text': text[:4900],
        'lang': lang,
        'voice': voice,
        'format': 'oggopus',
        'folderId': folder,
        'speed': speed,
    }
    if role and role.lower() not in ('none', '0', 'false'):
        form['emotion'] = role

    req = urllib.request.Request(
        TTS_URL_V1,
        data=urllib.parse.urlencode(form).encode('utf-8'),
        method='POST',
        headers={**auth, 'Content-Type': 'application/x-www-form-urlencoded'},
    )
    timeout = int(os.environ.get('VC_YANDEX_TTS_TIMEOUT', '25'))
    with _urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _decode_v3_audio(payload: dict) -> bytes:
    out = bytearray()
    result = payload.get('result') or {}
    chunk = result.get('audioChunk') or {}
    data = chunk.get('data')
    if data:
        out.extend(base64.b64decode(data))
    for item in payload.get('audioChunks') or []:
        data = (item or {}).get('data')
        if data:
            out.extend(base64.b64decode(data))
    return bytes(out)


def _synthesize_v3(
    text: str,
    voice: str,
    role: str,
    lang: str,
) -> bytes:
    folder = _load_folder_id()
    auth = _auth_header()
    speed = str(os.environ.get('VC_YANDEX_TTS_SPEED', '1.0'))
    body = {
        'text': text[:4900],
        'hints': [
            {'voice': voice},
            {'role': role},
            {'speed': speed},
        ],
        'outputAudioSpec': {
            'containerAudio': {'containerAudioType': 'OGG_OPUS'},
        },
    }
    req = urllib.request.Request(
        TTS_URL_V3,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={
            **auth,
            'Content-Type': 'application/json',
            'x-folder-id': folder,
        },
    )
    timeout = int(os.environ.get('VC_YANDEX_TTS_TIMEOUT', '30'))
    with _urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parts.append(_decode_v3_audio(json.loads(line)))
        except json.JSONDecodeError:
            continue
    if parts:
        return b''.join(parts)
    return _decode_v3_audio(json.loads(raw))


def synthesize_ogg(
    text: str,
    voice: str | None = None,
    emotion: str | None = None,
    lang: str | None = None,
) -> bytes:
    text = (text or '').strip()
    if not text:
        return b''

    profile = tts_profile(get_turn_lang() if lang is None else ('kk' if lang.startswith('kk') else 'ru'))
    voice = voice or profile['voice']
    role = emotion or profile['role']
    lang = lang or profile['lang']

    api = os.environ.get('VC_YANDEX_TTS_API', 'v3').lower()
    use_v3 = api == 'v3' or voice in ('saule', 'saule_ru', 'zhanar')

    if use_v3:
        try:
            return _synthesize_v3(text, voice, role, lang)
        except (urllib.error.HTTPError, RuntimeError, json.JSONDecodeError) as e:
            if api == 'v3':
                raise
            # fallback v1 для старых голосов
            pass

    return _synthesize_v1(text, voice, role, lang)


_TTS_SYNTH = ThreadPoolExecutor(max_workers=1)


class YandexSpeechPlayer:
    """Озвучка SpeechKit — один aplay на весь ответ, keep-alive HTTP."""

    def __init__(self, voice, alsa_device, t_question=None, log_fn=None, lang=None):
        prof = tts_profile(lang or get_turn_lang())
        self.voice = voice or prof['voice']
        self.role = prof['role']
        self.lang = prof['lang']
        self.alsa = alsa_device or 'default'
        self.t_question = t_question
        self.log_fn = log_fn
        self.ttfa_logged = False
        self.ap = None
        self.ff = None
        self._play_dev = None
        self._ogg_pending = bytearray()

    def _mark_ttfa(self, lang_code=''):
        if self.ttfa_logged or self.t_question is None or not self.log_fn:
            return
        self.ttfa_logged = True
        try:
            from led_status import led_set
            led_set('speak')
        except Exception:
            pass
        self.log_fn(f'⚡ TTFA {time.time() - self.t_question:.2f}s (Yandex {lang_code or self.lang})')

    def _start_ogg_pipeline(self, dev: str) -> bool:
        if self.ff is not None and self.ff.poll() is None:
            return True
        self.ff = subprocess.Popen(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'fatal',
                '-probesize', '32768', '-analyzeduration', '500000',
                '-f', 'ogg', '-i', 'pipe:0',
                '-f', 's16le', '-ar', '44100', '-ac', '2', 'pipe:1',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.ap = subprocess.Popen(
            [
                'aplay', '-q', '-D', dev,
                '-f', 'S16_LE', '-r', '44100', '-c', '2',
                '--period-size=1024', '--buffer-size=8192',
            ],
            stdin=self.ff.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ff.stdout.close()
        self._play_dev = dev
        return True

    def feed_ogg(self, ogg: bytes, lang_code: str = '', *, mark: bool = True) -> bool:
        """Поток OGG-чанков (gRPC/REST) — один ffmpeg на весь ответ."""
        min_chunk = int(os.environ.get('VC_OGG_MIN_CHUNK', '48'))
        if not ogg:
            return False
        if self.ff is None or self.ff.poll() is not None:
            self._ogg_pending.extend(ogg)
            if len(self._ogg_pending) < min_chunk:
                return False
            ogg = bytes(self._ogg_pending)
            self._ogg_pending.clear()
        try:
            from audio_route import ensure_speaker_route
            ensure_speaker_route(force=True)
        except ImportError:
            pass
        alsa = self.alsa or 'default'
        if self.ff is None or self.ff.poll() is not None:
            self.ff = None
            self.ap = None
            started = False
            for dev in (alsa, 'speaker' if alsa != 'speaker' else 'default'):
                if self._start_ogg_pipeline(dev):
                    started = True
                    break
            if not started:
                return False
            if mark:
                self._mark_ttfa(lang_code or self.lang)
            try:
                self.ff.stdin.write(ogg)
                self.ff.stdin.flush()
                return True
            except (BrokenPipeError, OSError) as exc:
                if self.log_fn:
                    self.log_fn(f'TTS ogg pipe: {exc}')
                return False
        if mark and not self.ttfa_logged:
            self._mark_ttfa(lang_code or self.lang)
        try:
            self.ff.stdin.write(ogg)
            self.ff.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as exc:
            if self.log_fn:
                self.log_fn(f'TTS ogg pipe: {exc}')
            return False

    def prefetch_text(self, text: str):
        """Синтез Yandex в фоне, пока LLM дописывает хвост."""
        text = (text or '').strip()
        if not text:
            return None
        prof = tts_profile(get_turn_lang())
        return _TTS_SYNTH.submit(
            synthesize_ogg,
            text,
            prof['voice'],
            prof['role'],
            prof['lang'],
        )

    def feed_prefetched(self, future) -> bool:
        if future is None:
            return False
        try:
            ogg = future.result(timeout=int(os.environ.get('VC_YANDEX_TTS_TIMEOUT', '15')))
        except Exception as exc:
            if self.log_fn:
                self.log_fn(f'TTS prefetch: {exc}')
            return False
        prof = tts_profile(get_turn_lang())
        return self.feed_ogg(ogg, prof['lang'])

    def feed_text(self, text: str):
        text = (text or '').strip()
        if not text:
            return
        reply_lang = get_turn_lang()
        prof = tts_profile(reply_lang)
        try:
            from yandex_tts_stream import iter_synthesize_ogg
        except ImportError:
            iter_synthesize_ogg = None
        try:
            from audio_route import ensure_speaker_route
            ensure_speaker_route(force=True)
        except ImportError:
            pass
        if iter_synthesize_ogg and os.environ.get('VC_YANDEX_TTS_STREAM', '1').lower() not in ('0', 'false', 'no'):
            played = False
            for ogg in iter_synthesize_ogg(text, prof['voice'], prof['role'], prof['lang']):
                if self.feed_ogg(ogg, prof['lang'], mark=not played):
                    played = True
            if played:
                return
        ogg = synthesize_ogg(text, voice=prof['voice'], emotion=prof['role'], lang=prof['lang'])
        if not ogg:
            if self.log_fn:
                self.log_fn('TTS: пустой ogg от SpeechKit')
            return
        self.feed_ogg(ogg, prof['lang'])

    def close(self):
        if self._ogg_pending and self.ff and self.ff.stdin:
            try:
                self.ff.stdin.write(bytes(self._ogg_pending))
                self._ogg_pending.clear()
            except OSError:
                pass
        if self.ff and self.ff.stdin:
            try:
                self.ff.stdin.close()
            except OSError:
                pass
        if self.ap:
            try:
                self.ap.wait(timeout=90)
            except subprocess.TimeoutExpired:
                self.ap.kill()
        if self.ff:
            try:
                self.ff.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.ff.kill()
        self.ff = None
        self.ap = None


def speak_yandex(text, voice=None, alsa_device='default', t_question=None, log_fn=None):
    player = YandexSpeechPlayer(voice, alsa_device, t_question, log_fn)
    try:
        player.feed_text(text)
    finally:
        player.close()


def speak_yandex_stream_reply(text, alsa_device='default', t_question=None, log_fn=None):
    """Потоковая озвучка — первый звук до полного синтеза."""
    text = (text or '').strip()
    if not text:
        return
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=False)
    except ImportError:
        pass
    try:
        from yandex_tts_stream import iter_synthesize_ogg
    except ImportError:
        speak_yandex_short(text, alsa_device=alsa_device, t_question=t_question, log_fn=log_fn)
        return
    prof = tts_profile(get_turn_lang())
    player = YandexSpeechPlayer(None, alsa_device, t_question, log_fn)
    try:
        for ogg in iter_synthesize_ogg(text, prof['voice'], prof['role'], prof['lang']):
            player.feed_ogg(ogg, prof['lang'])
    finally:
        player.close()


def speak_yandex_short(text, alsa_device='default', t_question=None, log_fn=None):
    """Короткий ответ на команду: один REST + aplay (без gRPC-стрима)."""
    text = (text or '').strip()
    if not text:
        return
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=True)
    except ImportError:
        pass
    prof = tts_profile(get_turn_lang())
    t_api = time.time()
    ogg = synthesize_ogg(text, voice=prof['voice'], emotion=prof['role'], lang=prof['lang'])
    if not ogg:
        if log_fn:
            log_fn('TTS cmd: пустой ogg')
        return
    if log_fn:
        log_fn(f'⚡ TTS команда ({time.time() - t_api:.1f}s synth): {text[:50]}')
        if t_question is not None:
            log_fn(f'⚡ TTFA cmd {time.time() - t_question:.2f}s')
    alsa = alsa_device or 'default'
    for dev in (alsa, 'speaker' if alsa != 'speaker' else 'default'):
        ff = subprocess.Popen(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', 'pipe:0', '-f', 's16le', '-ar', '44100', '-ac', '2', 'pipe:1',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        ap = subprocess.Popen(
            ['aplay', '-q', '-D', dev, '-f', 'S16_LE', '-r', '44100', '-c', '2'],
            stdin=ff.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            ff.stdin.write(ogg)
            ff.stdin.close()
            ap.wait(timeout=max(25, len(text) // 6 + 20))
            ff.wait(timeout=5)
            if ap.returncode == 0:
                return
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired) as exc:
            if log_fn:
                log_fn(f'TTS cmd play: {exc}')
        finally:
            for p in (ap, ff):
                if p.poll() is None:
                    p.kill()


def warm_tts(log_fn=None):
    try:
        from yandex_tts_stream import iter_synthesize_ogg, _grpc_available
        if log_fn and _grpc_available():
            log_fn('Yandex TTS gRPC stream OK')
        for code in ('ru-RU', 'kk-KZ'):
            lang = 'kk' if code.startswith('kk') else 'ru'
            prof = tts_profile(lang)
            for _ in iter_synthesize_ogg('ок', prof['voice'], prof['role'], prof['lang']):
                break
        if log_fn:
            log_fn('Прогрев Yandex TTS (ru+kk) OK')
        try:
            from command_fast import warm_command_cache
            warm_command_cache(log_fn)
        except ImportError:
            pass
    except Exception as exc:
        if log_fn:
            log_fn(f'Прогрев Yandex TTS: {exc}')
