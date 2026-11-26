/* CyberYinYu · 控制台前端
   纯 JS，无构建、无 CDN。所有数据来自 /admin/api/*，写操作只有人格文本三件套。 */
'use strict';

/* ------------------------------------------------------------------ 基础 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) =>
  String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICONS = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
  chat: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/>',
  prompt: '<path d="M4 5h16"/><path d="M4 12h10"/><path d="M4 19h13"/>',
  persona: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  memory: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="1.6"/><path d="M4.5 18l5-5 3.5 3.5L16 13l4 5"/>',
  sliders: '<path d="M4 6h9"/><path d="M17 6h3"/><circle cx="15" cy="6" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/><circle cx="9" cy="12" r="2"/><path d="M4 18h9"/><path d="M17 18h3"/><circle cx="15" cy="18" r="2"/>',
  sticker: '<circle cx="12" cy="12" r="9"/><path d="M9 10h.01"/><path d="M15 10h.01"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0"/>',
  logs: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8Z"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;

/* token 从 URL 读一次就搬进 sessionStorage，并把地址栏里的那份清掉：
   URL 会进浏览器历史、Referer 和代理日志，token 不该待在那里。 */
const TOKEN = (() => {
  const fromUrl = new URLSearchParams(location.search).get('token') || '';
  if (fromUrl) {
    try { sessionStorage.setItem('cyy_webui_token', fromUrl); } catch (e) { /* 隐私模式下写不了，就当没有 */ }
    try {
      const url = new URL(location.href);
      url.searchParams.delete('token');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (e) { /* 老浏览器，留着也无妨 */ }
    return fromUrl;
  }
  try { return sessionStorage.getItem('cyy_webui_token') || ''; } catch (e) { return ''; }
})();
const withToken = (url) =>
  TOKEN ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN) : url;
/* 写接口必须带的自定义头。跨源页面带不出来（预检会先被拒），服务端靠它挡 CSRF。 */
const CSRF_HEADERS = { 'X-WebUI-Request': '1' };
const authHeaders = (extra = {}) => ({ ...CSRF_HEADERS, ...(TOKEN ? { 'X-WebUI-Token': TOKEN } : {}), ...extra });

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...authHeaders(opts.headers || {}) };
  const res = await fetch(withToken(path), { ...opts, headers });
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error(`接口返回的不是 JSON（HTTP ${res.status}）`);
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = 'ok', ms = 2800) {
  const box = document.createElement('div');
  box.className = `toast ${kind}`;
  box.textContent = msg;
  $('#toasts').appendChild(box);
  setTimeout(() => {
    box.style.opacity = '0';
    box.style.transform = 'translateY(6px)';
    box.style.transition = 'all .2s';
    setTimeout(() => box.remove(), 220);
  }, ms);
}

const fmtNum = (n) => Number(n || 0).toLocaleString('zh-CN');
const fmtBytes = (n) => {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}
const nowTime = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });

/* ------------------------------------------------------------------ 主题 */
const THEME_KEY = 'cyy-theme';
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#theme-label').textContent = theme === 'dark' ? '切换浅色' : '切换深色';
  $('#theme-icon').innerHTML = icon(theme === 'dark' ? 'sun' : 'moon');
  localStorage.setItem(THEME_KEY, theme);
}
applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
// 截图/演示时可以用 ?theme=light 直接定住主题，不写进 localStorage 之后也能改回来
const URL_THEME = new URLSearchParams(location.search).get('theme');
if (URL_THEME === 'light' || URL_THEME === 'dark') applyTheme(URL_THEME);
$('#theme-toggle').addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

/* ------------------------------------------------------------------ 路由 */
const PAGES = {
  overview: { label: '概览', icon: 'overview', sub: '运行状态、并发水位与通道开关', mount: mountOverview },
  usage: { label: '用量统计', icon: 'sliders', sub: '查看模型、工具与图像调用次数和 Token 用量', mount: mountUsage },
  chat: { label: '试聊', icon: 'chat', sub: '在隔离会话中验证人格、记忆与模型表现', mount: mountChat },
  prompt: { label: '提示词', icon: 'prompt', sub: '查看最终发给模型的完整 system prompt', mount: mountPrompt },
  persona: { label: '人格文本', icon: 'persona', sub: '管理风格、基础人格与群档案，改动即时生效', mount: mountPersona },
  memory: { label: '记忆', icon: 'memory', sub: '浏览并维护全局事实与各群的长期记忆', mount: mountMemory },
  stickers: { label: '表情', icon: 'sticker', sub: '检查表情别名、图片文件与缺失状态', mount: mountStickers },
  appearance: { label: '外观', icon: 'image', sub: '调整主题、背景与图标，仅影响本机浏览器', mount: mountAppearance },
  settings: { label: '设置', icon: 'sliders', sub: '管理 QQ、模型、通道与并发参数', mount: mountSettings },
  logs: { label: '日志', icon: 'logs', sub: '实时查看 bot.log，按级别与关键字过滤', mount: mountLogs },
};

const NAV_GROUPS = [
  { label: '工作台', kicker: 'WORKSPACE', items: ['overview', 'usage', 'chat'] },
  { label: '人格与内容', kicker: 'PERSONA & CONTENT', items: ['prompt', 'persona', 'memory', 'stickers'] },
  { label: '系统', kicker: 'SYSTEM', items: ['appearance', 'settings', 'logs'] },
];

let cleanup = null;
let lastWs = null;

function renderNav(current) {
  $('#nav').innerHTML = NAV_GROUPS.map((group) => `
    <div class="nav-group">
      <div class="nav-caption">${group.label}</div>
      ${group.items.map((key) => {
        const page = PAGES[key];
        return `<a class="nav-item ${key === current ? 'active' : ''}" href="#/${key}" title="${page.label}">
          ${icon(page.icon)}<span class="lbl">${page.label}</span>
        </a>`;
      }).join('')}
    </div>`
  ).join('');
}

function setWsPill(connected) {
  lastWs = connected;
  const pill = $('#ws-pill');
  pill.className = `pill ${connected ? 'on' : 'off'}`;
  pill.title = connected ? 'NapCat 已连接' : 'NapCat 未连接';
  pill.innerHTML = `<i class="dot"></i><span class="status-full">${connected ? 'NapCat 已连接' : 'NapCat 未连接'}</span><span class="status-compact">${connected ? '已连接' : '未连接'}</span>`;
}

/* 顶栏那颗灯在所有页面都得是准的，所以单走 /status（不需要授权、不查磁盘）。 */
async function refreshWsPill() {
  try {
    const res = await fetch(withToken('/status'));
    const data = await res.json();
    setWsPill(!!data.ws_connected);
  } catch (e) { /* 服务刚重启的瞬间会失败，下一轮补上 */ }
}

async function route(force = false) {
  const key = (location.hash.replace(/^#\/?/, '') || 'overview').split('?')[0];
  const page = PAGES[key] ? key : 'overview';
  if (!force && route.current === page) return;
  route.current = page;
  const version = route.version = (route.version || 0) + 1;

  if (cleanup) {
    try { cleanup(); } catch (e) { /* 页面自己的清理出错不该挡住跳转 */ }
    cleanup = null;
  }
  document.body.classList.remove('nav-open');
  $('#menu-toggle').setAttribute('aria-expanded', 'false');
  renderNav(page);
  $('#page-title').textContent = PAGES[page].label;
  $('#page-sub').textContent = PAGES[page].sub;
  const group = NAV_GROUPS.find((item) => item.items.includes(page));
  $('#page-kicker').textContent = `${group ? group.kicker : 'CONTROL DECK'} / ${page.toUpperCase()}`;
  const view = $('#view');
  view.className = page === 'chat' || page === 'logs' || page === 'usage' ? 'view dense' : 'view';
  view.innerHTML = '<div class="loading">加载中…</div>';
  refreshWsPill();

  try {
    const fn = await PAGES[page].mount(view);
    if (route.version !== version) { if (typeof fn === 'function') fn(); return; }
    cleanup = typeof fn === 'function' ? fn : null;
    $('#sync-note').textContent = `同步于 ${nowTime()}`;
  } catch (err) {    view.innerHTML = `<div class="error-box">加载失败：${esc(err.message)}<br><br>
      如果是 403，说明面板不是从本机打开的；远程访问请配 <code>WEBUI_TOKEN</code> 并在地址后加 <code>?token=</code>。</div>`;
  }
}

window.addEventListener('hashchange', () => route());
$('#btn-refresh').addEventListener('click', async () => {
  const button = $('#btn-refresh');
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add('loading');
  try { await route(true); } finally {
    button.disabled = false;
    button.classList.remove('loading');
  }
});
$('#menu-toggle').addEventListener('click', () => {
  const open = !document.body.classList.contains('nav-open');
  document.body.classList.toggle('nav-open', open);
  $('#menu-toggle').setAttribute('aria-expanded', String(open));
});
$('#nav-scrim').addEventListener('click', () => {
  document.body.classList.remove('nav-open');
  $('#menu-toggle').setAttribute('aria-expanded', 'false');
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    document.body.classList.remove('nav-open');
    $('#menu-toggle').setAttribute('aria-expanded', 'false');
  }
});

/* ------------------------------------------------------------------ 概览 */
/* k 是固定文案，这里统一转义；v 是**已经拼好的 HTML**，调用方必须自己对动态部分
   做 esc()（参数名写成 vHtml 就是提醒这件事）。 */
function kvRow(k, vHtml, mono = true) {
  return `<div class="kv-row"><span class="kv-k">${esc(k)}</span><span class="kv-v ${mono ? '' : 'wide'}">${vHtml}</span></div>`;
}

function overviewHtml(d) {
  const diag = d.diagnostics || {};
  const con = d.concurrency || {};
  const tokens = d.token_usage || {};
  const pct = con.limit ? Math.min(100, Math.round((con.active / con.limit) * 100)) : 0;
  return `
  <section class="overview-hero" aria-label="运行摘要">
    <div class="hero-mark">${icon('overview')}</div>
    <div class="hero-copy">
      <div class="hero-title" id="hero-title">${d.ws.connected ? 'NapCat 已连接' : 'NapCat 未连接'}</div>
      <div class="hero-note">${esc(d.bot.nickname)} · ${esc(d.model.chat)} · 数据每 4 秒自动更新</div>
    </div>
    <div class="hero-actions">
      <a class="quick-link" href="#/chat">${icon('chat')}开始试聊</a>
      <a class="quick-link" href="#/logs">${icon('logs')}查看日志</a>
    </div>
  </section>
  <div class="stat-grid">
    <div class="stat" data-tone="violet">
      <div class="stat-top"><div class="stat-label">已处理消息</div><div class="stat-icon">${icon('chat')}</div></div>
      <div class="stat-value" id="s-msgs">${fmtNum(diag.messages_processed)}</div>
      <div class="stat-note" id="s-msgs-note">收到事件 ${fmtNum(diag.events_received)} · 丢弃 ${fmtNum(diag.events_dropped)}</div>
    </div>
    <div class="stat" data-tone="cyan">
      <div class="stat-top"><div class="stat-label">异常 / 丢弃</div><div class="stat-icon">${icon('logs')}</div></div>
      <div class="stat-value" id="s-dropped">${fmtNum(diag.events_dropped)}</div>
      <div class="stat-note" id="s-dropped-note">取消 ${fmtNum(con.cancelled)} · 队列丢弃 ${fmtNum(con.dropped)}</div>
    </div>
    <div class="stat" data-tone="green">
      <div class="stat-top"><div class="stat-label">已运行</div><div class="stat-icon">${icon('sun')}</div></div>
      <div class="stat-value sm" id="s-uptime">${fmtDuration(d.uptime_seconds)}</div>
      <div class="stat-note" id="s-uptime-note">${esc(d.bot.nickname)} · ${esc(d.bot.qq)}</div>
    </div>
    <div class="stat" data-tone="pink">
      <div class="stat-top"><div class="stat-label">已知 Token <small>持久化累计</small></div><div class="stat-icon">${icon('sliders')}</div></div>
      <div class="stat-value" id="s-tokens">${usageMetric(tokens, 'total_tokens')}</div>
      <div class="stat-note" id="s-tokens-note">输入 ${usageMetric(tokens, 'prompt_tokens')} · 输出 ${usageMetric(tokens, 'completion_tokens')}</div>
    </div>
  </div>

  <div class="grid-2" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><div class="card-title">并发详情 <small>同会话串行 · 跨会话并行</small></div></div>
      <div class="card-body">
        <div class="meter-caption"><span>CONCURRENCY LOAD</span><span id="m-pct">${pct}%</span></div>
        <div class="meter"><div class="meter-fill" id="m-fill" style="width:${pct}%"></div></div>
        <div class="kv" style="margin-top:12px">
          ${kvRow('正在等模型的会话', `<span id="k-active">${con.active ?? 0}</span> / ${con.limit ?? '-'}`)}
          ${kvRow('队列中', `<span id="k-waiting">${con.waiting ?? 0}</span>`)}
          ${kvRow('峰值并发', `<span id="k-peak">${con.peak_active ?? 0}</span>`)}
          ${kvRow('活跃会话数', `<span id="k-convs">${con.conversations ?? 0}</span>`)}
          ${kvRow('完成 / 取消 / 丢弃', `<span id="k-done">${fmtNum(con.completed)}</span> / ${fmtNum(con.cancelled)} / ${fmtNum(con.dropped)}`)}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div class="card-title">通道与模型</div></div>
      <div class="card-body">
        <div class="kv">
          ${kvRow('聊天模型', esc(d.model.chat))}
          ${kvRow('看图模型', `${esc(d.model.vision)}${d.model.vision_split ? ' <span class="dim">（独立端点）</span>' : ''}`)}
          ${kvRow('端点', esc(d.model.host))}
          ${kvRow('Key', `${esc(d.model.key)} · 看图 ${esc(d.model.vision_key)}`)}
          ${kvRow('模型调用', `<span id="k-token-calls">${usageMetric(tokens, 'calls')}</span> 次 · 思考 <span id="k-token-reasoning">${usageMetric(tokens, 'reasoning_tokens')}</span> Token`)}
          ${kvRow('用量完整性', `<span id="k-token-last">${esc(tokens.error || `缺失 ${usageMetric(tokens, 'usage_missing')} · 部分 ${usageMetric(tokens, 'usage_partial')}`)}</span>`)}
          ${kvRow('OneBot', `${esc(d.ws.path)} · ${esc(d.ws.host)}:${esc(d.ws.port)}`, false)}
          ${kvRow('日志文件', esc(d.log_file || '（还没有）'), false)}
        </div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><div class="card-title">功能开关 <small>取自 otowa.env，改配置要重启服务</small></div></div>
    <div class="card-body">
      <div class="chips">
        ${d.features
          .map(
            (f) => `<div class="chip ${f.on ? 'on' : 'off'}"><i class="led"></i>
              <div><div class="chip-name">${esc(f.label)}</div><div class="chip-note">${f.on ? esc(f.note) : '已关闭'}</div></div></div>`
          )
          .join('')}
      </div>
    </div>
  </div>

  <div class="grid-3" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><div class="card-title">运行参数</div></div>
      <div class="card-body"><div class="kv">
        ${d.limits.map((row) => kvRow(row.label, esc(row.value))).join('')}
      </div></div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">人格规模</div></div>
      <div class="card-body"><div class="kv">
        ${kvRow('style.txt', `${fmtNum(d.persona.style_chars)} 字`)}
        ${kvRow('表情别名', `${d.persona.stickers} 个`)}
        ${kvRow('有记忆的群', `${d.persona.groups} 个`)}
        ${kvRow('记忆条目总数', `${d.persona.memory_entries} 条`)}
        ${kvRow('上下文容量', `${d.limits.find((x) => x.label === '上下文轮数')?.value || '-'} 轮`)}
      </div></div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">事件计数</div></div>
      <div class="card-body"><div class="kv">
        ${kvRow('收到事件', fmtNum(diag.events_received))}
        ${kvRow('处理消息', fmtNum(diag.messages_processed))}
        ${kvRow('回复机器人', fmtNum(diag.reply_to_bot_received))}
        ${kvRow('戳一戳', `${fmtNum(diag.pokes_replied)} / ${fmtNum(diag.pokes_received)}（回复/收到）`)}
        ${kvRow('丢弃事件', fmtNum(diag.events_dropped))}
        ${kvRow('最近事件', esc(diag.last_event_type || '—'))}
      </div></div>
    </div>
  </div>`;
}

function paintOverview(d) {
  const diag = d.diagnostics || {};
  const con = d.concurrency || {};
  const tokens = d.token_usage || {};
  const set = (id, val) => { const n = document.getElementById(id); if (n) n.textContent = val; };
  const setHtml = (id, html) => { const n = document.getElementById(id); if (n) n.innerHTML = html; };
  set('s-msgs', fmtNum(diag.messages_processed));
  set('s-msgs-note', `收到事件 ${fmtNum(diag.events_received)} · 丢弃 ${fmtNum(diag.events_dropped)}`);
  set('s-dropped', fmtNum(diag.events_dropped));
  set('s-dropped-note', `取消 ${fmtNum(con.cancelled)} · 队列丢弃 ${fmtNum(con.dropped)}`);
  set('s-uptime', fmtDuration(d.uptime_seconds));
  set('s-tokens', usageMetric(tokens, 'total_tokens'));
  set('s-tokens-note', `输入 ${usageMetric(tokens, 'prompt_tokens')} · 输出 ${usageMetric(tokens, 'completion_tokens')}`);
  set('k-token-calls', usageMetric(tokens, 'calls'));
  set('k-token-reasoning', usageMetric(tokens, 'reasoning_tokens'));
  set('k-token-last', tokens.error || `缺失 ${usageMetric(tokens, 'usage_missing')} · 部分 ${usageMetric(tokens, 'usage_partial')}`);
  set('k-active', con.active ?? 0);
  set('k-waiting', con.waiting ?? 0);
  set('k-peak', con.peak_active ?? 0);
  set('k-convs', con.conversations ?? 0);
  set('k-done', `${fmtNum(con.completed)} / ${fmtNum(con.cancelled)} / ${fmtNum(con.dropped)}`);
  const pct = con.limit ? Math.min(100, Math.round((con.active / con.limit) * 100)) : 0;
  const fill = document.getElementById('m-fill');
  if (fill) fill.style.width = `${pct}%`;
  set('m-pct', `${pct}%`);
  set('hero-title', d.ws.connected ? 'NapCat 已连接' : 'NapCat 未连接');
  $('#sync-note').textContent = `同步于 ${nowTime()}`;
}

async function mountOverview(root) {
  let alive = true;
  const data = await api('/admin/api/overview');
  if (!alive) return null;
  setWsPill(!!data.ws.connected);
  $('#side-bot').textContent = `${data.bot.nickname} · ${data.bot.qq}`;
  root.innerHTML = overviewHtml(data);

  const timer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const d = await api('/admin/api/overview');
      if (!alive) return;
      paintOverview(d);
      setWsPill(!!d.ws.connected);
    } catch (e) { /* 单次轮询失败不打扰用户，下一轮会补上 */ }
  }, 4000);

  root.addEventListener('click', (ev) => {
    if (ev.target.closest('[data-act="refresh"]')) route(true);
  });

  return () => { alive = false; clearInterval(timer); };
}

/* ------------------------------------------------------------------ 用量统计 */
function usageMetric(row, key) { const value = (row || {})[key]; return value == null ? '—' : fmtNum(value); }
function usageRows(items, columns) {
  return Object.entries(items || {}).sort((a, b) => (b[1].total_tokens || b[1].calls || 0) - (a[1].total_tokens || a[1].calls || 0)).map(([name, row]) => `<tr><td><b>${esc(name)}</b></td>${columns.map((key) => `<td class="mono">${usageMetric(row, key)}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${columns.length + 1}" class="dim">暂无调用记录</td></tr>`;
}
function usageTable(title, note, items, columns, labels) {
  return `<div class="card"><div class="card-head"><div class="card-title">${esc(title)} <small>${esc(note)}</small></div></div><div class="card-body tight usage-table-wrap"><table class="table"><thead><tr><th>名称</th>${labels.map((label) => `<th>${esc(label)}</th>`).join('')}</tr></thead><tbody>${usageRows(items, columns)}</tbody></table></div></div>`;
}
function usageHtml(data) {
  const model = data.model || {};
  const columns = ['calls', 'success', 'failed', 'cancelled', 'unknown', 'total_tokens', 'usage_missing', 'usage_partial'];
  const labels = ['实际调用', '成功', '失败', '取消', '未知', '已知 Token', '缺失', '部分'];
  return `<div class="usage-note"><b>SQLite 持久化计量</b><span>时间按服务端 ${esc(data.timezone)}。仅计实际请求；本地拒绝另列。未知值为“—”，已知 Token 是下界，不代表完整账单。</span><span>推理属于输出子集，不重复相加；SDK 内部重试不可见，显式重试分别计数。模型与功能是同一批调用的两种视图，不能相加。</span><span>工具仅归属自身独立 usage，不分摊父模型 Token；普通下载不适用。只保存计量，不保存正文、URL、图片或凭据。</span>${data.storage_errors ? '<b>注意：有计量写入失败，数据不完整</b>' : ''}</div>
    <div class="stat-grid usage-stats">
      <div class="stat" data-tone="violet"><div class="stat-label">模型调用</div><div class="stat-value">${usageMetric(model, 'calls')}</div><div class="stat-note">成功 ${usageMetric(model, 'success')} · 失败 ${usageMetric(model, 'failed')} · 取消 ${usageMetric(model, 'cancelled')}</div></div>
      <div class="stat" data-tone="pink"><div class="stat-label">已知 Token</div><div class="stat-value">${usageMetric(model, 'total_tokens')}</div><div class="stat-note">输入 ${usageMetric(model, 'prompt_tokens')} · 输出 ${usageMetric(model, 'completion_tokens')}</div></div>
      <div class="stat" data-tone="cyan"><div class="stat-label">已知推理 Token</div><div class="stat-value">${usageMetric(model, 'reasoning_tokens')}</div><div class="stat-note">已包含在输出中，不额外累加</div></div>
      <div class="stat" data-tone="green"><div class="stat-label">缺失 / 部分 usage</div><div class="stat-value">${usageMetric(model, 'usage_missing')} / ${usageMetric(model, 'usage_partial')}</div><div class="stat-note">失败也可能已计费，缺失不能估作 0</div></div>
    </div>
    <div class="usage-grid">${usageTable('按模型', '配置模型标识；相同标识跨通道合并', data.models, [...columns, 'prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'images'], [...labels, '输入', '输出', '推理子集', '生成图片'])}</div>
    <div class="usage-grid">${usageTable('按功能', '模型调用归因，图像 Token 已纳入', data.operations, [...columns, 'images'], [...labels, '生成图片'])}</div>
    ${usageTable('工具调用', '独立 usage；未知状态不记成功；图片数是实际搜索结果张数', data.tools, ['calls', 'success', 'failed', 'cancelled', 'unknown', 'unobserved', 'rejected', 'total_tokens', 'usage_missing', 'usage_partial', 'usage_not_applicable', 'images'], ['实际调用', '成功', '失败', '取消', '未知', '不可观测', '本地拒绝', '独立 Token', '缺失', '部分', '不适用', '结果图片'])}`;
}
async function mountUsage(root) {
  const routeVersion = route.version;
  let alive = true, busy = false;
  root.innerHTML = `<div class="usage-toolbar"><label>统计范围 <select class="select" id="usage-period"><option value="today">今天</option><option value="7d">近 7 天（含今天）</option><option value="all">全部</option></select></label><span id="usage-sync" role="status">加载中…</span><span class="dim">每 5 秒自动刷新，隐藏页面暂停</span></div><div id="usage-content"></div>`;
  const select = $('#usage-period', root);
  const content = $('#usage-content', root);
  const sync = $('#usage-sync', root);
  const refresh = async () => {
    if (!alive || busy) return;
    busy = true;
    const period = select.value;
    try {
      const data = await api(`/admin/api/usage?period=${encodeURIComponent(period)}`);
      if (!alive || route.version !== routeVersion) return;
      if (select.value !== period) return;
      content.innerHTML = usageHtml(data);
      sync.textContent = `同步于 ${nowTime()}`;
    } catch (error) {
      if (alive && route.version === routeVersion) sync.textContent = `刷新失败：${error.message}（保留上次数据）`;
    } finally {
      busy = false;
      if (alive && route.version === routeVersion && select.value !== period) refresh();
    }
  };
  const onChange = () => { if (busy) sync.textContent = '正在切换范围…'; else refresh(); };
  select.addEventListener('change', onChange);
  await refresh();
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
  return () => { alive = false; clearInterval(timer); select.removeEventListener('change', onChange); };
}

/* ------------------------------------------------------------------ 试聊 */
const CHAT_KEY = 'cyy-chat-settings';

function chatSettings() {
  try { return JSON.parse(localStorage.getItem(CHAT_KEY)) || {}; } catch (e) { return {}; }
}
function saveChatSettings(s) { localStorage.setItem(CHAT_KEY, JSON.stringify(s)); }

/* 试聊记录放模块里：点「刷新」重挂页面时不会把刚才的对话抹掉。
   它只是浏览器内存，刷新整页就没了——刻意不落 localStorage，免得留下一堆
   带真实群记忆的上下文。 */
const chatCache = { items: [] };

async function mountChat(root) {
  const settings = { mode: 'group', group_id: '', sender: '', heat: false, ...chatSettings() };
  const items = chatCache.items;
  let busy = false;

  root.innerHTML = `
    <div class="chat-shell">
      <div class="chat-tools">
        <label class="field"><span>场景</span>
          <select class="select" id="c-mode">
            <option value="group">群聊</option>
            <option value="private">私聊</option>
          </select>
        </label>
        <label class="field"><span>群号</span>
          <input class="input" id="c-group" placeholder="例如 1000000002" inputmode="numeric">
        </label>
        <label class="field"><span>以谁的身份</span>
          <input class="input" id="c-sender" placeholder="QQ 号，可留空" inputmode="numeric">
        </label>
        <label class="check" style="padding-bottom:9px"><input type="checkbox" id="c-heat"> 发情模式</label>
        <div class="spacer"></div>
        <button class="btn ghost small" id="c-clear" type="button">清空对话</button>
      </div>
      <div class="chat-log" id="chat-log">
        <div class="chat-empty">
          <div class="empty-orbit">${icon('chat')}</div>
          <b>输入试聊消息</b><br>
          线上同一套人格、记忆和工具，只是不经过 QQ；群聊场景会注入该群的长期记忆。<br>
          <span class="dim">Enter 发送 · Shift + Enter 换行 · 每次调用都真实计费</span>
        </div>
      </div>
      <div class="composer">
        <textarea class="input" id="c-input" rows="1" placeholder="说点什么…（Enter 发送 / Shift+Enter 换行）"></textarea>
        <button class="btn primary" id="c-send" type="button">发送</button>
      </div>
    </div>`;

  const log = $('#chat-log', root);
  const input = $('#c-input', root);
  const sendBtn = $('#c-send', root);

  $('#c-mode', root).value = settings.mode;
  $('#c-group', root).value = settings.group_id;
  $('#c-sender', root).value = settings.sender;
  $('#c-heat', root).checked = !!settings.heat;

  const persist = () => {
    const next = {
      mode: $('#c-mode', root).value,
      group_id: $('#c-group', root).value.trim(),
      sender: $('#c-sender', root).value.trim(),
      heat: $('#c-heat', root).checked,
    };
    saveChatSettings(next);
    return next;
  };

  const appendUser = (text, silent = false) => {
    const div = document.createElement('div');
    div.className = 'msg user';
    div.innerHTML = `<div class="who">我</div><div class="bubble-wrap">
      <div class="bubble">${esc(text)}</div><div class="msg-meta">${nowTime()}</div></div>`;
    log.appendChild(div);
    if (!silent) {
      items.push({ role: 'user', text });
      log.scrollTop = log.scrollHeight;
    }
  };

  const appendBot = (data, silent = false) => {
    const segs = (data.segments || []).map((seg) => {
      const parts = (seg.shown || []).map((piece) => {
        if (piece.startsWith('[图片]')) return `<span class="tag">${esc(piece)}</span>`;
        if (piece.startsWith('[表情')) return `<span class="tag">${esc(piece)}</span>`;
        return esc(piece);
      });
      return parts.join('');
    });
    const meta = [
      `${data.elapsed}s`,
      `${data.chars} 字`,
      `${(data.segments || []).length} 段`,
      data.memory_injected ? '已注入群记忆' : '',
      data.heat ? '发情模式' : '',
    ].filter(Boolean).map((t) => `<span>${esc(t)}</span>`).join('');

    const div = document.createElement('div');
    div.className = 'msg bot';
    div.innerHTML = `<div class="who">音</div><div class="bubble-wrap">
      <div class="bubble">${segs.join('<br>') || '<span class="dim">（空回复）</span>'}</div>
      <div class="msg-meta">${meta}</div></div>`;
    log.appendChild(div);
    if (!silent) {
      items.push({ role: 'bot', data });
      log.scrollTop = log.scrollHeight;
    }
  };

  const appendError = (msg) => {
    const div = document.createElement('div');
    div.className = 'msg bot msg-err';
    div.innerHTML = `<div class="who">!</div><div class="bubble-wrap"><div class="bubble">${esc(msg)}</div></div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };

  const appendTyping = () => {
    const div = document.createElement('div');
    div.className = 'msg bot';
    div.id = 'typing';
    div.innerHTML = `<div class="who">音</div><div class="bubble-wrap"><div class="bubble">
      <span class="typing"><i></i><i></i><i></i></span></div></div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };

  // 重挂页面时把上一轮的对话放回去（同一浏览器会话内有效）
  if (items.length) {
    log.innerHTML = '';
    items.forEach((item) => {
      if (item.role === 'user') appendUser(item.text, true);
      else appendBot(item.data, true);
    });
    log.scrollTop = log.scrollHeight;
  }

  const send = async () => {
    const text = input.value.trim();
    if (!text || busy) return;
    const conf = persist();
    if (conf.mode === 'private' && !conf.sender) {
      toast('私聊场景要填一个身份 QQ 号（发情模式按它判定）', 'err');
      return;
    }
    busy = true;
    sendBtn.disabled = true;
    input.value = '';
    input.style.height = 'auto';
    // 历史要在把本条塞进 items 之前截下来，否则这句话会在上下文里出现两次。
    const history = items.slice(-20).map((it) =>
      it.role === 'user'
        ? { role: 'user', content: it.text }
        : { role: 'assistant', content: it.data.reply }
    );
    appendUser(text);
    appendTyping();
    try {
      const data = await api('/admin/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          message: text,
          history,
          mode: conf.mode,
          group_id: conf.mode === 'group' ? conf.group_id : '',
          sender: conf.sender,
          heat: conf.mode === 'private' && conf.heat,
        }),
      });
      $('#typing')?.remove();
      appendBot(data);
    } catch (err) {
      $('#typing')?.remove();
      appendError(err.message);
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  };

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(140, input.scrollHeight)}px`;
  });
  $('#c-clear', root).addEventListener('click', () => {
    items.length = 0;
    log.innerHTML = `<div class="chat-empty">对话已清空。历史不会带到下一轮——每次只把最近 20 条发上去。</div>`;
  });
  $('#c-mode', root).addEventListener('change', () => {
    const privateMode = $('#c-mode', root).value === 'private';
    $('#c-heat', root).disabled = !privateMode;
    $('#c-group', root).disabled = privateMode;
    persist();
  });
  $('#c-mode', root).dispatchEvent(new Event('change'));
  input.focus();
  return () => { busy = false; };
}

/* ------------------------------------------------------------------ 提示词 */
async function mountPrompt(root) {
  let groups = [];
  try {
    const data = await api('/admin/api/memory');
    groups = (data.groups || []).filter((g) => g.count > 0);
  } catch (e) { /* 记忆接口失败不该挡住提示词预览 */ }

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div class="card-title">系统提示词 <small id="p-chars">—</small></div>
        <div class="row">
          <label class="field"><span>注入哪个群的记忆</span>
            <select class="select" id="p-group" style="min-width:220px">
              <option value="">不注入（私聊视角）</option>
              ${groups.map((g) => `<option value="${esc(g.group_id)}">${esc(g.group_id)}${g.group_name ? ' · ' + esc(g.group_name) : ''}（${g.count} 条）</option>`).join('')}
            </select>
          </label>
          <label class="check" style="margin-top:16px"><input type="checkbox" id="p-heat"> 发情模式</label>
          <button class="btn primary small" id="p-load" type="button" style="margin-top:16px">生成</button>
        </div>
      </div>
      <pre class="prompt-box" id="p-box">点「生成」看看模型这一轮读到的东西。</pre>
      <div class="editor-foot">
        <span id="p-note">提示词 = BASE_PROMPT + style.txt + 全局事实 + 群记忆（+ 热模式）。它必须逐字节稳定，前缀缓存才命中。</span>
        <span class="spacer"></span>
        <button class="btn ghost small" id="p-copy" type="button">复制全文</button>
      </div>
    </div>`;

  const box = $('#p-box', root);
  let current = '';

  const load = async () => {
    const groupId = $('#p-group', root).value;
    const heat = $('#p-heat', root).checked ? '1' : '0';
    box.textContent = '生成中…';
    try {
      const data = await api(`/admin/api/prompt?group_id=${encodeURIComponent(groupId)}&heat=${heat}`);
      current = data.prompt;
      box.textContent = data.prompt;
      $('#p-chars', root).textContent = `${data.chars} 字 · ${data.memory_injected ? '含群记忆' : '无群记忆'}`;
    } catch (err) {
      box.textContent = `生成失败：${err.message}`;
    }
  };

  $('#p-load', root).addEventListener('click', load);
  $('#p-copy', root).addEventListener('click', async () => {
    if (!current) return;
    try {
      await navigator.clipboard.writeText(current);
      toast('已复制');
    } catch (e) {
      toast('复制失败，浏览器不给剪贴板权限', 'err');
    }
  });
  await load();
  return null;
}

/* ------------------------------------------------------------------ 人格文本 */
async function mountPersona(root) {
  const data = await api('/admin/api/persona');
  const files = data.files || [];
  let activeKey = files[0]?.key;
  let dirty = false;

  root.innerHTML = `
    <div class="split">
      <div class="file-list" id="pf-list">
        ${files
          .map(
            (f) => `<button class="file-item" data-key="${esc(f.key)}" type="button">
              <div class="fi-name">${esc(f.label)}</div>
              <div class="fi-meta">${esc(f.path)}</div>
              <div class="fi-meta">${fmtBytes(f.size)} · ${esc(f.mtime)}</div>
              <div class="fi-note">${esc(f.note)}</div>
            </button>`
          )
          .join('')}
      </div>
      <div class="card" id="pf-editor"></div>
    </div>`;

  const editor = $('#pf-editor', root);

  const open = (key) => {
    const file = files.find((f) => f.key === key);
    if (!file) return;
    activeKey = key;
    dirty = false;
    $$('.file-item', root).forEach((n) => n.classList.toggle('active', n.dataset.key === key));
    editor.innerHTML = `
      <div class="card-head editor-head">
        <div>
          <div class="card-title">${esc(file.label)}</div>
          <div class="editor-path">${esc(file.path)} · ${esc(file.note)}</div>
        </div>
        <div class="row">
          <span class="editor-path" id="pf-count">${file.chars} 字</span>
        </div>
      </div>
      <textarea class="editor-area" id="pf-text" spellcheck="false"></textarea>
      <div class="editor-foot">
        <span id="pf-state">${file.exists ? '已加载' : '文件不存在，保存会新建'} · 保存时自动备份到 ${esc(data.backup_dir)}/</span>
        <span class="spacer"></span>
        <button class="btn ghost small" id="pf-reload" type="button">撤销修改</button>
        <button class="btn primary small" id="pf-save" type="button">保存</button>
      </div>`;
    const text = $('#pf-text', editor);
    text.value = file.content;
    text.addEventListener('input', () => {
      dirty = true;
      $('#pf-count', editor).textContent = `${text.value.length} 字`;
      $('#pf-state', editor).innerHTML = '<span class="dirty-dot">●</span> 有未保存的修改';
    });
    $('#pf-reload', editor).addEventListener('click', () => open(activeKey));
    $('#pf-save', editor).addEventListener('click', async () => {
      const btn = $('#pf-save', editor);
      btn.disabled = true;
      try {
        const res = await api('/admin/api/persona', {
          method: 'POST',
          body: JSON.stringify({ key: activeKey, content: text.value }),
        });
        file.content = text.value;
        file.size = res.file.size;
        file.chars = res.file.chars;
        file.mtime = res.file.mtime;
        dirty = false;
        $('#pf-count', editor).textContent = `${res.file.chars} 字`;
        $('#pf-state', editor).textContent = `已保存 ${res.file.mtime} · 备份 ${res.file.backup || '（无）'}`;
        toast(`${file.label} 已保存（备份 ${res.file.backup || '无'}）`);
      } catch (err) {
        toast(err.message, 'err', 4200);
      } finally {
        btn.disabled = false;
      }
    });
  };

  $('#pf-list', root).addEventListener('click', (ev) => {
    const btn = ev.target.closest('.file-item');
    if (!btn || btn.dataset.key === activeKey) return;
    if (dirty && !confirm('当前文件有未保存的修改，确定切换？')) return;
    open(btn.dataset.key);
  });

  const guard = (ev) => { if (dirty) ev.preventDefault(); };
  addEventListener('beforeunload', guard);
  open(activeKey);
  return () => removeEventListener('beforeunload', guard);
}

/* ------------------------------------------------------------------ 外观 */
const BG_KEY = 'cyy-bg';
const BG_CONTRAST_KEY = 'cyy-bg-readability-v1';

function bgSettings() {
  try {
    let conf = JSON.parse(localStorage.getItem(BG_KEY)) || {};
    if (!localStorage.getItem(BG_CONTRAST_KEY)) {
      conf = { ...conf, veil: Math.max(Number(conf.veil ?? 0.76), 0.76) };
      localStorage.setItem(BG_KEY, JSON.stringify(conf));
      localStorage.setItem(BG_CONTRAST_KEY, '1');
    }
    return conf;
  } catch (e) { return {}; }
}
function saveBgSettings(next) { localStorage.setItem(BG_KEY, JSON.stringify(next)); }

function applyBackground(conf) {
  const layer = $('#bg-layer');
  const veil = $('#bg-veil');
  if (!layer || !veil) return;
  const name = conf.name || '';
  if (name) {
    layer.style.backgroundImage = `url("${withToken('/admin/api/backgrounds/' + encodeURIComponent(name))}")`;
    layer.style.filter = conf.blur ? `blur(${conf.blur}px)` : '';
    layer.style.transform = conf.blur ? 'scale(1.04)' : '';
    layer.classList.add('on');
    document.body.classList.add('bg-on');
    veil.style.opacity = String(conf.veil ?? 0.76);
  } else {
    layer.classList.remove('on');
    layer.style.backgroundImage = '';
    layer.style.filter = '';
    layer.style.transform = '';
    document.body.classList.remove('bg-on');
    veil.style.opacity = '0';
  }
}

function uploadBackground(file) {
  const form = new FormData();
  form.append('file', file);
  const headers = authHeaders();  // 含 X-WebUI-Request，服务端靠它挡跨源写
  return fetch(withToken('/admin/api/backgrounds'), { method: 'POST', body: form, headers })
    .then((res) => res.json().then((data) => {
      if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }));
}

async function mountAppearance(root) {
  let conf = bgSettings();
  let items = [];
  try {
    const data = await api('/admin/api/backgrounds');
    items = data.items || [];
  } catch (e) { /* 目录还没建也算正常，下面空态能表达 */ }

  root.innerHTML = `
    <div class="stack">
        <div class="card">
          <div class="card-head"><div class="card-title">背景显示 <small>只存在这台浏览器</small></div></div>
          <div class="card-body">
            <div class="slider-row">
              <span>遮罩浓度</span>
              <input type="range" id="bg-veil" min="20" max="95" step="1" value="${Math.round((conf.veil ?? 0.76) * 100)}">
              <span class="val" id="bg-veil-val">${Math.round((conf.veil ?? 0.76) * 100)}%</span>
            </div>
            <div class="slider-row">
              <span>背景模糊</span>
              <input type="range" id="bg-blur" min="0" max="16" step="1" value="${conf.blur || 0}">
              <span class="val" id="bg-blur-val">${conf.blur || 0}px</span>
            </div>
            <div class="form-foot" style="margin-top:12px;font-size:11.5px;color:var(--dim)">
              遮罩越高文字越清楚，模糊只影响背景图。这些偏好存在 localStorage，换浏览器要重设。
            </div>
          </div>
        </div>

      <div class="card">
        <div class="card-head">
          <div class="card-title">背景图库 <small>data/webui/backgrounds</small></div>
          <div class="row">
            <button class="btn ghost small" id="bg-default" type="button">恢复默认</button>
            <button class="btn primary small" id="bg-pick" type="button">上传图片</button>
          </div>
        </div>
        <div class="card-body">
          <div class="upload-zone" id="bg-drop">
            <span>把图片拖到这里，或点右上角「上传图片」 · PNG / JPG / WEBP / GIF · 单张 ≤ 8 MB</span>
          </div>
          <div class="bg-grid" id="bg-grid" style="margin-top:14px"></div>
        </div>
      </div>
    </div>`;

  const grid = $('#bg-grid', root);
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/png,image/jpeg,image/webp,image/gif';
  fileInput.style.display = 'none';
  root.appendChild(fileInput);

  const drawGrid = () => {
    const cards = [];
    cards.push(`
      <div class="bg-card ${conf.name ? '' : 'selected'}" data-name="">
        <div class="shot none">无背景（霓虹渐变）</div>
        <div class="meta"><span>preset</span><span>内置</span></div>
      </div>`);
    items.forEach((item) => {
      cards.push(`
        <div class="bg-card ${conf.name === item.name ? 'selected' : ''}" data-name="${esc(item.name)}">
          <button class="del" data-del="${esc(item.name)}" title="删除">×</button>
          <div class="shot" style="background-image:url('${esc(withToken(item.url))}')"></div>
          <div class="meta"><span>${esc(item.added.slice(5))}</span><span>${fmtBytes(item.size)}</span></div>
        </div>`);
    });
    grid.innerHTML = cards.join('');
  };

  const pick = (name) => {
    conf = { ...conf, name };
    saveBgSettings(conf);
    applyBackground(conf);
    drawGrid();
  };

  grid.addEventListener('click', async (ev) => {
    const del = ev.target.closest('[data-del]');
    if (del) {
      ev.stopPropagation();
      if (!confirm('删掉这张背景图？文件会从磁盘移除。')) return;
      try {
        await api(`/admin/api/backgrounds/${encodeURIComponent(del.dataset.del)}`, { method: 'DELETE' });
        items = items.filter((i) => i.name !== del.dataset.del);
        if (conf.name === del.dataset.del) pick('');
        else drawGrid();
        toast('已删除');
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
    const card = ev.target.closest('.bg-card');
    if (card) pick(card.dataset.name);
  });

  const doUpload = async (file) => {
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { toast('图片超过 8 MB', 'err'); return; }
    toast('上传中…', 'ok', 1200);
    try {
      const data = await uploadBackground(file);
      items.unshift(data.item);
      pick(data.item.name);
      toast('已设为背景');
    } catch (err) { toast(err.message, 'err', 4200); }
  };

  $('#bg-pick', root).addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { doUpload(fileInput.files[0]); fileInput.value = ''; });
  $('#bg-default', root).addEventListener('click', () => { pick(''); toast('已恢复默认背景'); });

  const drop = $('#bg-drop', root);
  ['dragenter', 'dragover'].forEach((name) =>
    drop.addEventListener(name, (ev) => { ev.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((name) =>
    drop.addEventListener(name, () => drop.classList.remove('drag')));
  drop.addEventListener('drop', (ev) => {
    ev.preventDefault();
    doUpload(ev.dataTransfer?.files?.[0]);
  });
  drop.addEventListener('click', () => fileInput.click());

  const veil = $('#bg-veil', root);
  const blur = $('#bg-blur', root);
  veil.addEventListener('input', () => {
    conf = { ...conf, veil: Number(veil.value) / 100 };
    $('#bg-veil-val', root).textContent = `${veil.value}%`;
    saveBgSettings(conf);
    applyBackground(conf);
  });
  blur.addEventListener('input', () => {
    conf = { ...conf, blur: Number(blur.value) };
    $('#bg-blur-val', root).textContent = `${blur.value}px`;
    saveBgSettings(conf);
    applyBackground(conf);
  });

  drawGrid();
  return null;
}

/* ------------------------------------------------------------------ 记忆 */
const MEM_CATEGORIES = [
  ['group_context', '群背景', '介绍这个群是什么、因什么聚在一起，以及稳定的群体设定。', '例：本群是独立游戏开发交流群，成员主要讨论程序与美术。'],
  ['shared_context', '共同背景', '记录群成员都知道的经历、人物关系或共同约定。', '例：群友大多参加过今年的线下GameJam，彼此以作品名相称。'],
  ['recurring_topics', '常聊话题', '记录这个群反复讨论、长期感兴趣的内容方向。', '例：群里经常讨论独立游戏发行、Steam新品节和像素画。'],
  ['inside_jokes', '群内梗', '记录只有群成员才懂的笑点、典故和固定说法。', '例：“先发试玩版”是群里的老梗，用来调侃迟迟不发布的项目。'],
  ['group_preferences', '群偏好', '记录交流方式、内容尺度、称呼和互动习惯等偏好。', '例：群友偏好直接的技术反馈，不喜欢只发“挺好的”之类敷衍回复。'],
  ['ongoing_threads', '持续话题', '记录仍在推进、需要后续接续的具体事情或未完成讨论。', '例：群主正在筹备十二月联机试玩活动，报名时间尚未确定。'],
];
const memCategory = (value) => MEM_CATEGORIES.find(([key]) => key === value) || MEM_CATEGORIES[0];

async function mountMemory(root) {
  let data = await api('/admin/api/memory');
  let groups = data.groups || [];
  let globals = data.global_entries || [];
  const memes = data.memes || [];
  let tab = 'groups';
  let selected = groups.find((g) => g.count > 0)?.group_id || groups[0]?.group_id || '';
  let detail = null;
  let editing = null; // 群记忆表单：{id, category, content} | null
  let globalEditing = null; // 全局表单：{id, topic, content} | null

  root.innerHTML = `
    <details class="memory-workflow memory-help">
      <summary><strong>记忆使用帮助</strong><span>选范围 → 选类型 → 写一句独立可读的内容</span></summary>
      <ol class="memory-steps">
        <li><span>1</span><div><b>选择作用范围</b><small>当前群 / 所有对话</small></div></li>
        <li><span>2</span><div><b>判断记忆类型</b><small>看用途说明与示例</small></div></li>
        <li><span>3</span><div><b>填写并保存</b><small>一句独立可读的话</small></div></li>
      </ol>
    </details>
    <div class="card memory-card">
      <div class="card-head memory-tabs-head">
        <div>
          <div class="memory-section-kicker">作用范围</div>
          <div class="tabs" id="m-tabs">
            <button class="tab active" data-tab="groups" type="button">群记忆 <small>按群维护</small></button>
            <button class="tab" data-tab="global" type="button">全局事实 <small>所有对话</small></button>
            <button class="tab" data-tab="memes" type="button">本地梗库 <small>只读</small></button>
          </div>
        </div>
        <div class="editor-path" id="m-hint">写入走存储层自己的锁与校验，和机器人改记忆是同一条路</div>
      </div>
      <div id="m-body"></div>
    </div>`;

  const body = $('#m-body', root);

  const refreshIndex = async () => {
    data = await api('/admin/api/memory');
    groups = data.groups || [];
    globals = data.global_entries || [];
  };

  const renderDetail = () => {
    const box = document.getElementById('m-detail');
    if (!box) return;
    if (!selected) { box.innerHTML = '<div class="loading">还没有群记忆文件</div>'; return; }
    if (!detail) { box.innerHTML = '<div class="loading">读取中…</div>'; return; }

    const activeCategory = editing ? memCategory(editing.category) : MEM_CATEGORIES[0];
    const form = editing ? `
      <div class="mem-form memory-editor">
        <div class="memory-form-heading">
          <div>
            <span class="memory-scope">群记忆 · 仅当前群生效</span>
            <div class="memory-form-title">${editing.id ? '编辑这条群记忆' : '新增一条群记忆'}</div>
          </div>
          <span class="memory-scope-note">不会影响其他群</span>
        </div>
        <div class="memory-form-grid">
          <label class="field memory-category-field">
            <span>选择记忆类型</span>
            <select class="select" id="mf-cat" aria-describedby="mf-type-help">
              ${MEM_CATEGORIES.map(([value, label]) =>
                `<option value="${value}" ${editing.category === value ? 'selected' : ''}>${label}</option>`).join('')}
            </select>
          </label>
          <div class="memory-type-help" id="mf-type-help">
            <span class="tag-cat cat-${activeCategory[0]}" id="mf-type-badge">${activeCategory[1]}</span>
            <div>
              <div class="memory-type-desc" id="mf-type-desc">${activeCategory[2]}</div>
              <div class="memory-example" id="mf-type-example">${activeCategory[3]}</div>
            </div>
          </div>
        </div>
        <details class="memory-type-guide" id="mf-type-guide">
          <summary>分不清？查看全部 6 种类型的区别</summary>
          <div class="memory-guide-list">
            ${MEM_CATEGORIES.map(([value, label, description]) => `
              <div class="memory-guide-item">
                <span class="tag-cat cat-${value}">${label}</span>
                <span>${description}</span>
              </div>`).join('')}
          </div>
        </details>
        <label class="field memory-content-field">
          <span class="memory-content-label">填写群记忆内容 <small>当前类型：<b id="mf-writing-type">${activeCategory[1]}</b></small></span>
          <textarea class="input" id="mf-text" aria-describedby="mf-type-help" placeholder="${esc(activeCategory[3])} 写成一句能独立看懂的话；涉及人物时，主语请锚定 QQ 号。">${esc(editing.content)}</textarea>
        </label>
        <div class="form-foot">
          <span>${editing.id ? '编辑 ' + esc(editing.id) : '新增条目'}</span>
          <span id="mf-count">${(editing.content || '').length}/${300}</span>
          <span class="spacer"></span>
          <button class="btn ghost small" data-act="cancel" type="button">取消</button>
          <button class="btn primary small" data-act="save" type="button">保存</button>
        </div>
      </div>` : '';

    const list = detail.entries.length
      ? detail.entries.map((e) => `
        <div class="mem-entry">
          <div class="mem-content">${esc(e.content)}</div>
          <div class="mem-top">
            <span class="tag-cat cat-${esc(e.category)}" title="${esc(memCategory(e.category)[2])}">${esc(e.category_label)}</span>
            <span class="mem-actions">
              <button class="mini-btn" data-act="edit" data-id="${esc(e.id)}" type="button">编辑</button>
              <button class="mini-btn danger" data-act="del" data-id="${esc(e.id)}" type="button">删除</button>
            </span>
          </div>
          <details class="mem-details">
            <summary>来源与详情</summary>
            <div class="mem-meta">${esc(e.updated_at || '时间未知')}${e.source ? ' · ' + esc(e.source) : ''}</div>
            <div class="mem-id"><span class="mono">${esc(e.id)}</span><button class="mini-btn" data-act="copy-id" data-id="${esc(e.id)}" type="button">复制 ID</button></div>
          </details>
        </div>`).join('')
      : '<div class="empty">这个群还没有记忆条目。点右上角「+ 新增」写一条。</div>';

    box.innerHTML = `
      <div class="log-tools" style="border-bottom:1px solid var(--line-soft)">
        <span class="mono">${esc(detail.group_id)}</span>
        <span class="muted">${esc(detail.group_name || '未命名群')}</span>
        <span class="spacer"></span>
        <span class="num-badge ${detail.entries.length ? 'hot' : ''}">${detail.entries.length} 条</span>
        <button class="mini-btn" data-act="toggle" type="button">记忆：${detail.enabled ? '开' : '关'}</button>
        <button class="mini-btn" data-act="new" type="button">+ 新增</button>
      </div>
      ${form}
      <div class="card-body scroll tight">${list}</div>`;
  };

  const loadDetail = async () => {
    if (!selected) { detail = null; renderDetail(); return; }
    detail = null;
    renderDetail();
    try {
      detail = await api(`/admin/api/memory/${encodeURIComponent(selected)}`);
    } catch (err) {
      detail = { group_id: selected, group_name: '', enabled: true, entries: [], error: err.message };
    }
    renderDetail();
  };

  const renderGroups = () => {
    body.innerHTML = `
      <div class="memory-layout">
        <aside class="memory-index" aria-label="群记忆导航">
          <div class="memory-index-head">
            <div class="memory-index-title">
              <div><span>GROUP INDEX</span><strong>选择要维护的群</strong></div>
              <span class="num-badge">${groups.length} 个</span>
            </div>
            <label class="memory-group-search">
              <span>搜索群号或群名</span>
              <input class="input" id="m-group-search" type="search" placeholder="输入关键词筛选…" autocomplete="off">
            </label>
            <label class="memory-group-mobile">
              <span>当前维护的群</span>
              <select class="select" id="m-group-select">
                ${groups.map((g) => `<option value="${esc(g.group_id)}" ${g.group_id === selected ? 'selected' : ''}>${esc(g.group_id)}${g.group_name ? ' · ' + esc(g.group_name) : ''}（${g.count} 条）</option>`).join('')}
              </select>
            </label>
          </div>
          <div class="memory-table-wrap">
            <table class="table">
              <thead><tr><th>群号 / 群名</th><th>条数</th><th>状态</th><th>最近更新</th></tr></thead>
              <tbody>
                ${groups.length ? groups.map((g) => `
                  <tr class="clickable ${g.group_id === selected ? 'selected' : ''}" data-gid="${esc(g.group_id)}" data-search="${esc((g.group_id + ' ' + (g.group_name || '')).toLowerCase())}">
                    <td><div class="memory-group-id mono">${esc(g.group_id)}</div><div class="memory-group-name">${g.group_name ? esc(g.group_name) : '未命名群'}</div></td>
                    <td><span class="num-badge ${g.count ? 'hot' : ''}">${g.count}</span></td>
                    <td><span class="memory-group-state ${g.enabled ? '' : 'off'}">${g.enabled ? '开启' : '关闭'}</span></td>
                    <td class="mono dim">${esc(g.latest || '—')}</td>
                  </tr>`).join('') : '<tr><td colspan="4" class="empty">还没有任何群记忆文件</td></tr>'}
              </tbody>
            </table>
          </div>
          <div class="empty" id="m-group-filter-empty" hidden>没有匹配的群，试试其他群号或群名。</div>
        </aside>
        <div class="memory-detail" id="m-detail"></div>
      </div>`;

    const chooseGroup = (groupId) => {
      if (!groupId || groupId === selected) return;
      if (editing && !confirm('表单还没保存，切换群会丢掉，继续？')) return;
      selected = groupId;
      editing = null;
      body.querySelectorAll('tr.clickable').forEach((r) => r.classList.toggle('selected', r.dataset.gid === selected));
      const picker = body.querySelector('#m-group-select');
      if (picker) picker.value = selected;
      loadDetail();
    };
    body.querySelectorAll('tr.clickable').forEach((row) => {
      row.addEventListener('click', () => chooseGroup(row.dataset.gid));
    });
    body.querySelector('#m-group-select')?.addEventListener('change', (ev) => {
      const picker = ev.target;
      const previous = selected;
      chooseGroup(picker.value);
      if (selected === previous) picker.value = previous;
    });
    body.querySelector('#m-group-search')?.addEventListener('input', (ev) => {
      const query = ev.target.value.trim().toLowerCase();
      let visible = 0;
      body.querySelectorAll('tr.clickable').forEach((row) => {
        const matched = !query || row.dataset.search.includes(query);
        row.hidden = !matched;
        if (matched) visible += 1;
      });
      const empty = body.querySelector('#m-group-filter-empty');
      if (empty) empty.hidden = visible > 0;
    });
    renderDetail();
  };

  const globalHtml = () => {
    const form = globalEditing ? `
      <div class="mem-form memory-editor">
        <div class="memory-form-heading">
          <div>
            <span class="memory-scope global">全局事实 · 所有对话生效</span>
            <div class="memory-form-title">${globalEditing.id ? '编辑这条全局事实' : '新增一条全局事实'}</div>
          </div>
          <span class="memory-scope-note">包括所有群聊和私聊</span>
        </div>
        <div class="memory-substep">02 · 确认内容属于全局事实</div>
        <div class="memory-type-help global">
          <span class="tag-cat cat-global">全局事实</span>
          <div class="memory-type-desc">只写在世界观、称呼或基础设定层面始终成立的内容；某个群独有的背景请放到「群记忆」。</div>
        </div>
        <div class="memory-global-fields">
          <label class="field">
            <span>事实主题 <small>用于快速识别</small></span>
            <input class="input" id="gf-topic" maxlength="40" placeholder="例如：称呼、世界观、固定设定" value="${esc(globalEditing.topic || '')}">
          </label>
          <label class="field memory-content-field">
            <span class="memory-content-label">03 · 填写全局事实内容 <small>每条都会进入每一轮提示词</small></span>
            <textarea class="input" id="gf-text" placeholder="例如：机器人在所有对话中都称呼用户为“老师”。">${esc(globalEditing.content || '')}</textarea>
          </label>
        </div>
        <div class="form-foot">
          <span>${globalEditing.id ? '编辑 ' + esc(globalEditing.id) : '新增全局事实'}</span>
          <span id="gf-count">${(globalEditing.content || '').length}/${300}</span>
          <span class="spacer"></span>
          <button class="btn ghost small" data-act="gcancel" type="button">取消</button>
          <button class="btn primary small" data-act="gsave" type="button">保存</button>
        </div>
      </div>` : '';

    const list = globals.length
      ? globals.map((g) => `
        <div class="mem-entry">
          <div class="mem-content">${esc(g.content || '')}</div>
          <div class="mem-top">
            <span class="tag-cat cat-global" title="所有群聊和私聊都会读取">全局事实</span>
            ${g.topic ? `<strong>${esc(g.topic)}</strong>` : ''}
            <span class="mem-actions">
              ${g.id ? `
                <button class="mini-btn" data-act="gedit" data-id="${esc(g.id)}" type="button">编辑</button>
                <button class="mini-btn danger" data-act="gdel" data-id="${esc(g.id)}" type="button">删除</button>`
                : '<span class="dim" style="font-size:11px" title="早期条目没有 id：新增或改任何一条后会补齐">旧格式</span>'}
            </span>
          </div>
          <details class="mem-details">
            <summary>来源与详情</summary>
            <div class="mem-meta">${esc(g.source || '来源未知')}${g.updated_at ? ' · ' + esc(g.updated_at) : ''}</div>
            ${g.id ? `<div class="mem-id"><span class="mono">${esc(g.id)}</span><button class="mini-btn" data-act="copy-id" data-id="${esc(g.id)}" type="button">复制 ID</button></div>` : ''}
          </details>
        </div>`).join('')
      : '<div class="empty">还没有全局事实。它们会注入每一轮对话，慎用。</div>';

    return `
      <div class="log-tools" style="border-bottom:1px solid var(--line-soft)">
        <span class="muted">memory/global.json</span>
        <span class="spacer"></span>
        <span class="num-badge">${globals.length} 条</span>
        <button class="mini-btn" data-act="gnew" type="button">+ 新增</button>
      </div>
      ${form}
      <div class="card-body scroll tight">${list}</div>`;
  };

  const renderMemes = () => {
    body.innerHTML = memes.length
      ? `<div class="card-body tight"><table class="table">
          <thead><tr><th>梗</th><th>关键词</th><th>状态</th><th>说明</th></tr></thead>
          <tbody>${memes.map((m) => `
            <tr>
              <td><strong>${esc(m.name)}</strong></td>
              <td class="muted">${(m.keywords || []).map((k) => `<code>${esc(k)}</code>`).join(' ')}</td>
              <td>${m.enabled ? '<span class="pill on"><i class="dot"></i><span>启用</span></span>' : '<span class="muted">停用</span>'}</td>
              <td class="muted">${esc(m.trigger).slice(0, 90)}</td>
            </tr>`).join('')}</tbody></table></div>`
      : '<div class="empty">memory/memes.json 里没有条目</div>';
  };

  const draw = () => {
    if (tab === 'groups') { renderGroups(); loadDetail(); }
    else if (tab === 'global') body.innerHTML = globalHtml();
    else renderMemes();
  };

  const formValues = (form, selector) => {
    const node = form.querySelector(selector);
    return node ? node.value : '';
  };

  body.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    const form = btn.closest('.mem-form');

    if (act === 'copy-id') {
      try { await navigator.clipboard.writeText(btn.dataset.id || ''); toast('ID 已复制'); }
      catch (err) { toast('复制失败，浏览器未授予剪贴板权限', 'err'); }
      return;
    }
    if (act === 'new') {
      editing = { id: '', category: MEM_CATEGORIES[0][0], content: '' };
      renderDetail();
      body.querySelector('#mf-text')?.focus();
      return;
    }
    if (act === 'edit') {
      const entry = detail?.entries.find((e) => e.id === btn.dataset.id);
      if (!entry) return;
      const matched = MEM_CATEGORIES.find(([value]) => value === entry.category);
      editing = { id: entry.id, category: matched ? matched[0] : MEM_CATEGORIES[0][0], content: entry.content };
      renderDetail();
      return;
    }
    if (act === 'cancel') { editing = null; renderDetail(); return; }
    if (act === 'save' && form) {
      const category = formValues(form, '#mf-cat');
      const content = formValues(form, '#mf-text');
      btn.disabled = true;
      try {
        await api(`/admin/api/memory/${encodeURIComponent(selected)}/entries`, {
          method: 'POST',
          body: JSON.stringify({ id: editing?.id || '', category, content }),
        });
        toast('已保存');
        editing = null;
        await refreshIndex();
        renderGroups(); // 顺手刷新左边条数和开关状态
        await loadDetail();
      } catch (err) {
        toast(err.message, 'err', 5200);
      } finally { btn.disabled = false; }
      return;
    }
    if (act === 'del') {
      if (!confirm('删掉这条记忆？删了就找不回来了（文件里不会留副本）。')) return;
      try {
        await api(`/admin/api/memory/${encodeURIComponent(selected)}/entries/${encodeURIComponent(btn.dataset.id)}`, { method: 'DELETE' });
        toast('已删除');
        editing = null;
        await refreshIndex();
        renderGroups();
        await loadDetail();
      } catch (err) { toast(err.message, 'err', 5200); }
      return;
    }
    if (act === 'toggle') {
      const want = !(detail?.enabled ?? true);
      if (!confirm(want ? '打开这个群的记忆？' : '关闭后不再注入、也不会自动写入新条目。确定关闭？')) return;
      try {
        await api(`/admin/api/memory/${encodeURIComponent(selected)}/toggle`, {
          method: 'POST',
          body: JSON.stringify({ enabled: want }),
        });
        toast(want ? '记忆已开启' : '记忆已关闭');
        await refreshIndex();
        renderGroups();
        await loadDetail();
      } catch (err) { toast(err.message, 'err', 5200); }
      return;
    }
    if (act === 'gnew') {
      globalEditing = { id: '', topic: '', content: '' };
      body.innerHTML = globalHtml();
      body.querySelector('#gf-text')?.focus();
      return;
    }
    if (act === 'gedit') {
      const entry = globals.find((g) => (g.id || '') === btn.dataset.id);
      if (!entry) return;
      globalEditing = { id: entry.id || '', topic: entry.topic || '', content: entry.content || '' };
      body.innerHTML = globalHtml();
      return;
    }
    if (act === 'gcancel') { globalEditing = null; body.innerHTML = globalHtml(); return; }
    if (act === 'gsave' && form) {
      const topic = formValues(form, '#gf-topic');
      const content = formValues(form, '#gf-text');
      btn.disabled = true;
      try {
        await api('/admin/api/global/entries', {
          method: 'POST',
          body: JSON.stringify({ id: globalEditing?.id || '', topic, content }),
        });
        toast('已保存');
        globalEditing = null;
        await refreshIndex();
        body.innerHTML = globalHtml();
      } catch (err) {
        toast(err.message, 'err', 5200);
      } finally { btn.disabled = false; }
      return;
    }
    if (act === 'gdel') {
      if (!confirm('删掉这条全局事实？它会在下一轮对话立刻消失。')) return;
      try {
        await api(`/admin/api/global/entries/${encodeURIComponent(btn.dataset.id)}`, { method: 'DELETE' });
        toast('已删除');
        await refreshIndex();
        body.innerHTML = globalHtml();
      } catch (err) { toast(err.message, 'err', 5200); }
    }
  });

  // 类型切换时保留已输入内容，只同步说明、示例和输入区徽标。
  body.addEventListener('change', (ev) => {
    if (ev.target.id !== 'mf-cat') return;
    const category = memCategory(ev.target.value);
    const badge = body.querySelector('#mf-type-badge');
    if (badge) {
      badge.className = `tag-cat cat-${category[0]}`;
      badge.textContent = category[1];
    }
    const description = body.querySelector('#mf-type-desc');
    const example = body.querySelector('#mf-type-example');
    const writingType = body.querySelector('#mf-writing-type');
    const text = body.querySelector('#mf-text');
    if (description) description.textContent = category[2];
    if (example) example.textContent = category[3];
    if (writingType) writingType.textContent = category[1];
    if (text) text.placeholder = `${category[3]} 写成一句能独立看懂的话；涉及人物时，主语请锚定 QQ 号。`;
  });

  // 输入时的实时字数（300 字是存储层硬上限，早点让人看见）
  body.addEventListener('input', (ev) => {
    const target = ev.target;
    if (target.id === 'mf-text' || target.id === 'gf-text') {
      const counter = body.querySelector(target.id === 'mf-text' ? '#mf-count' : '#gf-count');
      if (counter) counter.textContent = `${target.value.length}/300`;
    }
  });

  $('#m-tabs', root).addEventListener('click', (ev) => {
    const btn = ev.target.closest('.tab');
    if (!btn) return;
    if (tab === 'groups' && editing && !confirm('表单还没保存，切换会丢掉，继续？')) return;
    tab = btn.dataset.tab;
    editing = null;
    globalEditing = null;
    $$('.tab', root).forEach((t) => t.classList.toggle('active', t === btn));
    $('#m-hint', root).textContent = tab === 'memes'
      ? '梗库字段多（出处/用法/立场），改坏一条会污染提示词，暂时只读'
      : '写入走存储层自己的锁与校验，和机器人改记忆是同一条路';
    draw();
  });

  draw();
  return null;
}

/* ------------------------------------------------------------------ 设置 */
const SECRET_SENTINEL = '\u0000'; // 只存在于前端：表示「这一项要清空」

function fieldInput(field, value) {
  const key = esc(field.key);
  if (field.type === 'bool') {
    const current = value === '' ? '' : String(value).toLowerCase() === 'true' ? 'true' : 'false';
    return `<select class="input" data-key="${key}">
      ${field.is_set ? '' : `<option value="" selected>继承默认（${String(field.effective).toLowerCase() === 'true' ? '开' : '关'}）</option>`}
      <option value="true" ${current === 'true' ? 'selected' : ''}>开</option>
      <option value="false" ${current === 'false' ? 'selected' : ''}>关</option>
    </select>`;
  }
  if (field.type === 'select') {
    const options = field.options || [];
    return `<select class="input" data-key="${key}">
      ${field.is_set ? '' : `<option value="" selected>继承默认（${esc(field.effective || '—')}）</option>`}
      ${options.map((opt) => `<option value="${esc(opt)}" ${String(value) === String(opt) ? 'selected' : ''}>${esc(opt)}</option>`).join('')}
    </select>`;
  }
  if (field.type === 'secret') {
    return `<div class="secret-row">
      <input class="input" type="password" data-key="${key}" autocomplete="new-password"
             placeholder="${field.is_set ? '已设置：' + esc(field.display) + '（留空不改）' : '未设置'}">
      <button class="mini-btn" type="button" data-reveal="${key}" title="显示输入">显示</button>
      ${field.is_set ? `<button class="mini-btn danger" type="button" data-clear="${key}" title="删掉这一行">清空</button>` : ''}
    </div>`;
  }
  const kind = field.type === 'int' || field.type === 'float' ? 'number' : 'text';
  const extra = [
    field.type === 'int' || field.type === 'float' ? `step="${field.type === 'int' ? 1 : 0.1}"` : '',
    field.min !== null && field.min !== undefined ? `min="${field.min}"` : '',
    field.max !== null && field.max !== undefined ? `max="${field.max}"` : '',
  ].filter(Boolean).join(' ');
  return `<input class="input" type="${kind}" data-key="${key}" value="${esc(value)}"
    placeholder="${field.is_set ? '' : '继承默认：' + esc(field.effective || '—')}" ${extra}>`;
}

function fieldCell(field) {
  const inherited = field.secret ? '未设置密钥' : `继承默认：${field.effective || '—'}`;
  return `<div class="field-cell ${field.stale ? 'is-stale' : ''}" data-cell="${esc(field.key)}" data-search="${esc((field.key + ' ' + field.label + ' ' + (field.hint || '')).toLowerCase())}">
    <div class="field-top">
      <span class="field-label">${esc(field.label)}</span>
      <span class="field-key" title="技术键名">${esc(field.key)}</span>
      <span class="field-flag" data-flag="${esc(field.key)}">${field.stale ? '<span class="warn-chip">待重启</span>' : ''}</span>
    </div>
    <div class="field-source"><span>配置文件值</span><strong>${field.is_set ? '已写入' : esc(inherited)}</strong></div>
    ${fieldInput(field, field.secret ? '' : field.value)}
    ${field.hint ? `<div class="field-hint">${esc(field.hint)}</div>` : ''}
    <div class="field-eff"><span>当前运行值</span><strong>${esc(field.effective) || '—'}</strong>${field.stale ? '<em>与文件不一致</em>' : ''}</div>
  </div>`;
}

async function mountSettings(root) {
  let view = await api('/admin/api/env');
  const dirty = new Map();
  const openGroups = new Map();
  let settingsQuery = '';

  root.innerHTML = `
    <div class="stack">
      <div class="card" id="st-bar">
        <div class="card-head">
          <div class="card-title">配置文件 <small id="st-file">${esc(view.file)} · ${esc(view.mtime)}</small></div>
          <div class="row">
            <span class="editor-path" id="st-count">没有改动</span>
            <button class="btn ghost small" id="st-reset" type="button">还原</button>
            <button class="btn primary small" id="st-save" type="button">保存</button>
          </div>
        </div>
        <div class="card-body" id="st-tip"></div>
      </div>
      <div id="st-sections"></div>
      <div class="settings-dock" id="st-dock" role="region" aria-label="配置保存与重启操作" hidden>
        <span class="settings-dock-dot"></span>
        <div class="settings-dock-copy">
          <strong id="st-dock-title"></strong>
          <span id="st-dock-note"></span>
        </div>
        <div class="row">
          <button class="btn ghost small" id="st-dock-restart" type="button" hidden>重启服务</button>
          <button class="btn primary" id="st-dock-save" type="button">保存修改</button>
        </div>
      </div>
    </div>`;

  const sectionsBox = $('#st-sections', root);
  const tip = $('#st-tip', root);

  const drawTip = () => {
    const pending = view.pending || [];
    tip.innerHTML = `
      <label class="settings-search">
        <span>搜索设置</span>
        <input class="input" id="st-search" type="search" value="${esc(settingsQuery)}" placeholder="输入中文名称或配置键，例如：模型、AI_MODEL、并发" autocomplete="off">
      </label>
      <div class="row wrap settings-restart-row">
        <span class="muted" style="font-size:12px">
          <code>otowa.env</code> 只在进程启动时读一次，所以<b>保存后必须重启才生效</b>；
          这里可以直接改，改完点「保存」，需要时再点「重启服务」。
        </span>
        <span class="spacer"></span>
        ${view.can_restart
          ? `<button class="btn ghost small" id="st-restart" type="button">重启服务（${esc(view.unit)}）</button>`
          : '<span class="dim" style="font-size:11.5px">本机没有 systemd，重启请手动</span>'}
      </div>
      ${Object.keys(view.duplicates || {}).length
        ? `<div class="notice warn" style="margin-top:10px">
             这些键在文件里写了不止一次（dotenv 只认最后一次，以前会“改不动还报成功”）：
             ${Object.entries(view.duplicates).map(([k, n]) => `<code>${esc(k)}</code>×${n}`).join(' ')}
             。保存其中任意一项时会顺手把重复行清掉。
           </div>`
        : ''}
      ${pending.length
        ? `<div class="notice warn" style="margin-top:10px">
             文件里这些项已经和进程里不一样了（改了还没重启）：${pending.map((k) => `<code>${esc(k)}</code>`).join(' ')}
           </div>`
        : ''}`;
    const restart = document.getElementById('st-restart');
    if (restart) restart.addEventListener('click', restartService);
    $('#st-search', root)?.addEventListener('input', (ev) => {
      settingsQuery = ev.target.value.trim().toLowerCase();
      applySettingsFilter();
    });
  };

  const applySettingsFilter = () => {
    let matched = 0;
    $$('.settings-group', sectionsBox).forEach((group) => {
      let groupMatches = 0;
      $$('.field-cell', group).forEach((cell) => {
        const show = !settingsQuery || (cell.dataset.search || '').includes(settingsQuery);
        cell.hidden = !show;
        if (show) groupMatches += 1;
      });
      group.hidden = groupMatches === 0;
      if (settingsQuery && groupMatches) group.open = true;
      else if (!settingsQuery && openGroups.has(group.dataset.group)) group.open = openGroups.get(group.dataset.group);
      const count = $('.settings-group-count', group);
      if (count) count.textContent = settingsQuery ? `${groupMatches} 项匹配` : `${groupMatches} 项`;
      matched += groupMatches;
    });
    $('#st-search-empty', sectionsBox).hidden = matched > 0;
  };

  const drawSections = () => {
    sectionsBox.innerHTML = view.sections.map((section, index) => {
      if (!openGroups.has(section.title)) openGroups.set(section.title, index < 2);
      return `<details class="card settings-group" data-group="${esc(section.title)}" ${openGroups.get(section.title) ? 'open' : ''}>
        <summary class="settings-group-head">
          <span class="card-title">${esc(section.title)}</span>
          <span class="settings-group-note">${esc(section.note)}</span>
          <span class="settings-group-count">${section.fields.length} 项</span>
        </summary>
        <div class="field-grid">${section.fields.map(fieldCell).join('')}</div>
      </details>`;
    }).join('') + '<div class="empty" id="st-search-empty" hidden>没有匹配的设置，换个中文名称或配置键试试。</div>';
    applySettingsFilter();
  };

  sectionsBox.addEventListener('toggle', (ev) => {
    const group = ev.target.closest?.('.settings-group');
    if (group && !settingsQuery) openGroups.set(group.dataset.group, group.open);
  }, true);

  const drawDock = () => {
    const changed = dirty.size;
    const pending = (view.pending || []).length;
    const dock = $('#st-dock', root);
    dock.hidden = changed === 0 && pending === 0;
    $('#st-dock-title', root).textContent = changed
      ? `${changed} 项修改尚未保存`
      : `${pending} 项配置已保存，等待重启`;
    $('#st-dock-note', root).textContent = changed
      ? '先保存修改，再重启服务使配置生效'
      : (view.can_restart ? '点击右侧「重启服务」后生效' : '本机没有 systemd，请手动重启');
    const save = $('#st-dock-save', root);
    save.hidden = changed === 0;
    save.disabled = changed === 0;
    save.textContent = changed ? `保存 ${changed} 项修改` : '保存修改';
    const restart = $('#st-dock-restart', root);
    restart.hidden = !view.can_restart || pending === 0;
    restart.disabled = changed > 0;
    restart.textContent = changed ? '先保存再重启' : `重启服务（${view.unit}）`;
  };

  const markDirty = (key) => {
    const cell = root.querySelector(`[data-flag="${CSS.escape(key)}"]`);
    if (cell) cell.innerHTML = dirty.has(key) ? '<span class="dirty-chip">已改</span>' : (
      view.pending.includes(key) ? '<span class="warn-chip">待重启</span>' : ''
    );
    const n = dirty.size;
    $('#st-count', root).textContent = n ? `${n} 项待保存` : '没有改动';
    $('#st-save', root).disabled = n === 0;
    drawDock();
  };

  root.addEventListener('input', (ev) => {
    const key = ev.target.dataset?.key;
    if (!key) return;
    const value = ev.target.value;
    if (value === '') dirty.delete(key);
    else dirty.set(key, value);
    markDirty(key);
  });

  root.addEventListener('change', (ev) => {
    const key = ev.target.dataset?.key;
    if (!key) return;
    const value = ev.target.value;
    if (value === '') dirty.delete(key);
    else dirty.set(key, value);
    markDirty(key);
  });

  root.addEventListener('click', async (ev) => {
    const reveal = ev.target.closest('[data-reveal]');
    if (reveal) {
      const input = root.querySelector(`input[data-key="${CSS.escape(reveal.dataset.reveal)}"]`);
      if (input) input.type = input.type === 'password' ? 'text' : 'password';
      return;
    }
    const clear = ev.target.closest('[data-clear]');
    if (clear) {
      const key = clear.dataset.clear;
      if (!confirm(`清空 ${key}？这一行会从 otowa.env 删掉，重启后回到默认值。`)) return;
      dirty.set(key, '');
      const cell = root.querySelector(`[data-cell="${CSS.escape(key)}"]`);
      if (cell) cell.querySelector('input')?.setAttribute('placeholder', '将删除这一行');
      markDirty(key);
      return;
    }
    if (ev.target.closest('#st-reset')) {
      dirty.clear();
      view = await api('/admin/api/env');
      drawTip();
      drawSections();
      $('#st-count', root).textContent = '没有改动';
      $('#st-save', root).disabled = true;
      drawDock();
      return;
    }
    if (ev.target.closest('#st-save, #st-dock-save')) {
      await saveChanges();
      return;
    }
    if (ev.target.closest('#st-dock-restart')) {
      await restartService();
      return;
    }
  });

  const saveChanges = async () => {
    if (!dirty.size) return;
    const changes = {};
    dirty.forEach((value, key) => { changes[key] = value === SECRET_SENTINEL ? '' : value; });
    const btn = $('#st-save', root);
    const dockBtn = $('#st-dock-save', root);
    btn.disabled = true;
    dockBtn.disabled = true;
    try {
      const res = await fetch(withToken('/admin/api/env'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ changes }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        const detail = data.errors ? Object.entries(data.errors).map(([k, v]) => `${k}：${v}`).join('\n') : '';
        throw new Error((data.error || `HTTP ${res.status}`) + (detail ? `\n${detail}` : ''));
      }
      toast(data.message || '已保存');
      dirty.clear();
      view = await api('/admin/api/env');
      drawTip();
      drawSections();
      drawDock();
      $('#st-file', root).textContent = `${view.file} · ${view.mtime}`;
      $('#st-count', root).textContent = '没有改动';
      btn.disabled = true;
      if (view.pending.length) {
        toast(`${view.pending.length} 项还没生效，记得重启`, 'ok', 4200);
      }
    } catch (err) {
      toast(err.message, 'err', 6000);
      btn.disabled = false;
      drawDock();
    }
  };

  const restartService = async () => {
    if (!confirm(`重启 ${view.unit}？会断线约 10 秒，正在回复的消息会丢。`)) return;
    try {
      const data = await api('/admin/api/restart', { method: 'POST', body: '{}' });
      toast(data.message || '正在重启…', 'ok', 4000);
      // 轮询 /health，服务回来后自动刷新页面
      let tries = 0;
      const timer = setInterval(async () => {
        tries += 1;
        try {
          const res = await fetch('/health', { cache: 'no-store' });
          if (res.ok) {
            clearInterval(timer);
            location.reload();
          }
        } catch (e) { /* 还没起来，继续等 */ }
        if (tries > 30) clearInterval(timer);
      }, 2000);
    } catch (err) {
      toast(err.message, 'err', 5200);
    }
  };

  drawTip();
  drawSections();
  drawDock();
  $('#st-save', root).disabled = true;
  return null;
}

/* ------------------------------------------------------------------ 表情 */
async function mountStickers(root) {
  const data = await api('/admin/api/stickers');
  const items = data.items || [];
  let previewUrl = '';
  let analysisController = null;
  let analysisBusy = false;
  let aliasTouched = false;
  root.innerHTML = `
    <div class="row wrap sticker-summary">
      <span class="muted" id="sticker-count"></span>
      <span class="spacer"></span>
      <input class="input sticker-search" id="sticker-search" type="search" placeholder="搜索别名或文件名…" aria-label="搜索表情">
      <button class="btn primary" id="sticker-add-open" type="button">+ 添加表情</button>
    </div>
    <form class="card sticker-add-panel" id="sticker-add-panel" hidden>
      <div class="card-head">
        <div class="card-title">添加新表情 <small>保存后即可通过 [[别名]] 使用</small></div>
        <button class="mini-btn" id="sticker-add-close" type="button">收起</button>
      </div>
      <div class="card-body">
        <div class="sticker-add-grid">
          <label class="field"><span>表情别名</span>
            <input class="input" id="sticker-alias" maxlength="16" required autocomplete="off" placeholder="例如：开心">
          </label>
          <div class="field"><span>图片文件</span>
            <label class="upload-zone sticker-upload" for="sticker-file">
              <input id="sticker-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" required hidden>
              <span id="sticker-file-label">选择 PNG / JPG / WEBP / GIF，最大 8 MB</span>
            </label>
          </div>
          <div class="sticker-add-preview" id="sticker-preview"><span class="dim">图片预览</span><img id="sticker-preview-img" alt="待添加表情预览" hidden></div>
        </div>
        <div class="sticker-analysis" id="sticker-analysis">
          <div class="row wrap sticker-analysis-status">
            <span class="analysis-beacon"></span><span id="sticker-analysis-text">选择图片后，视觉模型会自动识别并给出建议参数。</span>
            <span class="spacer"></span><span class="dim" id="sticker-confidence"></span>
          </div>
          <div class="sticker-analysis-fields" id="sticker-analysis-fields" hidden>
            <div class="sticker-param-grid">
              <label class="field"><span>分类</span><select class="select" id="sticker-category">
                <option>开心</option><option>难过</option><option>生气</option><option>惊讶</option><option>疑惑</option><option>无奈</option><option>嫌弃</option><option>害羞</option><option>鼓励</option><option>道歉</option><option>问候</option><option>其他</option>
              </select></label>
              <label class="field"><span>具体情绪</span><input class="input" id="sticker-emotion" maxlength="16" placeholder="例如：哭笑不得"></label>
              <label class="field sticker-param-wide"><span>画面描述</span><textarea class="input" id="sticker-description" maxlength="120" rows="2" placeholder="画面里有什么、写了什么"></textarea></label>
              <label class="field sticker-param-wide"><span>适用场景</span><textarea class="input" id="sticker-usage" maxlength="120" rows="2" placeholder="什么时候适合发这个表情"></textarea></label>
              <label class="field sticker-param-wide"><span>检索标签</span><input class="input" id="sticker-tags" placeholder="用逗号分隔，最多 6 个，例如：可爱, 点头, 赞同"></label>
            </div>
          </div>
        </div>
        <div class="row wrap sticker-add-foot">
          <span class="dim">AI 结果仅作建议，所有参数都可在保存前人工修改。</span>
          <span class="spacer"></span>
          <button class="btn primary" id="sticker-add-submit" type="submit">保存表情</button>
        </div>
      </div>
    </form>
    <div class="sticker-grid" id="sticker-grid"></div>`;
  const panel = $('#sticker-add-panel', root);
  const fileInput = $('#sticker-file', root);
  const preview = $('#sticker-preview-img', root);
  const draw = () => {
    const query = $('#sticker-search', root).value.trim().toLowerCase();
    const filtered = items.filter((i) => `${i.alias} ${i.file || ''} ${i.type || ''}`.toLowerCase().includes(query));
    const available = items.filter((i) => i.configured && i.exists).length;
    $('#sticker-count', root).textContent = query
      ? `找到 ${filtered.length} 个匹配项 · 全部 ${items.length} 个别名`
      : `共 ${items.length} 个别名，其中 ${available} 个已挂图片文件`;
    $('#sticker-grid', root).innerHTML = filtered.length ? filtered.map((i) => `
      <div class="sticker-card">
        <div class="sticker-thumb">
          ${i.exists
            ? `<img src="${esc(withToken(i.url))}" alt="${esc(i.alias)}" loading="lazy">`
            : `<span class="none">${i.configured ? '文件缺失' : '未配置图片'}</span>`}
        </div>
        <div class="sticker-info">
          <div class="row"><div class="sticker-alias">[[${esc(i.alias)}]]</div>${i.category ? `<span class="sticker-category">${esc(i.category)}</span>` : ''}</div>
          ${i.description ? `<div class="sticker-description" title="${esc(i.usage || i.description)}">${esc(i.description)}</div>` : ''}
          <div class="sticker-file">${i.file ? esc(i.file) : i.type ? esc(i.type) : '—'}${i.size ? ' · ' + fmtBytes(i.size) : ''}</div>
        </div>
      </div>`).join('') : '<div class="empty" style="grid-column:1/-1">没有匹配的表情，换个关键字试试。</div>';
  };
  const resetForm = () => {
    if (analysisController) analysisController.abort();
    analysisController = null;
    analysisBusy = false;
    aliasTouched = false;
    panel.reset();
    preview.hidden = true;
    preview.removeAttribute('src');
    $('#sticker-analysis-fields', root).hidden = true;
    $('#sticker-analysis-text', root).textContent = '选择图片后，视觉模型会自动识别并给出建议参数。';
    $('#sticker-confidence', root).textContent = '';
    $('#sticker-analysis', root).classList.remove('busy', 'failed', 'ready');
    $('#sticker-file-label', root).textContent = '选择 PNG / JPG / WEBP / GIF，最大 8 MB';
    const button = $('#sticker-add-submit', root);
    button.disabled = false;
    button.textContent = '保存表情';
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = '';
  };
  $('#sticker-add-open', root).addEventListener('click', () => {
    panel.hidden = false;
    $('#sticker-alias', root).focus();
  });
  $('#sticker-add-close', root).addEventListener('click', () => { panel.hidden = true; resetForm(); });
  $('#sticker-alias', root).addEventListener('input', () => { aliasTouched = true; });
  const analyzeFile = async (file) => {
    if (analysisController) analysisController.abort();
    const controller = new AbortController();
    analysisController = controller;
    analysisBusy = true;
    const button = $('#sticker-add-submit', root);
    button.disabled = true;
    button.textContent = '识别中…';
    $('#sticker-analysis-fields', root).hidden = true;
    $('#sticker-confidence', root).textContent = '';
    $('#sticker-analysis', root).classList.remove('failed', 'ready');
    $('#sticker-analysis', root).classList.add('busy');
    $('#sticker-analysis-text', root).textContent = '视觉模型正在识别画面、分类情绪并生成参数…';
    try {
      const body = new FormData();
      body.append('file', file);
      const response = await fetch(withToken('/admin/api/stickers/analyze'), {
        method: 'POST', headers: authHeaders(), body, signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || `HTTP ${response.status}`);
      if (analysisController !== controller) return;
      const suggestion = result.analysis;
      if (!aliasTouched) $('#sticker-alias', root).value = suggestion.alias || '';
      $('#sticker-category', root).value = suggestion.category || '其他';
      $('#sticker-emotion', root).value = suggestion.emotion || '';
      $('#sticker-description', root).value = suggestion.description || '';
      $('#sticker-usage', root).value = suggestion.usage || '';
      $('#sticker-tags', root).value = (suggestion.tags || []).join(', ');
      $('#sticker-analysis-fields', root).hidden = false;
      $('#sticker-analysis', root).classList.add('ready');
      $('#sticker-analysis-text', root).textContent = '识别完成，请核对并修改下面的参数。';
      $('#sticker-confidence', root).textContent = `置信度 ${Math.round((suggestion.confidence || 0) * 100)}%`;
    } catch (err) {
      if (err.name === 'AbortError' || analysisController !== controller) return;
      $('#sticker-analysis-fields', root).hidden = false;
      $('#sticker-category', root).value = '其他';
      $('#sticker-analysis', root).classList.add('failed');
      $('#sticker-analysis-text', root).textContent = `${err.message}；已切换为手动标注。`;
    } finally {
      if (analysisController === controller) {
        analysisController = null;
        analysisBusy = false;
        $('#sticker-analysis', root).classList.remove('busy');
        button.disabled = false;
        button.textContent = '保存表情';
      }
    }
  };
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (!file) { resetForm(); return; }
    if (file.size > 8 * 1024 * 1024) {
      toast('图片不能超过 8 MB', 'err');
      fileInput.value = '';
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    preview.hidden = false;
    $('#sticker-file-label', root).textContent = `${file.name} · ${fmtBytes(file.size)}`;
    analyzeFile(file);
  });
  panel.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (analysisBusy) { toast('请等待视觉分析完成', 'err'); return; }
    const alias = $('#sticker-alias', root).value.trim();
    const file = fileInput.files[0];
    if (!alias || alias.length > 16 || /[\[\]<>\\/\r\n\t]/.test(alias)) {
      toast('别名需为 1–16 个字符，不能包含方括号、尖括号、斜杠或换行', 'err');
      return;
    }
    if (!file) { toast('请选择一张表情图片', 'err'); return; }
    const button = $('#sticker-add-submit', root);
    button.disabled = true;
    button.textContent = '保存中…';
    try {
      const body = new FormData();
      body.append('alias', alias);
      body.append('file', file);
      for (const field of ['category', 'emotion', 'description', 'usage', 'tags']) {
        body.append(field, $(`#sticker-${field}`, root).value);
      }
      const confidenceMatch = $('#sticker-confidence', root).textContent.match(/(\d+)%/);
      body.append('confidence', confidenceMatch ? String(Number(confidenceMatch[1]) / 100) : '0');
      const response = await fetch(withToken('/admin/api/stickers'), {
        method: 'POST', headers: authHeaders(), body,
      });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || `HTTP ${response.status}`);
      items.push(result.item);
      $('#sticker-search', root).value = '';
      draw();
      panel.hidden = true;
      resetForm();
      toast(`表情 [[${alias}]] 已添加`);
    } catch (err) {
      toast(err.message, 'err', 4800);
    } finally {
      button.disabled = false;
      button.textContent = '保存表情';
    }
  });
  $('#sticker-search', root).addEventListener('input', draw);
  draw();
  return () => {
    if (analysisController) analysisController.abort();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  };
}

/* ------------------------------------------------------------------ 日志 */
async function mountLogs(root) {
  root.innerHTML = `
    <div class="card">
      <div class="log-tools">
        <select class="select" id="l-level">
          <option value="">全部级别</option>
          <option value="WARNING">WARNING 及以上</option>
          <option value="ERROR">只看 ERROR</option>
          <option value="INFO">只看 INFO</option>
        </select>
        <input class="input" id="l-q" placeholder="搜索关键字…">
        <label class="check"><input type="checkbox" id="l-auto" checked> 自动滚动</label>
        <span class="spacer"></span>
        <span class="editor-path" id="l-stat">连接中…</span>
        <button class="btn ghost small" id="l-pause" type="button">暂停</button>
        <button class="btn ghost small" id="l-clear" type="button">清屏</button>
      </div>
      <div class="log-box" id="l-box"></div>
    </div>`;

  const box = $('#l-box', root);
  const stat = $('#l-stat', root);
  const lines = [];
  const MAX = 3000;
  let paused = false;
  let source = null;

  const levelOf = (item) => (item.level || '').toUpperCase();
  const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
  const filterLevel = () => $('#l-level', root).value;
  const filterText = () => $('#l-q', root).value.trim().toLowerCase();

  const passes = (item) => {
    const lv = filterLevel();
    if (lv === 'ERROR') {
      if (!['ERROR', 'CRITICAL'].includes(levelOf(item))) return false;
    } else if (lv && (rank[levelOf(item)] ?? 1) < rank[lv]) {
      return false;
    }
    const q = filterText();
    if (q && !`${item.t} ${item.level} ${item.msg}`.toLowerCase().includes(q)) return false;
    return true;
  };

  const makeNode = (item) => {
    const div = document.createElement('div');
    const lv = levelOf(item);
    div.className = `log-line ${lv === 'ERROR' || lv === 'CRITICAL' ? 'err' : lv === 'WARNING' ? 'warn' : ''}`;
    div.innerHTML = `<span class="log-t">${esc(item.t || '')}</span>
      <span class="log-lv ${lv || 'none'}">${esc(lv || '··')}</span>
      <span class="log-msg">${esc(item.msg)}</span>`;
    return div;
  };

  const stick = () => {
    if ($('#l-auto', root).checked) box.scrollTop = box.scrollHeight;
  };

  const push = (item) => {
    lines.push(item);
    if (lines.length > MAX) lines.shift();
    if (!passes(item)) return;
    box.appendChild(makeNode(item));
    while (box.childElementCount > 1500) box.firstElementChild.remove();
    stick();
  };

  const redraw = () => {
    box.innerHTML = '';
    const shown = lines.filter(passes).slice(-1200);
    const frag = document.createDocumentFragment();
    shown.forEach((item) => frag.appendChild(makeNode(item)));
    box.appendChild(frag);
    stick();
  };

  const connect = (withBacklog = true) => {
    const url = `/admin/api/logs/stream${withBacklog ? '' : '?backlog=0'}`;
    source = new EventSource(withToken(url));
    source.onopen = () => { stat.textContent = '实时跟随中'; stat.style.color = ''; };
    source.onmessage = (ev) => {
      try { push(JSON.parse(ev.data)); } catch (e) { /* 心跳等非 JSON 帧忽略 */ }
    };
    source.onerror = () => {
      stat.textContent = '连接断开，浏览器会自动重连…';
      stat.style.color = 'var(--err)';
    };
  };

  $('#l-level', root).addEventListener('change', redraw);
  $('#l-q', root).addEventListener('input', () => { clearTimeout(route._qTimer); route._qTimer = setTimeout(redraw, 160); });
  $('#l-clear', root).addEventListener('click', () => { lines.length = 0; box.innerHTML = ''; });
  $('#l-pause', root).addEventListener('click', (ev) => {
    paused = !paused;
    if (paused) {
      source?.close();
      ev.target.textContent = '继续';
      stat.textContent = '已暂停（不会丢：暂停期间的新日志不推送）';
      stat.style.color = 'var(--warn)';
    } else {
      connect(false);
      stat.textContent = '实时跟随中';
      stat.style.color = '';
      ev.target.textContent = '暂停';
    }
  });

  // ?stream=0 只看快照、不挂 SSE：截图、录屏、或者只想翻历史时用得上。
  if (new URLSearchParams(location.search).get('stream') === '0') {
    $('#l-pause', root).disabled = true;
    api('/admin/api/logs?lines=300')
      .then((d) => {
        (d.lines || []).forEach(push);
        stat.textContent = `快照 ${(d.lines || []).length} 行（?stream=0 关掉了实时跟随）`;
      })
      .catch((err) => { stat.textContent = err.message; });
  } else {
    connect();
  }
  return () => { source?.close(); source = null; };
}

/* ------------------------------------------------------------------ 启动 */
applyBackground(bgSettings());
route();
setInterval(() => { if (!document.hidden) refreshWsPill(); }, 12000);
