# -*- coding: utf-8 -*-
"""Стриминг OpenRouter + edge-TTS для TTFA ~2.5 с."""

import json
import os
import re
import time
import subprocess
import urllib.error
import urllib.request

from fast_llm import build_system_prompt, _load_openrouter_key, llm_temperature, needs_web_search

SENTENCE_BREAK = re.compile(r'[.!?,;:\n]')  # noqa: F401 — legacy
_SPACE_PUNCT = re.compile(r'\s+([!.?,:;])')
_HTTP = None


def _clean_speech(text):
    t = (text or '').strip()
    t = _SPACE_PUNCT.sub(r'\1', t)
    return t


def _http_opener():
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib.request.build_opener(urllib.request.HTTPSHandler())
    return _HTTP


def stream_openrouter_deltas(prompt, memory=None, max_tokens=None, *, mode='voice'):
    key = _load_openrouter_key()
    if not key:
        return

    model = os.environ.get('VC_LLM_MODEL', 'google/gemini-2.0-flash-001')
    if max_tokens is None:
        max_tokens = _llm_max_tokens(prompt, mode=mode)
    timeout = int(os.environ.get('VC_LLM_TIMEOUT', '15'))

    if memory is not None:
        messages = memory.build_messages(prompt, mode=mode)
    else:
        messages = [
            {'role': 'system', 'content': build_system_prompt(prompt, mode=mode)},
            {'role': 'user', 'content': prompt},
        ]

    body = json.dumps(
        {
            'model': model,
            'max_tokens': max_tokens,
            'temperature': llm_temperature(),
            'stream': True,
            'messages': messages,
        },
        ensure_ascii=False,
    ).encode('utf-8')

    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=body,
        method='POST',
    )
    req.add_header('Authorization', f'Bearer {key}')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    req.add_header('Connection', 'keep-alive')
    req.add_header('HTTP-Referer', 'http://voice-column.local/')
    req.add_header('X-Title', 'Voice Column')

    try:
        with _http_opener().open(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if not payload or payload == '[DONE]':
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = data.get('choices') or []
                if not choices:
                    continue
                delta = choices[0].get('delta') or {}
                piece = delta.get('content') or ''
                if piece:
                    yield piece
    except (urllib.error.URLError, TimeoutError, OSError):
        return



class EdgeStreamPlayer:
    """Один ffmpeg+aplay на весь ответ — без underrun между фразами."""

    def __init__(self, voice, alsa_device, t_question=None, log_fn=None):
        self.voice = voice
        self.alsa = alsa_device or 'default'
        self.t_question = t_question
        self.log_fn = log_fn
        self.ttfa_logged = False
        self.ff = None
        self.ap = None
        self._pending = b''
        self._primed = False
        self._min_mp3 = int(os.environ.get('VC_TTS_MP3_PRIME', '2048'))

    def _start_pipeline(self):
        if self.ff is not None:
            return
        self.ff = subprocess.Popen(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-probesize', '32', '-analyzeduration', '0',
                '-i', 'pipe:0', '-f', 's16le', '-ar', '44100', '-ac', '2', 'pipe:1',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        self.ap = subprocess.Popen(
            [
                'aplay', '-q', '-D', self.alsa,
                '-f', 'S16_LE', '-r', '44100', '-c', '2',
                '--period-size=1024', '--buffer-size=16384',
            ],
            stdin=self.ff.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ff.stdout.close()

    def _mark_ttfa(self):
        if self.ttfa_logged or self.t_question is None or not self.log_fn:
            return
        self.ttfa_logged = True
        try:
            from led_status import led_set
            led_set('speak')
        except Exception:
            pass
        self.log_fn(f'⚡ TTFA {time.time() - self.t_question:.2f}s')

    def _write_mp3(self, data):
        if not data:
            return
        if not self._primed:
            self._pending += data
            if len(self._pending) < self._min_mp3:
                return
            data = self._pending
            self._pending = b''
            self._primed = True
            self._start_pipeline()
            self._mark_ttfa()
            self.ff.stdin.write(data)
            self.ff.stdin.flush()
            return
        self._start_pipeline()
        self.ff.stdin.write(data)
        self.ff.stdin.flush()

    def feed_text(self, text):
        text = _clean_speech(text)
        if not text:
            return
        import edge_tts

        comm = edge_tts.Communicate(text, self.voice)
        for chunk in comm.stream_sync():
            if chunk.get('type') != 'audio':
                continue
            self._write_mp3(chunk.get('data') or b'')

    def close(self):
        if self._pending and not self._primed:
            self._primed = True
            self._start_pipeline()
            self._mark_ttfa()
            self.ff.stdin.write(self._pending)
            self._pending = b''
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


def speak_edge_stream(text, voice, alsa_device, t_question=None, log_fn=None):
    player = EdgeStreamPlayer(voice, alsa_device, t_question, log_fn)
    try:
        player.feed_text(text)
    finally:
        player.close()


def _make_tts_player(voice, alsa_device, t_question, log_fn):
    if os.environ.get('VC_TTS_ENGINE', 'edge').lower() == 'yandex':
        try:
            from yandex_speechkit import credentials_ok, YandexSpeechPlayer
            if credentials_ok():
                return YandexSpeechPlayer(voice, alsa_device, t_question, log_fn)
        except ImportError:
            pass
    return EdgeStreamPlayer(voice, alsa_device, t_question, log_fn)


_GREETING = re.compile(
    r'^(?:привет|здравствуй|добрый|как\s+дела|как\s+ты|что\s+нового|салем|сәлем)[!.?\s]*',
    re.I,
)


def _llm_max_tokens(prompt: str, *, mode: str = 'voice') -> int:
    if mode == 'long':
        return int(os.environ.get('VC_LLM_LONG_MAX_TOKENS', os.environ.get('VC_LLM_DUAL_MAX_TOKENS', '480')))
    if mode == 'chat':
        return int(os.environ.get('VC_LLM_CHAT_MAX_TOKENS', '320'))
    base = int(os.environ.get('VC_LLM_MAX_TOKENS', '70'))
    if _GREETING.match((prompt or '').strip()):
        return min(base, int(os.environ.get('VC_LLM_GREETING_TOKENS', '40')))
    return base


def _find_early_tts_cut(buf: str) -> int:
    """Legacy — см. _next_tts_push."""
    return _next_tts_push(buf, 0)


def _next_tts_push(buf: str, spoken: int) -> int:
    """Индекс в buf: до куда уже можно flush в Yandex (первые слова / предложение)."""
    pending = buf[spoken:]
    if not pending.strip():
        return spoken

    min_chars = int(os.environ.get('VC_STREAM_MIN_CHARS', '5'))
    first_words = int(os.environ.get('VC_STREAM_FIRST_WORDS', '3'))

    # Законченное предложение
    m = re.search(r'[.!?…](?:\s|$|[""»])', pending)
    if m and m.end() >= min_chars:
        return spoken + m.end()

    # Первый push: N слов (не режем по одному слову через FIRST_CHARS)
    if spoken == 0:
        words = re.findall(r'\S+', pending)
        if len(words) >= first_words:
            pos = 0
            for w in words[:first_words]:
                idx = pending.find(w, pos)
                if idx < 0:
                    break
                pos = idx + len(w)
            else:
                return spoken + pos

    # Хвост — только по предложениям
    if spoken > 0:
        m = re.search(r'[.!?…](?:\s|$|[""»])', pending)
        if m and m.end() >= 4:
            return spoken + m.end()

    return spoken


def _pipe_llm_yandex_grpc(prompt, voice, alsa_device, t_question, log_fn, memory=None, *, mode='voice'):
    """
    LLM stream → Yandex gRPC StreamSynthesis.
    Как только 2–3 слова или предложение — force_synthesis (не ждём весь текст).
    """
    try:
        from yandex_tts_stream import YandexLlmStreamSynth, _grpc_available
        from yandex_speechkit import YandexSpeechPlayer, credentials_ok
        from lang_context import get_turn_lang, tts_profile
    except ImportError:
        return None
    if not credentials_ok() or not _grpc_available():
        return None

    try:
        from led_status import led_set
        led_set('think')
    except Exception:
        pass

    prof = tts_profile(get_turn_lang())
    player = YandexSpeechPlayer(voice, alsa_device, t_question, log_fn)
    synth = YandexLlmStreamSynth(
        prof['voice'],
        prof['role'],
        prof['lang'],
        on_ogg=lambda ogg: player.feed_ogg(ogg, prof['lang']),
        log_fn=log_fn,
    )
    if not synth.start():
        player.close()
        return None

    buf = ''
    spoken = 0
    t_llm = time.time()
    max_tokens = _llm_max_tokens(prompt, mode=mode)
    try:
        for delta in stream_openrouter_deltas(prompt, memory=memory, max_tokens=max_tokens, mode=mode):
            buf += delta
            synth.send_text(delta)
            while True:
                new_spoken = _next_tts_push(buf, spoken)
                if new_spoken <= spoken:
                    break
                chunk = _clean_speech(buf[spoken:new_spoken])
                synth.flush()
                if log_fn:
                    tag = '⚡ push' if spoken == 0 else '↳ push'
                    log_fn(f'{tag} ({time.time() - t_llm:.2f}s): {chunk[:70]!r}')
                spoken = new_spoken

        reply = _clean_speech(buf)
        if not reply:
            return ''
        if spoken < len(buf):
            synth.flush()
        synth.finish()
        return reply
    except Exception as exc:
        if log_fn:
            log_fn(f'LLM→Yandex pipe: {exc}')
        return None
    finally:
        player.close()


def _streaming_yandex_grpc(prompt, voice, alsa_device, t_question, log_fn, memory=None, *, mode='voice'):
    """LLM → gRPC StreamSynthesis → aplay."""
    reply = _pipe_llm_yandex_grpc(
        prompt, voice, alsa_device, t_question, log_fn, memory=memory, mode=mode,
    )
    if not reply:
        return None
    try:
        from reply_format import closing_question_suffix, ends_with_question
        if not ends_with_question(reply):
            extra = closing_question_suffix(reply, prompt)
            if extra.strip() and log_fn:
                log_fn(f'+ вопрос:{extra.strip()[:70]}')
    except ImportError:
        pass
    return reply


def streaming_assistant_speak(prompt, voice, alsa_device, t_question, log_fn, memory=None, *, mode='voice'):
    """Стрим LLM; TTS — gRPC StreamSynthesis или REST + ранний push."""
    if (
        os.environ.get('VC_YANDEX_TTS_GRPC', '1').lower() not in ('0', 'false', 'no')
        and os.environ.get('VC_TTS_ENGINE', 'edge').lower() == 'yandex'
    ):
        reply = _streaming_yandex_grpc(
            prompt, voice, alsa_device, t_question, log_fn, memory=memory, mode=mode,
        )
        if reply:
            return reply

    try:
        from led_status import led_set
        led_set('think')
    except Exception:
        pass
    buf = ''
    player = None
    spoken = 0
    t_llm = time.time()
    max_tokens = _llm_max_tokens(prompt, mode=mode)

    for delta in stream_openrouter_deltas(prompt, memory=memory, max_tokens=max_tokens, mode=mode):
        buf += delta
        while True:
            new_spoken = _next_tts_push(buf, spoken)
            if new_spoken <= spoken:
                break
            seg = _clean_speech(buf[spoken:new_spoken])
            if not seg:
                spoken = new_spoken
                continue
            player = player or _make_tts_player(voice, alsa_device, t_question, log_fn)
            if log_fn and spoken == 0:
                log_fn(f'⚡ push ({time.time() - t_llm:.2f}s): {seg[:70]!r}')
            if hasattr(player, 'feed_text'):
                player.feed_text(seg)
            spoken = new_spoken

    reply = _clean_speech(buf)
    if not reply:
        return ''

    player = player or _make_tts_player(voice, alsa_device, t_question, log_fn)
    try:
        tail = _clean_speech(buf[spoken:])
        if tail:
            player.feed_text(tail)
        elif not spoken:
            player.feed_text(reply)
    finally:
        player.close()
    return reply


def collect_llm_reply(prompt, memory=None, log_fn=None, *, mode='long'):
    """LLM целиком в текст — проверка длины перед озвучкой / Telegram."""
    if needs_web_search(prompt):
        return None
    if not _load_openrouter_key():
        return None
    buf = ''
    t0 = time.time()
    max_tokens = _llm_max_tokens(prompt, mode=mode)
    for delta in stream_openrouter_deltas(prompt, memory=memory, max_tokens=max_tokens, mode=mode):
        buf += delta
    reply = _clean_speech(buf)
    if log_fn and reply:
        log_fn(f'← LLM collect ({time.time() - t0:.1f}s, {len(reply)} chars): {reply[:100]}')
    return reply or None


def speak_reply_text(reply, voice, alsa_device, t_question, log_fn):
    """Озвучить готовый короткий ответ."""
    reply = _clean_speech(reply)
    if not reply:
        return
    try:
        from reply_format import trim_voice_reply
        reply = trim_voice_reply(reply)
    except ImportError:
        pass
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=True)
    except ImportError:
        pass
    if os.environ.get('VC_TTS_ENGINE', 'edge').lower() == 'yandex':
        try:
            from yandex_speechkit import speak_yandex_stream_reply
            speak_yandex_stream_reply(reply, alsa_device=alsa_device, t_question=t_question, log_fn=log_fn)
            return
        except ImportError:
            pass
    speak_edge_stream(reply, voice, alsa_device, t_question, log_fn)


def warm_pipeline(log_fn=None, full_tts=False):
    """Прогрев TLS + OpenRouter + edge-TTS."""
    try:
        for _ in stream_openrouter_deltas('ок'):
            break
        if log_fn:
            log_fn('Прогрев LLM OK')
    except Exception as exc:
        if log_fn:
            log_fn(f'Прогрев LLM: {exc}')
    try:
        from command_fast import warm_command_cache
        warm_command_cache(log_fn)
        from demo_scenario import enabled as demo_enabled, register_cached_phrases
        if demo_enabled():
            register_cached_phrases()
            warm_command_cache(log_fn)
            from demo_variants import warm_demo_cache as _warm_demo
            _warm_demo(log_fn)
    except ImportError:
        pass
    if full_tts:
        try:
            alsa = os.environ.get('VC_ALSA_DEVICE', 'speaker')
            voice = os.environ.get('VC_TTS_VOICE', 'ru-RU-SvetlanaNeural')
            if os.environ.get('VC_TTS_ENGINE', 'edge').lower() == 'yandex':
                from yandex_speechkit import warm_tts
                warm_tts(log_fn)
            else:
                player = EdgeStreamPlayer(voice, alsa)
                player.feed_text('ок')
                player.close()
                if log_fn:
                    log_fn('Прогрев TTS OK')
        except Exception as exc:
            if log_fn:
                log_fn(f'Прогрев TTS: {exc}')


def try_batch_reply(prompt, alsa_device, t_question, log_fn, memory=None, *, mode='voice'):
    """Короткие ответы: LLM одним запросом + REST TTS (быстрее gRPC-стрима на Pi)."""
    if needs_web_search(prompt):
        return None
    if os.environ.get('VC_REPLY_MODE', 'stream').lower() == 'stream':
        return None
    if not _load_openrouter_key():
        return None
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=False)
    except ImportError:
        pass
    from fast_llm import ask_openrouter

    t0 = time.time()
    reply = ask_openrouter(prompt, memory=memory, mode=mode)
    if not reply:
        return None
    try:
        from reply_format import trim_voice_reply
        if mode != 'chat' and mode != 'long':
            reply = trim_voice_reply(reply)
    except ImportError:
        pass
    if log_fn:
        log_fn(f'← batch LLM ({time.time() - t0:.1f}s): {reply[:120]}')

    voice = os.environ.get('VC_TTS_VOICE', 'ru-RU-SvetlanaNeural')
    if os.environ.get('VC_TTS_ENGINE', 'edge').lower() == 'yandex':
        try:
            from yandex_speechkit import speak_yandex_stream_reply
            speak_yandex_stream_reply(reply, alsa_device=alsa_device, t_question=t_question, log_fn=log_fn)
        except ImportError:
            speak_edge_stream(reply, voice, alsa_device, t_question, log_fn)
    else:
        speak_edge_stream(reply, voice, alsa_device, t_question, log_fn)

    if log_fn:
        log_fn(f'← batch total ({time.time() - t0:.1f}s)')
    return reply


def try_streaming_reply(prompt, voice, alsa_device, t_question, log_fn, memory=None, *, mode='voice'):
    if needs_web_search(prompt):
        return None
    if not _load_openrouter_key():
        return None
    try:
        from audio_route import ensure_speaker_route
        ensure_speaker_route(force=True)
    except ImportError:
        pass
    try:
        t0 = time.time()
        reply = streaming_assistant_speak(
            prompt, voice, alsa_device, t_question, log_fn, memory=memory, mode=mode,
        )
        if log_fn:
            log_fn(f'← stream LLM+TTS ({time.time() - t0:.1f}s): {(reply or "")[:120]}')
        return reply or None
    except Exception as exc:
        if log_fn:
            log_fn(f'stream pipeline: {exc}')
        return None
