# -*- coding: utf-8 -*-
"""wm8960: маршрут PCM → физический динамик перед aplay/TTS."""

from __future__ import annotations

import os
import subprocess
import time

_route_last = 0.0


def ensure_speaker_route(force: bool = False) -> None:
    """Настроить mixer wm8960 на Speaker (без alsactl store)."""
    global _route_last
    now = time.time()
    min_gap = float(os.environ.get('VC_MIXER_MIN_INTERVAL', '2.0'))
    if force and now - _route_last < min_gap:
        return
    if not force and now - _route_last < 10.0:
        return
    _route_last = now
    script = '/usr/local/bin/setup_wm8960_mixer.sh'
    if not os.path.isfile(script):
        script = os.path.join(os.path.dirname(__file__) or '.', 'setup_wm8960_mixer.sh')
    if not os.path.isfile(script):
        return
    env = os.environ.copy()
    env['VC_MIXER_NOSTORE'] = '1'
    try:
        subprocess.run(
            [script, '0', 'nostore'], capture_output=True, timeout=10, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
