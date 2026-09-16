/**
 * Admin Panel JavaScript — standalone SPA for port 8001.
 * Manages database profiles, clarification Q&A, and exploration.
 */
(function () {
  'use strict';

  // ---- State ----
  let currentProfileId = null;
  let currentTab = 'pending';
  let exploreEventSource = null;
  let allProfiles = [];

  // ---- DOM Refs ----
  const dbSelect = document.getElementById('admin-db-select');
  const dbInfo = document.getElementById('admin-db-info');
  const pendingBadge = document.getElementById('admin-pending-badge');
  const pendingList = document.getElementById('pending-list');
  const pendingEmpty = document.getElementById('pending-empty');
  const pendingLoading = document.getElementById('pending-loading');
  const answeredList = document.getElementById('answered-list');
  const answeredEmpty = document.getElementById('answered-empty');
  const answeredLoading = document.getElementById('answered-loading');
  const tabCountPending = document.getElementById('tab-count-pending');
  const tabCountAnswered = document.getElementById('tab-count-answered');
  const btnStartExplore = document.getElementById('btn-start-explore');
  const btnStopExplore = document.getElementById('btn-stop-explore');
  const exploreProgress = document.getElementById('explore-progress');
  const progressFill = document.getElementById('progress-fill');
  const progressText = document.getElementById('progress-text');
  const exploreQuestions = document.getElementById('explore-questions');
  const exploreSummary = document.getElementById('explore-summary');

  // ---- Init ----
  async function init() {
    await loadProfiles();
    setupTabs();
    setupExploreButtons();
  }

  // ---- Profile Loading ----

  async function loadProfiles() {
    try {
      const resp = await fetch('/api/admin/profiles');
      const data = await resp.json();
      allProfiles = data.profiles || [];

      dbSelect.innerHTML = '<option value="">选择数据库...</option>';
      for (const p of allProfiles) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${p.db_type}:${p.database})`;
        if (p.pending_count > 0) {
          opt.textContent += ` [${p.pending_count}待回答]`;
        }
        dbSelect.appendChild(opt);
      }

      // Auto-select first profile or restore from localStorage
      const savedPid = localStorage.getItem('admin_selected_profile');
      if (savedPid && allProfiles.find(p => p.id === savedPid)) {
        dbSelect.value = savedPid;
        await selectProfile(savedPid);
      } else if (allProfiles.length > 0) {
        dbSelect.value = allProfiles[0].id;
        await selectProfile(allProfiles[0].id);
      }
    } catch (err) {
      console.error('Failed to load profiles:', err);
    }
  }

  dbSelect.addEventListener('change', async () => {
    const pid = dbSelect.value;
    if (pid) {
      localStorage.setItem('admin_selected_profile', pid);
      await selectProfile(pid);
    } else {
      currentProfileId = null;
      clearAll();
    }
  });

  async function selectProfile(profileId) {
    currentProfileId = profileId;
    const profile = allProfiles.find(p => p.id === profileId);

    // Update info
    if (profile) {
      dbInfo.textContent = `${profile.host}:${profile.port} | ${profile.db_type} | ${profile.table_count} 张表`;
    }

    // Update pending badge
    const pendingCount = profile ? profile.pending_count : 0;
    if (pendingCount > 0) {
      pendingBadge.textContent = `${pendingCount} 待回答`;
      pendingBadge.style.display = '';
    } else {
      pendingBadge.style.display = 'none';
    }

    // Enable explore button
    btnStartExplore.disabled = false;

    // Load questions for current tab
    if (currentTab === 'pending') await loadPendingQuestions();
    else if (currentTab === 'answered') await loadAnsweredQuestions();
  }

  function clearAll() {
    pendingList.innerHTML = '';
    answeredList.innerHTML = '';
    pendingEmpty.style.display = 'none';
    answeredEmpty.style.display = 'none';
    pendingBadge.style.display = 'none';
    dbInfo.textContent = '';
    btnStartExplore.disabled = true;
    tabCountPending.textContent = '0';
    tabCountAnswered.textContent = '0';
  }

  // ---- Tabs ----

  function setupTabs() {
    document.querySelectorAll('.admin-tab').forEach(tab => {
      tab.addEventListener('click', async () => {
        document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.admin-tab-content').forEach(c => c.classList.remove('active'));

        currentTab = tab.dataset.tab;
        document.getElementById('tab-' + currentTab).classList.add('active');

        if (!currentProfileId) return;

        if (currentTab === 'pending') await loadPendingQuestions();
        else if (currentTab === 'answered') await loadAnsweredQuestions();
      });
    });
  }

  // ---- Questions API ----

  async function loadPendingQuestions() {
    if (!currentProfileId) return;
    pendingLoading.style.display = 'block';
    pendingList.innerHTML = '';
    pendingEmpty.style.display = 'none';

    try {
      const resp = await fetch(`/api/admin/questions?profile_id=${currentProfileId}&status=pending`);
      const data = await resp.json();
      pendingLoading.style.display = 'none';

      tabCountPending.textContent = data.questions.length;
      tabCountAnswered.textContent = data.total_count - data.questions.length;

      if (data.questions.length === 0) {
        pendingEmpty.style.display = '';
      } else {
        renderQuestionCards(data.questions, pendingList, true);
      }
    } catch (err) {
      pendingLoading.style.display = 'none';
      console.error('Failed to load questions:', err);
    }
  }

  async function loadAnsweredQuestions() {
    if (!currentProfileId) return;
    answeredLoading.style.display = 'block';
    answeredList.innerHTML = '';
    answeredEmpty.style.display = 'none';

    try {
      const resp = await fetch(`/api/admin/questions?profile_id=${currentProfileId}&status=answered`);
      const data = await resp.json();
      answeredLoading.style.display = 'none';
      tabCountAnswered.textContent = data.questions.length;

      if (data.questions.length === 0) {
        answeredEmpty.style.display = '';
      } else {
        renderQuestionCards(data.questions, answeredList, false);
      }
    } catch (err) {
      answeredLoading.style.display = 'none';
      console.error('Failed to load answered:', err);
    }
  }

  function renderQuestionCards(questions, container, isPending) {
    container.innerHTML = '';
    for (const q of questions) {
      const card = document.createElement('div');
      card.className = `admin-card ${q.status}`;
      card.dataset.qid = q.question_id;

      const colLabel = q.column_name
        ? `<code>${esc(q.table_name)}</code>.<code>${esc(q.column_name)}</code>`
        : `<code>${esc(q.table_name)}</code>`;

      let inner = `
        <div class="admin-card-header">
          <div class="admin-card-title">${colLabel}</div>
          <div class="admin-card-meta">${q.created_at}</div>
        </div>
        <div class="admin-card-question">${esc(q.question)}</div>
      `;

      // Sample values context
      if (q.sample_values && q.sample_values.length > 0) {
        inner += `<div class="admin-card-context">
          <div class="admin-context-label">样本值</div>
          <div class="admin-card-guess"><span class="guess-text">${esc(q.sample_values.slice(0, 10).join(', '))}</span></div>
        </div>`;
      }

      if (isPending) {
        inner += `
          <textarea class="admin-card-input" placeholder="输入列的含义，例如：01=在职, 02=离职..." rows="2"></textarea>
          <div class="admin-card-actions">
            <button class="btn-admin-action btn-admin-answer" data-action="answer">💾 回答</button>
            <button class="btn-admin-action btn-admin-agree" data-action="agree">✓ 同意推测</button>
            <button class="btn-admin-action btn-admin-supplement" data-action="supplement">✎ 补充说明</button>
            <button class="btn-admin-action btn-admin-ignore" data-action="ignore">✕ 忽略</button>
          </div>
        `;
      } else {
        inner += `
          <div class="admin-card-answered">
            <div class="answer-text">${esc(q.answer)}</div>
          </div>
          <div class="admin-card-meta" style="margin-top:6px">回答于: ${q.answered_at}</div>
        `;
      }

      card.innerHTML = inner;
      container.appendChild(card);

      // Bind action buttons for pending cards
      if (isPending) {
        card.querySelectorAll('.btn-admin-action').forEach(btn => {
          btn.addEventListener('click', () => handleCardAction(btn.dataset.action, q.question_id, card));
        });
      }
    }
  }

  async function handleCardAction(action, questionId, card) {
    if (action === 'ignore') {
      try {
        await fetch(`/api/admin/questions/${questionId}/ignore`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_id: currentProfileId }),
        });
        card.remove();
        await refreshAfterAction();
      } catch (err) {
        console.error('Ignore failed:', err);
      }
      return;
    }

    // For answer/agree/supplement — get textarea value
    const textarea = card.querySelector('.admin-card-input');
    let answer = textarea ? textarea.value.trim() : '';

    if (action === 'agree') {
      answer = answer || '同意推测，列含义如样本所示';
    } else if (action === 'supplement') {
      if (!answer) {
        textarea.focus();
        textarea.placeholder = '请输入补充说明...';
        return;
      }
    } else if (action === 'answer') {
      if (!answer) {
        textarea.focus();
        textarea.placeholder = '请输入答案...';
        return;
      }
    }

    try {
      const resp = await fetch(`/api/admin/questions/${questionId}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: answer, profile_id: currentProfileId }),
      });
      if (resp.ok) {
        card.remove();
        await refreshAfterAction();
      } else {
        const err = await resp.json();
        alert('保存失败: ' + (err.detail || 'Unknown error'));
      }
    } catch (err) {
      console.error('Answer failed:', err);
    }
  }

  async function refreshAfterAction() {
    // Refresh profile list to update badge counts
    await loadProfiles();
    // Re-select current profile
    if (currentProfileId) {
      dbSelect.value = currentProfileId;
      await selectProfile(currentProfileId);
    }
    // Reload questions
    if (currentTab === 'pending') await loadPendingQuestions();
    else if (currentTab === 'answered') await loadAnsweredQuestions();
  }

  // ---- Exploration ----

  function setupExploreButtons() {
    btnStartExplore.addEventListener('click', startExplore);
    btnStopExplore.addEventListener('click', stopExplore);
  }

  function startExplore() {
    if (!currentProfileId) {
      alert('请先选择数据库');
      return;
    }

    // Reset state
    exploreEventSource = null;
    exploreQuestions.innerHTML = '';
    exploreSummary.style.display = 'none';
    exploreProgress.style.display = '';
    progressFill.style.width = '0%';
    progressText.textContent = '正在连接数据库...';
    delete progressText.dataset.errorShown;

    btnStartExplore.style.display = 'none';
    btnStopExplore.style.display = '';

    const url = `/api/admin/explore-database?profile_id=${currentProfileId}`;
    exploreEventSource = new EventSource(url);

    exploreEventSource.addEventListener('progress', (e) => {
      try {
        const d = JSON.parse(e.data);
        progressText.textContent = `${d.phase || ''} ${d.title || ''}: ${d.detail || ''}`;

        // Parse progress from detail (e.g., "表 5/20 完成")
        const match = d.detail && d.detail.match(/(\d+)\/(\d+)/);
        if (match) {
          const pct = Math.round((parseInt(match[1]) / parseInt(match[2])) * 100);
          progressFill.style.width = Math.min(pct, 95) + '%';
        }

        if (d.status === 'done') {
          progressFill.style.width = '100%';
        }
      } catch (_) {}
    });

    exploreEventSource.addEventListener('admin_question', (e) => {
      try {
        const d = JSON.parse(e.data);
        renderExploreQuestionCard(d);
      } catch (_) {}
    });

    exploreEventSource.addEventListener('done', (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.status === 'error') {
          // Error already displayed via the explore_error event handler, don't overwrite
        } else {
          progressFill.style.width = '100%';
          progressText.textContent = '探索完成!';

          exploreSummary.style.display = '';
          exploreSummary.innerHTML = `
            ✅ 探索完成 — 发现 ${d.questions_submitted || 0} 个新问题，${d.skipped || 0} 个已跳过（已有答案）
          `;
        }

        finishExplore();
      } catch (_) {
        progressText.textContent = '探索完成';
        finishExplore();
      }
    });

    // Named SSE event for backend errors (uses non-reserved "explore_error" instead of "error"
    // because browsers treat event:error as a transport-level signal, not a MessageEvent)
    exploreEventSource.addEventListener('explore_error', (e) => {
      try {
        const d = JSON.parse(e.data);
        progressText.textContent = d.message || '探索出错';
        // Mark that we've already displayed a specific error
        progressText.dataset.errorShown = '1';
      } catch (_) {
        progressText.textContent = '探索出错或连接断开';
      }
      finishExplore();
    });

    // Native EventSource error — fires on connection drop / network error only
    exploreEventSource.addEventListener('error', (e) => {
      // Only show generic error if we haven't already shown a specific one from explore_error
      if (progressText.dataset.errorShown !== '1') {
        progressText.textContent = '探索出错或连接断开';
      }
      finishExplore();
    });
  }

  function stopExplore() {
    if (exploreEventSource) {
      exploreEventSource.close();
      exploreEventSource = null;
    }
    progressText.textContent = '已停止探索';
    finishExplore();
  }

  function finishExplore() {
    // Close EventSource to prevent automatic reconnection
    if (exploreEventSource) {
      exploreEventSource.close();
      exploreEventSource = null;
    }
    btnStartExplore.style.display = '';
    btnStopExplore.style.display = 'none';

    // Refresh data
    loadProfiles().then(() => {
      if (currentProfileId) {
        dbSelect.value = currentProfileId;
        selectProfile(currentProfileId);
      }
    });
    if (currentTab === 'pending') loadPendingQuestions();
  }

  function renderExploreQuestionCard(d) {
    const card = document.createElement('div');
    card.className = 'admin-card pending';
    card.dataset.qid = d.question_id;

    const colLabel = d.column_name
      ? `<code>${esc(d.table_name)}</code>.<code>${esc(d.column_name)}</code>`
      : `<code>${esc(d.table_name)}</code>`;

    card.innerHTML = `
      <div class="admin-card-header">
        <div class="admin-card-title">🆕 ${colLabel}</div>
        <div class="admin-card-meta">新发现</div>
      </div>
      <div class="admin-card-question">${esc(d.question)}</div>
      ${d.agent_guess ? `<div class="admin-card-guess"><span class="guess-label">🤖 推测:</span><span class="guess-text">${esc(d.agent_guess)}</span></div>` : ''}
      <textarea class="admin-card-input" placeholder="输入列的含义..." rows="2">${esc(d.agent_guess || '')}</textarea>
      <div class="admin-card-actions">
        <button class="btn-admin-action btn-admin-answer" data-action="answer">💾 回答</button>
        <button class="btn-admin-action btn-admin-agree" data-action="agree">✓ 同意推测</button>
        <button class="btn-admin-action btn-admin-ignore" data-action="ignore">✕ 忽略</button>
      </div>
    `;

    exploreQuestions.insertBefore(card, exploreQuestions.firstChild);

    // Bind actions
    const qid = d.question_id;
    card.querySelectorAll('.btn-admin-action').forEach(btn => {
      btn.addEventListener('click', () => handleCardAction(btn.dataset.action, qid, card));
    });
  }

  // ---- Helpers ----

  function esc(s) {
    if (!s) return '';
    const div = document.createElement('div');
    div.textContent = String(s);
    return div.innerHTML;
  }

  // ---- Start ----
  document.addEventListener('DOMContentLoaded', init);

})();
