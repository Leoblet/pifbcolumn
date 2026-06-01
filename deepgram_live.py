# -*- coding: utf-8 -*-
"""Deepgram Live STT — WebSocket, PCM 16 kHz linear16 во время записи."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse

from deepgram_stt import _keyterms, _load_api_key, _resolve_language, credentials_ok

_WS_OK = False
try:
    import websocket  # websocket-client

    _WS_OK = True
except ImportError:
    websocket = None  # type: ignore


def live_available() -> bool:
    return credentials_ok() and _WS_OK


def _listen_url() -> str:
    lang = _resolve_language()
    model = os.environ.get('VC_DEEPGRAM_MODEL', 'nova-3')
    endpointing = os.environ.get('VC_DEEPGRAM_ENDPOINTING', '300')
    utterance_end = max(1000, int(os.environ.get('VC_DEEPGRAM_UTTERANCE_END', '1000')))
    params = [
        ('model', model),
        ('encoding', 'linear16'),
        ('sample_rate', '16000'),
        ('channels', '1'),
        ('punctuate', 'true'),
        ('smart_format', 'true'),
        ('no_delay', 'true'),
        ('interim_results', 'true'),
        ('endpointing', endpointing),
        ('utterance_end_ms', str(utterance_end)),
        ('vad_events', 'true'),
    ]
    if lang == 'multi':
        params.append(('language', 'multi'))
    else:
        params.append(('language', lang))
    if os.environ.get('VC_DEEPGRAM_LIVE_KEYTERMS', '1').lower() not in ('0', 'false', 'no'):
        for term in _keyterms()[:8]:
            params.append(('keyterm', term))
    return f'wss://api.deepgram.com/v1/listen?{urllib.parse.urlencode(params)}'


class DeepgramLiveSTT:
    """Потоковая отправка PCM → финальный transcript после CloseStream."""

    def __init__(self, log_fn=None):
        self.log_fn = log_fn
        self._ws = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._opened = False
        self._closed = False
        self._done = threading.Event()
        self._err: str | None = None
        self._final_parts: list[str] = []
        self._last_text = ''
        self._last_conf = 0.0
        self._speech_final_text = ''
        self._close_sent = False

    def open(self) -> bool:
        if not live_available():
            return False
        key = _load_api_key()
        try:
            self._ws = websocket.create_connection(
                _listen_url(),
                header=[f'Authorization: Token {key}'],
                timeout=int(os.environ.get('VC_DEEPGRAM_CONNECT_TIMEOUT', '8')),
            )
            self._ws.settimeout(0.4)
        except Exception as exc:
            if self.log_fn:
                self.log_fn(f'Deepgram live connect: {exc}')
                try:
                    ue = max(1000, int(os.environ.get('VC_DEEPGRAM_UTTERANCE_END', '1000')))
                    self.log_fn(
                        f'Deepgram live params: model={os.environ.get("VC_DEEPGRAM_MODEL", "nova-3")} '
                        f'lang={_resolve_language()} endpointing={os.environ.get("VC_DEEPGRAM_ENDPOINTING", "300")} '
                        f'utterance_end_ms={ue}'
                    )
                except Exception:
                    pass
            self._ws = None
            return False

        self._opened = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        return True

    def _recv_loop(self):
        try:
            while self._ws and not self._closed:
                try:
                    raw = self._ws.recv()
                except Exception as exc:
                    name = type(exc).__name__
                    if self._closed or name in ('WebSocketConnectionClosedException', 'ConnectionResetError'):
                        break
                    if name == 'WebSocketTimeoutException':
                        continue
                    self._err = str(exc)
                    break

                if isinstance(raw, bytes):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    self._handle(data)
                except Exception as exc:
                    self._err = str(exc)
                    if self.log_fn:
                        self.log_fn(f'Deepgram live parse: {exc}')
        finally:
            self._done.set()

    @staticmethod
    def _alternatives(data: dict) -> list:
        ch = data.get('channel')
        if isinstance(ch, dict):
            return ch.get('alternatives') or []
        if isinstance(ch, list):
            alts = []
            for item in ch:
                if isinstance(item, dict):
                    alts.extend(item.get('alternatives') or [])
            if alts:
                return alts
        # UtteranceEnd / служебные: channel=[0,1] без alternatives
        return []

    def _handle(self, data: dict):
        msg_type = data.get('type') or ''
        if msg_type == 'Metadata':
            if self._close_sent:
                self._done.set()
            return
        if msg_type in ('SpeechStarted', 'UtteranceEnd'):
            return
        if msg_type == 'Error':
            self._err = data.get('message') or data.get('description') or 'Deepgram error'
            self._done.set()
            return

        alts = self._alternatives(data)
        if not alts:
            return
        alt = alts[0] if isinstance(alts[0], dict) else {}
        text = (alt.get('transcript') or '').strip()
        conf = float(alt.get('confidence') or 0.0)
        if not text:
            return

        with self._lock:
            self._last_text = text
            self._last_conf = max(self._last_conf, conf)
            if data.get('is_final'):
                if not self._final_parts or self._final_parts[-1] != text:
                    self._final_parts.append(text)
            if data.get('speech_final'):
                self._speech_final_text = text

    def get_interim(self) -> str:
        with self._lock:
            return self._last_text

    def phrase_looks_incomplete(self) -> bool:
        """Interim без speech_final — даём договорить."""
        with self._lock:
            if self._speech_final_text:
                return False
            text = self._last_text.strip()
            if not text:
                return False
            words = text.split()
            if len(words) >= 4:
                return True
            if text[-1] not in '.!?…':
                return len(text) >= 12
            return False

    def send_pcm(self, chunk: bytes):
        if not chunk or not self._ws or self._closed:
            return
        try:
            self._ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as exc:
            self._err = str(exc)
            if self.log_fn:
                self.log_fn(f'Deepgram live send: {exc}')

    def finish(self, timeout: float | None = None) -> tuple[str, float]:
        """CloseStream → дождаться финального transcript."""
        if not self._opened:
            return '', 0.0
        timeout = timeout if timeout is not None else float(os.environ.get('VC_DEEPGRAM_LIVE_FINISH', '2.5'))
        if self._ws and not self._closed:
            try:
                self._close_sent = True
                self._ws.send(json.dumps({'type': 'CloseStream'}))
            except Exception as exc:
                self._err = str(exc)
            deadline = time.time() + timeout
            while time.time() < deadline and not self._done.is_set():
                time.sleep(0.03)
        self._close_ws()
        text, conf = self.get_result()
        if text:
            try:
                from lang_context import detect_lang, set_turn_lang
                set_turn_lang(detect_lang(text))
            except ImportError:
                pass
        return text, conf

    def abort(self):
        self._close_ws()

    def _close_ws(self):
        self._closed = True
        self._done.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def has_result(self) -> bool:
        text, _ = self.get_result()
        return bool(text)

    def get_result(self) -> tuple[str, float]:
        with self._lock:
            if self._speech_final_text:
                return self._speech_final_text, self._last_conf
            if self._final_parts:
                return ' '.join(self._final_parts).strip(), self._last_conf
            return self._last_text, self._last_conf
