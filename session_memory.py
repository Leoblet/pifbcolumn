# -*- coding: utf-8 -*-
"""Память диалога и факты о пользователе (аллергии, ограничения)."""

import json
import os
import re

from fast_llm import build_system_prompt

FACT_RULES = [
    (re.compile(r'(?:запомни|не\s+забудь)[,:]?\s*(.+)', re.I), lambda m: m.group(1).strip()),
    (re.compile(r'мне\s+нельзя\s+(.+)', re.I), lambda m: f'Нельзя: {m.group(1).strip()}'),
    (re.compile(r'я\s+не\s+(?:могу|ем|ему|пью|пить|переношу)\s+(.+)', re.I), lambda m: f'Нельзя: {m.group(1).strip()}'),
    (re.compile(r'у\s+меня\s+аллерги(?:я|и)\s+(?:на\s+)?(.+)', re.I), lambda m: f'Аллергия: {m.group(1).strip()}'),
    (re.compile(r'я\s+(?:веган|вегетарианец|вегетарианка)', re.I), lambda m: m.group(0).strip()),
]

_memory = None


def _norm_fact(text):
    t = re.sub(r'\s+', ' ', (text or '').strip()).rstrip('.!?')
    return t[:200] if t else ''


class SessionMemory:
    def __init__(self):
        vc_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(vc_dir, 'user_memory.json')
        self.path = os.environ.get('VC_MEMORY_FILE', default_path)
        self.max_turns = int(os.environ.get('VC_MEMORY_TURNS', '10'))
        self.history = []
        self.facts = []
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            self.facts = [x for x in data.get('facts', []) if isinstance(x, str) and x.strip()]
        except (OSError, json.JSONDecodeError):
            self.facts = []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump({'facts': self.facts}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def extract_facts(self, text):
        if not text:
            return []
        added = []
        for pattern, fmt in FACT_RULES:
            m = pattern.search(text)
            if not m:
                continue
            fact = _norm_fact(fmt(m))
            if fact and fact not in self.facts:
                self.facts.append(fact)
                added.append(fact)
        if added:
            self.save()
        return added

    def build_system(self, user_text='', *, mode='voice'):
        parts = [build_system_prompt(user_text, mode=mode)]
        if self.facts:
            parts.append('Факты о пользователе (соблюдай всегда, не противоречь):')
            for fact in self.facts[-25:]:
                parts.append(f'- {fact}')
        if mode in ('long', 'chat'):
            parts.append(
                'Помни историю и факты. Можно завершить одним уместным вопросом.'
            )
        else:
            parts.append(
                'Помни историю и факты. В конце каждого ответа — один уместный вопрос. '
                'Если спрашивают «можно ли» то, что запрещено — ответь «нет», напомни почему и задай вопрос.'
            )
        return '\n'.join(parts)

    def build_messages(self, user_text, *, mode='voice'):
        messages = [{'role': 'system', 'content': self.build_system(user_text, mode=mode)}]
        for turn in self.history[-self.max_turns * 2 :]:
            messages.append(turn)
        messages.append({'role': 'user', 'content': user_text})
        return messages

    def add_turn(self, user_text, assistant_text):
        self.extract_facts(user_text)
        user_text = (user_text or '').strip()
        assistant_text = (assistant_text or '').strip()
        if user_text:
            self.history.append({'role': 'user', 'content': user_text})
        if assistant_text:
            self.history.append({'role': 'assistant', 'content': assistant_text})
        max_msgs = self.max_turns * 2
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]

    def clear_history(self):
        self.history = []

    def clear_facts(self):
        self.facts = []
        self.save()


def get_memory():
    global _memory
    if _memory is None:
        _memory = SessionMemory()
    return _memory


def is_memory_reset_command(text):
    t = (text or '').lower().strip()
    return t in (
        'забудь всё',
        'забудь все',
        'очисти память',
        'сбрось память',
        'новый разговор',
    )
