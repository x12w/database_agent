<script setup lang="ts">
import type { QuestionAskedEvent } from '../types/events.ts'

/**
 * `question.asked` 的内联卡片。
 *
 * 看到这张卡片的是**普通用户**，不是管理员：只说明"agent 在等一个确认"，
 * 不暴露内部流程，也不承诺谁去回答。标题直接用后端给的 `label`。
 */
defineProps<{ question: QuestionAskedEvent }>()
</script>

<template>
  <div class="ask">
    <div class="ask-head">
      <span class="ask-dot" aria-hidden="true" />
      <span class="ask-label">{{ question.label }}</span>
    </div>
    <div class="ask-target">
      <span class="mono">{{ question.table }}</span>
      <span class="sep" aria-hidden="true">·</span>
      <span class="mono">{{ question.column }}</span>
    </div>
    <div v-if="question.samples.length" class="ask-samples">
      <span class="ask-samples-label">出现过的取值</span>
      <span v-for="sample in question.samples.slice(0, 12)" :key="sample" class="chip">{{
        sample
      }}</span>
    </div>
    <p class="ask-hint">正在等待确认，确认后会自动继续。</p>
  </div>
</template>

<style scoped>
.ask {
  border: 1px solid rgba(229, 181, 103, 0.35);
  background: rgba(229, 181, 103, 0.06);
  border-radius: var(--radius-md);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ask-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-sm);
  color: var(--accent-warm);
}

.ask-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-warm);
  flex: none;
  animation: pulse 1.6s ease-in-out infinite;
}

.ask-target {
  display: flex;
  gap: 8px;
  font-size: var(--text-sm);
  color: var(--text-primary);
}

.sep {
  color: var(--text-dim);
}

.ask-samples {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.ask-samples-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-right: 2px;
}

.ask-hint {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
</style>
