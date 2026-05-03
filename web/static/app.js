/* ═══════════════════════════════════════════════════════════
   SleepBot 管理面板 — SPA
   手寫，不是 AI 吐的。歡迎來到深夜工作站。
   ═══════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────
const S = { user: null, guild: null, page: 'overview' };

const PREVIEW = (() => {
  const params = new URLSearchParams(window.location.search);
  const raw = (params.get('preview') || params.get('mock') || '').trim().toLowerCase();
  const enabled = ['1', 'true', 'yes', 'on'].includes(raw);
  const page = params.get('page') || 'overview';
  const allowedPages = new Set(['overview', 'recovery', 'thresholds', 'developer']);
  return {
    enabled,
    page: allowedPages.has(page) ? page : 'overview',
  };
})();

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

let _consoleSafetyWarned = false;

const PREVIEW_NOW = Math.floor(Date.now() / 1000);
const PREVIEW_GUILDS = [
  { id: 'guild-night-shift', name: '午夜值班站' },
  { id: 'guild-dream-watch', name: 'Dream Watch' },
  { id: 'guild-bot-only', name: 'Bot Sandbox' },
];

const PREVIEW_DB = {
  user: {
    id: 'preview-user-1',
    username: 'Tony Preview',
    avatar: '',
    is_developer: true,
    guilds: PREVIEW_GUILDS.slice(0, 2),
    guild_groups: {
      mine: [PREVIEW_GUILDS[0]],
      other_approved: [PREVIEW_GUILDS[1]],
      bot_only: [PREVIEW_GUILDS[2]],
      all: PREVIEW_GUILDS,
    },
  },
  overview: {
    channels: 42,
    roles: 17,
    events_24h: 9,
    pending_recoveries: 2,
  },
  events: {
    events: [
      { event_type: 'channel_delete', target_id: '122334455667788990', timestamp: PREVIEW_NOW - 420 },
      { event_type: 'admin_perm_remove', target_id: '998877665544332211', timestamp: PREVIEW_NOW - 1800 },
      { event_type: 'message_spam', target_id: 'chan-general', timestamp: PREVIEW_NOW - 5400 },
      { event_type: 'role_update', target_id: 'mod-role', timestamp: PREVIEW_NOW - 8600 },
    ],
  },
  maintenanceLogs: {
    logs: [
      { author_name: 'Tony', created_at: PREVIEW_NOW - 900, content: '把異常門檻從 3 調到 5，避免半夜誤報。' },
      { author_name: 'SleepBot', created_at: PREVIEW_NOW - 7200, content: '完成最近一次防禦狀態同步。' },
    ],
  },
  recoveryRequests: {
    requests: [
      {
        id: 'req-001',
        event_type: 'channel_delete',
        event_count: 3,
        status: 'pending',
        created_at: PREVIEW_NOW - 820,
      },
      {
        id: 'req-002',
        event_type: 'manual',
        event_count: 1,
        status: 'completed',
        created_at: PREVIEW_NOW - 14400,
        resolved_at: PREVIEW_NOW - 14320,
        result_channels: 2,
        result_roles: 1,
        result_messages: 18,
      },
      {
        id: 'req-003',
        event_type: 'role_update',
        event_count: 6,
        status: 'rejected',
        created_at: PREVIEW_NOW - 24800,
        resolved_at: PREVIEW_NOW - 24700,
        result_channels: 0,
        result_roles: 0,
        result_messages: 0,
      },
    ],
  },
  defense: {
    defense: {
      enabled: true,
      remaining_seconds: 0,
      disabled_until: null,
    },
  },
  thresholds: {
    thresholds: [
      { event_type: 'channel_delete', label: '頻道刪除', value: 3, default: 3, window_seconds: 300, default_window_seconds: 300 },
      { event_type: 'channel_update', label: '頻道修改', value: 5, default: 5, window_seconds: 300, default_window_seconds: 300 },
      { event_type: 'role_delete', label: '身分組刪除', value: 3, default: 3, window_seconds: 300, default_window_seconds: 300 },
      { event_type: 'admin_perm_remove', label: '管理員權限移除', value: 2, default: 2, window_seconds: 300, default_window_seconds: 300 },
      { event_type: 'message_spam', label: '訊息轟炸', value: 8, default: 8, window_seconds: 10, default_window_seconds: 10 },
    ],
  },
  devInfo: {
    is_developer: true,
  },
  rateLimitStats: {
    success: true,
    stats: {
      total_429s: 7,
      by_scope: { low: 2, medium: 3, high: 2 },
      by_limit_key: { 'POST /recovery/approve': 4, 'PUT /thresholds': 2, 'POST /defense/disable': 1 },
      by_bucket: { 'uid:preview-user-1': 5, 'ip:127.0.0.1': 2 },
    },
  },
  r2Quota: {
    default_quota_mb: 100,
    guild: {
      quota_mb: 180,
      is_override: true,
    },
  },
};

function clonePreview(data) {
  return JSON.parse(JSON.stringify(data));
}

function parsePreviewBody(body) {
  if (!body) return {};
  if (typeof body === 'string') {
    try {
      return JSON.parse(body);
    } catch (_) {
      return {};
    }
  }
  return body;
}

function buildPreviewSystemStats() {
  const tick = Math.floor(Date.now() / 3000) % 6;
  const cpu = 18 + tick * 4;
  const ramUsed = 2760 + tick * 45;
  const ramPercent = 42 + tick;
  const procCpu = 3 + tick;
  const procRam = 198 + tick * 3;
  const sent = 18 * 1024 + tick * 3800;
  const recv = 26 * 1024 + tick * 5200;
  return {
    success: true,
    timestamp: Math.floor(Date.now() / 1000),
    system: {
      cpu_percent: cpu,
      ram_used_mb: ramUsed,
      ram_total_mb: 8192,
      ram_percent: ramPercent,
    },
    process: {
      cpu_percent: procCpu,
      ram_mb: procRam,
    },
    network: {
      bytes_sent_per_s: sent,
      bytes_recv_per_s: recv,
    },
    redis: {
      available: true,
      used_memory_mb: 148.4,
      maxmemory_mb: 512,
      used_memory_percent: 29,
    },
    r2_usage: {
      available: true,
      total_quota_gb: 10,
      current_quota_gb: 2.384,
      remaining_quota_gb: 7.616,
      locked: false,
      lock_reason: '',
    },
  };
}

function previewApi(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const url = new URL(path, window.location.origin);
  const cleanPath = url.pathname;

  if (cleanPath === '/api/me') {
    return clonePreview(PREVIEW_DB.user);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/overview$/)) {
    return clonePreview(PREVIEW_DB.overview);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/events$/)) {
    return clonePreview(PREVIEW_DB.events);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/maintenance-logs$/)) {
    if (method === 'POST') {
      const payload = parsePreviewBody(opts.body);
      PREVIEW_DB.maintenanceLogs.logs.unshift({
        author_name: PREVIEW_DB.user.username,
        created_at: Math.floor(Date.now() / 1000),
        content: String(payload.content || '').trim() || '預覽模式新增的日誌',
      });
      return { success: true };
    }
    return clonePreview(PREVIEW_DB.maintenanceLogs);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/recovery-requests$/)) {
    return clonePreview(PREVIEW_DB.recoveryRequests);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/defense-status$/)) {
    return clonePreview(PREVIEW_DB.defense);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/recovery\/approve\/[^/]+$/)) {
    const id = cleanPath.split('/').pop();
    const req = PREVIEW_DB.recoveryRequests.requests.find(item => item.id === id);
    if (req) {
      req.status = 'completed';
      req.resolved_at = Math.floor(Date.now() / 1000);
      req.result_channels = 2;
      req.result_roles = 1;
      req.result_messages = 12;
    }
    return { channels: 2, roles: 1, messages: 12 };
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/recovery\/reject\/[^/]+$/)) {
    const id = cleanPath.split('/').pop();
    const req = PREVIEW_DB.recoveryRequests.requests.find(item => item.id === id);
    if (req) {
      req.status = 'rejected';
      req.resolved_at = Math.floor(Date.now() / 1000);
    }
    return { success: true };
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/recovery\/manual$/)) {
    PREVIEW_DB.recoveryRequests.requests.unshift({
      id: `req-manual-${Date.now()}`,
      event_type: 'manual',
      event_count: 1,
      status: 'completed',
      created_at: Math.floor(Date.now() / 1000),
      resolved_at: Math.floor(Date.now() / 1000),
      result_channels: 3,
      result_roles: 1,
      result_messages: 24,
    });
    return { queued: false, channels: 3, roles: 1, messages: 24 };
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/thresholds$/)) {
    if (method === 'PUT') {
      const payload = parsePreviewBody(opts.body);
      const next = payload.thresholds || {};
      PREVIEW_DB.thresholds.thresholds.forEach(item => {
        const updated = next[item.event_type];
        if (updated) {
          item.value = Number(updated.value || item.value);
          item.window_seconds = Number(updated.window_seconds || item.window_seconds);
        }
      });
      return { success: true };
    }
    return clonePreview(PREVIEW_DB.thresholds);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/defense\/disable$/)) {
    const until = Math.floor(Date.now() / 1000) + 3600;
    PREVIEW_DB.defense.defense.enabled = false;
    PREVIEW_DB.defense.defense.remaining_seconds = 3600;
    PREVIEW_DB.defense.defense.disabled_until = until;
    return clonePreview(PREVIEW_DB.defense);
  }
  if (cleanPath.match(/^\/api\/guilds\/[^/]+\/defense\/enable$/)) {
    PREVIEW_DB.defense.defense.enabled = true;
    PREVIEW_DB.defense.defense.remaining_seconds = 0;
    PREVIEW_DB.defense.defense.disabled_until = null;
    return { success: true };
  }
  if (cleanPath === '/api/dev/info') {
    return clonePreview(PREVIEW_DB.devInfo);
  }
  if (cleanPath === '/api/dev/ratelimit-stats') {
    return clonePreview(PREVIEW_DB.rateLimitStats);
  }
  if (cleanPath === '/api/dev/r2-quota') {
    if (method === 'PUT') {
      const payload = parsePreviewBody(opts.body);
      if (payload.scope === 'default' && Number(payload.quota_mb) > 0) {
        PREVIEW_DB.r2Quota.default_quota_mb = Number(payload.quota_mb);
        if (!PREVIEW_DB.r2Quota.guild.is_override) {
          PREVIEW_DB.r2Quota.guild.quota_mb = Number(payload.quota_mb);
        }
      }
      if (payload.scope === 'guild' && Number(payload.quota_mb) > 0) {
        PREVIEW_DB.r2Quota.guild.quota_mb = Number(payload.quota_mb);
        PREVIEW_DB.r2Quota.guild.is_override = true;
      }
      if (payload.scope === 'clear') {
        PREVIEW_DB.r2Quota.guild.quota_mb = PREVIEW_DB.r2Quota.default_quota_mb;
        PREVIEW_DB.r2Quota.guild.is_override = false;
      }
      return { message: '預覽配額已更新' };
    }
    return clonePreview(PREVIEW_DB.r2Quota);
  }
  if (cleanPath === '/api/dev/system-stats') {
    return buildPreviewSystemStats();
  }
  if (cleanPath === '/api/dev/reload') {
    const payload = parsePreviewBody(opts.body);
    const cog = String(payload.cog || '').trim();
    return {
      ok: cog ? [cog] : ['cogs.monitoring', 'cogs.recovery', 'cogs.system_commands'],
      failed: [],
      skipped: cog ? [] : ['cogs.web'],
    };
  }
  if (cleanPath.match(/^\/api\/dev\/guilds\/[^/]+\/commit$/)) {
    return {
      message: '預覽快照已建立',
      channels: 42,
      roles: 17,
      members: 126,
      snapshots_total: 9,
    };
  }

  throw new Error(`Preview route not mocked: ${method} ${cleanPath}`);
}

function warnConsolePasteScam() {
  if (_consoleSafetyWarned) return;
  _consoleSafetyWarned = true;

  const titleStyle = 'font-size:28px;font-weight:800;color:#ef4444;text-shadow:0 1px 0 rgba(0,0,0,.2)';
  const bodyStyle = 'font-size:14px;line-height:1.6;color:#f8fafc;background:#111827;padding:8px 10px;border-radius:6px';
  console.log('%c⚠️ 停下來！', titleStyle);
  console.log('%c如果有人教你把任何東西貼在這裡，你絕對被騙了。\n這通常是盜帳號或竊取權限的社交工程手法。\n不要貼上你看不懂的內容。\n\nIf someone tells you to paste something here, you are being scammed.\nThis is a social engineering trick to steal your account or permissions.\nNever paste code you do not fully understand.', bodyStyle);
}

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

function fmtDuration(seconds) {
  if (!seconds || seconds <= 0) return '0 分鐘';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分鐘`;
  const h = Math.floor(seconds / 3600);
  const m = Math.ceil((seconds % 3600) / 60);
  return m > 0 ? `${h} 小時 ${m} 分鐘` : `${h} 小時`;
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
  if (PREVIEW.enabled) {
    return previewApi(path, opts);
  }
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

// ── Hold-to-Confirm ──────────────────────────────────────
// 長按 durationMs 後才觸發 callback；中途放開則取消
function attachHoldToConfirm(btn, durationMs, callback) {
  let timer = null;
  let startText = btn.textContent;

  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    btn.classList.remove('holding');
    btn.style.removeProperty('--hold-duration');
    btn.textContent = startText;
    btn.disabled = false;
  };

  const start = (e) => {
    if (btn.disabled) return;
    e.preventDefault();
    startText = btn.textContent;
    btn.style.setProperty('--hold-duration', `${durationMs / 1000}s`);
    btn.classList.add('holding');
    btn.textContent = '⏳ 持續按住...';
    timer = setTimeout(() => {
      btn.classList.remove('holding');
      btn.textContent = startText;
      timer = null;
      callback();
    }, durationMs);
  };

  btn.addEventListener('mousedown', start);
  btn.addEventListener('touchstart', start, { passive: false });
  btn.addEventListener('mouseup', cancel);
  btn.addEventListener('mouseleave', cancel);
  btn.addEventListener('touchend', cancel);
  btn.addEventListener('touchcancel', cancel);
}

// ── Threshold natural-lang hint ──────────────────────────
function thresholdHintText(value, windowSeconds) {
  const v = Number(value) || 1;
  const w = Number(windowSeconds) || 300;
  const timeStr = w < 60
    ? `${w} 秒`
    : w % 60 === 0
      ? `${w / 60} 分鐘`
      : `${Math.floor(w / 60)} 分 ${w % 60} 秒`;
  return `目前設定：每${timeStr}出現${v}次即觸發`;
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

function getGuildGroups(user) {
  const groups = user?.guild_groups || {};
  const mine = Array.isArray(groups.mine) ? groups.mine : (Array.isArray(user?.guilds) ? user.guilds : []);
  const otherApproved = Array.isArray(groups.other_approved)
    ? groups.other_approved
    : (Array.isArray(groups.others) ? groups.others : []);
  const botOnly = Array.isArray(groups.bot_only) ? groups.bot_only : [];
  const all = Array.isArray(groups.all) ? groups.all : [...mine, ...otherApproved, ...botOnly];
  return { mine, otherApproved, botOnly, all };
}

function getSelectableGuilds(user) {
  const { mine, all, otherApproved, botOnly } = getGuildGroups(user);
  if (!user?.is_developer) return mine;
  if (mine.length || otherApproved.length || botOnly.length) return [...mine, ...otherApproved, ...botOnly];
  return all;
}

function buildGuildSelectHTML(user, selectedGuildId) {
  const { mine, otherApproved, botOnly } = getGuildGroups(user);
  const baseGuilds = Array.isArray(user?.guilds) ? user.guilds : [];

  if (!user?.is_developer) {
    return baseGuilds
      .map(g => `<option value="${esc(g.id)}" ${g.id === selectedGuildId ? 'selected' : ''}>${esc(g.name)}</option>`)
      .join('');
  }

  const groupToOptions = (items) => items
    .map(g => `<option value="${esc(g.id)}" ${g.id === selectedGuildId ? 'selected' : ''}>${esc(g.name)}</option>`)
    .join('');

  const chunks = [];
  if (mine.length) {
    chunks.push(`<optgroup label="我申請到的伺服器">${groupToOptions(mine)}</optgroup>`);
  }
  if (otherApproved.length) {
    chunks.push(`<optgroup label="他人核准的伺服器">${groupToOptions(otherApproved)}</optgroup>`);
  }
  if (botOnly.length) {
    chunks.push(`<optgroup label="僅 bot 在場（尚無核准者）">${groupToOptions(botOnly)}</optgroup>`);
  }

  if (!chunks.length) {
    return baseGuilds
      .map(g => `<option value="${esc(g.id)}" ${g.id === selectedGuildId ? 'selected' : ''}>${esc(g.name)}</option>`)
      .join('');
  }
  return chunks.join('');
}

// ── Render: app shell ────────────────────────────────────
function renderApp() {
  const u = S.user;
  const selectableGuilds = getSelectableGuilds(u);
  const guildOpts = buildGuildSelectHTML(u, S.guild);

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
          <li class="nav-item" data-page="developer" id="devTab" style="display:${u.is_developer ? 'block' : 'none'}"><span class="icon">🔧</span> 開發者</li>
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
          ${selectableGuilds.length > 1
            ? `<select class="guild-select" id="guildSelect">${guildOpts}</select>`
            : (selectableGuilds.length === 1 ? `<span style="font-size:.85rem;color:var(--text-3)">${esc(selectableGuilds[0].name)}</span>` : '')
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
  if (_devPoller && page !== 'developer') { clearInterval(_devPoller); _devPoller = null; }
  if (page !== 'recovery') { _clearRecoveryDefenseTimer(); }
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
  const pages = { overview: pageOverview, recovery: pageRecovery, thresholds: pageThresholds, developer: pageDeveloper };
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
    const known = EVENT_LABELS[ev.event_type];
    const info = known || { icon: '❓', label: esc(ev.event_type), cls: 'update' };
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
let _recoveryDefenseTimer = null;

function _clearRecoveryDefenseTimer() {
  if (_recoveryDefenseTimer !== null) {
    clearInterval(_recoveryDefenseTimer);
    _recoveryDefenseTimer = null;
  }
}

async function pageRecovery(el) {
  _clearRecoveryDefenseTimer();
  try {
    const [data, defenseResp] = await Promise.all([
      api(`/api/guilds/${S.guild}/recovery-requests`),
      api(`/api/guilds/${S.guild}/defense-status`),
    ]);
    const reqs = data.requests || [];
    const pending = reqs.filter(r => r.status === 'pending');
    const history = reqs.filter(r => r.status !== 'pending');
    const defense = defenseResp.defense || { enabled: true };

    el.innerHTML = `
      <div class="page-header"><h2>復原管理</h2><button class="btn btn-ghost btn-sm" id="refreshRecovery">↻ 重新整理</button></div>
      <div class="manual-section" style="margin-bottom:1.2rem;">
        <div class="info">
          <h3 id="defenseStatusTitle">${defense.enabled ? '🛡️ 防禦系統目前啟用中' : '🛑 防禦系統目前暫停中'}</h3>
          <p id="defenseStatusDesc">
            ${defense.enabled
              ? '異常偵測與自動防禦正在運作。'
              : `預計 <span id="defenseCountdown">${fmtDuration(defense.remaining_seconds)}</span> 後自動恢復。${defense.disabled_until ? `（${fmtTime(defense.disabled_until)}）` : ''}`}
          </p>
        </div>
        <button class="btn ${defense.enabled ? 'btn-danger' : 'btn-success'}" id="toggleDefenseRecovery">
          ${defense.enabled ? '暫時關閉 1 小時' : '立即重新啟用'}
        </button>
      </div>
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

    // 若防禦系統暫停中，啟動本地倒數計時器（每秒更新）
    if (!defense.enabled && defense.remaining_seconds > 0) {
      let remaining = defense.remaining_seconds;
      _recoveryDefenseTimer = setInterval(() => {
        remaining -= 1;
        const countdownEl = el.querySelector('#defenseCountdown');
        if (!countdownEl) { _clearRecoveryDefenseTimer(); return; }
        if (remaining <= 0) {
          _clearRecoveryDefenseTimer();
          // 倒數歸零後重新拉取頁面，確認是否已自動恢復
          pageRecovery(el);
        } else {
          countdownEl.textContent = fmtDuration(remaining);
        }
      }, 1000);
    }

    el.querySelector('#refreshRecovery')?.addEventListener('click', () => pageRecovery(el));
    el.querySelector('#toggleDefenseRecovery')?.addEventListener('click', async () => {
      if (defense.enabled) {
        await disableDefense(el, pageRecovery);
      } else {
        await enableDefense(el, pageRecovery);
      }
    });
    // manual recovery 改為長按確認
    const manualBtn = el.querySelector('#manualRecovery');
    if (manualBtn) attachHoldToConfirm(manualBtn, 2000, () => doManualRecovery(el));

    // approve / reject 改為長按確認
    el.querySelectorAll('[data-approve]').forEach(btn => {
      attachHoldToConfirm(btn, 1500, () => doApprove(btn.dataset.approve, el));
    });
    el.querySelectorAll('[data-reject]').forEach(btn => {
      attachHoldToConfirm(btn, 1200, () => doReject(btn.dataset.reject, el));
    });
  } catch (e) {
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

function renderPendingCard(r) {
  const known = EVENT_LABELS[r.event_type];
  const info = known || { icon: '❓', label: esc(r.event_type) };
  const st = STATUS_MAP[r.status] || STATUS_MAP.pending;
  return `
    <div class="request-card">
      <div class="request-info">
        <div class="request-type">${info.icon} ${info.label} <span class="badge ${st.cls}">${st.icon} ${st.label}</span></div>
        <div class="request-meta">事件數 ${r.event_count} · ${fmtTime(r.created_at)}</div>
      </div>
      <div class="request-actions">
        <button class="btn btn-success btn-sm btn-hold" data-approve="${r.id}" style="--hold-duration:1.5s" title="長按 1.5 秒確認核准">✅ 長按核准</button>
        <button class="btn btn-ghost btn-sm btn-hold" data-reject="${r.id}" style="--hold-duration:1.2s" title="長按 1.2 秒確認拒絕">❌ 長按拒絕</button>
      </div>
    </div>`;
}

function renderHistoryCard(r) {
  const known = EVENT_LABELS[r.event_type];
  const info = known || { icon: '❓', label: esc(r.event_type) };
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
    if (res?.queued) {
      toast(res.message || '系統正在關機，這筆手動復原已排程到下次開機執行', 'info');
    } else {
      const ch = Number.isFinite(res?.channels) ? res.channels : 0;
      const ro = Number.isFinite(res?.roles) ? res.roles : 0;
      const ms = Number.isFinite(res?.messages) ? res.messages : 0;
      toast(`手動復原完成！頻道 ${ch} · 身分組 ${ro} · 訊息 ${ms}`, 'success');
    }
    pageRecovery(el);
  } catch (e) { toast(e.message, 'error'); }
}

// ── Page: thresholds ─────────────────────────────────────
async function pageThresholds(el) {
  try {
    const [data, defenseResp] = await Promise.all([
      api(`/api/guilds/${S.guild}/thresholds`),
      api(`/api/guilds/${S.guild}/defense-status`),
    ]);
    const items = data.thresholds || [];
    const defense = defenseResp.defense || { enabled: true };
    const fmtWindow = (seconds) => {
      if (!seconds || seconds <= 0) return '5 分鐘';
      if (seconds % 60 === 0) return `${seconds / 60} 分鐘`;
      return `${seconds} 秒`;
    };

    const cards = items.map(t => `
      <div class="threshold-card">
        <div class="label">${esc(t.label)}</div>
        <div class="sublabel">預設值：${t.default} 次 / ${fmtWindow(t.default_window_seconds || t.window_seconds)}</div>
        <div class="input-row">
          <input type="number" class="form-input input-number"
                 data-event="${esc(t.event_type)}" value="${t.value}" min="1" max="100">
          <span class="unit">次</span>
        </div>
        <div class="input-row" style="margin-top:.45rem;gap:.5rem;align-items:center;">
          <input type="number" class="form-input input-number"
                 data-window-min="${esc(t.event_type)}" value="${Math.floor((t.window_seconds || 0) / 60)}" min="0" max="60">
          <span class="unit">分</span>
          <input type="number" class="form-input input-number"
                 data-window-sec="${esc(t.event_type)}" value="${(t.window_seconds || 0) % 60}" min="0" max="59">
          <span class="unit">秒</span>
          <span class="unit" style="margin-left:.25rem;">= ${fmtWindow(t.window_seconds)}</span>
        </div>
        <div class="threshold-hint" data-hint="${esc(t.event_type)}">${thresholdHintText(t.value, t.window_seconds)}</div>
      </div>`).join('');

    el.innerHTML = `
      <div class="page-header"><h2>門檻設定</h2></div>
      <div class="manual-section" style="margin-bottom:1.2rem;">
        <div class="info">
          <h3>${defense.enabled ? '🛡️ 防禦系統目前啟用中' : '🛑 防禦系統目前暫停中'}</h3>
          <p>
            ${defense.enabled
              ? '異常偵測與自動防禦正在運作。'
              : `預計 ${fmtDuration(defense.remaining_seconds)} 後自動恢復。${defense.disabled_until ? `（${fmtTime(defense.disabled_until)}）` : ''}`}
          </p>
        </div>
        <button class="btn ${defense.enabled ? 'btn-danger' : 'btn-success'}" id="toggleDefense">
          ${defense.enabled ? '暫時關閉 1 小時' : '立即重新啟用'}
        </button>
      </div>
      <p style="color:var(--text-3);font-size:.85rem;margin-bottom:1.3rem;">
        當某類事件在其對應時間窗內超過門檻次數時，系統會觸發異常警報並建立復原請求。<br>
        現在可同時調整「幾次 / 幾分幾秒」。數值越低越敏感，越高越寬鬆。調太低可能會誤報，請斟酌設定。
      </p>
      <div class="threshold-grid">${cards}</div>
      <button class="btn btn-primary" id="saveThresholds">💾 儲存設定</button>`;

    el.querySelector('#toggleDefense')?.addEventListener('click', async () => {
      if (defense.enabled) {
        await disableDefense(el);
      } else {
        await enableDefense(el);
      }
    });
    el.querySelector('#saveThresholds')?.addEventListener('click', () => saveThresholds(el));

    // 即時更新自然語言提示
    el.querySelectorAll('.threshold-card').forEach(card => {
      const updateHint = () => {
        const evInp = card.querySelector('input[data-event]');
        const minInp = card.querySelector('input[data-window-min]');
        const secInp = card.querySelector('input[data-window-sec]');
        const hint = card.querySelector('.threshold-hint');
        if (!evInp || !hint) return;
        const v = parseInt(evInp.value, 10) || 1;
        const mins = parseInt(minInp?.value || '0', 10) || 0;
        const secs = parseInt(secInp?.value || '0', 10) || 0;
        const ws = (mins * 60) + secs;
        hint.textContent = thresholdHintText(v, ws || 300);
        hint.classList.add('highlight');
        clearTimeout(hint._hTimer);
        hint._hTimer = setTimeout(() => hint.classList.remove('highlight'), 1200);
      };
      card.querySelectorAll('input').forEach(inp => inp.addEventListener('input', updateHint));
    });
  } catch (e) {
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

async function disableDefense(el, rerender = pageThresholds) {
  const ok = await showConfirm(
    '暫時關閉防禦系統',
    '防禦系統會暫停 1 小時（手動復原仍可使用），並在時間到後自動恢復。是否繼續？'
  );
  if (!ok) return;

  const btn = el.querySelector('#toggleDefense') || el.querySelector('#toggleDefenseRecovery');
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/guilds/${S.guild}/defense/disable`, {
      method: 'POST',
      body: JSON.stringify({ duration_seconds: 3600 }),
    });
    const until = res.defense?.disabled_until;
    toast(
      until
        ? `防禦系統已暫停，將於 ${fmtTime(until)} 自動恢復`
        : '防禦系統已暫停 1 小時',
      'info'
    );
    rerender(el);
  } catch (e) {
    toast(e.message, 'error');
    if (btn) btn.disabled = false;
  }
}

async function enableDefense(el, rerender = pageThresholds) {
  const ok = await showConfirm('啟用防禦系統', '確定要立即重新啟用防禦系統嗎？');
  if (!ok) return;

  const btn = el.querySelector('#toggleDefense') || el.querySelector('#toggleDefenseRecovery');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/guilds/${S.guild}/defense/enable`, { method: 'POST' });
    toast('防禦系統已重新啟用', 'success');
    rerender(el);
  } catch (e) {
    toast(e.message, 'error');
    if (btn) btn.disabled = false;
  }
}

async function saveThresholds(el) {
  const inputs = el.querySelectorAll('.threshold-card input[data-event]');
  const thresholds = {};
  for (const inp of inputs) {
    const eventType = inp.dataset.event;
    const v = parseInt(inp.value, 10);
    if (isNaN(v) || v < 1 || v > 100) {
      toast(`「${inp.closest('.threshold-card').querySelector('.label').textContent}」的值必須在 1–100 之間`, 'error');
      inp.focus();
      return;
    }

    const minInp = el.querySelector(`input[data-window-min="${CSS.escape(eventType)}"]`);
    const secInp = el.querySelector(`input[data-window-sec="${CSS.escape(eventType)}"]`);
    const mins = parseInt(minInp?.value || '0', 10);
    const secs = parseInt(secInp?.value || '0', 10);
    if (isNaN(mins) || mins < 0 || mins > 60 || isNaN(secs) || secs < 0 || secs > 59) {
      toast(`「${inp.closest('.threshold-card').querySelector('.label').textContent}」時間需為 0–60 分、0–59 秒`, 'error');
      (minInp || secInp || inp).focus();
      return;
    }
    const windowSeconds = (mins * 60) + secs;
    if (windowSeconds < 1 || windowSeconds > 3600) {
      toast(`「${inp.closest('.threshold-card').querySelector('.label').textContent}」時間窗需在 1–3600 秒`, 'error');
      (minInp || secInp || inp).focus();
      return;
    }

    thresholds[eventType] = { value: v, window_seconds: windowSeconds };
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


// ── Developer page state ─────────────────────────────────
const _devNetHistory = [];   // [{ts, sent, recv}]  最多 60 筆
let _devPoller = null;

// ── Page: developer ──────────────────────────────────────
async function pageDeveloper(el) {
  console.log('[pageDeveloper] Start, el:', el);
  if (_devPoller) { clearInterval(_devPoller); _devPoller = null; }
  try {
    console.log('[pageDeveloper] Calling /api/dev/info...');
    const devInfo = await api('/api/dev/info');
    console.log('[pageDeveloper] Got devInfo:', devInfo);

    if (!devInfo.is_developer) {
      el.innerHTML = emptyHTML('🔒', '你不是開發者');
      return;
    }
    console.log('[pageDeveloper] is_developer: true');

    let stat = { total_429s: 0, by_scope: {}, by_limit_key: {}, by_bucket: {} };
    try {
      console.log('[pageDeveloper] Calling /api/dev/ratelimit-stats...');
      const statsResp = await api('/api/dev/ratelimit-stats?minutes=5');
      console.log('[pageDeveloper] Got statsResp:', statsResp);
      if (statsResp && statsResp.stats && typeof statsResp.stats === 'object') {
        stat = statsResp.stats;
        console.log('[pageDeveloper] Assigned stat from API');
      }
    } catch (statsErr) {
      console.debug('dev stats unavailable', statsErr);
      // stat keeps default empty structure
    }
    console.log('[pageDeveloper] stat ready:', stat);

    let quotaState = { default_quota_mb: 100, guild: null };
    try {
      const query = S.guild ? `?gid=${encodeURIComponent(S.guild)}` : '';
      quotaState = await api(`/api/dev/r2-quota${query}`);
    } catch (quotaErr) {
      console.debug('dev r2 quota unavailable', quotaErr);
    }

    let html = `<div class="page-header"><h2>⚙️ 開發者面板</h2><button class="btn btn-ghost btn-sm" onclick="navigate('developer')">↻ 重新整理</button></div>`;

    html += `
    <div class="section">
      <div class="section-title">☁️ R2 配額控制</div>
      <p style="margin:.1rem 0 .7rem;color:var(--text-3);font-size:.85rem;line-height:1.6">
        所有伺服器預設上限為 100 MB，可在這裡調整全域預設，或覆寫目前選取伺服器的額度。
      </p>
      <div style="display:grid;gap:.8rem">
        <div style="display:flex;gap:.55rem;flex-wrap:wrap;align-items:center">
          <label style="font-size:.82rem;color:var(--text-3);min-width:110px">預設配額</label>
          <input id="devR2DefaultQuota" class="form-input" type="number" min="1" step="1" style="max-width:160px" value="${esc(String(quotaState.default_quota_mb || 100))}">
          <span style="font-size:.82rem;color:var(--text-3)">MB</span>
          <button class="btn btn-primary btn-sm" id="devR2DefaultSave">更新預設</button>
        </div>
        <div style="display:flex;gap:.55rem;flex-wrap:wrap;align-items:center">
          <label style="font-size:.82rem;color:var(--text-3);min-width:110px">目前伺服器</label>
          <input id="devR2GuildQuota" class="form-input" type="number" min="1" step="1" style="max-width:160px" value="${esc(String(quotaState.guild?.quota_mb || quotaState.default_quota_mb || 100))}" ${S.guild ? '' : 'disabled'}>
          <span style="font-size:.82rem;color:var(--text-3)">MB</span>
          <button class="btn btn-primary btn-sm" id="devR2GuildSave" ${S.guild ? '' : 'disabled'}>覆寫目前伺服器</button>
          <button class="btn btn-ghost btn-sm" id="devR2GuildClear" ${S.guild ? '' : 'disabled'}>清除覆寫</button>
        </div>
      </div>
      <div style="margin-top:.8rem;display:grid;gap:.4rem;font-size:.86rem">
        <div style="display:flex;justify-content:space-between;gap:1rem"><span style="color:var(--text-3)">總配額</span><strong id="devR2TotalQuota">—</strong></div>
        <div style="display:flex;justify-content:space-between;gap:1rem"><span style="color:var(--text-3)">目前配額</span><strong id="devR2CurrentQuota">—</strong></div>
        <div style="display:flex;justify-content:space-between;gap:1rem"><span style="color:var(--text-3)">剩餘配額</span><strong id="devR2RemainingQuota">—</strong></div>
        <div id="devR2QuotaStatus" style="font-size:.8rem;color:var(--text-3)">等待 daemon 資料...</div>
      </div>
      <div id="devR2QuotaResult" style="margin-top:.65rem;font-size:.82rem;color:var(--text-3)">—</div>
    </div>`;

    // ── Reload 控制 ──────────────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">♻️ 模組 Reload</div>
      <p style="margin:.1rem 0 .7rem;color:var(--text-3);font-size:.85rem;line-height:1.6">
        可輸入指定模組（如 cogs.monitoring）或直接 reload 全部。<br>
        為避免中斷目前面板連線，Web 端 reload 全部時會略過 cogs.web。
      </p>
      <div style="display:flex;gap:.55rem;flex-wrap:wrap">
        <input id="devReloadCog" class="form-input" style="max-width:320px" placeholder="輸入模組（例如 cogs.monitoring）">
        <button class="btn btn-primary btn-sm" id="devReloadOne">Reload 指定</button>
        <button class="btn btn-ghost btn-sm" id="devReloadAll">Reload 全部</button>
      </div>
      <div id="devReloadResult" style="margin-top:.65rem;font-size:.82rem;color:var(--text-3)">尚未執行</div>
    </div>`;

    // ── Commit 控制 ──────────────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">📌 快照 Commit</div>
      <p style="margin:.1rem 0 .7rem;color:var(--text-3);font-size:.85rem;line-height:1.6">
        對「目前上方選到的伺服器」執行一次 \`>>commit\` 等效流程（寫入 Redis 快照）。
      </p>
      <div style="display:flex;gap:.55rem;flex-wrap:wrap;align-items:center">
        <button class="btn btn-primary btn-sm" id="devCommitCurrent">對目前伺服器執行 Commit</button>
      </div>
      <div id="devCommitResult" style="margin-top:.65rem;font-size:.82rem;color:var(--text-3)">尚未執行</div>
    </div>`;

    // ── 系統資源表 ────────────────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">🖥️ 系統資源</div>
      <table style="width:100%;font-size:.9rem;border-collapse:collapse" id="devSysTable">
        <tr style="border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:.45rem .6rem;color:var(--text-3)">項目</th>
          <th style="text-align:right;padding:.45rem .6rem;color:var(--text-3)">數值</th>
        </tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.4rem .6rem">系統 CPU</td><td style="text-align:right;padding:.4rem .6rem" id="dsc">—</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.4rem .6rem">系統 RAM 使用</td><td style="text-align:right;padding:.4rem .6rem" id="dsr">—</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.4rem .6rem">RAM 使用率</td><td style="text-align:right;padding:.4rem .6rem" id="dsrp">—</td></tr>
      </table>
    </div>`;

    // ── 程式資源表 ────────────────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">🤖 Bot 程式資源</div>
      <table style="width:100%;font-size:.9rem;border-collapse:collapse">
        <tr style="border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:.45rem .6rem;color:var(--text-3)">項目</th>
          <th style="text-align:right;padding:.45rem .6rem;color:var(--text-3)">數值</th>
        </tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.4rem .6rem">程式 CPU</td><td style="text-align:right;padding:.4rem .6rem" id="dpc">—</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.4rem .6rem">程式 RAM (RSS)</td><td style="text-align:right;padding:.4rem .6rem" id="dpr">—</td></tr>
      </table>
    </div>`;

    // ── System Health Score ──────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">💠 系統健康指數</div>
      <div id="devHealthScore" class="health-score-ring good">
        <span class="score-num">—</span>
        <div>
          <div class="score-label">Health Score</div>
          <div id="devHealthDetail" style="font-size:.72rem;color:var(--text-3);margin-top:.2rem">計算中...</div>
        </div>
      </div>
    </div>`;

    // ── Redis RAM dot-matrix ─────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">🗄️ Redis 記憶體（點陣矩陣）</div>
      <div id="devRedis" style="font-size:.9rem">—</div>
    </div>`;

    // ── 網路圖 ────────────────────────────────────────────
    html += `
    <div class="section">
      <div class="section-title">🌐 網路 I/O（即時，每 3 秒更新）</div>
      <div style="display:flex;gap:1rem;margin-bottom:.6rem">
        <span style="font-size:.85rem;color:#4ade80">▲ 上傳：<strong id="devNetUp">—</strong></span>
        <span style="font-size:.85rem;color:#60a5fa">▼ 下載：<strong id="devNetDn">—</strong></span>
      </div>
      <canvas id="devNetCanvas" width="600" height="120" style="width:100%;background:rgba(4,10,18,0.8);border-radius:.5rem"></canvas>
    </div>`;

    // ── Rate-limit 統計 terminal 樣式 ────────────────────
    html += `
    <div class="section">
      <div class="section-title">📟 Rate-limit 統計（最近 5 分鐘）<span id="devRlTs" style="font-size:.75rem;color:var(--text-3);margin-left:.6rem"></span></div>
      <div class="dev-terminal-block" id="devRlBody"><span class="term-dim">Loading...</span></div>
    </div>`;

    el.innerHTML = html;
    console.log('[pageDeveloper] Set innerHTML, stat:', stat);
    
    try {
      console.log('[pageDeveloper] Calling _devRenderRl...');
      _devRenderRl(stat);
      console.log('[pageDeveloper] _devRenderRl done');
    } catch (e) {
      console.error('Failed to render rate-limit stats:', e);
      const body = document.getElementById('devRlBody');
      if (body) body.innerHTML = '<span style="color:var(--text-3)">統計暫時無法顯示</span>';
    }
    
    try {
      console.log('[pageDeveloper] Calling _devStartPoller...');
      _devStartPoller(el);
      console.log('[pageDeveloper] _devStartPoller done');
    } catch (e) {
      console.error('Failed to start dev poller:', e);
      // Still allow page to render even if poller fails
    }

    console.log('[pageDeveloper] Adding event listeners...');
    el.querySelector('#devReloadOne')?.addEventListener('click', async () => {
      const input = el.querySelector('#devReloadCog');
      const cog = (input?.value || '').trim();
      if (!cog) {
        toast('請先輸入模組名稱', 'info');
        input?.focus();
        return;
      }
      await _devReload(el, cog);
    });
    el.querySelector('#devReloadAll')?.addEventListener('click', async () => {
      await _devReload(el, '');
    });
    el.querySelector('#devR2DefaultSave')?.addEventListener('click', async () => {
      await _devSetDefaultQuota(el);
    });
    el.querySelector('#devR2GuildSave')?.addEventListener('click', async () => {
      await _devSetGuildQuota(el);
    });
    el.querySelector('#devR2GuildClear')?.addEventListener('click', async () => {
      await _devClearGuildQuota(el);
    });
    el.querySelector('#devCommitCurrent')?.addEventListener('click', async () => {
      await _devCommitCurrent(el);
    });
    _devRenderR2Quota(el, quotaState);
    console.log('[pageDeveloper] Done!');

  } catch (e) {
    console.error('[pageDeveloper] Caught error:', e);
    if (_devPoller) { clearInterval(_devPoller); _devPoller = null; }
    if (e.message === 'unauthorized') return;
    el.innerHTML = emptyHTML('😴', randomError());
  }
}

function _fmtBytes(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB/s';
  if (b >= 1024) return (b / 1024).toFixed(1) + ' KB/s';
  return b.toFixed(0) + ' B/s';
}

function _fmtGiB(v) {
  const n = Number(v || 0);
  return `${n.toFixed(3)} GB`;
}

function _devDrawChart(canvas) {
  if (!canvas || _devNetHistory.length < 2) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const pts = _devNetHistory.slice(-60);
  const maxVal = Math.max(...pts.flatMap(p => [p.sent, p.recv]), 1);

  const padL = 52, padR = 8, padT = 8, padB = 20;
  const cw = w - padL - padR, ch = h - padT - padB;

  // Grid lines
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padT + ch - (ch * i / 4);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.35)';
    ctx.font = '9px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(_fmtBytes(maxVal * i / 4), padL - 4, y + 3);
  }

  function drawLine(getV, color) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    pts.forEach((p, i) => {
      const x = padL + (i / (pts.length - 1)) * cw;
      const y = padT + ch - (getV(p) / maxVal) * ch;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Fill
    ctx.save();
    ctx.globalAlpha = 0.12;
    ctx.fillStyle = color;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = padL + (i / (pts.length - 1)) * cw;
      const y = padT + ch - (getV(p) / maxVal) * ch;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.lineTo(padL + cw, padT + ch);
    ctx.lineTo(padL, padT + ch);
    ctx.closePath(); ctx.fill();
    ctx.restore();
  }

  drawLine(p => p.sent, '#4ade80');
  drawLine(p => p.recv, '#60a5fa');

  // Legend
  ctx.font = '10px sans-serif';
  ctx.fillStyle = '#4ade80'; ctx.fillRect(padL, h - 14, 10, 3);
  ctx.fillStyle = 'rgba(255,255,255,.6)'; ctx.textAlign = 'left';
  ctx.fillText('上傳', padL + 14, h - 10);
  ctx.fillStyle = '#60a5fa'; ctx.fillRect(padL + 60, h - 14, 10, 3);
  ctx.fillStyle = 'rgba(255,255,255,.6)';
  ctx.fillText('下載', padL + 74, h - 10);
}

function _devUpdateDOM(d) {
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  if (d.system) {
    set('dsc', d.system.cpu_percent + '%');
    set('dsr', d.system.ram_used_mb.toFixed(0) + ' MB / ' + d.system.ram_total_mb.toFixed(0) + ' MB');
    set('dsrp', d.system.ram_percent + '%');
  }
  if (d.process) {
    set('dpc', d.process.cpu_percent + '%');
    set('dpr', d.process.ram_mb.toFixed(1) + ' MB');
  }
  if (d.network) {
    set('devNetUp', _fmtBytes(d.network.bytes_sent_per_s));
    set('devNetDn', _fmtBytes(d.network.bytes_recv_per_s));
    _devNetHistory.push({ ts: d.timestamp, sent: d.network.bytes_sent_per_s, recv: d.network.bytes_recv_per_s });
    if (_devNetHistory.length > 60) _devNetHistory.shift();
    const canvas = document.getElementById('devNetCanvas');
    if (canvas) {
      canvas.width = canvas.offsetWidth || 600;
      _devDrawChart(canvas);
    }
  }
  if (d.redis) {
    const el = document.getElementById('devRedis');
    if (el) {
      if (!d.redis.available) {
        el.innerHTML = '<span class="term-dim">Redis offline — no connection</span>';
      } else {
        const used = d.redis.used_memory_mb;
        const max = d.redis.maxmemory_mb;
        const pct = d.redis.used_memory_percent ?? 0;
        const barPct = Math.min(100, Math.round(pct));
        const dotTotal = 60;
        const dotFilled = Math.round((barPct / 100) * dotTotal);
        const dotCls = barPct > 85 ? 'danger' : barPct > 60 ? 'warn' : 'filled';
        const dots = Array.from({ length: dotTotal }, (_, i) =>
          `<span class="dot${i < dotFilled ? ' ' + dotCls : ''}"></span>`
        ).join('');
        el.innerHTML = `
          <div style="font-size:.8rem;color:var(--text-3);margin-bottom:.35rem;font-family:var(--font-mono)">
            used <span style="color:#e2e8f0">${used.toFixed(1)} MB</span> /
            max <span style="color:#e2e8f0">${max ? max.toFixed(1) + ' MB' : 'unlimited'}</span>
            — <span style="color:${barPct > 85 ? 'var(--red)' : barPct > 60 ? 'var(--yellow)' : '#4ade80'}">${barPct}%</span>
          </div>
          <div class="dot-matrix">${dots}</div>`;
      }
    }
  }

  // System Health Score
  _devUpdateHealthScore(d);

  if (d.r2_usage) {
    set('devR2TotalQuota', _fmtGiB(d.r2_usage.total_quota_gb));
    set('devR2CurrentQuota', _fmtGiB(d.r2_usage.current_quota_gb));
    set('devR2RemainingQuota', _fmtGiB(d.r2_usage.remaining_quota_gb));

    const statusEl = document.getElementById('devR2QuotaStatus');
    if (!statusEl) return;

    if (!d.r2_usage.available) {
      statusEl.textContent = d.r2_usage.error ? `daemon 無法取得：${d.r2_usage.error}` : 'daemon 未啟用';
      statusEl.style.color = 'var(--text-3)';
      return;
    }

    if (d.r2_usage.locked) {
      statusEl.textContent = `已鎖定：${d.r2_usage.lock_reason || '達到上限'}`;
      statusEl.style.color = 'var(--danger)';
    } else {
      statusEl.textContent = '狀態正常';
      statusEl.style.color = 'var(--text-3)';
    }
  }
}

// ── System Health Score algorithm ───────────────────────
// 加權：Redis 延遲風險 40%、R2 剩餘配額 30%、RAM 使用率 30%
function _devComputeHealthScore(d) {
  let score = 100;
  const reasons = [];

  // Redis latency risk (inferred from memory pressure)
  if (d.redis && d.redis.available) {
    const redisPct = d.redis.used_memory_percent ?? 0;
    if (redisPct > 90)       { score -= 40; reasons.push(`Redis ${redisPct}% — critical`); }
    else if (redisPct > 75)  { score -= 20; reasons.push(`Redis ${redisPct}% — elevated`); }
    else if (redisPct > 55)  { score -= 8;  reasons.push(`Redis ${redisPct}% — watch`); }
  } else if (d.redis && !d.redis.available) {
    score -= 40; reasons.push('Redis offline');
  }

  // R2 remaining quota
  if (d.r2_usage && d.r2_usage.available) {
    const r2Pct = d.r2_usage.total_quota_gb > 0
      ? ((d.r2_usage.remaining_quota_gb / d.r2_usage.total_quota_gb) * 100)
      : 100;
    if (r2Pct < 5)        { score -= 30; reasons.push(`R2 < 5% left`); }
    else if (r2Pct < 15)  { score -= 15; reasons.push(`R2 ${r2Pct.toFixed(0)}% left`); }
    else if (r2Pct < 30)  { score -= 5;  reasons.push(`R2 ${r2Pct.toFixed(0)}% left`); }
    if (d.r2_usage.locked) { score -= 20; reasons.push('R2 locked'); }
  }

  // RAM
  if (d.system) {
    const ramPct = d.system.ram_percent ?? 0;
    if (ramPct > 92)       { score -= 30; reasons.push(`RAM ${ramPct}%`); }
    else if (ramPct > 80)  { score -= 15; reasons.push(`RAM ${ramPct}%`); }
    else if (ramPct > 70)  { score -= 5;  reasons.push(`RAM ${ramPct}%`); }
  }

  return { score: Math.max(0, Math.min(100, Math.round(score))), reasons };
}

function _devUpdateHealthScore(d) {
  const ring = document.getElementById('devHealthScore');
  const detail = document.getElementById('devHealthDetail');
  if (!ring || !detail) return;

  const { score, reasons } = _devComputeHealthScore(d);
  const numEl = ring.querySelector('.score-num');
  if (numEl) numEl.textContent = score;

  ring.className = 'health-score-ring';
  if (score >= 85)      ring.classList.add('good');
  else if (score >= 60) ring.classList.add('warn');
  else                  ring.classList.add('danger');

  detail.textContent = reasons.length ? reasons.join(' · ') : 'All systems nominal';
}

function _devRenderRl(stat) {
  const body = document.getElementById('devRlBody');
  if (!body) return;

  const now = new Date().toLocaleTimeString('en-GB', { hour12: false });
  const total = stat.total_429s || 0;

  let lines = [];
  lines.push(`<span class="term-dim">$ rate-limit-stats --window 5m  [${now}]</span>`);
  lines.push(`<span class="term-label">total_429s</span>   <span class="${total > 0 ? 'term-err' : 'term-ok'}">${total}</span>`);
  lines.push('');

  if (stat.by_scope && Object.keys(stat.by_scope).length) {
    lines.push('<span class="term-label">scope breakdown</span>');
    for (const [scope, count] of Object.entries(stat.by_scope)) {
      const bar = '█'.repeat(Math.min(20, count)) + '░'.repeat(Math.max(0, 20 - count));
      lines.push(`  <span class="term-dim">${scope.padEnd(10)}</span> <span class="term-val">${bar}</span> <span class="term-warn">${count}</span>`);
    }
    lines.push('');
  }

  if (stat.by_limit_key && Object.keys(stat.by_limit_key).length) {
    lines.push('<span class="term-label">by endpoint</span>');
    for (const [key, count] of Object.entries(stat.by_limit_key)) {
      const pct = total > 0 ? Math.round((count / total) * 100) : 0;
      lines.push(`  <span class="term-dim">${esc(key).padEnd(38)}</span> <span class="term-val">${String(count).padStart(4)}</span>  <span class="term-dim">(${pct}%)</span>`);
    }
    lines.push('');
  }

  if (stat.by_bucket && Object.keys(stat.by_bucket).length) {
    lines.push('<span class="term-label">hot buckets (top 10)</span>');
    const sorted = Object.entries(stat.by_bucket).sort((a, b) => b[1] - a[1]).slice(0, 10);
    for (const [bucket, count] of sorted) {
      const pct = total > 0 ? Math.round((count / total) * 100) : 0;
      const filled = Math.round(pct / 5);
      const miniBar = '[' + '|'.repeat(filled) + ' '.repeat(Math.max(0, 20 - filled)) + ']';
      lines.push(`  <span class="term-dim">${esc(bucket).padEnd(30)}</span> <span class="term-warn">${miniBar}</span> <span class="term-val">${count}</span>`);
    }
  }

  body.innerHTML = lines.map(l => l === '' ? '<br>' : `<div>${l}</div>`).join('');
  const ts = document.getElementById('devRlTs');
  if (ts) ts.textContent = now;
}

async function _devReload(el, cog) {
  const resultEl = el.querySelector('#devReloadResult');
  const oneBtn = el.querySelector('#devReloadOne');
  const allBtn = el.querySelector('#devReloadAll');
  if (oneBtn) oneBtn.disabled = true;
  if (allBtn) allBtn.disabled = true;
  if (resultEl) resultEl.textContent = '執行中...';

  try {
    const payload = cog ? { cog } : {};
    const res = await api('/api/dev/reload', {
      method: 'POST',
      body: JSON.stringify(payload),
    });

    const ok = Array.isArray(res.ok) ? res.ok : [];
    const failed = Array.isArray(res.failed) ? res.failed : [];
    const skipped = Array.isArray(res.skipped) ? res.skipped : [];

    const lines = [];
    if (ok.length) lines.push(`✅ 成功：${ok.join(', ')}`);
    if (failed.length) lines.push(`❌ 失敗：${failed.join(' | ')}`);
    if (skipped.length) lines.push(`⏭️ 略過：${skipped.join(', ')}`);
    if (resultEl) resultEl.textContent = lines.join(' / ') || '無可 reload 模組';

    if (failed.length) {
      toast(`Reload 完成，但有 ${failed.length} 個失敗`, 'error');
    } else {
      toast('Reload 完成', 'success');
    }
  } catch (e) {
    if (resultEl) resultEl.textContent = `❌ 執行失敗：${e.message}`;
    toast(e.message, 'error');
  }

  if (oneBtn) oneBtn.disabled = false;
  if (allBtn) allBtn.disabled = false;
}

async function _devCommitCurrent(el) {
  const resultEl = el.querySelector('#devCommitResult');
  const btn = el.querySelector('#devCommitCurrent');
  if (!S.guild) {
    toast('目前沒有可操作的伺服器', 'error');
    return;
  }

  const ok = await showConfirm('執行 Commit', '確定要對目前選取的伺服器執行快照 Commit 嗎？');
  if (!ok) return;

  if (btn) btn.disabled = true;
  if (resultEl) resultEl.textContent = '執行中...';

  try {
    const res = await api(`/api/dev/guilds/${encodeURIComponent(S.guild)}/commit`, {
      method: 'POST',
    });
    const line = `✅ ${res.message}（頻道 ${res.channels}、身分組 ${res.roles}、成員 ${res.members}、總快照 ${res.snapshots_total}）`;
    if (resultEl) resultEl.textContent = line;
    toast('Commit 完成', 'success');
  } catch (e) {
    if (resultEl) resultEl.textContent = `❌ 執行失敗：${e.message}`;
    toast(e.message, 'error');
  }

  if (btn) btn.disabled = false;
}

function _devRenderR2Quota(el, data) {
  const resultEl = el.querySelector('#devR2QuotaResult');
  const guild = data?.guild || null;
  const defaultQuota = Number(data?.default_quota_mb || 100);
  const guildQuota = Number(guild?.quota_mb || defaultQuota);
  const defaultInput = el.querySelector('#devR2DefaultQuota');
  const guildInput = el.querySelector('#devR2GuildQuota');
  if (defaultInput) defaultInput.value = String(defaultQuota);
  if (guildInput) guildInput.value = String(guildQuota);
  if (resultEl) {
    if (!S.guild) {
      resultEl.textContent = `預設 ${defaultQuota.toFixed(0)} MB，目前未選取伺服器。`;
      return;
    }
    const mode = guild?.is_override ? '目前伺服器使用覆寫配額' : '目前伺服器沿用預設配額';
    resultEl.textContent = `${mode}：${guildQuota.toFixed(0)} MB（預設 ${defaultQuota.toFixed(0)} MB）`;
  }
}

async function _devRefreshR2Quota(el) {
  const query = S.guild ? `?gid=${encodeURIComponent(S.guild)}` : '';
  const data = await api(`/api/dev/r2-quota${query}`);
  _devRenderR2Quota(el, data);
  return data;
}

async function _devSetDefaultQuota(el) {
  const input = el.querySelector('#devR2DefaultQuota');
  const quota = Number(input?.value || 0);
  if (!Number.isFinite(quota) || quota <= 0) {
    toast('請輸入有效的預設配額', 'error');
    input?.focus();
    return;
  }
  try {
    const res = await api('/api/dev/r2-quota', {
      method: 'PUT',
      body: JSON.stringify({ scope: 'default', quota_mb: quota }),
    });
    await _devRefreshR2Quota(el);
    toast(res.message || '預設配額已更新', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function _devSetGuildQuota(el) {
  if (!S.guild) {
    toast('請先選取伺服器', 'error');
    return;
  }
  const input = el.querySelector('#devR2GuildQuota');
  const quota = Number(input?.value || 0);
  if (!Number.isFinite(quota) || quota <= 0) {
    toast('請輸入有效的伺服器配額', 'error');
    input?.focus();
    return;
  }
  try {
    const res = await api('/api/dev/r2-quota', {
      method: 'PUT',
      body: JSON.stringify({ scope: 'guild', guild_id: S.guild, quota_mb: quota }),
    });
    await _devRefreshR2Quota(el);
    toast(res.message || '伺服器配額已更新', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function _devClearGuildQuota(el) {
  if (!S.guild) {
    toast('請先選取伺服器', 'error');
    return;
  }
  try {
    const res = await api('/api/dev/r2-quota', {
      method: 'PUT',
      body: JSON.stringify({ scope: 'clear', guild_id: S.guild }),
    });
    await _devRefreshR2Quota(el);
    toast(res.message || '伺服器配額已恢復預設', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

let _devRlTick = 0;
function _devStartPoller(el) {
  const tick = async () => {
    if (!document.getElementById('devNetCanvas')) {
      clearInterval(_devPoller); _devPoller = null; return;
    }
    _devRlTick++;
    try {
      const d = await api('/api/dev/system-stats');
      if (d.success) _devUpdateDOM(d);
    } catch (_) { /* silently ignore */ }
    // Rate-limit stats: update every 5 ticks (~15s)
    if (_devRlTick % 5 === 0) {
      try {
        const r = await api('/api/dev/ratelimit-stats?minutes=5');
        if (r.success) _devRenderRl(r.stats || {});
      } catch (_) { /* silently ignore */ }
    }
  };
  tick();
  _devPoller = setInterval(tick, 3000);
}

// ── Init ─────────────────────────────────────────────────
async function init() {
  try {
    const data = await api('/api/me');
    S.user = data;
    if (PREVIEW.enabled) {
      S.page = PREVIEW.page;
    }
    const selectableGuilds = getSelectableGuilds(data);
    if (selectableGuilds.length > 0) {
      S.guild = selectableGuilds[0].id;
    }
    if (!S.guild) {
      document.getElementById('app').innerHTML = `
        <div class="login-wrap">
          <div class="login-card">
            <div class="moon">🌙</div>
            <h1>SleepBot</h1>
            <p class="tagline">目前找不到你可管理的伺服器。<br>請先在 Discord 上完成核准流程。</p>
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
document.addEventListener('DOMContentLoaded', warnConsolePasteScam);
