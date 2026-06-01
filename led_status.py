# -*- coding: utf-8 -*-
"""LED-индикация состояния колонки: idle / wake / listen / think / speak / error / music."""

from __future__ import annotations

import os
import threading
import time

# Состояния
S_IDLE = 'idle'
S_WAKE = 'wake'
S_LISTEN = 'listen'
S_THINK = 'think'
S_SPEAK = 'speak'
S_ERROR = 'error'
S_MUSIC = 'music'

_PATTERNS = {
    S_IDLE: ('pulse', 2.5),
    S_WAKE: ('flash', 0.15),
    S_LISTEN: ('solid', 1.0),
    S_THINK: ('blink', 0.35),
    S_SPEAK: ('solid', 1.0),
    S_ERROR: ('blink', 0.2),
    S_MUSIC: ('pulse', 1.2),
}


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = (h or '000000').strip().lstrip('#')
    if len(h) == 6:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0, 0, 0


def _env_color(name: str, default: str) -> tuple[int, int, int]:
    key = f'VC_LED_COLOR_{name.upper()}'
    return _hex_rgb(os.environ.get(key, default))


def _log(msg: str):
    print(f'[led] {msg}', flush=True)


def _default_led_type() -> str:
    explicit = os.environ.get('VC_LED_TYPE', '').strip().lower()
    if explicit:
        return explicit
    # ReSpeaker / vBot: кольцо WS2812 на GPIO10
    return 'ws281x'


class _ActLed:
    """Встроенный зелёный LED на Pi (ACT) — без проводов."""

    def __init__(self, name: str = 'ACT'):
        self._name = name
        self._base = f'/sys/class/leds/{name}'
        if not os.path.isdir(self._base):
            raise FileNotFoundError(f'нет {self._base}')
        self._write('trigger', 'none')
        self.off()

    def _write(self, key: str, val: str):
        with open(os.path.join(self._base, key), 'w', encoding='ascii') as f:
            f.write(val)

    def on(self):
        self._write('brightness', '1')

    def off(self):
        self._write('brightness', '0')

    def cleanup(self):
        try:
            self.off()
            self._write('trigger', 'mmc0')
        except OSError:
            pass


class _GpioLed:
    def __init__(self, pin: int, active_low: bool):
        self._pin = pin
        self._active_low = active_low
        self._lgpio_h = None
        try:
            import lgpio

            self._lgpio_h = lgpio.gpiochip_open(0)
            level = 1 if active_low else 0
            lgpio.gpio_claim_output(self._lgpio_h, pin, level)
            self.off()
            return
        except Exception as exc:
            _log(f'lgpio pin {pin}: {exc}, fallback RPi.GPIO')

        import RPi.GPIO as GPIO

        self.GPIO = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(pin, GPIO.OUT)
        self.off()

    def _write(self, on: bool):
        if self._lgpio_h is not None:
            import lgpio

            level = 0 if (on ^ self._active_low) else 1
            lgpio.gpio_write(self._lgpio_h, self._pin, level)
            return
        if self._active_low:
            level = self.GPIO.LOW if on else self.GPIO.HIGH
        else:
            level = self.GPIO.HIGH if on else self.GPIO.LOW
        self.GPIO.output(self._pin, level)

    def on(self):
        self._write(True)

    def off(self):
        self._write(False)

    def cleanup(self):
        try:
            self.off()
            if self._lgpio_h is not None:
                import lgpio

                lgpio.gpio_free(self._lgpio_h, self._pin)
                lgpio.gpiochip_close(self._lgpio_h)
            else:
                self.GPIO.cleanup(self._pin)
        except Exception:
            pass


class _Ws281xLed:
    """Кольцо WS2812 — как vBot (GPIO10, PixelStrip)."""

    def __init__(self, pin: int, count: int, brightness: int, invert: bool = False, dma: int = 10):
        from rpi_ws281x import PixelStrip, Color

        self._Color = Color
        self._strip = PixelStrip(count, pin, 800000, dma, invert, brightness, 0)
        self._count = count
        self._strip.begin()
        self.clear()

    def set_rgb(self, r: int, g: int, b: int):
        c = self._Color(r, g, b)
        for i in range(self._count):
            self._strip.setPixelColor(i, c)
        self._strip.show()

    def clear(self):
        self.set_rgb(0, 0, 0)

    def cleanup(self):
        self.clear()


class LedStatus:
    """Singleton-подобный контроллер LED."""

    _COLORS = {
        S_IDLE: lambda: _env_color('idle', '001020'),
        S_WAKE: lambda: _env_color('wake', 'ffffff'),
        S_LISTEN: lambda: _env_color('listen', '0055ff'),
        S_THINK: lambda: _env_color('think', '0000ff'),
        S_SPEAK: lambda: _env_color('speak', '00ff44'),
        S_ERROR: lambda: _env_color('error', 'ff2222'),
        S_MUSIC: lambda: _env_color('music', '8800ff'),
    }

    def __init__(self):
        self.enabled = os.environ.get('VC_LED', '1').lower() not in ('0', 'false', 'no', 'off')
        self._type = _default_led_type()
        self._pin = int(os.environ.get('VC_LED_GPIO', '10'))
        self._act_name = os.environ.get('VC_LED_ACT', 'ACT')
        self._count = int(os.environ.get('VC_LED_COUNT', '24'))
        self._brightness = int(float(os.environ.get('VC_LED_BRIGHTNESS', '30')) * 255 / 100)
        self._invert = os.environ.get('VC_LED_INVERT', '0').lower() in ('1', 'true', 'yes')
        self._dma = int(os.environ.get('VC_LED_DMA', '10'))
        self._active_low = os.environ.get('VC_LED_ACTIVE_LOW', '0').lower() in ('1', 'true', 'yes')
        self._lock = threading.Lock()
        self._state = S_IDLE
        self._stop = threading.Event()
        self._thread = None
        self._hw = None
        self._simple = None
        if self.enabled:
            self._init_hw()

    def _init_hw(self):
        if self._type == 'none':
            self.enabled = False
            return
        try:
            if self._type == 'ws281x':
                self._hw = _Ws281xLed(
                    self._pin, self._count, self._brightness, self._invert, self._dma,
                )
                _log(f'ok ws281x GPIO{self._pin} x{self._count} (ReSpeaker/vBot)')
                return
            if self._type == 'act':
                self._simple = _ActLed(self._act_name)
                _log(f'ok act ({self._act_name}) — зелёный LED на плате Pi')
                return
            if self._type == 'gpio':
                self._simple = _GpioLed(self._pin, self._active_low)
                _log(f'ok gpio pin={self._pin}')
                return
        except Exception as exc:
            _log(f'init {self._type} failed: {exc}')
            if self._type != 'act' and os.path.isdir(f'/sys/class/leds/{self._act_name}'):
                try:
                    self._type = 'act'
                    self._simple = _ActLed(self._act_name)
                    _log(f'fallback act ({self._act_name})')
                    return
                except Exception as exc2:
                    _log(f'fallback act failed: {exc2}')
            self.enabled = False

    def _apply_rgb(self, r: int, g: int, b: int, on: bool = True):
        if not self.enabled:
            return
        if self._hw:
            if on and (r or g or b):
                self._hw.set_rgb(r, g, b)
            else:
                self._hw.clear()
        elif self._simple:
            self._simple.on() if on and (r + g + b) > 20 else self._simple.off()

    def _stop_thread(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._stop.clear()
        self._thread = None

    def _start_pattern(self, state: str):
        pattern, speed = _PATTERNS.get(state, ('solid', 1.0))
        rgb = self._COLORS.get(state, self._COLORS[S_IDLE])()

        if pattern == 'solid':
            self._apply_rgb(*rgb, on=True)
            return

        def run():
            if pattern == 'flash':
                self._apply_rgb(*rgb, on=True)
                time.sleep(speed)
                self._apply_rgb(0, 0, 0, on=False)
                return
            if pattern == 'blink':
                while not self._stop.is_set():
                    self._apply_rgb(*rgb, on=True)
                    if self._stop.wait(speed):
                        break
                    self._apply_rgb(0, 0, 0, on=False)
                    if self._stop.wait(speed):
                        break
                return
            if pattern == 'pulse':
                while not self._stop.is_set():
                    self._apply_rgb(*rgb, on=True)
                    if self._stop.wait(speed * 0.5):
                        break
                    self._apply_rgb(0, 0, 0, on=False)
                    if self._stop.wait(speed * 0.5):
                        break
                return
            while not self._stop.is_set():
                self._apply_rgb(*rgb, on=True)
                if self._stop.wait(speed * 3):
                    break
                self._apply_rgb(0, 0, 0, on=False)
                if self._stop.wait(speed * 3):
                    break

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def set(self, state: str):
        if not self.enabled or state not in _PATTERNS:
            return
        with self._lock:
            if state == self._state and state not in (S_THINK, S_ERROR, S_MUSIC, S_IDLE):
                return
            self._state = state
            self._stop_thread()
            self._start_pattern(state)

    def cleanup(self):
        with self._lock:
            self._stop_thread()
            if self._hw:
                self._hw.cleanup()
            elif self._simple:
                self._simple.cleanup()
            self.enabled = False


_led: LedStatus | None = None


def get_led() -> LedStatus:
    global _led
    if _led is None:
        _led = LedStatus()
    return _led


def led_set(state: str):
    try:
        get_led().set(state)
    except Exception:
        pass


def led_idle_if_no_music():
    try:
        from music_player import is_music_playing
        led_set(S_MUSIC if is_music_playing() else S_IDLE)
    except Exception:
        led_set(S_IDLE)
