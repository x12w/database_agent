<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import AdminQuestionCard from '../components/AdminQuestionCard.vue'
import ExplorePanel from '../components/ExplorePanel.vue'
import { fetchAdminQuestions } from '../api/admin.ts'
import { navigate } from '../router.ts'
import { databasesStore } from '../stores/databases.ts'
import type { AdminQuestion } from '../types/api.ts'

/**
 * 管理面板：待回答 / 已回答 / 数据库探索。
 * 选中的数据库沿用旧前端的 `admin_selected_profile` 键。
 */
const PROFILE_KEY = 'admin_selected_profile'

const state = databasesStore.state
const tab = ref<'pending' | 'answered' | 'explore'>('pending')
const pending = ref<AdminQuestion[]>([])
const answered = ref<AdminQuestion[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const profileId = ref<string | null>(null)

const pendingCount = computed(() => pending.value.length)
const answeredCount = computed(() => answered.value.length)

function rememberProfile(id: string | null): void {
  try {
    if (id) localStorage.setItem(PROFILE_KEY, id)
    else localStorage.removeItem(PROFILE_KEY)
  } catch {
    /* 忽略 */
  }
}

onMounted(async () => {
  if (state.profiles.length === 0) await databasesStore.load()
  let saved: string | null = null
  try {
    saved = localStorage.getItem(PROFILE_KEY)
  } catch {
    saved = null
  }
  const valid = state.profiles.some((p) => p.id === saved)
  profileId.value = valid ? saved : state.currentId ?? state.profiles[0]?.id ?? null
  await loadAll()
})

watch(profileId, (id) => {
  rememberProfile(id)
  void loadAll()
})

async function loadAll(): Promise<void> {
  if (!profileId.value) return
  loading.value = true
  error.value = null
  try {
    const [pendingResult, answeredResult] = await Promise.all([
      fetchAdminQuestions('pending', profileId.value),
      fetchAdminQuestions('answered', profileId.value),
    ])
    pending.value = pendingResult.questions ?? []
    answered.value = answeredResult.questions ?? []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function reloadPending(): Promise<void> {
  if (!profileId.value) return
  const result = await fetchAdminQuestions('pending', profileId.value)
  pending.value = result.questions ?? []
  const answeredResult = await fetchAdminQuestions('answered', profileId.value)
  answered.value = answeredResult.questions ?? []
}
</script>

<template>
  <div class="admin">
    <header class="bar">
      <div class="left">
        <h1>管理面板</h1>
        <span class="muted">数据库管理员问答</span>
      </div>
      <div class="right">
        <select
          class="db-select"
          :value="profileId ?? ''"
          @change="profileId = ($event.target as HTMLSelectElement).value || null"
        >
          <option v-if="state.profiles.length === 0" value="">无可用数据库</option>
          <option v-for="profile in state.profiles" :key="profile.id" :value="profile.id">
            {{ profile.name || profile.database }} ({{ profile.db_type }}:{{ profile.database }})
          </option>
        </select>
        <span v-if="pendingCount > 0" class="pending-badge">{{ pendingCount }} 待回答</span>
        <button type="button" class="btn" @click="navigate('/')">返回查询</button>
      </div>
    </header>

    <nav class="tabs">
      <button type="button" :class="{ active: tab === 'pending' }" @click="tab = 'pending'">
        待回答 <span class="count">{{ pendingCount }}</span>
      </button>
      <button type="button" :class="{ active: tab === 'answered' }" @click="tab = 'answered'">
        已回答 <span class="count">{{ answeredCount }}</span>
      </button>
      <button type="button" :class="{ active: tab === 'explore' }" @click="tab = 'explore'">
        数据库探索
      </button>
      <button type="button" class="btn-ghost refresh" title="刷新" @click="loadAll">⟳</button>
    </nav>

    <main class="content">
      <p v-if="error" class="error">{{ error }}</p>

      <template v-if="tab === 'pending'">
        <p v-if="loading" class="muted">加载中…</p>
        <p v-else-if="pending.length === 0" class="muted">暂无待回答的问题。</p>
        <div v-else class="list">
          <AdminQuestionCard
            v-for="question in pending"
            :key="question.question_id"
            :question="question"
            :profile-id="profileId"
            @done="reloadPending"
            @ignored="reloadPending"
          />
        </div>
      </template>

      <template v-else-if="tab === 'answered'">
        <p v-if="loading" class="muted">加载中…</p>
        <p v-else-if="answered.length === 0" class="muted">还没有已回答的问题。</p>
        <div v-else class="list">
          <AdminQuestionCard
            v-for="question in answered"
            :key="question.question_id"
            :question="question"
            :profile-id="profileId"
            :pending="false"
          />
        </div>
      </template>

      <ExplorePanel v-else :profile-id="profileId" @questions-changed="reloadPending" />
    </main>
  </div>
</template>

<style scoped>
.admin {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
}

.left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

h1 {
  font-size: var(--text-base);
  font-weight: 600;
}

.left .muted {
  font-size: var(--text-xs);
}

.right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.db-select {
  max-width: 280px;
  padding: 5px 8px;
  font-size: var(--text-sm);
}

.pending-badge {
  font-size: var(--text-xs);
  color: var(--accent-warm);
  border: 1px solid rgba(229, 181, 103, 0.4);
  border-radius: 999px;
  padding: 2px 8px;
}

.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 18px;
  border-bottom: 1px solid var(--border);
}

.tabs button {
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--text-muted);
  transition: var(--transition);
}

.tabs button:hover {
  color: var(--text-secondary);
  background: var(--bg-elevated);
}

.tabs button.active {
  color: var(--text-primary);
  background: var(--bg-elevated);
}

.count {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-dim);
  margin-left: 4px;
}

.refresh {
  margin-left: auto;
}

.content {
  flex: 1;
  overflow: auto;
  padding: 18px;
}

.list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 820px;
}

.error {
  color: var(--accent-red);
  font-size: var(--text-sm);
}
</style>
