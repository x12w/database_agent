<script setup lang="ts">
import { chatStore } from '../stores/chat.ts'

/**
 * 会话历史（本地持久化）。切换会话不会清空别的会话；
 * 切换数据库会按旧行为开一个新会话。
 */
const state = chatStore.state

function fmt(ts: number): string {
  const d = new Date(ts)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const time = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  return sameDay ? time : `${d.getMonth() + 1}/${d.getDate()} ${time}`
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-head">
      <span class="title">会话</span>
      <button type="button" class="btn-ghost" title="新建会话" @click="chatStore.newChat()">＋</button>
    </div>
    <ul class="list">
      <li v-for="session in state.sessions" :key="session.id">
        <div class="row" :class="{ active: session.id === state.activeSessionId }">
          <button type="button" class="open" @click="chatStore.selectSession(session.id)">
            <span class="name">{{ session.title }}</span>
            <span class="when mono">{{ fmt(session.updatedAt) }}</span>
          </button>
          <button
            type="button"
            class="del"
            title="删除会话"
            @click.stop="chatStore.deleteSession(session.id)"
          >
            ×
          </button>
        </div>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 220px;
  flex: none;
  border-right: 1px solid var(--border);
  background: var(--bg-surface);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}

.title {
  font-size: var(--text-xs);
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.list {
  list-style: none;
  overflow: auto;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.row {
  display: flex;
  align-items: center;
  border-radius: var(--radius-sm);
}

.row:hover {
  background: var(--bg-elevated);
}

.row.active {
  background: var(--bg-elevated);
  box-shadow: inset 2px 0 0 var(--accent-cool);
}

.open {
  flex: 1;
  min-width: 0;
  text-align: left;
  padding: 7px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.name {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row.active .name {
  color: var(--text-primary);
}

.when {
  font-size: 10px;
  color: var(--text-dim);
}

.del {
  flex: none;
  padding: 0 8px;
  color: var(--text-dim);
  opacity: 0;
  transition: var(--transition);
}

.row:hover .del {
  opacity: 1;
}

.del:hover {
  color: var(--accent-red);
}

@media (max-width: 860px) {
  .sidebar {
    display: none;
  }
}
</style>
