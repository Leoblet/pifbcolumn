const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const toastEl = $('#toast');
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  setTimeout(() => toastEl.classList.remove('show'), 2400);
}

function token() {
  return localStorage.getItem('vc_token') || '';
}

(function initTokenFromUrl() {
  const q = new URLSearchParams(location.search);
  const t = q.get('token');
  if (t) {
    localStorage.setItem('vc_token', t);
    history.replaceState({}, '', location.pathname);
  }
})();

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const t = token();
  if (t) headers['X-Token'] = t;
  if (opts.body && typeof opts.body === 'object') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    if (r.status === 401) {
      showToast('Доступ запрещён');
    }
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

(function buildRing() {
  const ring = $('#led-ring');
  if (!ring) return;
  for (let i = 0; i < 24; i++) {
    const dot = document.createElement('span');
    dot.className = 'ring-dot';
    dot.style.setProperty('--a', `${(360 / 24) * i}deg`);
    dot.style.setProperty('--i', String(i));
    ring.appendChild(dot);
  }
})();

function setRingState(s) {
  const ring = $('#led-ring');
  const label = $('#ring-label');
  const hint = $('#ring-hint');
  const mini = $('#orb-mini');
  if (!ring) return;

  ring.className = 'ring';
  mini?.classList.remove('on', 'music');

  if (s.music) {
    ring.classList.add('state-music');
    label.textContent = 'Музыка';
    hint.textContent = 'играет';
    mini?.classList.add('music');
  } else if (s.service === 'busy') {
    ring.classList.add('state-think');
    label.textContent = 'Занята';
    hint.textContent = 'веб-запрос';
    mini?.classList.add('on');
  } else if (s.service === 'active') {
    ring.classList.add('state-on');
    label.textContent = 'Слушает';
    hint.textContent = s.demo_mode ? '«Дворецкий»' : '«Айдана» / «Дворецкий»';
    mini?.classList.add('on');
  } else {
    ring.classList.add('state-off');
    label.textContent = 'Стоп';
    hint.textContent = 'сервис выключен';
  }
}

const SETTING_LABELS = {
  VC_LANG_MODE: { label: 'Язык', type: 'select', options: ['bilingual', 'ru', 'kk'] },
  VC_STT_ENGINE: { label: 'STT', type: 'select', options: ['deepgram_first', 'deepgram', 'google_first', 'yandex', 'google', 'vosk'] },
  VC_TTS_ENGINE: { label: 'TTS', type: 'select', options: ['yandex', 'edge', 'espeak'] },
  VC_WAKE_COOLDOWN: { label: 'Пауза wake (с)', type: 'number', step: '0.1' },
  VC_RECORD_SEC: { label: 'Запись (с)', type: 'number', step: '1' },
  VC_SILENCE_TIMEOUT: { label: 'Тишина (с)', type: 'number', step: '0.1' },
  VC_WAKE_STT_FALLBACK: { label: 'Google wake fallback', type: 'select', options: ['0', '1'] },
  VC_FAST_LLM: { label: 'Быстрый LLM', type: 'select', options: ['1', '0'] },
  VC_STREAM: { label: 'Стрим TTS', type: 'select', options: ['1', '0'] },
  VC_LED: { label: 'LED', type: 'select', options: ['1', '0'] },
};

function renderSettings(env, editable) {
  const box = $('#settings');
  if (!box) return;
  box.innerHTML = '';
  editable.filter((k) => SETTING_LABELS[k]).forEach((key) => {
    const meta = SETTING_LABELS[key];
    const val = env[key] ?? '';
    const div = document.createElement('div');
    div.className = 'field';
    let inner = `<label>${meta.label}</label>`;
    if (meta.type === 'select') {
      inner += `<select data-key="${key}">${meta.options.map((o) =>
        `<option value="${o}"${String(val) === o ? ' selected' : ''}>${o}</option>`).join('')}</select>`;
    } else {
      inner += `<input data-key="${key}" type="${meta.type}" step="${meta.step || ''}" value="${val}">`;
    }
    div.innerHTML = inner;
    box.appendChild(div);
  });
}

function collectSettings() {
  const out = {};
  $('#settings')?.querySelectorAll('[data-key]').forEach((el) => {
    out[el.dataset.key] = el.value;
  });
  return out;
}

function renderFacts(facts) {
  const list = $('#facts-list');
  const empty = $('#facts-empty');
  if (!list) return;
  list.innerHTML = '';
  const items = facts || [];
  items.forEach((f) => {
    const li = document.createElement('li');
    li.textContent = f;
    list.appendChild(li);
  });
  empty?.classList.toggle('show', items.length === 0);
}

async function refreshStatus() {
  const s = await api('/api/status');
  const pill = $('#status-pill');
  if (pill) {
    const labels = { active: '● Онлайн', busy: '◐ Занята', inactive: '○ Оффлайн' };
    pill.textContent = labels[s.service] || '…';
    pill.className = 'pill ' + (s.service === 'inactive' ? 'bad' : s.service === 'busy' ? 'warn' : 'ok');
  }

  setRingState(s);

  const host = $('#host-label');
  if (host) host.textContent = s.host || 'voice-column';

  const vol = s.volume ?? s.env?.VC_VOLUME_PERCENT ?? 90;
  const led = s.led_brightness ?? s.env?.VC_LED_BRIGHTNESS ?? 35;
  if ($('#volume')) $('#volume').value = vol;
  if ($('#volume-val')) $('#volume-val').textContent = vol + '%';
  if ($('#led')) $('#led').value = led;
  if ($('#led-val')) $('#led-val').textContent = led + '%';

  const env = s.env || {};
  $('#stats').innerHTML = [
    ['STT', env.VC_STT_ENGINE || '—'],
    ['TTS', env.VC_TTS_ENGINE || '—'],
    ['Язык', env.VC_LANG_MODE || '—'],
    ['ZeroClaw', s.zeroclaw],
    ['Память', `${s.memory?.history_count || 0} реплик`],
    ['Факты', s.memory?.facts_count || 0],
  ].map(([k, v]) => `<div class="stat"><b>${k}</b><span>${v}</span></div>`).join('');

  renderFacts(s.memory?.facts || []);
  renderSetup(s.setup);
  toggleDemoPanel(!!s.demo_mode);
  updateDemoButton(!!s.demo_mode);
}

function updateDemoButton(on) {
  const btn = $('#btn-demo');
  if (!btn) return;
  btn.classList.toggle('demo-on', on);
  btn.textContent = on ? '🎭 Демо ✓' : '🎭 Демо';
}

async function setDemoMode(on) {
  const headers = { 'Content-Type': 'application/json' };
  const t = token();
  if (t) headers['X-Token'] = t;
  const body = typeof on === 'boolean' ? { enabled: on } : { toggle: true };
  const r = await fetch('/api/demo', { method: 'POST', headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error('Не удалось переключить демо');
  return r.json();
}

function toggleDemoPanel(on) {
  const box = $('#demo-pipe');
  if (!box) return;
  box.classList.toggle('hidden', !on);
  if (on) refreshDemo().catch(() => {});
}

async function refreshDemo() {
  const box = $('#demo-pipe');
  if (!box || box.classList.contains('hidden')) return;
  const d = await fetch('/api/demo').then((r) => r.json()).catch(() => null);
  if (!d?.pipeline?.length) return;
  const steps = $('#demo-steps');
  const last = $('#demo-last');
  if (!steps) return;
  let start = 0;
  for (let i = d.pipeline.length - 1; i >= 0; i--) {
    if (d.pipeline[i].stage === 'WAKE') {
      start = i;
      break;
    }
  }
  const tail = d.pipeline.slice(start);
  steps.innerHTML = tail.map((row, i) => {
    const active = i === tail.length - 1 ? ' active' : '';
    return `<div class="demo-step${active}"><b>${row.stage}</b><span>${esc(row.detail || '—')}</span></div>`;
  }).join('');
  if (last && d.last_stage) {
    last.textContent = `${d.last_stage}: ${(d.last_detail || '').slice(0, 120)}`;
  }
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderSetup(setup) {
  if (!setup) return;
  const wifiEl = $('#setup-wifi');
  const urlEl = $('#setup-url');
  const mdnsEl = $('#setup-mdns');
  if (wifiEl) {
    wifiEl.textContent = setup.wifi_connected
      ? `Wi-Fi: ${setup.ssid || 'подключено'} · ${setup.ip || setup.hostname}`
      : `Wi-Fi не подключён — сеть «${setup.ap_ssid || 'Kolonka-Setup'}»`;
  }
  if (urlEl && setup.ui_url) {
    urlEl.hidden = false;
    urlEl.textContent = setup.ui_url;
  }
  if (mdnsEl && setup.ui_url_mdns) {
    mdnsEl.hidden = false;
    mdnsEl.textContent = 'Или: ' + setup.ui_url_mdns;
  }
}

$('#btn-copy-url')?.addEventListener('click', async () => {
  const url = $('#setup-url')?.textContent?.trim();
  if (!url || url === '—') {
    showToast('Ссылка недоступна');
    return;
  }
  try {
    await navigator.clipboard.writeText(url);
    showToast('Ссылка скопирована');
  } catch (_) {
    showToast(url);
  }
});

$('#btn-wifi-reset')?.addEventListener('click', async () => {
  if (!confirm('Сбросить Wi-Fi? Колонка перезагрузится и создаст сеть Kolonka-Setup для нового владельца.')) {
    return;
  }
  try {
    await api('/api/wifi/reset', { method: 'POST' });
    showToast('Перезагрузка… подключитесь к Kolonka-Setup');
  } catch (err) {
    showToast(err.message);
  }
});

async function refreshLogs() {
  const d = await api('/api/logs?n=40');
  const el = $('#log');
  if (el) {
    el.textContent = (d.lines || []).join('\n');
    el.scrollTop = el.scrollHeight;
  }
}

async function loadEnv() {
  const d = await api('/api/env');
  renderSettings(d.env, d.editable);
}

$$('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    $$('.tab').forEach((b) => b.classList.remove('active'));
    $$('.panel').forEach((p) => p.classList.remove('active'));
    btn.classList.add('active');
    $(`#panel-${btn.dataset.tab}`)?.classList.add('active');
  });
});

let volTimer;
$('#volume')?.addEventListener('input', (e) => {
  $('#volume-val').textContent = e.target.value + '%';
  clearTimeout(volTimer);
  volTimer = setTimeout(async () => {
    try {
      await api('/api/volume', { method: 'POST', body: { percent: +e.target.value } });
      showToast('Громкость ' + e.target.value + '%');
    } catch (err) { showToast(err.message); }
  }, 400);
});

let ledTimer;
$('#led')?.addEventListener('input', (e) => {
  $('#led-val').textContent = e.target.value + '%';
  clearTimeout(ledTimer);
  ledTimer = setTimeout(async () => {
    try {
      await api('/api/led', { method: 'POST', body: { brightness: +e.target.value } });
      showToast('LED ' + e.target.value + '%');
    } catch (err) { showToast(err.message); }
  }, 400);
});

$('#btn-restart')?.addEventListener('click', () =>
  api('/api/service/restart', { method: 'POST' }).then(() => showToast('Перезапуск…')).catch((e) => showToast(e.message)));

$('#btn-demo')?.addEventListener('click', () =>
  setDemoMode(undefined)
    .then((r) => {
      updateDemoButton(!!r.enabled);
      toggleDemoPanel(!!r.enabled);
      showToast(r.enabled ? 'Демо включено' : 'Демо выключено');
    })
    .catch((e) => showToast(e.message)));

$('#btn-stop')?.addEventListener('click', () =>
  api('/api/service/stop', { method: 'POST' }).then(() => showToast('Стоп')).catch((e) => showToast(e.message)));
$('#btn-start')?.addEventListener('click', () =>
  api('/api/service/start', { method: 'POST' }).then(() => showToast('Старт')).catch((e) => showToast(e.message)));
$('#btn-stop-music')?.addEventListener('click', () =>
  api('/api/music/stop', { method: 'POST' }).then(() => showToast('Музыка стоп')).catch((e) => showToast(e.message)));

$('#music-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#music-input');
  const q = input?.value.trim();
  if (!q) return;
  try {
    const r = await api('/api/music/play', { method: 'POST', body: { query: q } });
    showToast(r.msg || 'Ищу…');
    input.value = '';
    setTimeout(tick, 1500);
  } catch (err) {
    showToast(err.message);
  }
});
$('#btn-clear-mem')?.addEventListener('click', () =>
  api('/api/memory/clear', { method: 'POST' }).then(() => { showToast('Память очищена'); tick(); }).catch((e) => showToast(e.message)));
$('#btn-test')?.addEventListener('click', () =>
  api('/api/say', { method: 'POST', body: { text: 'Привет! Я Колонка, всё работает.', mode: 'speak' } })
    .then(() => showToast('Тест голоса…')).catch((e) => showToast(e.message)));

$('#btn-save-env')?.addEventListener('click', async () => {
  await api('/api/env', { method: 'POST', body: collectSettings() });
  showToast('Сохранено');
  setTimeout(tick, 2000);
});

const chatLog = $('#chat-log');
const chatHistory = JSON.parse(localStorage.getItem('vc_chat') || '[]');

function saveChat() {
  localStorage.setItem('vc_chat', JSON.stringify(chatHistory.slice(-30)));
}

function renderChat() {
  if (!chatLog) return;
  chatLog.innerHTML = chatHistory.map((m) =>
    `<div class="msg ${m.role}">${escapeHtml(m.text)}</div>`).join('');
  chatLog.scrollTop = chatLog.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function addChat(role, text) {
  chatHistory.push({ role, text, ts: Date.now() });
  saveChat();
  renderChat();
}

async function askColumn(text) {
  addChat('user', text);
  addChat('sys', '…');
  try {
    const r = await api('/api/say', { method: 'POST', body: { text, mode: 'ask' } });
    if (chatHistory.length && chatHistory[chatHistory.length - 1].role === 'sys') {
      chatHistory.pop();
    }
    if (r.action === 'music') {
      addChat('bot', '🎵 Ищу и включаю…');
      showToast('Музыка');
      pollForMusic();
      return;
    }
    if (r.action === 'music_stop') {
      addChat('bot', '⏹ ' + (r.msg || 'Остановила.'));
      showToast('Музыка стоп');
      return;
    }
    if (r.action === 'volume') {
      addChat('bot', '🔊 ' + (r.msg || ''));
      showToast(r.msg || 'Громкость');
      return;
    }
    addChat('sys', 'Думаю… (~5–20 с)');
    showToast('Запрос отправлен');
    pollForAnswer();
  } catch (err) {
    if (chatHistory.length && chatHistory[chatHistory.length - 1].role === 'sys') {
      chatHistory.pop();
      saveChat();
      renderChat();
    }
    showToast(err.message);
  }
}

function pollForMusic() {
  let n = 0;
  const iv = setInterval(async () => {
    n += 1;
    if (n > 20) {
      clearInterval(iv);
      return;
    }
    try {
      const s = await api('/api/status');
      if (s.music) {
        addChat('bot', '🎵 Играет');
        clearInterval(iv);
      }
    } catch (_) { /* ignore */ }
  }, 2000);
}

let pollCount = 0;
function pollForAnswer() {
  pollCount = 0;
  const iv = setInterval(async () => {
    pollCount += 1;
    if (pollCount > 24) {
      clearInterval(iv);
      return;
    }
    try {
      const d = await api('/api/logs?n=30');
      const lines = d.lines || [];
      const replyLine = [...lines].reverse().find((l) =>
        l.includes('← stream') || l.includes('← ZeroClaw') || l.includes('← fast LLM'));
      if (replyLine) {
        const m = replyLine.match(/:\s*(.+)$/);
        if (m && m[1].length > 5) {
          if (chatHistory.length && chatHistory[chatHistory.length - 1].role === 'sys') {
            chatHistory.pop();
          }
          addChat('bot', m[1].trim().slice(0, 400));
          clearInterval(iv);
        }
      }
    } catch (_) { /* ignore */ }
  }, 3000);
}

$('#chat-form')?.addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#chat-input');
  const text = input?.value.trim();
  if (!text) return;
  input.value = '';
  askColumn(text).catch((err) => showToast(err.message));
});

renderChat();

async function tick() {
  try {
    await refreshStatus();
    if ($('#log-auto')?.checked) await refreshLogs();
  } catch (e) {
    $('#status-pill').textContent = '✕ Нет связи';
    $('#status-pill').className = 'pill bad';
  }
}

loadEnv().catch(() => {});
tick();
setInterval(tick, 5000);
setInterval(() => {
  if (!$('#demo-pipe')?.classList.contains('hidden')) refreshDemo().catch(() => {});
}, 1200);
