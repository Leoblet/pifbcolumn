# -*- coding: utf-8 -*-
"""Голосовое управление громкостью колонки."""

from __future__ import annotations

import os
import re
import subprocess

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
VOLUME_STEP = int(os.environ.get('VC_VOLUME_STEP', '5'))
MIXER_SCRIPT = os.environ.get('VC_MIXER_SCRIPT', '/usr/local/bin/setup_wm8960_mixer.sh')

VOLUME_CMD_RE = re.compile(
    r'(?:'
    r'(?:установи|поставь|сделай|выставь)\s+(?:громкость\s+)?(?:на\s+)?|'
    r'громкость\s+(?:на\s+)?|'
    r'звук\s+(?:на\s+)?'
    r')'
    r'(\d{1,3})'
    r'(?:\s*(?:%|процент(?:а|ов)?))?',
    re.I,
)

LOUDER_RE = re.compile(
    r'погромче|'
    r'(?:^|\s)громче(?:\s|$)|'
    r'(?:прибавь|увеличь|добавь)\s+(?:громкость|звук)|'
    r'громкость\s+(?:выше|больше|побольше)',
    re.I,
)

QUIETER_RE = re.compile(
    r'потише|'
    r'(?:^|\s)тише(?:\s|$)|'
    r'(?:убавь|уменьши|снизь)\s+(?:громкость|звук)|'
    r'громкость\s+(?:ниже|меньше|поменьше)',
    re.I,
)


def _read_env_file() -> dict[str, str]:
    data: dict[str, str] = {}
    if not os.path.isfile(ENV_FILE):
        return data
    with open(ENV_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            data[k.strip()] = v.strip()
    return data


def _write_env_file(updates: dict[str, str]) -> None:
    lines = []
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, encoding='utf-8') as f:
            lines = f.read().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        k = line.split('=', 1)[0].strip() if '=' in line and not line.strip().startswith('#') else ''
        if k in updates:
            out.append(f'{k}={updates[k]}')
            seen.add(k)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f'{k}={v}')
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')


def get_volume_percent() -> int:
    raw = os.environ.get('VC_VOLUME_PERCENT') or _read_env_file().get('VC_VOLUME_PERCENT', '90')
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        return 90


def _levels(pct: int) -> tuple[str, str, str]:
    pct = max(0, min(100, pct))
    duck = str(max(0.02, round(pct / 100 * 0.05, 2)))
    music = str(max(0.1, round(pct / 100, 2)))
    return str(pct), music, duck


def _mixer_env(pct: int) -> dict[str, str]:
    """Производные уровни wm8960 amixer для VC_VOLUME_PERCENT."""
    pct = max(0, min(100, int(pct)))
    return {
        'VC_VOLUME_PERCENT': str(pct),
        'VC_SPEAKER_VOL': str(max(0, min(127, 127 * pct // 100))),
        'VC_PLAYBACK_VOL': str(max(0, min(255, 255 * pct // 100))),
        'VC_HEADPHONE_VOL': str(max(0, min(127, 127 * pct // 100))),
    }


def set_volume_percent(pct: int, apply_mixer: bool = True) -> int:
    """Выставить громкость 0–100, обновить .env и mixer."""
    pct = max(0, min(100, int(pct)))
    vol_s, music, duck = _levels(pct)
    updates = {
        **_mixer_env(pct),
        'VC_MUSIC_NORMAL_VOL': music,
        'VC_MUSIC_DUCK_VOL': duck,
    }
    _write_env_file(updates)
    for k, v in updates.items():
        os.environ[k] = v

    try:
        import music_player as mp
        mp.MUSIC_NORMAL_VOL = float(music)
        mp.MUSIC_DUCK_VOL = float(duck)
    except ImportError:
        pass

    if apply_mixer and os.path.isfile(MIXER_SCRIPT):
        subprocess.run(
            ['bash', MIXER_SCRIPT, '0'],
            capture_output=True,
            timeout=12,
        )
    return pct


def parse_volume_command(text: str) -> int | None:
    """None — не команда громкости; иначе целевой процент."""
    t = (text or '').strip()
    if not t:
        return None

    m = VOLUME_CMD_RE.search(t)
    if m:
        return max(0, min(100, int(m.group(1))))

    cur = get_volume_percent()
    if LOUDER_RE.search(t):
        return min(100, cur + VOLUME_STEP)
    if QUIETER_RE.search(t):
        return max(0, cur - VOLUME_STEP)
    return None


def is_volume_command(text: str) -> bool:
    return parse_volume_command(text) is not None


def try_handle_volume(text: str) -> str | None:
    """Обработать команду громкости. None — не про громкость."""
    target = parse_volume_command(text)
    if target is None:
        return None
    before = get_volume_percent()
    after = set_volume_percent(target)
    if after == before:
        return f'Громкость уже {after} процентов.'
    if after > before:
        return f'Сделала громче. Сейчас {after} процентов.'
    return f'Сделала тише. Сейчас {after} процентов.'
