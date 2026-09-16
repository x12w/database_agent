<script setup lang="ts">
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed } from 'vue'

/**
 * markdown 渲染。
 *
 * 只在**回答流结束之后**使用（流式过程中一律走插值），并且渲染结果必须
 * 过一遍 DOMPurify —— 模型输出是外部内容，`v-html` 不能直接吃。
 * marked 与 dompurify 都是本地 npm 依赖，不依赖 CDN。
 */
const props = defineProps<{ text: string }>()

marked.setOptions({ gfm: true, breaks: true })

const html = computed(() => {
  const raw = marked.parse(props.text, { async: false }) as string
  return DOMPurify.sanitize(raw, { ADD_ATTR: ['target', 'rel'] })
})
</script>

<template>
  <div class="markdown" v-html="html" />
</template>
