<script setup lang="ts">
import { ref } from 'vue'

/**
 * 默认收起的内容：SQL 预览、原始报错。
 *
 * 这两个字段后端默认**根本不会发**（`sql_preview` 只在允许展示时才有值）；
 * 万一发了，也必须由用户点开才可见 —— 这是"不暴露多余信息"的落实点。
 */
defineProps<{
  label: string
  text: string
  tone?: 'sql' | 'error'
}>()

const open = ref(false)
</script>

<template>
  <div class="disclosure">
    <button
      type="button"
      class="disclosure-toggle"
      :aria-expanded="open"
      @click.stop="open = !open"
    >
      <span class="caret" :class="{ open }" aria-hidden="true">▸</span>
      {{ label }}
    </button>
    <pre v-if="open" class="disclosure-body" :data-tone="tone ?? 'sql'">{{ text }}</pre>
  </div>
</template>

<style scoped>
.disclosure {
  margin-top: 6px;
}

.disclosure-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 10px;
  transition: var(--transition);
}

.disclosure-toggle:hover {
  color: var(--text-secondary);
  border-color: var(--border-light);
}

.caret {
  display: inline-block;
  transition: transform 120ms ease;
}

.caret.open {
  transform: rotate(90deg);
}

.disclosure-body {
  margin-top: 6px;
  padding: 10px 12px;
  background: var(--bg-code);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 18em;
  overflow: auto;
  color: var(--text-secondary);
}

.disclosure-body[data-tone='error'] {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.35);
}
</style>
