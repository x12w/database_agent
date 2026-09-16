<script setup lang="ts">
import { computed } from 'vue'

/**
 * 过程文本（`thought.delta`）的流式区块。
 *
 * - 流式中：暗淡斜体，只显示尾部（长文时不滚动页面，视觉上一直在"走"）；
 * - 结束后：折叠成一行预览，由父级步骤行控制展开。
 *
 * 文本一律走插值（`{{ }}`）—— 旧实现用 `innerHTML += text` 注入未转义的
 * 模型输出（app/static/app.js:684），这里从根上不会重现。
 */
const props = withDefaults(
  defineProps<{
    text: string
    streaming?: boolean
    collapsed?: boolean
    tailChars?: number
  }>(),
  { streaming: false, collapsed: false, tailChars: 320 },
)

const STREAMING_MAX_LINES = 4

const preview = computed(() => {
  const oneLine = props.text.replace(/\s+/g, ' ').trim()
  return oneLine.length > 80 ? `${oneLine.slice(0, 80)}…` : oneLine
})

/** 流式时只保留尾部若干字符，避免长文把视口顶跑。 */
const streamingTail = computed(() => {
  const text = props.text
  if (text.length <= props.tailChars) return text
  return `…${text.slice(-props.tailChars)}`
})

const streamingStyle = computed(() => ({ '--stream-max-lines': String(STREAMING_MAX_LINES) }))
</script>

<template>
  <div v-if="text" class="thought">
    <div v-if="streaming" class="thought-stream" :style="streamingStyle">
      <span class="thought-text">{{ streamingTail }}</span><span class="caret" aria-hidden="true" />
    </div>
    <div v-else-if="collapsed" class="thought-preview">
      <span class="thought-mark" aria-hidden="true">思考</span>
      <span class="thought-text">{{ preview }}</span>
    </div>
    <pre v-else class="thought-full">{{ text }}</pre>
  </div>
</template>

<style scoped>
.thought {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--text-dim);
  line-height: 1.6;
}

.thought-stream {
  max-height: calc(var(--stream-max-lines) * 1.6em);
  overflow: hidden;
  position: relative;
  font-style: italic;
  mask-image: linear-gradient(to bottom, transparent, #000 1.1em);
  -webkit-mask-image: linear-gradient(to bottom, transparent, #000 1.1em);
}

.thought-full {
  font-style: italic;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 22em;
  overflow: auto;
}

.thought-preview {
  display: flex;
  gap: 8px;
  align-items: baseline;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.thought-mark {
  flex: none;
  font-family: var(--font-ui);
  font-size: var(--text-xs);
  letter-spacing: 0.08em;
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 5px;
}

.thought-preview .thought-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.caret {
  display: inline-block;
  width: 6px;
  height: 1em;
  margin-left: 3px;
  vertical-align: text-bottom;
  background: var(--accent-cool);
  opacity: 0.7;
  animation: blink 1.1s steps(2, start) infinite;
}

@keyframes blink {
  0%,
  60% {
    opacity: 0.75;
  }
  61%,
  100% {
    opacity: 0;
  }
}
</style>
