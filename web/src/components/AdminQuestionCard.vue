<script setup lang="ts">
import { ref } from 'vue'

import { answerAdminQuestion, ignoreAdminQuestion } from '../api/admin.ts'
import type { AdminQuestion } from '../types/api.ts'

/**
 * 管理员问答卡片。三类"回答"动作的语义与旧实现一致：
 * 回答 / 同意推测（可空，落到一句默认同意）/ 补充说明（必填），外加忽略。
 */
const props = withDefaults(
  defineProps<{
    question: AdminQuestion
    profileId: string | null
    pending?: boolean
    /** 探索流里新发现的问题：卡片会带上 agent 的推测并预填。 */
    guess?: string | null
    fresh?: boolean
  }>(),
  { pending: true, guess: null, fresh: false },
)

const emit = defineEmits<{ done: []; ignored: [] }>()

const draft = ref(props.guess ?? '')
const placeholder = ref('输入列的含义，例如：01=在职, 02=离职…')
const busy = ref(false)
const error = ref<string | null>(null)

async function act(action: 'answer' | 'agree' | 'supplement' | 'ignore'): Promise<void> {
  if (busy.value) return
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
    emit('done')
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

    <template v-if="pending">
      <p v-if="guess" class="guess">推测：{{ guess }}</p>
      <textarea v-model="draft" rows="2" :placeholder="placeholder" />
      <div class="actions">
        <button type="button" class="btn btn-primary" :disabled="busy" @click="act('answer')">回答</button>
        <button type="button" class="btn" :disabled="busy" @click="act('agree')">同意推测</button>
        <button type="button" class="btn" :disabled="busy" @click="act('supplement')">补充说明</button>
        <button type="button" class="btn danger" :disabled="busy" @click="act('ignore')">忽略</button>
        <span v-if="error" class="error">{{ error }}</span>
      </div>
    </template>

    <template v-else>
      <div class="answered">{{ question.answer }}</div>
      <p class="meta">回答于 {{ question.answered_at }}</p>
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
