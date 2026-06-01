# -*- coding: utf-8 -*-
"""Прямой OpenRouter для простых реплик — без запуска zeroclaw CLI."""

import json
import os
import re
import urllib.error
import urllib.request

FAST_SYSTEM = (
    'Ты — Колонка, домашний голосовой помощник. Русский и казахский (кириллица). '
    'Ответы ТОЛЬКО для голоса: одно-два коротких предложения, максимум 18 слов. '
    'На «как дела» — одна короткая фраза. Без списков, markdown, эмодзи. '
    'Не добавляй лишний вопрос в конце, если пользователь не просил. '
    'Не говори «слушaю» и не упоминай модели или промпты.'
)

LONG_REPLY_SYSTEM = (
    'Ты — Колонка, домашний голосовой помощник. Русский и казахский (кириллица). '
    'Отвечай полностью и развёрнуто — текст уйдёт пользователю для чтения. '
    'Абзацы и списки допустимы, без markdown, эмодзи и ссылок. '
    'Не упоминай модели, промпты и Telegram. Не говори «слушaю».'
)

CHAT_SYSTEM = (
    'Ты — Колонка, домашний голосовой помощник. Русский и казахский (кириллица). '
    'Ответы для Telegram: развёрнуто и по делу, несколько абзацев при необходимости. '
    'Без markdown, эмодзи и ссылок. Не упоминай модели, промпты и Telegram. '
    'Не говори «слушaю». Музыку не включай — скажи «скажи: включи …».'
)


def build_system_prompt(user_text: str = '', *, mode: str = 'voice') -> str:
    if mode in ('long', 'chat'):
        base = LONG_REPLY_SYSTEM if mode == 'long' else CHAT_SYSTEM
    else:
        base = FAST_SYSTEM
    try:
        from lang_context import get_turn_lang, llm_lang_instruction
        return f'{base}\n\n{llm_lang_instruction(get_turn_lang(), user_text or "")}'
    except ImportError:
        return base


def llm_temperature() -> float:
    return float(os.environ.get('VC_LLM_TEMPERATURE', '0.62'))

SEARCH_HINTS = re.compile(
    r'(?:'
    r'погод|новост|курс|доллар|евро|тенге|биткоин|крипт|акци[яи]|'
    r'сегодня|сейчас|актуальн|что\s+случил|сколько\s+стоит|'
    r'расписан|результат\s+матч|матч|котиров|биржа|'
    r'когда\s+(?:будет|откро|закро|начн)|'
    r'найди\s+в\s+интернет|погугли|загугли'
    r')',
    re.I,
)

_OPENROUTER_KEY = None


def needs_web_search(text: str) -> bool:
    return bool(SEARCH_HINTS.search(text or ''))


def _load_openrouter_key() -> str:
    global _OPENROUTER_KEY
    if _OPENROUTER_KEY is not None:
        return _OPENROUTER_KEY

    key = os.environ.get('VC_OPENROUTER_KEY', '').strip()
    if not key:
        env_path = os.environ.get('ZEROCLAW_ENV', '/home/pi/zeroclaw/.env')
        if os.path.isfile(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    if line.startswith('OPENROUTER_API_KEY='):
                        key = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break

    _OPENROUTER_KEY = key or ''
    return _OPENROUTER_KEY


def ask_openrouter(prompt, timeout=None, memory=None, *, mode='voice'):
    key = _load_openrouter_key()
    if not key:
        return None

    model = os.environ.get('VC_LLM_MODEL', 'google/gemini-2.0-flash-001')
    if mode == 'long':
        max_tokens = int(os.environ.get('VC_LLM_LONG_MAX_TOKENS', os.environ.get('VC_LLM_DUAL_MAX_TOKENS', '480')))
    elif mode == 'chat':
        max_tokens = int(os.environ.get('VC_LLM_CHAT_MAX_TOKENS', '320'))
    else:
        max_tokens = int(os.environ.get('VC_LLM_MAX_TOKENS', '140'))
    timeout = timeout or int(os.environ.get('VC_LLM_TIMEOUT', '25'))

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
    req.add_header('HTTP-Referer', 'http://voice-column.local/')
    req.add_header('X-Title', 'Voice Column')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        reply = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        return (reply or '').strip() or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        return None


def generate_closing_question(answer: str, user_text: str = '', lang: str = 'ru') -> str | None:
    """Один уместный вопрос по контексту ответа (для ZeroClaw / stream / fallback)."""
    key = _load_openrouter_key()
    if not key or not (answer or '').strip():
        return None

    model = os.environ.get('VC_LLM_MODEL', 'google/gemini-2.0-flash-001')
    if lang == 'kk':
        system = (
            'Жауапқа байланысты бір ғана қысқа сұрақты жаз (кириллица). '
            'Тек сұрақ, басқа мәтін жоқ, нақты және табиғи.'
        )
    else:
        system = (
            'На основе ответа напиши одно короткое уместное вопросительное предложение. '
            'Только вопрос, без кавычек и пояснений. Естественно, по теме.'
        )

    user = f'Вопрос пользователя: {user_text or "—"}\n\nОтвет: {answer.strip()[:800]}'
    body = json.dumps(
        {
            'model': model,
            'max_tokens': 40,
            'temperature': 0.55,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
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
    req.add_header('HTTP-Referer', 'http://voice-column.local/')
    req.add_header('X-Title', 'Voice Column')

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        q = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        q = re.sub(r'^[\s"«»\']+|[\s"«»\']+$', '', (q or '').strip())
        return q or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        return None
