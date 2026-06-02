#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Музыка: локальные mp3 + YouTube (стрим, кэш, фон + ducking при голосе)."""

from __future__ import annotations

import array
import glob
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from difflib import SequenceMatcher

MUSIC_DIR = os.environ.get('VC_MUSIC_DIR', '/home/pi/voice_column/Music_Local')
MUSIC_TIMEOUT = int(os.environ.get('VC_MUSIC_TIMEOUT', '600'))
SEARCH_TIMEOUT = int(os.environ.get('VC_MUSIC_SEARCH_TIMEOUT', '25'))
URL_TIMEOUT = int(os.environ.get('VC_MUSIC_URL_TIMEOUT', '40'))
MUSIC_STREAM = os.environ.get('VC_MUSIC_STREAM', '1').lower() not in ('0', 'false', 'no')
MUSIC_DUCK_VOL = float(os.environ.get('VC_MUSIC_DUCK_VOL', '0.05'))
MUSIC_NORMAL_VOL = float(os.environ.get('VC_MUSIC_NORMAL_VOL', '1.0'))
CACHE_FILE = os.environ.get(
    'VC_MUSIC_CACHE', '/home/pi/voice_column/music_cache.json'
)
CACHE_TTL = int(os.environ.get('VC_MUSIC_CACHE_TTL', str(7 * 86400)))

GENERIC_WORDS = (
    'музык', 'music', 'песн', 'трек', 'включи', 'поставь', 'play',
    'мою', 'слушать', 'снг', 'русск', 'играй', 'запусти',
)
_QUERY_JUNK = frozenset(
    {'ть', 'ти', 'и', 'а', 'я', 'у', 'о', 'в', 'на', 'the', 'a', 'to', 'ok', 'э', 'ну'},
)
MUSIC_CMD_RE = re.compile(
    r'(?:^|\s)(?:включи|поставь|играй|запусти|play|включить|поставить)'
    r'(?:\s+(?:музыку|песню|трек|композицию|видео))?',
    re.I,
)
STOP_MUSIC_RE = re.compile(
    r'(?:'
    r'(?:стоп|выключи|убери|останови|остановить|пауза)\s*(?:музык|песн|трек|композици)?|'
    r'(?:стоп|пауза)$'
    r')',
    re.I,
)
_VI_RE = re.compile(
    r'[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]',
    re.I,
)
_CYR_RE = re.compile(r'[а-яёА-ЯЁ]')


def _ytdlp_cmd():
    local_bin = os.path.expanduser('~/.local/bin/yt-dlp')
    if os.path.isfile(local_bin):
        return [local_bin]
    return [sys.executable, '-m', 'yt_dlp']


def log(msg):
    print(f'[music] {msg}', flush=True)


def is_stop_music_command(text: str) -> bool:
    t = (text or '').lower().strip()
    return bool(t and STOP_MUSIC_RE.search(t))


def _scale_pcm(pcm: bytes, factor: float) -> bytes:
    if factor >= 0.99 or not pcm:
        return pcm
    samples = array.array('h')
    samples.frombytes(pcm)
    for i in range(len(samples)):
        v = int(samples[i] * factor)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        samples[i] = v
    return samples.tobytes()


class MusicController:
    """Фоновое воспроизведение с приглушением, пока колонка говорит."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._duck = False
        self._thread = None
        self._title = ''

    def is_playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def title(self) -> str:
        return self._title

    def _volume(self) -> float:
        with self._lock:
            return MUSIC_DUCK_VOL if self._duck else MUSIC_NORMAL_VOL

    def duck(self):
        with self._lock:
            self._duck = True
            log('Музыка тише (фон)')

    def unduck(self):
        with self._lock:
            if self._duck:
                self._duck = False
                log('Музыка громче')

    def stop(self):
        was_playing = self.is_playing()
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=4)
        self._stop.clear()
        self._thread = None
        self._title = ''
        if was_playing:
            try:
                from demo_scenario import enabled as _dme_st, demo_stage as _dst_st
                if _dme_st():
                    _dst_st('MUSIC_STOP', 'остановлена', log_fn=lambda m: print(m, flush=True))
            except Exception:
                pass
        try:
            from led_status import led_idle_if_no_music
            led_idle_if_no_music()
        except Exception:
            pass

    def _led_music(self):
        try:
            from led_status import led_set
            led_set('music')
        except Exception:
            pass

    def _finish_playback(self):
        try:
            from led_status import led_idle_if_no_music
            led_idle_if_no_music()
        except Exception:
            pass

    def wait_until_done(self, timeout=None):
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def start_file(self, path: str, alsa_device: str, title: str):
        self.stop()
        self._stop.clear()
        self._title = title
        self._led_music()
        try:
            from demo_scenario import enabled as _dme_m, demo_stage as _dst_m
            if _dme_m():
                import os as _os
                _dst_m('MUSIC_START', f'▶ {title[:60]}', log_fn=lambda m: print(m, flush=True))
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._run_file,
            args=(path, alsa_device),
            daemon=True,
        )
        self._thread.start()

    def start_url(self, url: str, alsa_device: str, title: str):
        self.stop()
        self._stop.clear()
        self._title = title
        self._led_music()
        self._thread = threading.Thread(
            target=self._run_url,
            args=(url, alsa_device),
            daemon=True,
        )
        self._thread.start()

    def _pipe_pcm(self, ff: subprocess.Popen, alsa_device: str) -> bool:
        ap = subprocess.Popen(
            [
                'aplay', '-q', '-D', alsa_device,
                '-f', 'S16_LE', '-r', '44100', '-c', '2',
                '--period-size=512', '--buffer-size=4096',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ok = True
        try:
            while not self._stop.is_set():
                chunk = ff.stdout.read(8192)
                if not chunk:
                    break
                vol = self._volume()
                if vol < 0.99:
                    chunk = _scale_pcm(chunk, vol)

                ap.stdin.write(chunk)
            ap.stdin.close()
            ap.wait(timeout=5)
        except (BrokenPipeError, OSError) as e:
            log(f'pipe: {e}')
            ok = False
        finally:
            if ff.poll() is None:
                ff.kill()
            if ap.poll() is None:
                ap.kill()
        return ok and not self._stop.is_set()

    def _run_file(self, path: str, alsa_device: str):
        ext = os.path.splitext(path)[1].lower()
        if ext == '.mp3':
            # mpg123 стартует ~50мс vs ffmpeg ~400мс
            ff = subprocess.Popen(
                ['mpg123', '-q', '-s', '--rate', '44100', '--stereo', path],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._pipe_pcm(ff, alsa_device)
            self._finish_playback()
            return
        self._run_url(path, alsa_device)

    def _run_url(self, url: str, alsa_device: str):
        ff = subprocess.Popen(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
                '-i', url, '-vn', '-sn', '-f', 's16le', '-ar', '44100', '-ac', '2', 'pipe:1',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._pipe_pcm(ff, alsa_device)
        self._finish_playback()


_controller = None
_duck_depth = 0
_duck_lock = threading.Lock()


def get_controller() -> MusicController:
    global _controller
    if _controller is None:
        _controller = MusicController()
    return _controller


def is_music_playing() -> bool:
    return get_controller().is_playing()


def voice_duck_depth() -> int:
    with _duck_lock:
        return _duck_depth


def duck_music():
    global _duck_depth
    with _duck_lock:
        _duck_depth += 1
        if _duck_depth == 1 and is_music_playing():
            get_controller().duck()


def unduck_music():
    global _duck_depth
    with _duck_lock:
        if _duck_depth <= 0:
            return
        _duck_depth -= 1
        if _duck_depth == 0 and is_music_playing():
            get_controller().unduck()


def stop_music():
    get_controller().stop()


@contextmanager
def duck_while_voice():
    if not is_music_playing():
        yield
        return
    duck_music()
    try:
        yield
    finally:
        unduck_music()


def start_music(text: str, alsa_device: str = 'default') -> tuple[bool, str]:
    """Запуск в фоне — wake loop продолжает слушать."""
    query = extract_query(text)
    return start_music_by_query(query, alsa_device, source_text=text)


def start_music_by_query(query: str, alsa_device: str = 'default', *, source_text: str = '') -> tuple[bool, str]:
    """Запуск по готовому запросу (демо / API)."""
    query = re.sub(r'\s+', ' ', (query or '').strip())
    if not _query_usable(query):
        log(f'Музыка: слабый запрос «{query}» из «{(source_text or query)[:60]}»')
        return False, 'Не поняла, что включить. Скажи: включи и название.'
    ctrl = get_controller()

    local = _best_local(query)
    if local:
        path, name = local
        log(f'Локально (фон): {name}')
        ctrl.start_file(path, alsa_device, name)
        return True, f'Включаю {name}'

    def _yt_worker():
        if not _youtube_search_play_bg(query, alsa_device):
            log(f'Не нашла: {query}')

    threading.Thread(target=_yt_worker, daemon=True).start()
    return True, f'Ищу {query}'


def _youtube_search_play_bg(query: str, alsa_device: str = 'default') -> bool:
    log(f'Ищу: {query}')
    url, title = _resolve_youtube(query)
    if not url:
        if _youtube_download_play(query, alsa_device):
            return True
        return False
    log(f'Фон: {title[:50] if title else query}')
    get_controller().start_url(url, alsa_device, title or query)
    return True


def is_music_command(text: str) -> bool:
    t = (text or '').lower().strip()
    if not t:
        return False
    if MUSIC_CMD_RE.search(t):
        return True
    return any(w in t for w in GENERIC_WORDS)


def extract_query(text: str) -> str:
    q = (text or '').strip()
    q = MUSIC_CMD_RE.sub(' ', q)
    for w in GENERIC_WORDS:
        q = re.sub(rf'\b{re.escape(w)}\b', ' ', q, flags=re.I)
    words = [w for w in q.split() if w.lower() not in _QUERY_JUNK and len(w) >= 2]
    q = re.sub(r'\s+', ' ', ' '.join(words)).strip()
    return q or 'русская музыка'


def _query_usable(query: str) -> bool:
    q = (query or '').strip()
    if len(q) < 3:
        return False
    words = [w for w in q.split() if len(w) >= 2]
    return len(words) >= 1 and sum(len(w) for w in words) >= 4


def _cache_load() -> dict:
    if not os.path.isfile(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_save(data: dict):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE) or '.', exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _cache_get(query: str) -> tuple[str | None, str | None]:
    key = query.lower().strip()
    entry = _cache_load().get(key)
    if not entry:
        return None, None
    if time.time() - entry.get('ts', 0) > CACHE_TTL:
        return None, None
    return entry.get('url'), entry.get('title')


def _cache_put(query: str, url: str, title: str):
    data = _cache_load()
    data[query.lower().strip()] = {'url': url, 'title': title, 'ts': time.time()}
    _cache_save(data)


def _sng_local_files():
    if not os.path.isdir(MUSIC_DIR):
        return []
    out = []
    for ext in ('*.mp3', '*.wav', '*.flac', '*.ogg'):
        for path in glob.glob(os.path.join(MUSIC_DIR, ext)):
            name = os.path.basename(path)
            if _VI_RE.search(name):
                continue
            if _CYR_RE.search(name) or name.isascii():
                out.append(path)
    return out


def _best_local(query: str):
    files = _sng_local_files()
    if not files:
        return None
    q = (query or '').lower().strip()
    if not q or any(w in q for w in GENERIC_WORDS) and len(q) < 12:
        path = random.choice(files)
        return path, os.path.splitext(os.path.basename(path))[0]
    scored = []
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        score = SequenceMatcher(None, q, name.lower()).ratio()
        if q in name.lower() or name.lower() in q:
            score = max(score, 0.85)
        scored.append((score, path, name))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= 0.35:
        _, path, name = scored[0]
        return path, name
    return None


def _play_path(path: str, alsa_device: str = 'default') -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext == '.mp3':
        for cmd in (
            ['mpg123', '-q', '-o', 'alsa', '-a', alsa_device, path],
            ['mpg123', '-q', path],
        ):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=MUSIC_TIMEOUT).returncode == 0:
                    return True
            except FileNotFoundError:
                break
            except subprocess.TimeoutExpired:
                return False

    wav = path.replace(ext, '.play.wav') if ext else path + '.play.wav'
    try:
        if ext in ('.mp3', '.ogg', '.flac', '.m4a', '.webm', '.opus'):
            subprocess.run(
                ['ffmpeg', '-y', '-loglevel', 'error', '-i', path, '-ar', '44100', '-ac', '2', wav],
                check=True,
                capture_output=True,
                timeout=120,
            )
            path = wav
        r = subprocess.run(
            ['aplay', '-q', '-D', alsa_device, path],
            capture_output=True,
            timeout=MUSIC_TIMEOUT,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log(f'Ошибка воспроизведения: {e}')
        return False
    finally:
        if wav != path and os.path.exists(wav):
            try:
                os.unlink(wav)
            except OSError:
                pass


def _ytdlp_flat_pick(query: str) -> tuple[str | None, str | None]:
    """Шаг 1: flat-поиск — быстрее полного yt-dlp (~15 с на Pi)."""
    search = f'ytsearch8:{query}'
    if _CYR_RE.search(query):
        search = f'ytsearch8:{query} official'
    cmd = _ytdlp_cmd() + [
        '--flat-playlist',
        '--print', 'id',
        '--print', 'title',
        '--playlist-end', '8',
        '--socket-timeout', '10',
        search,
    ]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=SEARCH_TIMEOUT, text=True, errors='replace',
        ).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        log(f'flat-поиск: {e}')
        return None, None

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for i in range(0, len(lines) - 1, 2):
        vid, title = lines[i], lines[i + 1]
        if len(vid) != 11 or _VI_RE.search(title):
            continue
        return vid, title
    if len(lines) >= 2:
        return lines[0], lines[1]
    return None, None


def _ytdlp_audio_url(video_id: str) -> str | None:
    """Шаг 2: URL аудиопотока по id (несколько client/format — YouTube часто ломает один)."""
    watch = f'https://www.youtube.com/watch?v={video_id}'
    clients = [
        c.strip()
        for c in os.environ.get('VC_YTDLP_CLIENTS', 'tv_embedded,mweb,android,web').split(',')
        if c.strip()
    ]
    formats = ['ba/b/worstaudio/worst', 'bestaudio/best', 'worstaudio/worst']
    last_err = ''
    for client in clients:
        for fmt in formats:
            cmd = _ytdlp_cmd() + [
                '-g',
                '-f', fmt,
                '--no-playlist',
                '--socket-timeout', '15',
                '--extractor-args', f'youtube:player_client={client}',
                watch,
            ]
            try:
                out = subprocess.check_output(
                    cmd, stderr=subprocess.PIPE, timeout=URL_TIMEOUT, text=True, errors='replace',
                ).strip()
                url = out.splitlines()[0] if out else ''
                if url.startswith('http'):
                    return url
            except subprocess.CalledProcessError as e:
                last_err = (e.stderr or str(e))[:180]
                log(f'get-url {client}/{fmt}: {last_err}')
            except (FileNotFoundError, subprocess.TimeoutExpired, IndexError) as e:
                last_err = str(e)[:180]
                log(f'get-url {client}/{fmt}: {last_err}')
    if last_err:
        log(f'get-url: все клиенты не сработали ({video_id})')
    return None


def _resolve_youtube(query: str) -> tuple[str | None, str | None]:
    cached_url, cached_title = _cache_get(query)
    if cached_url:
        log(f'Кэш: {cached_title or query}')
        return cached_url, cached_title

    t0 = time.time()
    vid, title = _ytdlp_flat_pick(query)
    if not vid:
        return None, None
    log(f'Нашла за {time.time() - t0:.1f}s: {title[:55]}')

    t1 = time.time()
    url = _ytdlp_audio_url(vid)
    if not url:
        log('Стрим URL не получен — пробую загрузку mp3…')
        return None, None
    log(f'URL за {time.time() - t1:.1f}s (всего {time.time() - t0:.1f}s)')
    _cache_put(query, url, title)
    return url, title


def _stream_url(url: str, alsa_device: str = 'default') -> bool:
    ff = subprocess.Popen(
        [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
            '-i', url, '-vn', '-sn', '-f', 's16le', '-ar', '44100', '-ac', '2', 'pipe:1',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ap = subprocess.Popen(
        [
            'aplay', '-q', '-D', alsa_device,
            '-f', 'S16_LE', '-r', '44100', '-c', '2',
            '--period-size=512', '--buffer-size=4096',
        ],
        stdin=ff.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if ff.stdout:
        ff.stdout.close()
    try:
        ap.wait(timeout=MUSIC_TIMEOUT)
    except subprocess.TimeoutExpired:
        ap.kill()
        ff.kill()
        return False
    ff.wait(timeout=10)
    return ap.returncode == 0


def _youtube_download_play(query: str, alsa_device: str = 'default') -> bool:
    search = f'ytsearch1:{query}'
    tmpdir = tempfile.mkdtemp(prefix='vc_music_')
    out_path = os.path.join(tmpdir, 'track.%(ext)s')
    try:
        subprocess.run(
            _ytdlp_cmd() + [
                '-x', '--audio-format', 'mp3', '--audio-quality', '9',
                '-o', out_path, '--no-playlist', search,
            ],
            check=True,
            capture_output=True,
            timeout=MUSIC_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False

    mp3 = None
    for p in glob.glob(os.path.join(tmpdir, 'track.*')):
        if p.endswith(('.mp3', '.m4a', '.webm', '.opus')):
            mp3 = p
            break
    if not mp3:
        return False
    ok = _play_path(mp3, alsa_device)
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass
    return ok


def _youtube_search_play(query: str, alsa_device: str = 'default') -> bool:
    log(f'Ищу: {query}')
    url, title = _resolve_youtube(query)
    if not url:
        return False
    if MUSIC_STREAM:
        log(f'Стрим: {title[:50] if title else query}')
        if _stream_url(url, alsa_device):
            return True
        log('Стрим не удался — загрузка mp3…')
        _cache_load()  # drop bad cache entry on failure
        data = _cache_load()
        data.pop(query.lower().strip(), None)
        _cache_save(data)
    return _youtube_download_play(query, alsa_device)


def play_music(text: str, alsa_device: str = 'default') -> tuple[bool, str]:
    """Синхронный режим (CLI/тесты): ждёт окончания трека."""
    ok, msg = start_music(text, alsa_device)
    if ok:
        get_controller().wait_until_done()
    return ok, msg
