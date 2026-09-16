<script setup lang="ts">
import { computed } from 'vue'

import ArtifactGallery from './ArtifactGallery.vue'
import FailureCard from './FailureCard.vue'
import MarkdownBlock from './MarkdownBlock.vue'
import StepStream from './StepStream.vue'
import { formatDuration, runElapsedMs, type RunState } from '../core/run.ts'

/**
 * 一次提问的完整呈现：用户消息 → 步骤流 → 回答（+ 图表 / 失败 / 备注）。
 *
 * 流式中的回答走插值（转义），只有结束后才交给 markdown 渲染器 ——
 * 旧实现把模型输出直接 `innerHTML +=` 进 DOM，那是 XSS。
 */
const props = defineProps<{
  text: string
  run: RunState
  now: number
  canRetry: boolean
}>()

defineEmits<{ retry: [] }>()

const finished = computed(() => props.run.status !== 'running')

/** 没有被任何步骤引用的产物（后端可能不把 artifact 挂到具体动作上）。 */
const looseArtifacts = computed(() => {
  const attached = new Set<string>()
  for (const step of props.run.steps) for (const id of step.artifactIds) attached.add(id)
  return props.run.artifacts.filter((a) => !attached.has(a.id))
})

const statsLine = computed(() => {
  if (!finished.value) return ''
  const parts: string[] = [formatDuration(runElapsedMs(props.run, props.now))]
  const { queries, rows, charts } = props.run.stats
  if (queries > 0) parts.push(`${queries} 次查询`)
  if (rows > 0) parts.push(`${rows} 行数据`)
  if (charts > 0) parts.push(`${charts} 张图表`)
  return parts.join(' · ')
})
</script>

<template>
  <article class="turn">
    <div class="user">
      <span class="who" aria-hidden="true">你</span>
      <p class="user-text">{{ text }}</p>
    </div>

    <StepStream :run="run" :now="now" />

    <div v-if="run.answerStarted || run.answer" class="answer">
      <!-- 流式中：插值转义，逐字增长 -->
      <p v-if="!finished" class="answer-stream">{{ run.answer }}<span class="caret" /></p>
      <!-- 结束后：整段 markdown 渲染（已经过 sanitize） -->
      <MarkdownBlock v-else-if="run.answer" :text="run.answer" />
    </div>

    <ArtifactGallery v-if="looseArtifacts.length" :artifacts="looseArtifacts" />

    <p v-if="run.caveats.length" class="caveats">
      <span v-for="caveat in run.caveats" :key="caveat">{{ caveat }}</span>
    </p>

    <FailureCard
      v-if="run.failure"
      :failure="run.failure"
      :can-retry="canRetry"
      @retry="$emit('retry')"
    />

    <p v-if="statsLine" class="stats mono">{{ run.status === 'partial' ? '部分完成' : '完成' }} · {{ statsLine }}</p>
  </article>
</template>

<style scoped>
.turn {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px 0 22px;
  border-bottom: 1px solid var(--border);
}

.turn:last-child {
  border-bottom: none;
}

.user {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.who {
  flex: none;
  font-size: var(--text-xs);
  color: var(--accent-warm);
  border: 1px solid rgba(229, 181, 103, 0.4);
  border-radius: 4px;
  padding: 0 6px;
  line-height: 1.5;
}

.user-text {
  font-size: var(--text-base);
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}

.answer {
  padding-left: 2px;
}

.answer-stream {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-primary);
}

.caret {
  display: inline-block;
  width: 7px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--accent-cool);
  animation: blink 1.1s steps(2, start) infinite;
}

@keyframes blink {
  0%,
  60% {
    opacity: 0.8;
  }
  61%,
  100% {
    opacity: 0;
  }
}

.caveats {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--text-xs);
  color: var(--accent-warm-dim);
}

.stats {
  font-size: var(--text-xs);
  color: var(--text-dim);
}
</style>
