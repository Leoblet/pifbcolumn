# -*- coding: utf-8 -*-
"""Fast Butler — роутер: fast LLM vs ZeroClaw lite vs ZeroClaw full."""

from __future__ import annotations

import os
import re

from fast_llm import needs_web_search

FULL_ZC_HINTS = re.compile(
    r'(?:'
    r'файл|прочитай|запиш|сохрани|создай\s+файл|'
    r'shell|терминал|выполни\s+команд|bash|'
    r'cron|расписан|напомни|напоминан|'
    r'памят|запомни|забудь\s+факт|'
    r'git|github|репозитор|'
    r'браузер|открой\s+сайт|http'
    r')',
    re.I,
)

EXEC_HINTS = re.compile(
    r'(?:'
    r'включ|выключ|громк|погромче|потише|'
    r'музык|плейлист|песн|трек|'
    r'стоп|пауза|'
    r'демо|дворецкий'
    r')',
    re.I,
)


def enabled() -> bool:
    return os.environ.get('VC_FAST_AGENT', '1').lower() not in ('0', 'false', 'no')


def classify(text: str) -> tuple[str, str]:
    """Returns (route, pipeline_label). route: fast_llm | zeroclaw_lite | zeroclaw_full | legacy"""
    if not enabled():
        return 'legacy', ''

    t = (text or '').strip()
    if not t:
        return 'fast_llm', 'FAST → OpenRouter'

    if FULL_ZC_HINTS.search(t):
        return 'zeroclaw_full', 'ZeroClaw full (tools)'

    if needs_web_search(t):
        mode = os.environ.get('VC_FAST_AGENT_WEB', 'lite').lower()
        if mode in ('full', 'zeroclaw', 'zeroclaw_full'):
            return 'zeroclaw_full', 'ZeroClaw (web search)'
        return 'zeroclaw_lite', 'FAST → ZeroClaw lite (search)'

    return 'fast_llm', 'FAST → OpenRouter'


def is_executor_command(text: str) -> bool:
    return bool(EXEC_HINTS.search(text or ''))
