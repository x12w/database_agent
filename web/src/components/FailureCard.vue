<script setup lang="ts">
import DisclosureBlock from './DisclosureBlock.vue'
import type { RunFailure } from '../core/run.ts'

/**
 * `run.failed` 的展示：友好文案 + 可重试时的"重试"。
 *
 * `error_detail`（原始报错）永远在折叠里，且后端默认根本不发。
 */
defineProps<{
  failure: RunFailure
  canRetry: boolean
}>()

defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="failure" role="status">
    <div class="failure-row">
      <span class="failure-icon" aria-hidden="true">✕</span>
      <span class="failure-message">{{ failure.message }}</span>
      <button v-if="canRetry && failure.retryable" type="button" class="btn retry" @click="$emit('retry')">
        重试
      </button>
    </div>
    <DisclosureBlock
      v-if="failure.errorDetail"
      class="detail"
      label="查看错误详情"
      tone="error"
      :text="failure.errorDetail"
    />
  </div>
</template>

<style scoped>
.failure {
  border: 1px solid rgba(224, 85, 106, 0.35);
  background: rgba(224, 85, 106, 0.06);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  margin-top: 10px;
}

.failure-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.failure-icon {
  color: var(--accent-red);
  font-size: var(--text-sm);
}

.failure-message {
  font-size: var(--text-sm);
  color: var(--text-primary);
  flex: 1;
}

.retry {
  flex: none;
}
</style>
