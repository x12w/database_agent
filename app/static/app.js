/* ============================================================
   MySQL Intelligent Query Assistant — Frontend Logic v3
   ============================================================ */
console.log('[DB Agent] v3 loaded');

// ---- Global bridge: called by HTML onclick handlers ----
window._dbReady = false;
window.onDbChange = async function(val) {
  if (!val || !window._api) return;
  window._api.currentDbId = val;
  window._api.newChat();
  // Update status and placeholder directly (no IIFE dependency)
  try {
    const resp = await fetch('/api/databases/' + val + '/status');
    const data = await resp.json();
    // Status
    const si = document.getElementById('status-indicator');
    const sl = document.getElementById('status-label');
    if (si && sl) {
      si.className = 'status-dot';
      sl.textContent = '就绪 · ' + (data.table_count || '?') + ' 表 · ' + (data.name || '');
    }
    // Placeholder
    const input = document.getElementById('message-input');
    if (input && data.name) {
      input.placeholder = '向 ' + data.name + ' 提问，例如：' + ((data.suggestions||[])[0]||'查询数据');
    }
    // Suggestions
    if (data.suggestions && data.suggestions.length > 0 && window._api._setSuggestions) {
      window._api._setSuggestions(data.suggestions);
    }
  } catch(e) { console.error('onDbChange error:', e); }
};
window.onManageDb = function() { if (window._api) window._api.showDbManager(); else alert('JS 尚未初始化，请刷新页面'); };

(function() {
'use strict';

// ---- State ----
let currentEventSource = null;
let sessionId = generateId();
let currentDbId = null;
let thinkingPanelVisible = true;
let currentAgentMessage = null;
let currentThinkingEntries = [];

// ---- DOM refs ----
let chatMessages, chatForm, messageInput, btnSend;
let thinkingContent, thinkingPanel;
let charCount, statusIndicator, statusLabel, modeBadge;
let btnTogglePanel, btnNewChat, btnRefreshSchema;

// ---- Initialization ----
document.addEventListener('DOMContentLoaded', () => {
  console.log('[DB Agent] DOM ready');
  chatMessages = document.getElementById('chat-messages');
  chatForm = document.getElementById('chat-form');
  messageInput = document.getElementById('message-input');
  btnSend = document.getElementById('btn-send');
  thinkingContent = document.getElementById('thinking-content');
  thinkingPanel = document.getElementById('thinking-panel');
  charCount = document.getElementById('char-count');
  statusIndicator = document.getElementById('status-indicator');
  statusLabel = document.getElementById('status-label');
  modeBadge = document.getElementById('mode-badge');
  btnTogglePanel = document.getElementById('btn-toggle-panel');
  btnNewChat = document.getElementById('btn-new-chat');
  btnRefreshSchema = document.getElementById('btn-refresh-schema');

  // Visual indicator that JS loaded
  const sel = document.getElementById('db-selector');
  if (sel) {
    sel.style.border = '2px solid #4a9eff';
    sel.title = 'JS v2 loaded ✓';
  }

  checkHealth();
  loadDatabases();
  setupEventListeners();
  messageInput.focus();
});

function setupEventListeners() {
  chatForm.addEventListener('submit', handleSubmit);
  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(e); }
  });
  messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
    charCount.textContent = `${messageInput.value.length} / 4000`;
  });
  btnTogglePanel.addEventListener('click', toggleThinkingPanel);
  btnNewChat.addEventListener('click', newChat);
  btnRefreshSchema.addEventListener('click', refreshSchema);
  document.querySelectorAll('.example-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      messageInput.value = chip.dataset.query;
      handleSubmit(new Event('submit'));
    });
  });

  // DB selector
  const dbSel = document.getElementById('db-selector');
  if (dbSel) dbSel.addEventListener('change', (e) => {
    if (e.target.value) { currentDbId = e.target.value; newChat(); }
  });

  // DB manage button uses onclick in HTML — no duplicate listener here

  // Click status label to force schema refresh
  statusLabel.addEventListener('click', () => {
    if (currentDbId) {
      statusLabel.textContent = '刷新中...';
      updateDbStatus(currentDbId, true);
    }
  });
  statusLabel.style.cursor = 'pointer';
}

// ---- Health & Status ----
async function checkHealth() {
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    modeBadge.textContent = data.llm_available ? 'LLM模式' : '关键词模式';
    modeBadge.className = 'mode-badge ' + (data.llm_available ? 'llm' : 'limited');
    // Status label updated by updateDbStatus when database is selected
  } catch (e) {
    statusIndicator.className = 'status-dot error';
    statusLabel.textContent = '服务未启动';
  }
}

async function updateDbStatus(dbId, forceRefresh) {
  if (!dbId) return;
  try {
    let url = `/api/databases/${dbId}/status`;
    if (forceRefresh) url += '?refresh=1';
    const resp = await fetch(url);
    const data = await resp.json();
    statusIndicator.className = 'status-dot';
    statusLabel.textContent = `就绪 · ${data.table_count || '?'} 表 · ${data.name || ''}`;
    statusLabel.title = '点击刷新数据库缓存';
    // Update input placeholder
    const input = document.getElementById('message-input');
    if (input && data.name) {
      input.placeholder = `向 ${data.name} 提问，例如：${(data.suggestions||['查询数据'])[0]}`;
    }
    // Update welcome suggestions
    if (data.suggestions && data.suggestions.length > 0) {
      updateSuggestions(data.suggestions);
    }
  } catch (e) {
    statusIndicator.className = 'status-dot warning';
    statusLabel.textContent = '数据库未连接';
  }
}

function updateSuggestions(suggestions) {
  if (!suggestions || suggestions.length === 0) return;
  // Cache for newChat()
  _lastSuggestions = suggestions;
  // Update visible chips
  const chips = document.querySelectorAll('.example-chip');
  chips.forEach((chip, i) => {
    if (suggestions[i]) {
      chip.textContent = suggestions[i];
      chip.dataset.query = suggestions[i];
    }
  });
}

// ---- Database Management ----
async function loadDatabases() {
  const selector = document.getElementById('db-selector');
  if (!selector) return console.error('[DB Agent] #db-selector not found');
  console.log('[DB Agent] Loading databases...');
  try {
    const resp = await fetch('/api/databases');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const profiles = data.profiles || [];
    console.log('[DB Agent] Loaded', profiles.length, 'profiles');
    selector.innerHTML = '';
    if (profiles.length === 0) {
      selector.innerHTML = '<option value="">无数据库</option>';
      return;
    }
    profiles.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.database})`;
      if (p.connected) { opt.selected = true; currentDbId = p.id; }
      selector.appendChild(opt);
    });
    // Select the current or first database
    const currentExists = profiles.some(p => p.id === currentDbId);
    if (!currentExists) {
      currentDbId = profiles[0]?.id || null;
    }
    if (currentDbId) {
      selector.value = currentDbId;
    }
    console.log('[DB Agent] Current DB:', currentDbId);
    // Update status for current DB
    if (currentDbId) updateDbStatus(currentDbId);
  } catch (e) {
    console.error('[DB Agent] loadDatabases failed:', e);
    selector.innerHTML = '<option value="">加载失败</option>';
  }
}

async function showDbManager() {
  let html = '<div class="db-manager-overlay" id="db-manager-overlay">';
  html += '<div class="db-manager-dialog"><h3>数据库管理</h3>';

  try {
    const resp = await fetch('/api/databases');
    const data = await resp.json();
    html += '<div class="db-manager-list">';
    (data.profiles || []).forEach(p => {
      html += `<div class="db-manager-item"><span><strong>${esc(p.name)}</strong> — ${esc(p.database)}@${esc(p.host)}:${p.port} <em>${p.db_type||'mysql'}</em></span>`;
      html += `<div class="db-manager-item-actions">`;
      html += `<button class="btn-sm btn-edit" data-edit="${p.id}">编辑</button>`;
      html += `<button class="btn-sm btn-danger" data-del="${p.id}">删除</button>`;
      html += `</div></div>`;
    });
    html += '</div>';
  } catch (e) { html += '<p class="error">无法加载</p>'; }

  html += '<h4 id="db-form-title">添加新数据库</h4>';
  html += '<form id="db-add-form" class="db-add-form" autocomplete="on">';
  html += '<input type="hidden" name="editId" value="">';
  html += '<input name="name" placeholder="名称（如：电商数据库）" required autocomplete="off">';
  html += '<select name="db_type" class="db-type-select"><option value="mysql">MySQL</option><option value="postgresql">PostgreSQL</option><option value="dameng">Dameng(达梦)</option><option value="mariadb">MariaDB</option><option value="sqlite">SQLite</option></select>';
  html += '<input name="host" placeholder="主机" value="127.0.0.1" autocomplete="off">';
  html += '<input name="port" placeholder="端口" value="3306" type="number" autocomplete="off">';
  html += '<input name="user" placeholder="用户名" value="root" autocomplete="off">';
  html += '<input name="password" placeholder="密码" type="password" autocomplete="off">';
  html += '<input name="database" placeholder="数据库名" required autocomplete="off">';
  html += '<input name="description" placeholder="备注（可选）" autocomplete="off">';
  html += '<div class="db-manager-actions">';
  html += '<button type="submit" id="db-submit-btn">添加并测试</button>';
  html += '<button type="button" class="btn-close" id="btn-close-mgr">关闭</button>';
  html += '</div></form><div id="db-add-msg"></div></div></div>';

  const exist = document.getElementById('db-manager-overlay');
  if (exist) exist.remove();
  document.body.insertAdjacentHTML('beforeend', html);

  // Delete buttons
  document.querySelectorAll('.btn-danger').forEach(b => {
    b.addEventListener('click', async () => {
      if (!confirm('确定删除？')) return;
      await fetch(`/api/databases/${b.dataset.del}`, {method:'DELETE'});
      closeDbManager(); loadDatabases();
    });
  });

  // Edit buttons — populate form with existing data
  document.querySelectorAll('.btn-edit').forEach(b => {
    b.addEventListener('click', async () => {
      const resp = await fetch('/api/databases');
      const data = await resp.json();
      const p = (data.profiles||[]).find(x => x.id === b.dataset.edit);
      if (!p) return;
      const f = document.getElementById('db-add-form');
      f.editId.value = p.id;
      f.name.value = p.name;
      f.db_type.value = p.db_type || 'mysql';
      f.host.value = p.host;
      f.port.value = p.port;
      f.user.value = p.user;
      f.password.value = p.password;
      f.database.value = p.database;
      f.description.value = p.description || '';
      document.getElementById('db-form-title').textContent = '编辑数据库';
      document.getElementById('db-submit-btn').textContent = '更新并测试';
      document.getElementById('db-add-msg').textContent = '';
      document.getElementById('db-add-msg').className = '';
    });
  });

  // Close
  document.getElementById('btn-close-mgr').addEventListener('click', closeDbManager);
  // Overlay: only close if BOTH mousedown and mouseup are on overlay background
  let _overlayMousedownTarget = null;
  document.getElementById('db-manager-overlay').addEventListener('mousedown', (e) => {
    _overlayMousedownTarget = e.target;
  });
  document.getElementById('db-manager-overlay').addEventListener('mouseup', (e) => {
    if (e.target.id === 'db-manager-overlay' && _overlayMousedownTarget && _overlayMousedownTarget.id === 'db-manager-overlay') {
      closeDbManager();
    }
  });

  // Form submit
  document.getElementById('db-add-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target; const msg = document.getElementById('db-add-msg');
    const editId = f.editId.value;
    msg.textContent = '测试连接中...'; msg.className = '';

    const payload = {
      name: f.name.value,
      host: f.host.value,
      port: parseInt(f.port.value) || 3306,
      user: f.user.value,
      password: f.password.value,
      database: f.database.value,
      db_type: f.db_type.value,
      description: f.description.value,
    };

    try {
      const method = editId ? 'PUT' : 'POST';
      const url = editId ? `/api/databases/${editId}` : '/api/databases';
      const resp = await fetch(url, {
        method, headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)});
      const r = await resp.json();
      if (resp.ok) {
        msg.textContent = '✓ ' + (r.message || '保存成功');
        msg.className = 'success';
        // Auto-select the new/updated database
        if (r.profile && r.profile.id) {
          currentDbId = r.profile.id;
          const sel = document.getElementById('db-selector');
          if (sel) sel.value = currentDbId;
        }
        setTimeout(() => { closeDbManager(); loadDatabases(); }, 1000);
      } else {
        msg.textContent = '✗ ' + (r.detail || '失败');
        msg.className = 'error';
      }
    } catch(err) { msg.textContent = '✗ ' + err.message; msg.className = 'error'; }
  });
}

window.closeDbManager = function() {
  const o = document.getElementById('db-manager-overlay');
  if (o) o.remove();
};

// ---- Table Filter ----

window.openTableFilter = function() {
  const dbSel = document.getElementById('db-selector');
  if (!dbSel || !dbSel.value) { alert('请先选择数据库'); return; }
  const dbId = dbSel.value;

  // Toggle: close if already open
  const exist = document.getElementById('table-filter-overlay');
  if (exist) { exist.remove(); return; }

  // Show skeleton immediately with loading state
  const skeleton = `<div class="overlay" id="table-filter-overlay" onclick="if(event.target===this)closeTableFilter()">
    <div class="overlay-panel" style="max-width:900px;">
      <div class="overlay-header">
        <h3>筛选表（仅在选中表中查询）</h3>
        <button class="btn-ghost" onclick="closeTableFilter()">✕</button>
      </div>
      <div class="tf-loading" style="padding:40px;text-align:center;color:var(--text-muted);">
        <div class="spinner"></div>
        <p style="margin-top:12px;">加载中...</p>
      </div>
    </div>
  </div>`;
  document.body.insertAdjacentHTML('beforeend', skeleton);

  // Fetch data in background
  (async () => {
    // Fetch tables and current filter in parallel
    const [tablesResp, filterResp] = await Promise.allSettled([
      fetch(`/api/tables?db_id=${encodeURIComponent(dbId)}`).then(r => r.json()),
      fetch(`/api/session/${encodeURIComponent(sessionId)}/table-filter`).then(r => r.json()).catch(() => ({}))
    ]);

    const tables = tablesResp.status === 'fulfilled' ? (tablesResp.value.tables || []) : [];
    const filterData = filterResp.status === 'fulfilled' ? filterResp.value : {};
    let currentFilter = filterData.table_filter;  // null = all, [...] = filtered

    // Fallback to localStorage
    if (!currentFilter) {
      const saved = localStorage.getItem('tf_' + dbId);
      if (saved) { try { currentFilter = JSON.parse(saved); } catch(_) {} }
    }

    if (!tables.length) {
      document.getElementById('table-filter-overlay').querySelector('.tf-loading').innerHTML = '<p style="padding:40px;text-align:center;color:var(--text-muted);">数据库中没有表或加载失败</p>';
      return;
    }

    const isAllSelected = (currentFilter === null || currentFilter === undefined);
    const filterSet = isAllSelected ? new Set() : new Set(currentFilter);
    const countN = isAllSelected ? tables.length : currentFilter.length;

    // Replace loading with actual content
    const panel = document.querySelector('#table-filter-overlay .overlay-panel');
    const loadingEl = panel.querySelector('.tf-loading');
    const content = document.createElement('div');
    content.innerHTML = `
      <div style="padding:12px 20px;border-bottom:1px solid var(--border);display:flex;gap:12px;">
        <button class="btn-sm" onclick="selectAllTables()">全选</button>
        <button class="btn-sm" onclick="clearAllTables()">全不选</button>
        <button class="btn-sm" onclick="deselectEmptyTables()">去除空表</button>
        <span style="color:var(--text-muted);font-size:0.8rem;align-self:center;">已选 <span id="tf-count">${countN}</span>/${tables.length} 张</span>
      </div>
      <div class="table-filter-grid" style="max-height:65vh;overflow-y:auto;padding:12px 20px;">
        ${tables.map(t => {
          const checked = isAllSelected || filterSet.has(t.name);
          return `<label class="tf-card">
            <input type="checkbox" value="${esc(t.name)}" ${checked ? 'checked' : ''} onchange="updateTfCount()">
            <span class="tf-name">${esc(t.name)}</span>
            <span class="tf-meta">${t.row_count}行 · ${t.column_count}列</span>
          </label>`;
        }).join('')}
      </div>
      <div class="overlay-footer">
        <button class="btn btn-primary" onclick="saveTableFilter()" style="width:100%;">保存筛选并重建缓存</button>
      </div>`;
    loadingEl.replaceWith(...content.children);
  })();
};

window.selectAllTables = function() {
  document.querySelectorAll('#table-filter-overlay input[type=checkbox]').forEach(cb => { cb.checked = true; });
  updateTfCount();
};

window.clearAllTables = function() {
  document.querySelectorAll('#table-filter-overlay input[type=checkbox]').forEach(cb => { cb.checked = false; });
  updateTfCount();
};

window.deselectEmptyTables = function() {
  document.querySelectorAll('#table-filter-overlay .tf-card').forEach(card => {
    const meta = card.querySelector('.tf-meta');
    if (meta && meta.textContent.startsWith('0行')) {
      card.querySelector('input[type=checkbox]').checked = false;
    }
  });
  updateTfCount();
};

window.updateTfCount = function() {
  const cbs = document.querySelectorAll('#table-filter-overlay input[type=checkbox]');
  const cnt = document.querySelectorAll('#table-filter-overlay input[type=checkbox]:checked').length;
  const el = document.getElementById('tf-count');
  if (el) el.textContent = cnt;
};

window.saveTableFilter = async function() {
  const checked = Array.from(document.querySelectorAll('#table-filter-overlay input[type=checkbox]:checked'))
    .map(cb => cb.value);
  const allCbs = document.querySelectorAll('#table-filter-overlay input[type=checkbox]');
  const tables = checked.length === allCbs.length ? null : checked;
  const dbId = document.getElementById('db-selector')?.value || '';

  // Close modal immediately
  closeTableFilter();

  // Show rebuild progress
  const sl = document.getElementById('status-label');
  const si = document.getElementById('status-indicator');
  if (sl) sl.textContent = '保存筛选...';
  if (si) si.className = 'status-dot warning';

  // Step 1: Save filter to session
  try {
    const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}/table-filter`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tables: tables}),
    });
    const data = await resp.json();
    if (!data.success) throw new Error(data.message || '保存失败');
  } catch(e) {
    alert('保存筛选失败: ' + (e.message || '网络错误'));
    if (sl) sl.textContent = '保存失败';
    if (si) si.className = 'status-dot error';
    return;
  }

  // Step 2: Persist to localStorage
  if (tables) {
    localStorage.setItem('tf_' + dbId, JSON.stringify(tables));
  } else {
    localStorage.removeItem('tf_' + dbId);
  }

  // Step 3: Explicit cache rebuild with progress
  if (sl) sl.textContent = (tables ? `筛选 ${tables.length} 张表 · 重建缓存中...` : '全部表 · 重建缓存中...');

  try {
    let rebuildUrl = `/api/databases/${encodeURIComponent(dbId)}/rebuild-cache`;
    if (sessionId) rebuildUrl += `?session_id=${encodeURIComponent(sessionId)}`;
    const resp = await fetch(rebuildUrl, {method:'POST'});
    const data = await resp.json();
    if (data.success) {
      if (sl) sl.textContent = '就绪 · ' + data.message;
      if (si) { si.className = 'status-dot'; setTimeout(() => { si.className = 'status-dot'; }, 2000); }
    } else {
      throw new Error(data.detail || data.message || '重建失败');
    }
  } catch(e) {
    if (sl) sl.textContent = '筛选已保存，缓存重建失败: ' + (e.message || '');
    if (si) si.className = 'status-dot error';
  }
};

window.closeTableFilter = function() {
  const o = document.getElementById('table-filter-overlay');
  if (o) o.remove();
};

window.refreshConnection = async function() {
  const dbSel = document.getElementById('db-selector');
  if (!dbSel || !dbSel.value) return;
  const dbId = dbSel.value;
  const sl = document.getElementById('status-label');
  if (sl) sl.textContent = '刷新中...';
  try {
    const resp = await fetch(`/api/databases/${encodeURIComponent(dbId)}/refresh`, {method:'POST'});
    const data = await resp.json();
    if (sl) sl.textContent = '就绪 · ' + (data.table_count || '?') + ' 表 · ' + (data.name || '');
    setTimeout(() => { if (sl) sl.textContent = '就绪 · ' + (data.table_count || '?') + ' 表 · ' + (data.name || ''); }, 2000);
  } catch(e) { if (sl) sl.textContent = '刷新失败'; }
};

// ---- Message Handling ----
function handleSubmit(e) {
  e.preventDefault();
  const message = messageInput.value.trim();
  if (!message || currentEventSource) return;
  const wm = document.querySelector('.welcome-message');
  if (wm) wm.remove();
  addMessage('user', message);
  messageInput.value = ''; messageInput.style.height = 'auto';
  charCount.textContent = '0 / 4000';
  currentAgentMessage = createAgentMessage();
  currentThinkingEntries = [];
  thinkingContent.innerHTML = '';
  btnSend.disabled = true; btnSend.classList.add('loading');
  sendMessage(message);
}

function sendMessage(message) {
  let url = `/chat/stream?message=${encodeURIComponent(message)}&session_id=${sessionId}`;
  if (currentDbId) url += `&db_id=${encodeURIComponent(currentDbId)}`;
  // Send table filter from localStorage so it survives page reloads
  const saved = localStorage.getItem('tf_' + (currentDbId || ''));
  if (saved) {
    try {
      const tf = JSON.parse(saved);
      if (Array.isArray(tf) && tf.length > 0) url += `&table_filter=${encodeURIComponent(tf.join(','))}`;
    } catch(_) {}
  }
  currentEventSource = new EventSource(url);

  // Timeout: close connection if no events for 180 seconds
  const STREAM_TIMEOUT_MS = 300000;  // 5 min for multi-round queries
  let timeoutId = setTimeout(() => {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
      appendToAgentMessage('\n\n> ⚠️ 请求超时，请重试');
      finishAgentMessage(null);
      closeStream();
    }
  }, STREAM_TIMEOUT_MS);

  // Reset timeout on each event
  const resetTimeout = () => { clearTimeout(timeoutId); timeoutId = setTimeout(() => {
    if (currentEventSource) { currentEventSource.close(); currentEventSource = null; appendToAgentMessage('\n\n> ⚠️ 请求超时，请重试'); finishAgentMessage(null); closeStream(); }
  }, STREAM_TIMEOUT_MS); };

  const ev = (type, fn) => currentEventSource.addEventListener(type, (e) => { resetTimeout(); fn(e); });

  ev('status', (e) => addThinkingEntry('status', '⚡', JSON.parse(e.data).message));
  ev('thinking', (e) => addThinkingEntry('thinking', '💭', JSON.parse(e.data).text));
  ev('chart', (e) => {
    const d = JSON.parse(e.data);
    if (d.images && d.images.length > 0) {
      addThinkingEntry('chart', '📈', `生成 ${d.images.length} 张图表`);
      d.images.forEach(url => appendToAgentMessage(renderChartImage(url)));
    }
  });
  ev('tool_call', (e) => {
    const d = JSON.parse(e.data);
    let detail = d.input?.sql || (d.input?.query ? `搜索: "${d.input.query}"` : JSON.stringify(d.input));
    addThinkingEntry('tool_call', '🔧', d.tool, detail);
    // Collect SQL queries for later card display (not mixed into NL text)
    if (d.input?.sql) {
      // Render SQL card immediately in chat area with running status
      const sqlCardId = 'sql-' + Date.now();
      pendingSqlQueries.push({ sql: d.input.sql, result: null, success: null, cardId: sqlCardId });
      appendToAgentMessage(renderSqlLiveCard(sqlCardId, d.input.sql, null));
    }
    // Non-SQL tool calls also shown as cards
    if (!d.input?.sql && d.tool !== 'query_database') {
      const detail = d.input?.query ? `搜索: "${d.input.query}"` : (d.input?.table_name || JSON.stringify(d.input||{}));
      appendToAgentMessage(renderProgressCard('🔧', d.tool, 'running', detail.substring(0,120)));
    }
  });
  ev('tool_result', (e) => {
    const d = JSON.parse(e.data);
    const preview = d.raw_result ? `${d.raw_result.row_count}行·${d.raw_result.execution_ms}ms` : (d.output||'').substring(0,120);
    // Update the last pending SQL card with result
    if (pendingSqlQueries.length > 0) {
      const last = pendingSqlQueries[pendingSqlQueries.length - 1];
      if (last.result === null) {
        last.result = preview;
        last.success = d.success;
        // Update the live card
        const card = document.getElementById(last.cardId);
        if (card) {
          card.className = 'sql-live-card ' + (d.success ? 'sql-live-done' : 'sql-live-fail');
          const resultEl = card.querySelector('.sql-live-result');
          if (resultEl) resultEl.textContent = preview;
          const statusEl = card.querySelector('.sql-live-status');
          if (statusEl) statusEl.textContent = d.success ? '✅' : '❌';
        }
      }
    }
  });
  ev('progress', (e) => {
    const d = JSON.parse(e.data);
    appendToAgentMessage(renderProgressCard(d.phase, d.title, d.status, d.detail));
  });
  ev('text', (e) => appendToAgentMessage(JSON.parse(e.data).text));
  ev('done', (e) => { clearTimeout(timeoutId); finishAgentMessage(JSON.parse(e.data)); closeStream(); });
  ev('admin_question', (e) => {
    try {
      const d = JSON.parse(e.data);
      addThinkingEntry('admin_question', '❓', `管理员提问: ${d.table_name}${d.column_name ? '.' + d.column_name : ''} — ${d.question}`);
      showAdminNotification(d);
    } catch (_) {}
  });
  ev('error', (e) => {
    clearTimeout(timeoutId); let msg = '连接中断';
    try { if (e.data) msg = JSON.parse(e.data).message || msg; } catch(_){}
    addThinkingEntry('error','✗',msg); appendToAgentMessage(`\n\n> ⚠️ ${msg}`);
    finishAgentMessage(null); closeStream();
  });
  currentEventSource.onerror = () => { clearTimeout(timeoutId); closeStream(); };
}

function closeStream() {
  if (currentEventSource) { currentEventSource.close(); currentEventSource = null; }
  btnSend.disabled = false; btnSend.classList.remove('loading');
  currentAgentMessage = null; messageInput.focus();
}

// ---- Rendering ----
function addMessage(role, content) {
  const div = document.createElement('div'); div.className = `message ${role}`;
  const avatar = document.createElement('div'); avatar.className = 'message-avatar';
  avatar.textContent = role === 'user' ? '你' : 'AI';
  const body = document.createElement('div'); body.className = 'message-body';
  const header = document.createElement('div'); header.className = 'message-header';
  const rs = document.createElement('span'); rs.className = 'message-role';
  rs.textContent = role === 'user' ? '你' : 'Assistant';
  const ts = document.createElement('span'); ts.className = 'message-time';
  ts.textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  header.appendChild(rs); header.appendChild(ts);
  const cd = document.createElement('div'); cd.className = 'message-content';
  cd.innerHTML = role === 'user' ? esc(content) : renderMarkdown(content);
  body.appendChild(header); body.appendChild(cd);
  div.appendChild(avatar); div.appendChild(body);
  chatMessages.appendChild(div); scrollToBottom();
  return div;
}

function createAgentMessage() {
  pendingSqlQueries = [];  // Reset for new message
  const div = document.createElement('div'); div.className = 'message agent';
  div.innerHTML = `<div class="message-avatar">AI</div><div class="message-body"><div class="message-header"><span class="message-role">Assistant</span><span class="message-time">${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</span></div><div class="message-content typing-cursor"></div></div>`;
  chatMessages.appendChild(div); scrollToBottom();
  return div;
}

function appendToAgentMessage(text) {
  if (!currentAgentMessage) return;
  const cd = currentAgentMessage.querySelector('.message-content');
  cd.classList.remove('typing-cursor');
  cd.innerHTML += text;
  cd.classList.add('typing-cursor');
  scrollToBottom();
}

function finishAgentMessage(data) {
  if (!currentAgentMessage) return;
  const cd = currentAgentMessage.querySelector('.message-content');
  cd.classList.remove('typing-cursor');

  // Save all existing process cards (progress, SQL, charts) before replacing content
  const existingChildren = Array.from(cd.children);

  // Render the final NL answer
  const finalText = (data && data.message) ? data.message : '';
  const cleanHtml = renderMarkdown(finalText).replace(
    /<div class="sql-block">[\s\S]*?<\/div>\s*/g, ''
  );

  // Build final structure: collapsible process + NL answer
  cd.innerHTML = '';

  // Collapsible process section
  if (existingChildren.length > 0 || (data && data.images && data.images.length > 0)) {
    const wrapper = document.createElement('div');
    wrapper.className = 'process-section';
    wrapper.innerHTML = '<div class="process-toggle" onclick="this.parentElement.classList.toggle(\'process-collapsed\')">📋 思考过程 <span class="process-arrow">▼</span></div>';
    const body = document.createElement('div');
    body.className = 'process-body';
    existingChildren.forEach(el => body.appendChild(el));
    // Chart images
    if (data && data.images && data.images.length > 0) {
      data.images.forEach(url => { body.innerHTML += renderChartImage(url); });
    }
    wrapper.appendChild(body);
    cd.appendChild(wrapper);
  }

  // Final answer
  const answer = document.createElement('div');
  answer.className = 'final-answer';
  answer.innerHTML = cleanHtml;
  cd.appendChild(answer);

  // Source badge
  if (data) {
    const badge = document.createElement('div');
    badge.className = 'source-badge ' + (data.db_queries > 0 ? 'db' : 'doc');
    badge.textContent = data.db_queries > 0
      ? `📊 数据库查询 · ${data.db_queries}次 · ${data.response_time_ms}ms`
      : `📄 文档检索 · ${data.response_time_ms}ms`;
    cd.appendChild(badge);
  }
}

function renderChartImage(url) {
  return `<div class="chart-container"><img src="${esc(url)}" alt="图表" class="chat-chart" loading="lazy" /></div>`;
}

function renderSqlLiveCard(id, sql, result) {
  return `<div class="sql-live-card sql-live-running" id="${id}">
    <div class="sql-live-header" onclick="this.parentElement.classList.toggle('sql-live-collapsed')">
      <span class="sql-live-status">⏳</span>
      <span class="sql-live-title">SQL 查询</span>
      <span class="sql-live-toggle">▼</span>
    </div>
    <div class="sql-live-body">
      <pre class="sql-live-code">${esc(sql)}</pre>
      <div class="sql-live-result">${result || '执行中...'}</div>
    </div>
  </div>`;
}

function renderProgressCard(phase, title, status, detail) {
  const statusClass = status === 'running' ? 'progress-running' : status === 'done' ? 'progress-done' : 'progress-fail';
  const pulse = status === 'running' ? ' progress-pulse' : '';
  return `<div class="progress-card ${statusClass}">
    <div class="progress-icon${pulse}">${esc(phase)}</div>
    <div class="progress-body">
      <div class="progress-title">${esc(title)}</div>
      ${detail ? `<div class="progress-detail">${esc(detail)}</div>` : ''}
    </div>
  </div>`;
}

// ---- SQL Query Cards (collapsible, separate from NL text) ----

let pendingSqlQueries = [];

function renderSqlCard(sql, result, success, index) {
  const statusIcon = success === true ? '✅' : success === false ? '❌' : '⏳';
  const resultText = result || '';
  return `
    <div class="sql-card" id="sql-card-${index}">
      <div class="sql-card-header" onclick="document.getElementById('sql-card-body-${index}').classList.toggle('collapsed'); this.querySelector('.sql-toggle').textContent = document.getElementById('sql-card-body-${index}').classList.contains('collapsed') ? '▶' : '▼';">
        <span class="sql-card-status">${statusIcon}</span>
        <span class="sql-card-title">SQL 查询 #${index + 1}</span>
        <span class="sql-card-preview">${esc(resultText).substring(0, 60)}</span>
        <span class="sql-toggle">▼</span>
      </div>
      <div class="sql-card-body" id="sql-card-body-${index}">
        <pre class="sql-card-code">${esc(sql)}</pre>
        ${resultText ? `<div class="sql-card-result">${esc(resultText)}</div>` : ''}
      </div>
    </div>`;
}

function renderAllSqlCards(container) {
  const wrapper = document.createElement('div');
  wrapper.className = 'sql-cards-wrapper';
  wrapper.innerHTML = pendingSqlQueries
    .map((q, i) => renderSqlCard(q.sql, q.result, q.success, i))
    .join('');
  container.appendChild(wrapper);
}

function renderMarkdown(text) {
  if (!text) return '';
  try {
    if (typeof marked !== 'undefined' && marked.parse) {
      return marked.parse(text)
        .replace(/```sql\n([\s\S]*?)```/g, (_,sql) => renderSqlBlockHtml(sql.trim()))
        .replace(/```(\w*)\n([\s\S]*?)```/g, (_,lang,code) => `<div class="sql-block"><div class="sql-block-header"><span class="sql-label">${lang||'code'}</span></div><pre>${esc(code.trim())}</pre></div>`);
    }
  } catch(_) {}
  return esc(text).replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');
}

function renderSqlBlock(sql) { return `<div class="sql-block"><div class="sql-block-header"><span class="sql-label">SQL</span></div><pre>${esc(sql)}</pre></div>`; }
function renderSqlBlockHtml(sql) { return `<div class="sql-block"><div class="sql-block-header"><span class="sql-label">SQL</span></div><pre>${esc(sql)}</pre></div>`; }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ---- Thinking Panel ----
function addThinkingEntry(type, prefix, text, detail) {
  const entry = document.createElement('div'); entry.className = `think-entry ${type}`;
  let h = `<span class="prefix">${prefix}</span> `;
  if (text) h += `<span class="text">${esc(text)}</span>`;
  if (detail) h += type==='tool_call' ? `<span class="sql-code">${esc(detail)}</span>` : `<span class="result-preview">${esc(detail)}</span>`;
  entry.innerHTML = h;
  const empty = thinkingContent.querySelector('.thinking-empty');
  if (empty) empty.remove();
  thinkingContent.appendChild(entry);
  thinkingContent.scrollTop = thinkingContent.scrollHeight;
}

function toggleThinkingPanel() {
  thinkingPanelVisible = !thinkingPanelVisible;
  thinkingPanel.classList.toggle('collapsed', !thinkingPanelVisible);
}

// ---- Admin Notification Banner ----

function showAdminNotification(d) {
  // Remove existing notification if any
  const existing = document.querySelector('.admin-notification');
  if (existing) existing.remove();

  const banner = document.createElement('div');
  banner.className = 'admin-notification';
  banner.innerHTML = `
    <span>❓ 新的管理员问题需要处理：<code>${escHtml(d.table_name)}${d.column_name ? '.' + escHtml(d.column_name) : ''}</code></span>
    <a href="http://localhost:8001" target="_blank" class="admin-notification-link">打开管理面板 →</a>
    <button class="admin-notification-close" title="关闭">&times;</button>
  `;
  banner.querySelector('.admin-notification-close').addEventListener('click', () => banner.remove());

  // Insert at top of chat messages
  const chatMsgs = document.getElementById('chat-messages');
  if (chatMsgs) {
    chatMsgs.insertBefore(banner, chatMsgs.firstChild);
  }

  // Auto-dismiss after 30 seconds
  setTimeout(() => { if (banner.parentNode) banner.remove(); }, 30000);
}

function escHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}


// ---- Actions ----
// Cache of last suggestions for current DB
let _lastSuggestions = ['介绍一下当前数据库架构', '数据库有哪些表？'];

function newChat() {
  sessionId = generateId();
  const chips = _lastSuggestions.map(s =>
    `<button class="example-chip" data-query="${esc(s)}">${esc(s)}</button>`
  ).join('');
  chatMessages.innerHTML = `<div class="welcome-message"><div class="welcome-icon"><svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1" width="48" height="48"><ellipse cx="24" cy="12" rx="16" ry="6"/><path d="M8 12v12c0 3 7 6 16 6s16-3 16-6V12"/><path d="M8 24v12c0 3 7 6 16 6s16-3 16-6V24"/></svg></div><h2>欢迎使用 MySQL 智能查询助手</h2><p>用自然语言提问，我将自动查询数据库并给出回答。</p><div class="welcome-examples"><span class="example-label">试试这些：</span>${chips}</div></div>`;
  document.querySelectorAll('.example-chip').forEach(chip => {
    chip.addEventListener('click', () => { messageInput.value = chip.dataset.query; handleSubmit(new Event('submit')); });
  });
  thinkingContent.innerHTML = '<div class="thinking-empty"><span class="muted">等待查询...</span></div>';
  messageInput.focus();
}

async function refreshSchema() {
  if (!currentDbId) {
    addThinkingEntry('error','✗','请先选择一个数据库');
    return;
  }
  const startTime = Date.now();
  addThinkingEntry('status','🔄','正在完全重建缓存（删除旧缓存 + 重新构建）...');
  statusLabel.textContent = '重建中...';
  statusIndicator.className = 'status-dot warning';
  btnRefreshSchema.disabled = true;

  try {
    let rebuildUrl = `/api/databases/${currentDbId}/rebuild-cache`;
    if (sessionId) rebuildUrl += `?session_id=${encodeURIComponent(sessionId)}`;
    const resp = await fetch(rebuildUrl, { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const d = await resp.json();
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    addThinkingEntry('tool_result','✓',
      `缓存重建完成 · ${d.table_count} 张表 · 耗时 ${elapsed}s`);
    statusLabel.textContent = `就绪 · ${d.table_count} 表 · ${d.name || ''}`;
    statusLabel.title = '点击强制刷新 | 缓存已重建';
    statusIndicator.className = 'status-dot';
    checkHealth();
  } catch(e) {
    addThinkingEntry('error','✗','缓存重建失败: ' + e.message);
    statusLabel.textContent = '重建失败';
    statusIndicator.className = 'status-dot error';
  } finally {
    btnRefreshSchema.disabled = false;
  }
}

// ---- Utils ----
function generateId() { return Math.random().toString(36).substring(2,10); }
function scrollToBottom() { chatMessages.scrollTop = chatMessages.scrollHeight; }

function closeDbManager() {
  const o = document.getElementById('db-manager-overlay');
  if (o) o.remove();
}

// ---- Expose internal functions via window._api ----
window._api = {
  get currentDbId() { return currentDbId; },
  set currentDbId(v) { currentDbId = v; },
  newChat: newChat,
  showDbManager: showDbManager,
  loadDatabases: loadDatabases,
  updateDbStatus: updateDbStatus,
  _setSuggestions: function(s) { _lastSuggestions = s; updateSuggestions(s); },
};
window._dbReady = true;
console.log('[DB Agent] API ready, _dbReady=true');

})(); // END IIFE
