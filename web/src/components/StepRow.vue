<script setup lang="ts">
import { computed, ref } from 'vue'

import DisclosureBlock from './DisclosureBlock.vue'
import QuestionCard from './QuestionCard.vue'
import ThoughtBlock from './ThoughtBlock.vue'
import { formatDuration, stepElapsedMs, type RunState, type Step } from '../core/run.ts'

/**
 * 步骤流里的一行。
 *
 * 结构固定为三栏：状态字形 · 文案（+ 摘要）· 右对齐耗时。
 * 阶段行与动作行共用同一套几何，只有缩进与字重不同 —— 这样整条列表
 * 看起来是一条连续的线，而不是一堆卡片。
 */
const props = defineProps<{
  step: Step
  run: RunState
  now: number
}>()

const override = ref<boolean | null>(null)

const expanded = computed(() => {
  if (override.value !== null) return override.value
  return props.step.status === 'running' || props.step.kind === 'question'
})

const elapsed = computed(() => stepElapsedMs(props.step, props.run, props.now))

/** 进行中显示事件自带的 detail；结束后显示一句话结果。 */
const lineText = computed(() => {
  if (props.step.status === 'running') return props.step.detail ?? ''
  if (props.step.summary) return props.step.summary
  if (props.step.rowCount !== null) return `返回 ${props.step.rowCount} 行`
  return ''
})

const artifacts = computed(() =>
  props.run.artifacts.filter((a) => props.step.artifactIds.includes(a.id)),
)

const hasBody = computed(
  () =>
    props.step.thought.length > 0 ||
    props.step.question !== null ||
    props.step.disclosures.length > 0 ||
    artifacts.value.length > 0,
)

function toggle(): void {
  if (!hasBody.value) return
  override.value = !expanded.value
}
</script>

<template>
  <div
    class="step"
    :class="[`kind-${step.kind}`, `status-${step.status}`]"
    :style="{ '--depth': String(step.depth) }"
  >
    <button
      type="button"
      class="head"
      :aria-expanded="hasBody ? expanded : undefined"
      :disabled="!hasBody"
      @click="toggle"
    >
      <span class="glyph" aria-hidden="true">
        <span v-if="step.status === 'running'" class="spinner" />
        <span v-else-if="step.status === 'failed'" class="mark fail">✕</span>
        <span v-else-if="step.kind === 'question'" class="mark ask">?</span>
        <span v-else class="mark ok">✓</span>
      </span>

      <span class="text">
        <span class="label">{{ step.label }}</span>
        <span v-if="step.target" class="target mono">{{ step.target }}</span>
        <span v-if="lineText" class="line">{{ lineText }}</span>
      </span>

      <span v-if="elapsed > 0" class="duration mono">{{ formatDuration(elapsed) }}</span>
    </button>

    <p v-if="step.retryNote" class="retry">{{ step.retryNote }}</p>

    <div v-if="hasBody && expanded" class="body">
      <QuestionCard v-if="step.question" :question="step.question" />
      <ThoughtBlock
        v-if="step.thought"
        :text="step.thought"
        :streaming="step.status === 'running'"
      />
      <DisclosureBlock
        v-for="disclosure in step.disclosures"
        :key="disclosure.id"
        :label="disclosure.label"
        :text="disclosure.text"
        :tone="disclosure.id === 'error' ? 'error' : 'sql'"
      />
      <div v-if="artifacts.length" class="artifacts">
        <img
          v-for="artifact in artifacts"
          :key="artifact.id"
          :src="artifact.url"
          :alt="artifact.title || '图表'"
          class="artifact-image"
          loading="lazy"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.step {
  position: relative;
  padding-left: calc(var(--depth) * var(--step-indent));
}

/* 左侧引导线：整列共用一条，视觉上把步骤连成一条流。 */
.step::before {
  content: '';
  position: absolute;
  left: calc(var(--depth) * var(--step-indent) + 7px);
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border);
}

.step:first-child::before {
  top: 12px;
}

.step:last-child::before {
  bottom: calc(100% - 14px);
}

.head {
  display: grid;
  grid-template-columns: var(--step-gutter) 1fr auto;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 3px 0;
  border-radius: var(--radius-sm);
}

.head:disabled {
  opacity: 1;
  cursor: default;
}

.head:not(:disabled):hover .label {
  color: var(--text-primary);
}

.glyph {
  position: relative;
  z-index: 1;
  justify-self: start;
  width: 15px;
  height: 15px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  background: var(--bg-primary);
  border-radius: 50%;
}

.mark {
  color: var(--text-dim);
}

.mark.ok {
  color: var(--accent-green);
}

.mark.fail {
  color: var(--accent-red);
}

.mark.ask {
  color: var(--accent-warm);
}

.spinner {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 1.5px solid rgba(92, 201, 183, 0.25);
  border-top-color: var(--accent-cool);
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.text {
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  font-size: var(--text-sm);
  line-height: 1.5;
}

.label {
  color: var(--text-secondary);
  transition: var(--transition);
}

.kind-stage .label {
  color: var(--text-primary);
  font-weight: 500;
}

.status-running .label {
  color: var(--text-primary);
}

.status-ok .label,
.status-ok .line {
  color: var(--text-muted);
}

.status-failed .label {
  color: var(--accent-red);
}

.target {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.line {
  font-size: var(--text-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.duration {
  font-size: var(--text-xs);
  color: var(--text-dim);
  padding-left: 8px;
}

.retry {
  margin: 0 0 2px calc(var(--step-gutter) + 8px);
  font-size: var(--text-xs);
  color: var(--accent-warm-dim);
}

.body {
  margin: 4px 0 10px calc(var(--step-gutter) + 8px);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.artifacts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.artifact-image {
  max-width: min(420px, 100%);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: #fff;
}
</style>
