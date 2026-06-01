# -*- coding: utf-8 -*-
"""Yandex TTS v3 — REST NDJSON stream + gRPC StreamSynthesis."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Callable, Iterator

import urllib.request

from yandex_speechkit import (
    _auth_header,
    _decode_v3_audio,
    _load_folder_id,
    _urlopen,
    tts_profile,
)
from lang_context import get_turn_lang

TTS_URL_V3 = 'https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis'
TTS_GRPC_HOST = 'tts.api.cloud.yandex.net:443'


def _v3_request_body(text: str, voice: str, role: str, speed: str) -> dict:
    return {
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


def iter_synthesize_v3_ogg(
    text: str,
    voice: str,
    role: str,
    lang: str,
) -> Iterator[bytes]:
    """REST utteranceSynthesis — чанки OGG по мере прихода (server stream NDJSON)."""
    folder = _load_folder_id()
    auth = _auth_header()
    if not auth or not folder:
        return
    speed = str(os.environ.get('VC_YANDEX_TTS_SPEED', '1.0'))
    body = json.dumps(_v3_request_body(text, voice, role, speed), ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        TTS_URL_V3,
        data=body,
        method='POST',
        headers={
            **auth,
            'Content-Type': 'application/json',
            'x-folder-id': folder,
        },
    )
    timeout = int(os.environ.get('VC_YANDEX_TTS_TIMEOUT', '20'))
    with _urlopen(req, timeout=timeout) as resp:
        line_buf = b''
        while True:
            chunk = resp.read(2048)
            if not chunk:
                break
            line_buf += chunk
            while b'\n' in line_buf:
                raw_line, line_buf = line_buf.split(b'\n', 1)
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line.decode('utf-8'))
                except json.JSONDecodeError:
                    continue
                ogg = _decode_v3_audio(payload)
                if ogg:
                    yield ogg
        tail = line_buf.strip()
        if tail:
            try:
                ogg = _decode_v3_audio(json.loads(tail.decode('utf-8')))
                if ogg:
                    yield ogg
            except json.JSONDecodeError:
                pass


def _grpc_available() -> bool:
    if os.environ.get('VC_YANDEX_TTS_GRPC', '1').lower() in ('0', 'false', 'no'):
        return False
    try:
        import grpc  # noqa: F401
        from yandex_grpc import stream_tts_pb2  # noqa: F401
        return True
    except ImportError:
        return False


def _grpc_metadata() -> list[tuple[str, str]]:
    from yandex_speechkit import _load_api_key, _load_iam_token

    folder = _load_folder_id()
    iam = _load_iam_token()
    key = _load_api_key()
    meta = []
    if iam:
        meta.append(('authorization', f'Bearer {iam}'))
    elif key:
        meta.append(('authorization', f'Api-Key {key}'))
    if folder:
        meta.append(('x-folder-id', folder))
    return meta


def iter_synthesize_grpc_ogg(
    text: str,
    voice: str,
    role: str,
    lang: str,
) -> Iterator[bytes]:
    """gRPC StreamSynthesis — один текст, поток audio_chunk."""
    import grpc
    from yandex_grpc import stream_tts_pb2, stream_tts_pb2_grpc

    speed = float(os.environ.get('VC_YANDEX_TTS_SPEED', '1.0'))
    opts = stream_tts_pb2.SynthesisOptions(
        voice=voice,
        role=role or '',
        speed=speed,
        output_audio_spec=stream_tts_pb2.AudioFormatOptions(
            container_audio=stream_tts_pb2.ContainerAudio(
                container_audio_type=stream_tts_pb2.ContainerAudio.OGG_OPUS,
            ),
        ),
    )

    def _requests():
        yield stream_tts_pb2.StreamSynthesisRequest(options=opts)
        yield stream_tts_pb2.StreamSynthesisRequest(
            synthesis_input=stream_tts_pb2.SynthesisInput(text=text[:4900]),
        )
        yield stream_tts_pb2.StreamSynthesisRequest(
            force_synthesis=stream_tts_pb2.ForceSynthesisEvent(),
        )

    creds = grpc.ssl_channel_credentials()
    channel = grpc.secure_channel(TTS_GRPC_HOST, creds)
    stub = stream_tts_pb2_grpc.SynthesizerStub(channel)
    for resp in stub.StreamSynthesis(_requests(), metadata=_grpc_metadata(), timeout=timeout_grpc()):
        if resp.audio_chunk and resp.audio_chunk.data:
            yield bytes(resp.audio_chunk.data)
    channel.close()


def timeout_grpc() -> int:
    return int(os.environ.get('VC_YANDEX_TTS_TIMEOUT', '20'))


def iter_synthesize_ogg(text: str, voice: str | None = None, role: str | None = None, lang: str | None = None):
    """Лучший доступный стрим: gRPC → REST NDJSON."""
    text = (text or '').strip()
    if not text:
        return
    prof = tts_profile(get_turn_lang() if lang is None else ('kk' if str(lang).startswith('kk') else 'ru'))
    voice = voice or prof['voice']
    role = role or prof['role']
    lang = lang or prof['lang']
    if _grpc_available():
        try:
            yield from iter_synthesize_grpc_ogg(text, voice, role, lang)
            return
        except Exception:
            pass
    yield from iter_synthesize_v3_ogg(text, voice, role, lang)


class YandexLlmStreamSynth:
    """gRPC StreamSynthesis + LLM: текст кусками → аудио потоком."""

    def __init__(self, voice, role, lang, on_ogg: Callable[[bytes], None], log_fn=None):
        self.voice = voice
        self.role = role
        self.lang = lang
        self.on_ogg = on_ogg
        self.log_fn = log_fn
        self._q: queue.Queue = queue.Queue()
        self._done = threading.Event()
        self._err: Exception | None = None
        self._thread: threading.Thread | None = None
        self._stub = None
        self._request_q: queue.Queue | None = None

    def _request_iter(self):
        import grpc
        from yandex_grpc import stream_tts_pb2

        speed = float(os.environ.get('VC_YANDEX_TTS_SPEED', '1.0'))
        yield stream_tts_pb2.StreamSynthesisRequest(
            options=stream_tts_pb2.SynthesisOptions(
                voice=self.voice,
                role=self.role or '',
                speed=speed,
                output_audio_spec=stream_tts_pb2.AudioFormatOptions(
                    container_audio=stream_tts_pb2.ContainerAudio(
                        container_audio_type=stream_tts_pb2.ContainerAudio.OGG_OPUS,
                    ),
                ),
            ),
        )
        while True:
            item = self._request_q.get()
            if item is None:
                break
            if item == 'force':
                yield stream_tts_pb2.StreamSynthesisRequest(
                    force_synthesis=stream_tts_pb2.ForceSynthesisEvent(),
                )
            elif isinstance(item, str) and item:
                yield stream_tts_pb2.StreamSynthesisRequest(
                    synthesis_input=stream_tts_pb2.SynthesisInput(text=item[:4900]),
                )

    def _run(self):
        import grpc
        from yandex_grpc import stream_tts_pb2_grpc

        try:
            creds = grpc.ssl_channel_credentials()
            channel = grpc.secure_channel(TTS_GRPC_HOST, creds)
            stub = stream_tts_pb2_grpc.SynthesizerStub(channel)
            for resp in stub.StreamSynthesis(
                self._request_iter(),
                metadata=_grpc_metadata(),
                timeout=timeout_grpc(),
            ):
                if resp.audio_chunk and resp.audio_chunk.data:
                    self.on_ogg(bytes(resp.audio_chunk.data))
            channel.close()
        except Exception as exc:
            self._err = exc
            if self.log_fn:
                self.log_fn(f'gRPC TTS stream: {exc}')
        finally:
            self._done.set()

    def start(self) -> bool:
        if not _grpc_available():
            return False
        self._request_q = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def send_text(self, text: str):
        if self._request_q is not None and text:
            self._request_q.put(text)

    def flush(self):
        if self._request_q is not None:
            self._request_q.put('force')

    def finish(self):
        if self._request_q is not None:
            self._request_q.put(None)
        if self._thread:
            self._thread.join(timeout=timeout_grpc() + 5)
        if self._err:
            raise self._err
