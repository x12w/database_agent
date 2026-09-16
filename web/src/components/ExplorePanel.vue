<script setup lang="ts">
import { computed, ref } from 'vue'

import AdminQuestionCard from './AdminQuestionCard.vue'
import { exploreDatabase } from '../api/admin.ts'
import { databasesStore } from '../stores/databases.ts'
import type { ExploreQuestionEvent } from '../types/api.ts'

/**
 * 主动探索数据库：一条独立 SSE 通道（事件名 progress / admin_question /
 * done / explore_error），进度条 + 就地冒出的新问题卡片。
 */
const emit = defineEmits<{ questionsChanged: [] }>()

const props = defineProps<{ profileId: string | null }>()

const running = ref(false)
const progressText = ref('')
const progressPct = ref(0)
const questions = ref<ExploreQuestionEvent[]>([])
const summary = ref<string | null>(null)
const errored = ref(false)

let controller: AbortController | null = null

const canStart = computed(() => props.profileId !== null && !running.value)

function start(): void {
  if (!props.profileId) return
  running.value = true
  errored.value = false
  summary.value = null
  progressPct.value = 0
  progressText.value = '正在连接数据库…'
  questions.value = []
  controller = new AbortController()

  void exploreDatabase(
    props.profileId,
    null,
    {
      onProgress: (event) => {
        progressText.value = [event.phase, event.title].filter(Boolean).join(' · ') + (event.detail ? `: ${event.detail}` : '')
        const match = event.detail?.match(/(\d+)\s*\/\s*(\d+)/)
        if (match && match[1] && match[2]) {
          const done = Number(match[1])
          const total = Number(match[2])
          if (total > 0) progressPct.value = Math.min(95, Math.round((done / total) * 100))
        }
        if (event.status === 'done') progressPct.value = 100
      },
      onQuestion: (event) => {
        questions.value.unshift(event)
      },
      onDone: (event) => {
        if (event.status === 'error') return
        progressPct.value = 100
        progressText.value = '探索完成'
        summary.value = `发现 ${event.questions_submitted ?? 0} 个新问题，跳过 ${event.skipped ?? 0} 个（已有答案）`
        emit('questionsChanged')
      },
      onError: (message) => {
        errored.value = true
        progressText.value = message
      },
      onClose: () => {
        running.value = false
        controller = null
        void databasesStore.refreshStatus(false)
      },
    },
    controller.signal,
  )
}

function stop(): void {
  controller?.abort()
  controller = null
  running.value = false
  progressText.value = '已停止探索'
}

function onCardDone(event: ExploreQuestionEvent): void {
  questions.value = questions.value.filter((q) => q.question_id !== event.question_id)
  emit('questionsChanged')
}

function onCardIgnored(event: ExploreQuestionEvent): void {
  questions.value = questions.value.filter((q) => q.question_id !== event.question_id)
}
</script>

<template>
  <section class="explore">
    <p class="intro muted">
      扫描所有表，识别编码/缩写列，并就其业务含义向你提问。回答后的知识会用于后续查询。
    </p>

    <div v-if="running || progressText" class="progress">
      <div class="bar"><div class="fill" :style="{ width: `${progressPct}%` }" /></div>
      <p class="progress-text" :class="{ error: errored }">{{ progressText }}</p>
    </div>

    <div class="controls">
      <button v-if="!running" type="button" class="btn btn-primary" :disabled="!canStart" @click="start">
        开始探索
      </button>
      <button v-else type="button" class="btn danger" @click="stop">停止探索</button>
    </div>

    <p v-if="summary" class="summary">{{ summary }}</p>

    <div v-if="questions.length" class="cards">
      <AdminQuestionCard
        v-for="question in questions"
        :key="question.question_id"
        :question="{
          question_id: question.question_id,
          table_name: question.table_name,
          column_name: question.column_name,
          question: question.question,
          sample_values: question.sample_values,
          created_at: '新发现',
        }"
        :profile-id="profileId"
        :guess="question.agent_guess ?? null"
        fresh
        @done="onCardDone(question)"
        @ignored="onCardIgnored(question)"
      />
    </div>
  </section>
</template>

<style scoped>
.explore {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 820px;
}

.intro {
  font-size: var(--text-sm);
}

.progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bar {
  height: 4px;
  border-radius: 999px;
  background: var(--bg-elevated);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--accent-cool);
  transition: width 300ms ease;
}

.progress-text {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.progress-text.error {
  color: var(--accent-red);
}

.controls {
  display: flex;
  gap: 8px;
}

.danger {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.35);
}

.summary {
  font-size: var(--text-sm);
  color: var(--accent-green);
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}
</style>
