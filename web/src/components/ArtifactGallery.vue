<script setup lang="ts">
import type { Artifact } from '../core/run.ts'

/** `artifact.created` → 图表。`url` 由后端直接给出，前端只负责展示。 */
defineProps<{ artifacts: Artifact[] }>()
</script>

<template>
  <div v-if="artifacts.length" class="artifacts">
    <figure v-for="artifact in artifacts" :key="artifact.id" class="artifact">
      <img
        v-if="artifact.kind === 'chart'"
        :src="artifact.url"
        :alt="artifact.title || '图表'"
        class="artifact-image"
        loading="lazy"
      />
      <a v-else :href="artifact.url" class="artifact-link" target="_blank" rel="noopener noreferrer">
        {{ artifact.title || '下载结果' }}
      </a>
      <figcaption v-if="artifact.title">{{ artifact.title }}</figcaption>
    </figure>
  </div>
</template>

<style scoped>
.artifacts {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 10px;
}

.artifact {
  margin: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  padding: 8px;
  max-width: 100%;
}

.artifact-image {
  display: block;
  max-width: min(520px, 100%);
  border-radius: var(--radius-sm);
  background: #fff;
}

.artifact-link {
  color: var(--accent-cool);
  font-size: var(--text-sm);
}

figcaption {
  margin-top: 6px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
</style>
