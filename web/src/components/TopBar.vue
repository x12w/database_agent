<script setup lang="ts">
import { computed } from 'vue'

import { databasesStore } from '../stores/databases.ts'
import { navigate } from '../router.ts'

/**
 * 顶栏：数据库选择、连接状态、以及旧前端保留下来的一组操作
 * （新建会话 / 筛选表 / 管理数据库 / 重建缓存）。
 */
const emit = defineEmits<{
  newChat: []
  openFilter: []
  openManager: []
  rebuild: []
  select: [id: string]
}>()

const state = databasesStore.state

const options = computed(() =>
  state.profiles.map((p) => ({
    id: p.id,
    label: p.name ? `${p.name} (${p.database})` : p.database,
  })),
)

function onSelect(event: Event): void {
  // 切换数据库由上层处理（要同时重置会话与表筛选）。
  emit('select', (event.target as HTMLSelectElement).value)
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <svg class="logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <ellipse cx="12" cy="6" rx="8" ry="3" />
        <path d="M4 6v6c0 1.5 3.5 3 8 3s8-1.5 8-3V6" />
        <path d="M4 12v6c0 1.5 3.5 3 8 3s8-1.5 8-3v-6" />
      </svg>
      <h1>dbchat<span class="sub">智能查询助手</span></h1>
    </div>

    <div class="actions">
      <select class="db-select" :value="state.currentId ?? ''" :disabled="state.loading" @change="onSelect">
        <option v-if="state.profiles.length === 0" value="">无可用数据库</option>
        <option v-for="option in options" :key="option.id" :value="option.id">{{ option.label }}</option>
      </select>

      <button type="button" class="btn-ghost" title="筛选表" @click="$emit('openFilter')">筛选</button>
      <button type="button" class="btn-ghost" title="管理数据库" @click="$emit('openManager')">管理</button>

      <button
        type="button"
        class="status"
        :title="state.statusLabel"
        @click="databasesStore.refreshConnection()"
      >
        <span class="status-dot" :data-tone="state.statusTone" />
        <span class="status-label">{{ state.busyLabel ?? state.statusLabel }}</span>
      </button>

      <span class="mode-badge" :data-mode="state.health?.llm_available === false ? 'limited' : 'llm'">
        {{ databasesStore.llmMode }}
      </span>

      <button type="button" class="btn-ghost" title="新建会话" @click="$emit('newChat')">＋</button>
      <button type="button" class="btn-ghost" title="重建数据库缓存" @click="$emit('rebuild')">⟳</button>
      <button type="button" class="btn-ghost" title="管理面板" @click="navigate('/admin')">⚙</button>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.logo {
  width: 22px;
  height: 22px;
  color: var(--accent-cool);
  flex: none;
}

h1 {
  font-size: var(--text-base);
  font-weight: 600;
  white-space: nowrap;
}

.sub {
  margin-left: 8px;
  font-size: var(--text-xs);
  font-weight: 400;
  color: var(--text-muted);
}

.actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.db-select {
  max-width: 260px;
  padding: 5px 8px;
  font-size: var(--text-sm);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 10px;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.status:hover {
  background: var(--bg-elevated);
  color: var(--text-secondary);
}

.status-label {
  white-space: nowrap;
}

.mode-badge {
  font-size: var(--text-xs);
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-light);
  color: var(--text-muted);
}

.mode-badge[data-mode='llm'] {
  color: var(--accent-cool);
  border-color: rgba(92, 201, 183, 0.35);
}

@media (max-width: 720px) {
  .brand .sub,
  .mode-badge,
  .status-label {
    display: none;
  }
}
</style>
