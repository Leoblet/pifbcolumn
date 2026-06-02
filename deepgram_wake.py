# -*- coding: utf-8 -*-
"""Deepgram Wake Word — постоянный стриминг, детекция вейк-ворда."""
from __future__ import annotations
import json, os, re, threading, time, urllib.parse
import pyaudio

try:
    import websocket
    _WS_OK = True
except ImportError:
    _WS_OK = False

RATE = 16000
CHUNK = int(os.environ.get('VC_DG_WAKE_CHUNK', '512'))

def _norm(t: str) -> str:
    return re.sub(r'\s+', ' ', (t or '').lower().strip())

def _dg_url(lang='ru') -> str:
    model = os.environ.get('VC_DEEPGRAM_MODEL', 'nova-3')
    p = [('model',model),('encoding','linear16'),('sample_rate',str(RATE)),
         ('channels','1'),('interim_results','true'),('endpointing','50'),
         ('vad_events','true'),('language',lang)]
    return f'wss://api.deepgram.com/v1/listen?{urllib.parse.urlencode(p)}'


class DeepgramWakeDetector:
    def __init__(self, wake_phrases, on_wake, mic_index=None, lang='ru', log_fn=None):
        self._phrases = [p.lower().strip() for p in wake_phrases if p.strip()]
        self.on_wake = on_wake
        self.mic_index = mic_index
        self.lang = lang
        self.log = log_fn or (lambda m: print(f'[dg_wake] {m}', flush=True))
        self._running = False
        self._triggered = False
        self._paused = False
        self._connected = threading.Event()
        self._api_key = os.environ.get('VC_DEEPGRAM_API_KEY', '')

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False

    def pause(self):
        """Остановить микрофон на время команды."""
        self._paused = True
        self._connected.clear()

    def resume(self):
        self._triggered = False
        self._paused = False
        self._connected.clear()

    def _check_wake(self, text):
        if self._triggered or not text:
            return
        n = _norm(text)
        for phrase in self._phrases:
            if phrase in n:
                self._triggered = True
                trailing = n.split(phrase, 1)[-1].strip(' .,!?')
                self.log(f'Wake: {text!r} → {trailing!r}')
                self.on_wake(phrase, trailing)
                return

    def _run(self):
        self.log('Запуск...')
        pa = pyaudio.PyAudio()

        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue
            self._connected.clear()
            stream = None
            ws = None
            try:
                stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                                  input=True, input_device_index=self.mic_index,
                                  frames_per_buffer=CHUNK)

                def on_open(w):
                    self._connected.set()
                    self.log('Слушаю (Deepgram connected)...')

                def on_msg(w, msg):
                    try:
                        d = json.loads(msg)
                        if not isinstance(d, dict) or d.get('type') not in ('Results', None):
                            return
                        alts = d.get('channel', {}).get('alternatives', [])
                        if alts:
                            self._check_wake(alts[0].get('transcript', ''))
                    except Exception:
                        pass

                def on_err(w, e):
                    self.log(f'WS err: {e}')

                def on_close(w, code, msg):
                    self._connected.clear()
                    self.log(f'WS closed: {code}')

                ws = websocket.WebSocketApp(
                    _dg_url(self.lang),
                    header=[f'Authorization: Token {self._api_key}'],
                    on_open=on_open,
                    on_message=on_msg,
                    on_error=on_err,
                    on_close=on_close,
                )
                wst = threading.Thread(target=ws.run_forever,
                                       kwargs={'ping_interval':20,'ping_timeout':10},
                                       daemon=True)
                wst.start()

                # Ждём подключения макс 5 сек
                if not self._connected.wait(timeout=5):
                    self.log('Timeout подключения, retry...')
                    ws.close()
                    time.sleep(2)
                    continue

                # Шлём аудио пока подключены
                while self._running and self._connected.is_set():
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    try:
                        ws.send_bytes(data)
                    except Exception:
                        break

                ws.close()
                wst.join(timeout=2)

            except Exception as e:
                self.log(f'Ошибка: {e}')
            finally:
                if stream:
                    try: stream.stop_stream(); stream.close()
                    except Exception: pass
            time.sleep(1)

        pa.terminate()
        self.log('Остановлен')