<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import StepRow from './StepRow.vue'
import { countRetries, runSummaryLine, shouldCollapse, type RunState } from '../core/run.ts'

/**
 * 一条连续的步骤列表。
 *
 * 回答开始流出后自动折叠成一行 `▸ 已完成 7 步 · 12.4s`；用户随时可以再展开
 * （表头那一行既是折叠态也是展开态的控制条，两种状态共用一个位置）。
 */
const props = defineProps<{
  run: RunState
  now: number
}>()

const expanded = ref(!shouldCollapse(props.run))

watch(
  () => props.run.answerStarted,
  (started) => {
    if (started) expanded.value = false
  },
)

const summary = computed(() => runSummaryLine(props.run, props.now))
const retries = computed(() => countRetries(props.run))
const hasSteps = computed(() => props.run.steps.length > 0)
</script>

<template>
  <div v-if="hasSteps" class="stream">
    <button
      type="button"
      class="stream-head"
      :class="{ collapsed: !expanded }"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="caret" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
      <span class="head-text">{{ summary }}</span>
      <span v-if="retries > 0" class="head-retry">↻ {{ retries }}</span>
      <span v-if="run.willLoadSchema && run.status === 'running'" class="head-note">
        首次会先加载数据库结构，可能较慢
      </span>
    </button>

    <div v-if="expanded" class="rows">
      <StepRow v-for="step in run.steps" :key="step.id" :step="step" :run="run" :now="now" />
    </div>
  </div>
</template>

<style scoped>
.stream {
  border-left: 1px solid transparent;
}

.stream-head {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  text-align: left;
  padding: 3px 0;
  color: var(--text-muted);
  font-size: var(--text-sm);
  transition: var(--transition);
}

.stream-head:hover {
  color: var(--text-secondary);
}

.stream-head.collapsed {
  color: var(--text-secondary);
}

.caret {
  font-size: 10px;
  color: var(--text-dim);
  width: 12px;
}

.head-text {
  font-variant-numeric: tabular-nums;
}

.head-retry {
  font-size: var(--text-xs);
  color: var(--accent-warm-dim);
}

.head-note {
  font-size: var(--text-xs);
  color: var(--text-dim);
  margin-left: 4px;
}

.rows {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
</style>
