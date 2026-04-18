/* ═══════════════════════════════════════════════════════════
   SleepBot 管理面板 — SPA
   手寫，不是 AI 吐的。歡迎來到深夜工作站。
   ═══════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────
const S = { user: null, guild: null, page: 'overview' };

const EVENT_LABELS = {
  channel_delete:    { icon: '🗑️', label: '頻道刪除',       cls: 'delete' },
  channel_update:    { icon: '✏️', label: '頻道修改',       cls: 'update' },
  role_delete:       { icon: '🗑️', label: '身分組刪除',     cls: 'delete' },
  role_update:       { icon: '✏️', label: '身分組修改',     cls: 'update' },
  admin_perm_remove: { icon: '⚠️', label: '管理員權限移除', cls: 'admin'  },
  message_spam:      { icon: '💬', label: '訊息轟炸',       cls: 'admin'  },
  manual:            { icon: '🔧', label: '手動復原',       cls: 'update' },
};

const STATUS_MAP = {
  pending:   { label: '待處理', cls: 'badge-pending',   icon: '⏳' },
  approved:  { label: '已核准', cls: 'badge-approved',  icon: '✅' },
  rejected:  { label: '已拒絕', cls: 'badge-rejected',  icon: '❌' },
  manual:    { label: '手動',   cls: 'badge-manual',    icon: '🔧' },
  completed: { label: '已完成', cls: 'badge-completed', icon: '✅' },
};

const ERRORS = [
  '面板伺服器正在打瞌睡，等它醒來再試試',
  '連線被月亮遮住了，檢查一下網路吧',
  '資料迷路了，可能還在夢遊',
];

// ── Utils ────────────────────────────────────────────────
function randomError() { return ERRORS[Math.floor(Math.random() * ERRORS.length)]; }

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-TW', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function relTime(ts) {
  if (!ts) return '';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60)    return '剛剛';
  if (diff < 3600)  return `${Math.floor(diff / 60)} 分鐘前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小時前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

function getGreeting(name) {
  const h = new Date().getHours();
  if (h >= 5  && h < 12) return `早安，<strong>${esc(name)}</strong>。今天的伺服器一切平靜`;
  if (h >= 12 && h < 14) return `午安，<strong>${esc(name)}</strong>。吃飽了嗎？`;
  if (h >= 14 && h < 18) return `下午好，<strong>${esc(name)}</strong>。來看看最近有什麼動靜`;
  if (h >= 18 && h < 22) return `晚上好，<strong>${esc(name)}</strong>。今天辛苦了`;
  return `夜深了，<strong>${esc(name)}</strong>。伺服器的事交給我吧`;
}

function esc(s) {
  const el = document.createElement('span');
  el.textContent = s || '';
  return el.innerHTML;
}

function loadingHTML() {
  return '<div class="loading-state"><div class="loading-dots"><span></span><span></span><span></span></div></div>';
}

function emptyHTML(icon, text) {
  return `<div class="empty-state"><div class="empty-icon">${icon}</div><div class="empty-text">${text}</div></div>`;
}

// ── API helper ───────────────────────────────────────────
async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (resp.status === 401) {
    renderLogin();
    throw new Error('unauthorized');
  }
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

// ── Toast ────────────────────────────────────────────────
function toast(text, type = 'info') {
  const icons = { success: '✅', error: '❌', info: '💡' };
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="toast-icon">${icons[type] || '💡'}</span><span class="toast-text">${esc(text)}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 300); }, 4000);
}

// ── Confirm modal ────────────────────────────────────────
function showConfirm(title, msg) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal">
        <h3>${esc(title)}</h3>
        <p>${esc(msg)}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" data-r="cancel">取消</button>
          <button class="btn btn-danger" data-r="ok">確認執行</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => {
      const r = e.target.dataset.r;
      if (r === 'cancel')  { overlay.remove(); resolve(false); }
      if (r === 'ok')      { overlay.remove(); resolve(true);  }
    });
  });
}

// ── Discord SVG icon ─────────────────────────────────────
const DISCORD_SVG = '<svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515a.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0a12.64 12.64 0 0 0-.617-1.25a.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057a19.9 19.9 0 0 0 5.993 3.03a.078.078 0 0 0 .084-.028a14.09 14.09 0 0 0 1.226-1.994a.076.076 0 0 0-.041-.106a13.107 13.107 0 0 1-1.872-.892a.077.077 0 0 1-.008-.128a10.2 10.2 0 0 0 .372-.292a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127a12.299 12.299 0 0 1-1.873.892a.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028a19.839 19.839 0 0 0 6.002-3.03a.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419c0-1.333.956-2.419 2.157-2.419c1.21 0 2.176 1.095 2.157 2.42c0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419c0-1.333.955-2.419 2.157-2.419c1.21 0 2.176 1.095 2.157 2.42c0 1.333-.946 2.418-2.157 2.418z"/></svg>';

// ── Render: login ────────────────────────────────────────
function renderLogin() {
  S.user = null;
  document.getElementById('app').innerHTML = `
    <div class="login-wrap">
      <div class="login-card">
        <div class="moon">🌙</div>
        <h1>SleepBot</h1>
        <p class="tagline">守護你的伺服器，即使在你沉睡時</p>
        <a href="/auth/login" class="btn-discord">${DISCORD_SVG} 用 Discord 登入</a>
        <div class="login-footer">登入後管理復原、門檻設定與更多</div>
      </div>
    </div>`;
}

// ── Render: app shell ────────────────────────────────────
function renderApp() {
  const u = S.user;
  const guilds = u.guilds || [];
  const guildOpts = guilds.map(g =>
    `<option value="${esc(g.id)}" ${g.id === S.guild ? 'selected' : ''}>${esc(g.name)}</option>`
  ).join('');

  const avatarEl = u.avatar
    ? `<img class="user-avatar" src="${esc(u.avatar)}" alt="">`
    : `<div class="user-avatar" style="display:flex;align-items:center;justify-content:center;font-size:.8rem;color:var(--text-3)">👤</div>`;

  document.getElementById('app').innerHTML = `
    <div class="app-wrap">
      <nav class="sidebar" id="sidebar">
        <div class="sidebar-brand"><span class="moon">🌙</span> SleepBot</div>
        <ul class="nav-list">
          <li class="nav-item active" data-page="overview"><span class="icon">📊</span> 概覽</li>
          <li class="nav-item" data-page="recovery"><span class="icon">🔄</span> 復原管理</li>
          <li class="nav-item" data-page="thresholds"><span class="icon">⚙️</span> 門檻設定</li>
        </ul>
        <div class="sidebar-footer">
          <div class="user-block">${avatarEl}<span class="user-name">${esc(u.username)}</span></div>
          <a href="/auth/logout" class="btn-logout">登出</a>
        </div>
      </nav>
      <div class="main-content">
        <div class="topbar">
          <button class="mobile-toggle" id="menuToggle">☰</button>
          <div class="greeting">${getGreeting(u.username)}</div>
          ${guilds.length > 1
            ? `<select class="guild-select" id="guildSelect">${guildOpts}</select>`
            : (guilds.length === 1 ? `<span style="font-size:.85rem;color:var(--text-3)">${esc(guilds[0].name)}</span>` : '')
          }
        </div>
        <div class="page-area" id="pageArea">${loadingHTML()}</div>
      </div>
    </div>`;

  // Event: nav
  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => navigate(el.dataset.page));
  });
  // Event: guild selector
  const sel = document.getElementById('guildSelect');
  if (sel) sel.addEventListener('change', () => { S.guild = sel.value; renderPage(); });
  // Event: mobile menu
  const toggle = document.getElementById('menuToggle');
  if (toggle) toggle.addEventListener('click', () => document.getElementById('sidebar').classList.toggle('open'));

  renderPage();
}

// ── Navigation ───────────────────────────────────────────
function navigate(page) {
  S.page = page;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });
  renderPage();
}

function renderPage() {
  const area = document.getElementById('pageArea');
  if (!area) return;
  area.innerHTML = loadingHTML();
  const pages = { overview: pageOverview, recovery: pageRecovery, thresholds: pageThresholds };
  (pages[S.page] || pageOverview)(area);
}

// ── Page: overview ───────────────────────────────────────
async function pageOverview(el) {
  try {
    const [overview, events, logs] = await Promise.all([
      api(`/api/guilds/${S.guild}/overview`),
      api(`/api/guilds/${S.guild}/events`),
      api(`/api/guilds/${S.guild}/maintenance-logs`),
    ]);
    el.innerHTML = `
      <div class="page-header"><h2>概覽</h2><button class="btn btn-ghost btn-sm" id="refreshOverview">↻ 重新整理</button></div>
      <div class="stats-grid">
        ${statCard('頻道', overview.channels, 'purple')}
        ${statCard('身分組', overview.roles, 'amber')}
        ${statCard('24h 事件', overview.events_24h, 'teal')}
        ${statCard('待處理復原', overview.pending_recoveries, overview.pending_recoveries > 0 ? 'danger' : 'green')}
      </div>
      <div class="section">
        <div class="section-title">📋 最近事件</div>
        ${renderEventsTable(events.events)}
      </div>
      <div class="section">
        <div class="section-title">📝 維護日誌</div>
        ${renderLogSection(logs.logs)}
      </div>`;
    el.querySelector('#refreshOverview')?.addEventListener('click', () => pageOverview(el));
    attachLogHandlers(el);
  } catch (e) {
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

function statCard(label, value, color) {
  return `<div class="stat-card ${color}"><div class="stat-label">${label}</div><div class="stat-value">${value ?? '—'}</div></div>`;
}

function renderEventsTable(events) {
  if (!events || !events.length) return emptyHTML('🌙', '過去一片寧靜，什麼事都沒發生');
  const rows = events.slice(0, 15).map(ev => {
    const info = EVENT_LABELS[ev.event_type] || { icon: '❓', label: ev.event_type, cls: 'update' };
    return `<tr>
      <td><span class="badge-event ${info.cls}">${info.icon} ${info.label}</span></td>
      <td class="mono">${esc(ev.target_id)}</td>
      <td title="${fmtTime(ev.timestamp)}">${relTime(ev.timestamp)}</td>
    </tr>`;
  }).join('');
  return `<table class="data-table"><thead><tr><th>類型</th><th>目標</th><th>時間</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLogSection(logs) {
  const entries = (!logs || !logs.length)
    ? emptyHTML('📓', '還沒有維護日誌，記錄你的心路歷程吧')
    : logs.map(l => `
        <div class="log-entry">
          <div class="log-meta">
            <span class="log-author">${esc(l.author_name)}</span>
            <span class="log-time">${relTime(l.created_at)}</span>
          </div>
          <div class="log-text">${esc(l.content)}</div>
        </div>`).join('');
  return `
    <div class="card">
      <div class="log-compose">
        <textarea class="form-input" id="logInput" placeholder="寫下最近做了什麼、修了什麼 bug..." maxlength="500"></textarea>
        <button class="btn btn-primary btn-sm" id="logSubmit">發布</button>
      </div>
      ${entries}
    </div>`;
}

function attachLogHandlers(el) {
  const btn = el.querySelector('#logSubmit');
  const inp = el.querySelector('#logInput');
  if (!btn || !inp) return;
  btn.addEventListener('click', async () => {
    const text = inp.value.trim();
    if (!text) return;
    btn.disabled = true;
    try {
      await api(`/api/guilds/${S.guild}/maintenance-logs`, {
        method: 'POST', body: JSON.stringify({ content: text }),
      });
      toast('日誌已發布', 'success');
      pageOverview(el);
    } catch (e) {
      toast(e.message, 'error');
    }
    btn.disabled = false;
  });
}

// ── Page: recovery ───────────────────────────────────────
async function pageRecovery(el) {
  try {
    const data = await api(`/api/guilds/${S.guild}/recovery-requests`);
    const reqs = data.requests || [];
    const pending = reqs.filter(r => r.status === 'pending');
    const history = reqs.filter(r => r.status !== 'pending');

    el.innerHTML = `
      <div class="page-header"><h2>復原管理</h2><button class="btn btn-ghost btn-sm" id="refreshRecovery">↻ 重新整理</button></div>
      <div class="manual-section">
        <div class="info">
          <h3>🔧 手動復原</h3>
          <p>立即將伺服器結構回復至約 5 分鐘前的狀態</p>
        </div>
        <button class="btn btn-danger" id="manualRecovery">執行手動復原</button>
      </div>
      <div class="section">
        <div class="section-title">⏳ 待處理請求</div>
        ${pending.length ? pending.map(renderPendingCard).join('') : emptyHTML('✨', '沒有待處理的復原請求 — 一切安好')}
      </div>
      <div class="section">
        <div class="section-title">📜 歷史記錄</div>
        ${history.length ? history.map(renderHistoryCard).join('') : emptyHTML('📭', '還沒有復原記錄')}
      </div>`;

    el.querySelector('#refreshRecovery')?.addEventListener('click', () => pageRecovery(el));
    el.querySelector('#manualRecovery')?.addEventListener('click', () => doManualRecovery(el));
    el.querySelectorAll('[data-approve]').forEach(btn => {
      btn.addEventListener('click', () => doApprove(btn.dataset.approve, el));
    });
    el.querySelectorAll('[data-reject]').forEach(btn => {
      btn.addEventListener('click', () => doReject(btn.dataset.reject, el));
    });
  } catch (e) {
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

function renderPendingCard(r) {
  const info = EVENT_LABELS[r.event_type] || { icon: '❓', label: r.event_type };
  const st = STATUS_MAP[r.status] || STATUS_MAP.pending;
  return `
    <div class="request-card">
      <div class="request-info">
        <div class="request-type">${info.icon} ${info.label} <span class="badge ${st.cls}">${st.icon} ${st.label}</span></div>
        <div class="request-meta">事件數 ${r.event_count} · ${fmtTime(r.created_at)}</div>
      </div>
      <div class="request-actions">
        <button class="btn btn-success btn-sm" data-approve="${r.id}">✅ 核准</button>
        <button class="btn btn-ghost btn-sm" data-reject="${r.id}">❌ 拒絕</button>
      </div>
    </div>`;
}

function renderHistoryCard(r) {
  const info = EVENT_LABELS[r.event_type] || { icon: '❓', label: r.event_type };
  const st = STATUS_MAP[r.status] || { label: r.status, cls: 'badge-manual', icon: '•' };
  let result = '';
  if (r.status === 'approved' || r.status === 'completed') {
    result = `<div class="request-result">頻道 ${r.result_channels} · 身分組 ${r.result_roles} · 訊息 ${r.result_messages}</div>`;
  }
  return `
    <div class="request-card">
      <div class="request-info">
        <div class="request-type">${info.icon} ${info.label} <span class="badge ${st.cls}">${st.icon} ${st.label}</span></div>
        <div class="request-meta">${fmtTime(r.created_at)}${r.resolved_at ? ' → ' + fmtTime(r.resolved_at) : ''}</div>
        ${result}
      </div>
    </div>`;
}

async function doApprove(id, el) {
  if (!await showConfirm('核准復原', '確定要核准並執行此復原請求嗎？這會還原伺服器結構至異常發生前的狀態。')) return;
  try {
    const res = await api(`/api/guilds/${S.guild}/recovery/approve/${id}`, { method: 'POST' });
    toast(`復原完成！頻道 ${res.channels} · 身分組 ${res.roles} · 訊息 ${res.messages}`, 'success');
    pageRecovery(el);
  } catch (e) { toast(e.message, 'error'); }
}

async function doReject(id, el) {
  if (!await showConfirm('拒絕復原', '確定拒絕這個復原請求嗎？')) return;
  try {
    await api(`/api/guilds/${S.guild}/recovery/reject/${id}`, { method: 'POST' });
    toast('已拒絕復原請求', 'info');
    pageRecovery(el);
  } catch (e) { toast(e.message, 'error'); }
}

async function doManualRecovery(el) {
  if (!await showConfirm('手動復原', '這會將伺服器結構回復至約 5 分鐘前的快照狀態。\n此操作無法撤銷，請確認你真的需要這麼做。')) return;
  try {
    const res = await api(`/api/guilds/${S.guild}/recovery/manual`, { method: 'POST' });
    toast(`手動復原完成！頻道 ${res.channels} · 身分組 ${res.roles} · 訊息 ${res.messages}`, 'success');
    pageRecovery(el);
  } catch (e) { toast(e.message, 'error'); }
}

// ── Page: thresholds ─────────────────────────────────────
async function pageThresholds(el) {
  try {
    const data = await api(`/api/guilds/${S.guild}/thresholds`);
    const items = data.thresholds || [];
    const fmtWindow = (seconds) => {
      if (!seconds || seconds <= 0) return '5 分鐘';
      if (seconds % 60 === 0) return `${seconds / 60} 分鐘`;
      return `${seconds} 秒`;
    };

    const cards = items.map(t => `
      <div class="threshold-card">
        <div class="label">${esc(t.label)}</div>
        <div class="sublabel">預設值：${t.default} 次 / ${fmtWindow(t.window_seconds)}</div>
        <div class="input-row">
          <input type="number" class="form-input input-number"
                 data-event="${esc(t.event_type)}" value="${t.value}" min="1" max="100">
          <span class="unit">次 / ${fmtWindow(t.window_seconds)}</span>
        </div>
      </div>`).join('');

    el.innerHTML = `
      <div class="page-header"><h2>門檻設定</h2></div>
      <p style="color:var(--text-3);font-size:.85rem;margin-bottom:1.3rem;">
        當某類事件在其對應時間窗內超過門檻次數時，系統會觸發異常警報並建立復原請求。<br>
        數值越低越敏感，越高越寬鬆。調太低可能會誤報，請斟酌設定。
      </p>
      <div class="threshold-grid">${cards}</div>
      <button class="btn btn-primary" id="saveThresholds">💾 儲存設定</button>`;

    el.querySelector('#saveThresholds')?.addEventListener('click', () => saveThresholds(el));
  } catch (e) {
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

async function saveThresholds(el) {
  const inputs = el.querySelectorAll('.threshold-card input[data-event]');
  const thresholds = {};
  for (const inp of inputs) {
    const v = parseInt(inp.value, 10);
    if (isNaN(v) || v < 1 || v > 100) {
      toast(`「${inp.closest('.threshold-card').querySelector('.label').textContent}」的值必須在 1–100 之間`, 'error');
      inp.focus();
      return;
    }
    thresholds[inp.dataset.event] = v;
  }
  const btn = el.querySelector('#saveThresholds');
  btn.disabled = true;
  try {
    await api(`/api/guilds/${S.guild}/thresholds`, {
      method: 'PUT', body: JSON.stringify({ thresholds }),
    });
    toast('門檻設定已儲存', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
  btn.disabled = false;
}

// ── Init ─────────────────────────────────────────────────
async function init() {
  try {
    const data = await api('/api/me');
    S.user = data;
    if (data.guilds && data.guilds.length > 0) {
      S.guild = data.guilds[0].id;
    }
    if (!S.guild) {
      document.getElementById('app').innerHTML = `
        <div class="login-wrap">
          <div class="login-card">
            <div class="moon">🌙</div>
            <h1>SleepBot</h1>
            <p class="tagline">你目前不是任何伺服器的核准者。<br>請先在 Discord 上接受核准者邀請。</p>
            <a href="/auth/logout" class="btn-ghost" style="display:inline-block;padding:.6rem 1.4rem;">登出</a>
          </div>
        </div>`;
      return;
    }
    renderApp();
  } catch (e) {
    if (e.message === 'unauthorized') { renderLogin(); return; }
    document.getElementById('app').innerHTML = `
      <div class="login-wrap">
        <div class="login-card">
          <div class="moon">😴</div>
          <h1>連不上</h1>
          <p class="tagline">${randomError()}</p>
          <button class="btn btn-ghost" onclick="location.reload()">重試</button>
        </div>
      </div>`;
  }
}

document.addEventListener('DOMContentLoaded', init);
