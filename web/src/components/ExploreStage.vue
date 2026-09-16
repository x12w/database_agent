<script setup lang="ts">
import { computed } from 'vue'

import AdminQuestionCard from './AdminQuestionCard.vue'
import DisclosureBlock from './DisclosureBlock.vue'
import {
  exploreProgressText,
  exploreRatio,
  exploreStatusLabel,
  exploreSummaryText,
  type ExploreQuestionEntry,
  type ExploreState,
} from '../core/explore.ts'
import type { AdminQuestion } from '../types/api.ts'

/**
 * 探索通道的展示层（进度条 + 就地冒出的问答卡片 + 结束总结）。
 *
 * 它是**纯展示**的：状态由 `core/explore.ts` 的归约产出，事件从哪来
 * （真实 SSE 还是 `/dev` 的夹具回放）与它无关 —— 和聊天通道里
 * `RunView` / `StepStream` 的分工一致。
 */
const props = withDefaults(
  defineProps<{
    state: ExploreState
    profileId: string | null
    /** 夹具模式：卡片只展示形状，不真的提交。 */
    interactive?: boolean
  }>(),
  { interactive: true },
)

const emit = defineEmits<{
  /** 管理员就地回答了一张卡片，参数是他给出的答案。 */
  answered: [questionId: string, answer: string]
  /** 管理员忽略了（或夹具里点了忽略）一张卡片。 */
  ignored: [questionId: string]
}>()

const progressText = computed(() => exploreProgressText(props.state))
const statusLabel = computed(() => exploreStatusLabel(props.state))
const ratio = computed(() => exploreRatio(props.state))
const summary = computed(() => exploreSummaryText(props.state))
const pending = computed(() => props.state.questions.filter((e) => !e.deduped && !e.resolved))
const answered = computed(() => props.state.questions.filter((e) => e.deduped || e.resolved))

/** 新的卡片在上 —— 与旧面板 `insertBefore(firstChild)` 的观感一致。 */
function newestFirst(entries: ExploreQuestionEntry[]): ExploreQuestionEntry[] {
  return [...entries].reverse()
}

/** 契约事件 → 卡片需要的形状。表名列名**照实展示**：读者是管理员。 */
function toQuestion(entry: ExploreQuestionEntry): AdminQuestion {
  const ev = entry.event
  return {
    question_id: ev.question_id,
    table_name: ev.table,
    column_name: ev.column,
    question: ev.question,
    sample_values: ev.samples,
  }
}

function cardNote(entry: ExploreQuestionEntry): string | null {
  if (entry.deduped) return '已有答案（历史提问去重命中，未重复提问）'
  if (entry.resolved) {
    return entry.resolved.answer === null ? '已忽略' : '本次已回答'
  }
  return null
}

function cardAnswer(entry: ExploreQuestionEntry): string | undefined {
  if (entry.deduped) return entry.event.existing_answer ?? ''
  if (entry.resolved) return entry.resolved.answer ?? '（已忽略，不再提问）'
  return undefined
}

const tone = computed(() => {
  if (props.state.status === 'failed') return 'error'
  if (props.state.status === 'waiting') return 'warn'
  if (props.state.status === 'ok') return 'ok'
  return 'busy'
})
</script>

<template>
  <div class="stage">
    <div v-if="state.status !== 'idle'" class="progress">
      <div class="bar"><div class="fill" :data-tone="tone" :style="{ width: `${ratio * 100}%` }" /></div>
      <p class="progress-line">
        <span class="status" :data-tone="tone">{{ statusLabel }}</span>
        <span class="progress-text">{{ progressText }}</span>
        <span class="count mono">{{ state.done }}/{{ state.total || '?' }}</span>
      </p>
    </div>

    <p v-if="summary" class="summary" :data-tone="tone">{{ summary }}</p>

    <!-- 探索通道的读者是管理员：原始报错默认收起，需要时展开即可看。 -->
    <DisclosureBlock
      v-if="state.failure?.errorDetail"
      label="查看错误详情"
      :text="state.failure.errorDetail"
      tone="error"
    />

    <div v-if="state.questions.length" class="cards">
      <p class="cards-head">
        发现 {{ state.questions.length }} 个可疑列
        <span v-if="pending.length" class="muted">· {{ pending.length }} 个待回答</span>
      </p>
      <AdminQuestionCard
        v-for="entry in newestFirst(state.questions)"
        :key="entry.event.question_id"
        :question="{ ...toQuestion(entry), answer: cardAnswer(entry) }"
        :profile-id="profileId"
        :pending="!entry.deduped && !entry.resolved"
        :guess="entry.event.agent_guess || null"
        :context-columns="entry.event.context_columns"
        :context-rows="entry.event.context_rows"
        :note="cardNote(entry)"
        :readonly="!interactive"
        :fresh="!entry.deduped && !entry.resolved && state.status === 'running'"
        @done="(answer: string) => emit('answered', entry.event.question_id, answer)"
        @ignored="emit('ignored', entry.event.question_id)"
      />
      <p v-if="answered.length" class="cards-foot muted">
        本次已处理 {{ answered.length }} 个（去重命中与就地回答）
      </p>
    </div>
  </div>
</template>

<style scoped>
.stage {
  display: flex;
  flex-direction: column;
  gap: 12px;
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

.fill[data-tone='warn'] {
  background: var(--accent-warm);
}

.fill[data-tone='ok'] {
  background: var(--accent-green);
}

.fill[data-tone='error'] {
  background: var(--accent-red);
}

.progress-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: var(--text-xs);
}

.status {
  color: var(--accent-cool);
  flex: none;
}

.status[data-tone='warn'] {
  color: var(--accent-warm);
}

.status[data-tone='ok'] {
  color: var(--accent-green);
}

.status[data-tone='error'] {
  color: var(--accent-red);
}

.progress-text {
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.count {
  margin-left: auto;
  color: var(--text-dim);
}

.summary {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.summary[data-tone='ok'] {
  color: var(--accent-green);
}

.summary[data-tone='warn'] {
  color: var(--accent-warm);
}

.summary[data-tone='error'] {
  color: var(--accent-red);
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}

.cards-head {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.cards-foot {
  font-size: var(--text-xs);
}
</style>
