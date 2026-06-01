#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP API и веб-панель управления колонкой (voice-column)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VC_DIR = Path(os.environ.get('VC_DIR', '/home/pi/voice_column'))
ENV_FILE = Path(os.environ.get('VC_ENV_FILE', VC_DIR / '.env'))
WEB_DIR = Path(os.environ.get('VC_WEB_DIR', VC_DIR / 'web'))
PORT = int(os.environ.get('VC_UI_PORT', '8765'))
MEMORY_FILE = VC_DIR / 'user_memory.json'
BUSY_FILE = Path('/tmp/vc_web_busy')
_SAY_LOCK = threading.Lock()


def _token() -> str:
    t = os.environ.get('VC_UI_TOKEN', '').strip()
    if t:
        return t
    return _read_env().get('VC_UI_TOKEN', '').strip()

SECRET_KEYS = re.compile(
    r'API_KEY|SECRET|PASSWORD|TOKEN|IAM',
    re.I,
)

EDITABLE_ENV = {
    'VC_VOLUME_PERCENT',
    'VC_LED_BRIGHTNESS',
    'VC_MUSIC_NORMAL_VOL',
    'VC_WAKE_COOLDOWN',
    'VC_RECORD_SEC',
    'VC_SILENCE_TIMEOUT',
    'VC_LANG_MODE',
    'VC_LED',
    'VC_STT_ENGINE',
    'VC_TTS_ENGINE',
    'VC_WAKE_STT_FALLBACK',
    'VC_FAST_LLM',
    'VC_STREAM',
}

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.png': 'image/png',
}


def _run(cmd: list[str], timeout=30, sudo_systemctl=False, extra_env: dict | None = None) -> tuple[int, str]:
    if sudo_systemctl and cmd and cmd[0] == 'systemctl':
        cmd = ['sudo', '-n'] + cmd
    env = os.environ.copy()
    env.update(_read_env())
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(VC_DIR),
            env=env,
        )
        out = (r.stdout or '') + (r.stderr or '')
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 1, 'timeout'
    except Exception as exc:
        return 1, str(exc)


def _systemctl(action: str, unit: str = 'voice-column-wake') -> tuple[bool, str]:
    code, out = _run(['systemctl', action, unit], sudo_systemctl=True)
    return code == 0, out


def _service_active(unit: str = 'voice-column-wake') -> bool:
    code, out = _run(['systemctl', 'is-active', unit])
    return code == 0 and out.strip() == 'active'


def _web_busy() -> bool:
    try:
        return BUSY_FILE.is_file()
    except OSError:
        return False


def _set_web_busy(on: bool) -> None:
    try:
        if on:
            BUSY_FILE.write_text(str(time.time()), encoding='utf-8')
        elif BUSY_FILE.is_file():
            BUSY_FILE.unlink()
    except OSError:
        pass


def _ensure_wake_running(retries: int = 3) -> bool:
    if _service_active():
        return True
    for _ in range(retries):
        ok, _ = _systemctl('start')
        time.sleep(1.5)
        if _service_active():
            return True
    return False


def _read_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return data
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        data[k.strip()] = v.strip()
    return data


def _write_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding='utf-8').splitlines() if ENV_FILE.is_file() else []
    seen = set()
    out: list[str] = []
    for line in lines:
        if '=' in line and not line.strip().startswith('#'):
            k = line.split('=', 1)[0].strip()
            if k in updates:
                out.append(f'{k}={updates[k]}')
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f'{k}={v}')
    ENV_FILE.write_text('\n'.join(out) + '\n', encoding='utf-8')


def _public_env(data: dict[str, str]) -> dict[str, str]:
    pub = {}
    for k, v in data.items():
        if SECRET_KEYS.search(k):
            pub[k] = '***' if v else ''
        else:
            pub[k] = v
    return pub


def _apply_volume(pct: int) -> None:
    pct = max(0, min(100, pct))
    duck = max(0.02, round(pct / 100 * 0.05, 2))
    music = max(0.1, round(pct / 100, 2))
    _write_env({
        'VC_VOLUME_PERCENT': str(pct),
        'VC_SPEAKER_VOL': str(127 * pct // 100),
        'VC_PLAYBACK_VOL': str(255 * pct // 100),
        'VC_HEADPHONE_VOL': str(127 * pct // 100),
        'VC_MUSIC_NORMAL_VOL': str(music),
        'VC_MUSIC_DUCK_VOL': str(duck),
    })
    _run(['systemctl', 'restart', 'wm8960-mixer.service'], timeout=15, sudo_systemctl=True)


def _music_playing() -> bool:
    code, out = _run([
        sys.executable, '-c',
        'import sys; sys.path.insert(0,"."); from music_player import is_music_playing; print(is_music_playing())',
    ], timeout=10)
    return code == 0 and out.strip().lower() == 'true'


def _stop_music() -> tuple[bool, str]:
    return _run([
        sys.executable, '-c',
        'import sys; sys.path.insert(0,"."); from music_player import stop_music; stop_music(); print("ok")',
    ], timeout=10)


def _start_music(query: str) -> tuple[bool, str]:
    q = (query or '').strip()
    if not q:
        return False, 'empty'
    return _queue_wake_command(q)


def _queue_wake_command(text: str) -> tuple[bool, str]:
    """Команда в wake-процесс (музыка, стоп, LLM-команды)."""
    text = (text or '').strip()
    if not text:
        return False, 'empty'
    path = Path('/tmp/vc_web_cmd.txt')
    try:
        path.write_text(text + '\n', encoding='utf-8')
        return True, 'queued'
    except OSError as exc:
        return False, str(exc)


def _try_fast_command(text: str) -> tuple[bool, str, str]:
    """Команды из чата без LLM. Returns (handled, message, action)."""
    text = (text or '').strip()
    if not text:
        return False, '', ''
    sys.path.insert(0, str(VC_DIR))
    try:
        from volume_control import try_handle_volume
        vol = try_handle_volume(text)
        if vol is not None:
            _speak_inline(vol)
            return True, vol, 'volume'
    except ImportError:
        pass
    try:
        from music_player import is_music_command, is_stop_music_command
        from command_fast import speak_command
        alsa = _read_env().get('VC_ALSA_DEVICE', 'speaker')
        if is_stop_music_command(text):
            _queue_wake_command(text)
            speak_command(
                'stop_music', 'Остановила.',
                alsa_device=alsa, log_fn=lambda m: print(f'[column_api] {m}', flush=True),
            )
            return True, 'Остановила.', 'music_stop'
        if is_music_command(text):
            _queue_wake_command(text)
            speak_command(
                'music_start', 'Ищу.',
                alsa_device=alsa, log_fn=lambda m: print(f'[column_api] {m}', flush=True),
            )
            return True, 'Ищу…', 'music'
    except ImportError:
        pass
    return False, '', ''


def _speak_inline(text: str) -> tuple[bool, str]:
    """TTS без остановки wake (тест голоса, короткие фразы)."""
    text = (text or '').strip()
    if not text:
        return False, 'empty'
    _run(['/usr/local/bin/setup_wm8960_mixer.sh', '0'], timeout=12)
    try:
        sys.path.insert(0, str(VC_DIR))
        from command_fast import speak_command
        speak_command(
            'web_speak',
            text,
            alsa_device=_read_env().get('VC_ALSA_DEVICE', 'speaker'),
            log_fn=lambda m: print(f'[column_api] {m}', flush=True),
        )
        return True, 'ok'
    except ImportError:
        pass
    try:
        from yandex_speechkit import credentials_ok, speak_yandex_short
        if credentials_ok():
            speak_yandex_short(
                text,
                alsa_device=_read_env().get('VC_ALSA_DEVICE', 'speaker'),
                log_fn=lambda m: print(f'[column_api] {m}', flush=True),
            )
            return True, 'ok'
    except ImportError:
        pass
    return _run([
        sys.executable, str(VC_DIR / 'voice_column.py'), '--speak', text,
    ], timeout=120)


def _say(text: str, mode: str = 'speak') -> tuple[bool, str]:
    """mode=speak — TTS; mode=ask — сначала команды, потом LLM."""
    text = (text or '').strip()
    if not text:
        return False, 'empty'

    if mode == 'speak':
        with _SAY_LOCK:
            _set_web_busy(True)
            try:
                return _speak_inline(text)
            finally:
                _set_web_busy(False)

    handled, msg, _action = _try_fast_command(text)
    if handled:
        return True, msg

    with _SAY_LOCK:
        _set_web_busy(True)
        was_wake = _service_active()
        if was_wake:
            _systemctl('stop')
            time.sleep(0.8)

        try:
            _run(['/usr/local/bin/setup_wm8960_mixer.sh', '0'], timeout=12)
            cmd = [sys.executable, str(VC_DIR / 'voice_column.py'), '--text', text]
            code, out = _run(cmd, timeout=180)
            return code == 0, out[-3000:]
        finally:
            if was_wake:
                _ensure_wake_running()
            _set_web_busy(False)


def _say_async(text: str, mode: str = 'speak'):
    def worker():
        ok, out = _say(text, mode)
        tag = 'ok' if ok else 'FAIL'
        print(f'[column_api] say {tag} mode={mode}: {out[-400:]}', flush=True)

    threading.Thread(target=worker, daemon=True).start()


def _logs(n: int = 40) -> str:
    n = max(5, min(200, n))
    _, out = _run([
        'journalctl', '-u', 'voice-column-wake', '-n', str(n), '--no-pager', '-o', 'cat',
    ], timeout=15)
    return out


def _memory_data() -> dict:
    if not MEMORY_FILE.is_file():
        return {'facts': [], 'history': []}
    try:
        mem = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {'facts': [], 'history': []}
    facts = [x for x in (mem.get('facts') or []) if isinstance(x, str) and x.strip()]
    history = [x for x in (mem.get('history') or []) if isinstance(x, dict)]
    return {'facts': facts, 'history': history[-20:]}


def _current_ssid() -> str:
    code, out = _run(['iwgetid', '-r'], timeout=5)
    if code == 0 and out.strip():
        return out.strip()
    code, out = _run(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi'], timeout=5)
    if code == 0:
        for line in out.splitlines():
            if line.startswith('yes:'):
                return line.split(':', 1)[1].strip()
    return ''


def _setup_info() -> dict:
    env = _read_env()
    ip = ''
    code, out = _run(['hostname', '-I'], timeout=5)
    if code == 0:
        ip = out.split()[0] if out.split() else ''
    token = env.get('VC_UI_TOKEN', '')
    port = env.get('VC_UI_PORT', '8765')
    ssid = _current_ssid()
    ui_url = ''
    ui_mdns = ''
    setup_file = VC_DIR / 'setup.url'
    if setup_file.is_file():
        lines = [ln.strip() for ln in setup_file.read_text(encoding='utf-8').splitlines() if ln.strip()]
        if lines:
            ui_url = lines[0]
        if len(lines) > 1:
            ui_mdns = lines[1]
    if not ui_url and ip:
        ui_url = f'http://{ip}:{port}/'
        if token:
            ui_url += f'?token={token}'
        ui_mdns = f'http://kolonka.local:{port}/?token={token}' if token else f'http://kolonka.local:{port}/'
    try:
        import socket
        hostname = socket.gethostname()
    except OSError:
        hostname = 'kolonka'
    return {
        'hostname': hostname,
        'ip': ip,
        'ssid': ssid,
        'wifi_connected': bool(ssid),
        'ui_url': ui_url,
        'ui_url_mdns': ui_mdns,
        'ap_ssid': env.get('VC_WIFI_AP_SSID', 'Kolonka-Setup'),
        'has_token': bool(token),
    }


def _wifi_reset_async():
    def worker():
        script = VC_DIR / 'reset_wifi.sh'
        if script.is_file():
            _run(['sudo', '-n', str(script)], timeout=30, sudo_systemctl=False)
        else:
            _run(['sudo', '-n', 'reboot'], timeout=10, sudo_systemctl=False)

    threading.Thread(target=worker, daemon=True).start()


def _status() -> dict:
    env = _read_env()
    mem = _memory_data()
    facts = mem.get('facts') or []
    history = mem.get('history') or []
    vol = env.get('VC_VOLUME_PERCENT', '90')
    led = env.get('VC_LED_BRIGHTNESS', '35')
    try:
        import socket
        host = socket.gethostname()
    except OSError:
        host = 'pi'
    return {
        'service': (
            'busy' if _web_busy()
            else ('active' if _service_active() else 'inactive')
        ),
        'demo_mode': _read_env().get('VC_DEMO_MODE', '0') not in ('0', 'false', 'no', ''),
        'api': 'active',
        'zeroclaw': 'active' if _service_active('zeroclaw') else 'inactive',
        'music': _music_playing(),
        'host': host,
        'volume': int(vol) if str(vol).isdigit() else vol,
        'led_brightness': int(led) if str(led).isdigit() else led,
        'env': _public_env(env),
        'memory': {
            'facts_count': len(facts),
            'history_count': len(history),
            'facts': facts[-15:],
        },
        'setup': _setup_info(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'ColumnUI/1.0'

    def log_message(self, fmt, *args):
        print(f'[column_api] {self.address_string()} {fmt % args}', flush=True)

    def _auth_ok(self) -> bool:
        token = _token()
        if not token:
            return True
        q = parse_qs(urlparse(self.path).query)
        hdr = self.headers.get('X-Token', '')
        return hdr == token or (q.get('token', [''])[0] == token)

    def _json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        ext = path.suffix.lower()
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict:
        n = int(self.headers.get('Content-Length', 0))
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Token')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if path in ('/', '/index.html'):
            self._file(WEB_DIR / 'index.html')
            return
        if path.startswith('/static/'):
            target = (WEB_DIR / path.lstrip('/')).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())):
                self.send_error(403)
                return
            self._file(target)
            return
        if path == '/favicon.ico':
            self.send_error(404)
            return
        if path == '/api/ping':
            self._json(200, {'ok': True, 'auth_required': bool(_token())})
            return
        if path in ('/setup', '/setup.html'):
            self._file(WEB_DIR / 'setup.html')
            return
        if path in ('/instrukciya', '/instrukciya.html'):
            instr = VC_DIR / 'KOLONKA_INSTRUKCIYA.html'
            if instr.is_file():
                self._file(instr)
            else:
                self._file(WEB_DIR / 'instrukciya.html')
            return
        if path == '/api/setup':
            self._json(200, _setup_info())
            return
        if path == '/api/demo':
            try:
                from demo_scenario import get_demo_state
                self._json(200, get_demo_state())
            except ImportError:
                self._json(200, {'enabled': False, 'pipeline': []})
            return
        if path in ('/demo', '/demo.html'):
            demo_page = WEB_DIR / 'demo.html'
            if demo_page.is_file():
                self._file(demo_page)
            else:
                self.send_error(404)
            return
        if not self._auth_ok():
            self._json(401, {'error': 'unauthorized'})
            return
        if path == '/api/status':
            self._json(200, _status())
            return
        if path == '/api/logs':
            n = int(qs.get('n', ['40'])[0])
            self._json(200, {'lines': _logs(n).splitlines()})
            return
        if path == '/api/env':
            self._json(200, {'env': _public_env(_read_env()), 'editable': sorted(EDITABLE_ENV)})
            return
        if path == '/api/memory':
            self._json(200, _memory_data())
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/demo':
            body = self._body_json()
            try:
                from demo_scenario import enabled, set_demo_mode, demo_reset_pipeline
                if 'toggle' in body:
                    on = not enabled()
                else:
                    on = str(body.get('enabled', '1')).lower() not in ('0', 'false', 'no', '')
                set_demo_mode(on)
                if on:
                    demo_reset_pipeline()
                self._json(200, {'ok': True, 'enabled': on})
            except ImportError:
                self._json(500, {'ok': False, 'error': 'demo_scenario missing'})
            return

        if not self._auth_ok():
            self._json(401, {'error': 'unauthorized'})
            return
        body = self._body_json()

        if path == '/api/service/restart':
            ok, msg = _systemctl('restart')
            self._json(200 if ok else 500, {'ok': ok, 'msg': msg})
            return
        if path == '/api/service/stop':
            ok, msg = _systemctl('stop')
            self._json(200 if ok else 500, {'ok': ok, 'msg': msg})
            return
        if path == '/api/service/start':
            ok, msg = _systemctl('start')
            self._json(200 if ok else 500, {'ok': ok, 'msg': msg})
            return
        if path == '/api/say':
            text = body.get('text', '')
            mode = body.get('mode', 'speak')
            if mode not in ('speak', 'ask'):
                mode = 'speak'
            if mode == 'ask':
                handled, msg, action = _try_fast_command(str(text))
                if handled:
                    self._json(202, {'ok': True, 'action': action, 'msg': msg})
                    return
            _say_async(str(text), mode)
            self._json(202, {'ok': True, 'queued': True, 'mode': mode})
            return
        if path == '/api/music/stop':
            ok, msg = _queue_wake_command('выключи музыку')
            self._json(200 if ok else 500, {'ok': ok, 'msg': msg})
            return
        if path == '/api/music/play':
            query = body.get('query', '') or body.get('text', '')
            q = str(query).strip()
            if not q:
                self._json(400, {'ok': False, 'error': 'empty'})
                return
            if not q.lower().startswith(('включи', 'поставь', 'играй', 'play')):
                q = 'включи ' + q
            ok, msg = _queue_wake_command(q)
            self._json(200 if ok else 500, {'ok': ok, 'msg': msg or 'Ищу…'})
            return
        if path == '/api/volume':
            pct = int(body.get('percent', 35))
            _apply_volume(pct)
            self._json(200, {'ok': True, 'percent': pct})
            return
        if path == '/api/led':
            pct = max(0, min(100, int(body.get('brightness', 30))))
            _write_env({'VC_LED_BRIGHTNESS': str(pct)})
            self._json(200, {'ok': True, 'brightness': pct, 'note': 'restart_wake_for_led'})
            return
        if path == '/api/env':
            updates = {}
            for k, v in body.items():
                if k in EDITABLE_ENV:
                    updates[k] = str(v)
            if updates:
                _write_env(updates)
                if 'VC_VOLUME_PERCENT' in updates:
                    _apply_volume(int(updates['VC_VOLUME_PERCENT']))
                restart_keys = {
                    'VC_STT_ENGINE', 'VC_TTS_ENGINE', 'VC_STREAM', 'VC_FAST_LLM',
                    'VC_LANG_MODE', 'VC_WAKE_STT_FALLBACK', 'VC_LED', 'VC_RECORD_SEC',
                    'VC_SILENCE_TIMEOUT', 'VC_WAKE_COOLDOWN',
                }
                if any(k in updates for k in restart_keys):
                    _systemctl('restart')
            self._json(200, {'ok': True, 'updated': list(updates.keys())})
            return
        if path == '/api/memory/clear':
            if MEMORY_FILE.is_file():
                MEMORY_FILE.write_text(
                    json.dumps({'facts': [], 'history': []}, ensure_ascii=False, indent=2),
                    encoding='utf-8',
                )
            self._json(200, {'ok': True})
            return
        if path == '/api/wifi/reset':
            _wifi_reset_async()
            self._json(202, {'ok': True, 'msg': 'Сброс Wi-Fi — перезагрузка через ~10 с'})
            return
        self.send_error(404)


def main():
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get('VC_UI_HOST', '0.0.0.0')
    httpd = ThreadingHTTPServer((host, PORT), Handler)
    print(f'[column_api] http://{host}:{PORT}  web={WEB_DIR}', flush=True)
    if _token():
        print('[column_api] auth: X-Token header', flush=True)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
