<script setup lang="ts">
import { computed, ref } from 'vue'

import { answerAdminQuestion, ignoreAdminQuestion } from '../api/admin.ts'
import type { AdminQuestion } from '../types/api.ts'

/**
 * 管理员问答卡片。三类"回答"动作的语义与旧实现一致：
 * 回答 / 同意推测（可空，落到一句默认同意）/ 补充说明（必填），外加忽略。
 *
 * 两个来源共用它：待回答列表（HTTP）与探索流（SSE 契约事件）。探索流的卡片
 * 还会带上判断依据 —— agent 的推测、该列的样例行上下文。
 */
const props = withDefaults(
  defineProps<{
    question: AdminQuestion
    profileId: string | null
    pending?: boolean
    /** 探索流里新发现的问题：卡片会带上 agent 的推测并预填。 */
    guess?: string | null
    fresh?: boolean
    /** 该列的样例行上下文：列名 + 若干行的相关取值。 */
    contextColumns?: string[]
    contextRows?: string[][]
    /** 取代"回答于 xxx"的一行说明（去重命中等场景）。 */
    note?: string | null
    /** 只读展示（开发夹具里用来演示卡片的样子，不真的提交）。 */
    readonly?: boolean
  }>(),
  {
    pending: true,
    guess: null,
    fresh: false,
    contextColumns: () => [],
    contextRows: () => [],
    note: null,
    readonly: false,
  },
)

const emit = defineEmits<{
  /** 参数是最终提交的答案文本（"同意推测"用的是回落的那句默认文案）。 */
  done: [answer: string]
  ignored: []
}>()

const draft = ref(props.guess ?? '')
const placeholder = ref('输入列的含义，例如：01=在职, 02=离职…')
const busy = ref(false)
const error = ref<string | null>(null)

const locked = computed(() => props.readonly || busy.value)
const hasContext = computed(() => props.contextColumns.length > 0 && props.contextRows.length > 0)

async function act(action: 'answer' | 'agree' | 'supplement' | 'ignore'): Promise<void> {
  if (locked.value) return
  error.value = null
  if (action === 'ignore') {
    busy.value = true
    try {
      await ignoreAdminQuestion(props.question.question_id, props.profileId)
      emit('ignored')
    } catch (err) {
      error.value = err instanceof Error ? err.message : '忽略失败'
    } finally {
      busy.value = false
    }
    return
  }

  let answer = draft.value.trim()
  if (action === 'agree' && !answer) answer = '同意推测，列含义如样本所示'
  if (!answer) {
    placeholder.value = action === 'supplement' ? '请输入补充说明…' : '请输入答案…'
    return
  }

  busy.value = true
  try {
    await answerAdminQuestion(props.question.question_id, answer, props.profileId)
    emit('done', answer)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card" :class="{ fresh }">
    <div class="card-head">
      <span class="target mono">
        <span v-if="fresh" class="new">新发现</span>
        {{ question.table_name }}<template v-if="question.column_name">.{{ question.column_name }}</template>
      </span>
      <span class="meta mono">{{ question.created_at }}</span>
    </div>

    <p class="question">{{ question.question }}</p>

    <div v-if="question.sample_values?.length" class="samples">
      <span class="samples-label">样本值</span>
      <span class="samples-values mono">{{ question.sample_values.slice(0, 10).join(', ') }}</span>
    </div>

    <!-- 判断依据：该列在表中的样例行。管理员是照这几行做判断的。 -->
    <div v-if="hasContext" class="context">
      <span class="samples-label">上下文</span>
      <div class="context-scroll">
        <table class="mono">
          <thead>
            <tr>
              <th v-for="column in contextColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in contextRows" :key="index">
              <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <template v-if="pending">
      <p v-if="guess" class="guess">推测：{{ guess }}</p>
      <textarea v-model="draft" rows="2" :placeholder="placeholder" :disabled="locked" />
      <div class="actions">
        <button type="button" class="btn btn-primary" :disabled="locked" @click="act('answer')">回答</button>
        <button type="button" class="btn" :disabled="locked" @click="act('agree')">同意推测</button>
        <button type="button" class="btn" :disabled="locked" @click="act('supplement')">补充说明</button>
        <button type="button" class="btn danger" :disabled="locked" @click="act('ignore')">忽略</button>
        <span v-if="error" class="error">{{ error }}</span>
      </div>
    </template>

    <template v-else>
      <div class="answered">{{ question.answer }}</div>
      <p class="meta">{{ note ?? `回答于 ${question.answered_at}` }}</p>
    </template>
  </div>
</template>

<style scoped>
.card {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.card.fresh {
  border-color: rgba(229, 181, 103, 0.4);
}

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.target {
  font-size: var(--text-sm);
  color: var(--text-primary);
}

.new {
  font-family: var(--font-ui);
  font-size: var(--text-xs);
  color: var(--accent-warm);
  border: 1px solid rgba(229, 181, 103, 0.4);
  border-radius: 4px;
  padding: 0 5px;
  margin-right: 6px;
}

.meta {
  font-size: var(--text-xs);
  color: var(--text-dim);
}

.question {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.samples {
  display: flex;
  gap: 8px;
  font-size: var(--text-xs);
  align-items: baseline;
}

.samples-label {
  color: var(--text-muted);
  flex: none;
}

.samples-values {
  color: var(--text-secondary);
  word-break: break-word;
}

.context {
  display: flex;
  gap: 8px;
  font-size: var(--text-xs);
  align-items: flex-start;
}

.context-scroll {
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-code);
  max-height: 11em;
}

.context-scroll table {
  border-collapse: collapse;
  font-size: var(--text-xs);
  min-width: 100%;
}

.context-scroll th,
.context-scroll td {
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  padding: 2px 8px;
  text-align: left;
  white-space: nowrap;
}

.context-scroll th {
  color: var(--text-muted);
  font-weight: 500;
  position: sticky;
  top: 0;
  background: var(--bg-elevated);
}

.context-scroll tbody tr:last-child td {
  border-bottom: none;
}

.guess {
  font-size: var(--text-xs);
  color: var(--accent-cool);
}

textarea {
  font-size: var(--text-sm);
  resize: vertical;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.danger {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.35);
}

.error {
  color: var(--accent-red);
  font-size: var(--text-xs);
}

.answered {
  font-size: var(--text-sm);
  color: var(--text-primary);
  border-left: 2px solid var(--accent-green);
  padding-left: 10px;
  white-space: pre-wrap;
}
</style>
