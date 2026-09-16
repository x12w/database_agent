<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'

import ExploreStage from './ExploreStage.vue'
import { exploreDatabase } from '../api/admin.ts'
import {
  applyExploreEvent,
  createExploreState,
  exploreStatusLabel,
  markEntryResolved,
  resetExploreProgress,
} from '../core/explore.ts'
import { databasesStore } from '../stores/databases.ts'

/**
 * 主动探索数据库：一条独立 SSE 通道。
 *
 * 事件不再是旧实现的 `progress` / `admin_question` / `done` / `explore_error`，
 * 而是契约里的 `explore.*`（`src/dbchat/events/explore.py`）。归约全在
 * `core/explore.ts`（纯函数），这里只负责连接、中止与把状态交给展示层。
 */
const emit = defineEmits<{ questionsChanged: [] }>()

const props = defineProps<{ profileId: string | null }>()

const state = reactive(createExploreState())
const running = ref(false)
const transportError = ref<string | null>(null)
/** 本次探索针对的库。卡片上的回答必须回给**发起探索时**的那个库。 */
const sessionProfileId = ref<string | null>(null)

let controller: AbortController | null = null

const canStart = computed(() => props.profileId !== null && !running.value)
const statusLabel = computed(() => exploreStatusLabel(state))

function start(): void {
  if (!props.profileId) return
  running.value = true
  transportError.value = null
  sessionProfileId.value = props.profileId
  // 只重置进度：上一轮发现的卡片留在原地，重新探索不该清空管理员刚看过的东西。
  resetExploreProgress(state)

  const ctrl = new AbortController()
  controller = ctrl

  void exploreDatabase(
    props.profileId,
    null,
    {
      onEvent: (event) => {
        applyExploreEvent(state, event)
        if (event.type === 'explore.finished' || event.type === 'explore.failed') {
          emit('questionsChanged')
        }
      },
      // 传输层中断（连接掉了）。业务失败走 explore.failed 事件，不在这里。
      onError: (error) => {
        if (ctrl.signal.aborted) return
        transportError.value = error instanceof Error ? error.message : '探索连接中断'
      },
      onClose: () => {
        if (controller !== ctrl) return // 已经开始了新的一轮，旧的收尾不要插进来
        running.value = false
        controller = null
        if (state.status === 'running') {
          // 流结束了却没有 explore.finished / explore.failed：不能留一个永远转圈的进度条。
          if (transportError.value) {
            state.status = 'failed'
            state.failure = {
              code: 'internal',
              message: '探索连接提前结束，本次探索没有跑完。',
              errorDetail: transportError.value,
            }
          } else {
            state.status = 'stopped'
          }
          emit('questionsChanged')
        }
        void databasesStore.refreshStatus(false)
      },
    },
    ctrl.signal,
  )
}

function stop(): void {
  const ctrl = controller
  running.value = false
  state.status = 'stopped'
  controller = null
  ctrl?.abort()
  emit('questionsChanged')
}

// 换库时不能继续跑：卡片已经带着旧库的上下文，而面板已经是新库的了。
watch(
  () => props.profileId,
  () => {
    if (running.value) stop()
  },
)

// 离开管理页时把流关掉，不留一条没人收的 SSE 连接（也不重连 —— 自己解析分帧的
// 好处就在这里：断开就是断开）。
onBeforeUnmount(() => {
  const ctrl = controller
  controller = null
  ctrl?.abort()
})

function onAnswered(questionId: string, answer: string): void {
  markEntryResolved(state, questionId, answer)
  emit('questionsChanged')
}

function onIgnored(questionId: string): void {
  markEntryResolved(state, questionId, null)
  emit('questionsChanged')
}
</script>

<template>
  <section class="explore">
    <p class="intro muted">
      扫描所有表，识别编码/缩写列，并就其业务含义向你提问。回答后的知识会用于后续查询。
    </p>

    <div class="controls">
      <button v-if="!running" type="button" class="btn btn-primary" :disabled="!canStart" @click="start">
        开始探索
      </button>
      <button v-else type="button" class="btn danger" @click="stop">停止探索</button>
      <span v-if="state.status !== 'idle'" class="state mono">{{ statusLabel }}</span>
    </div>

    <p v-if="transportError" class="error">{{ transportError }}</p>

    <ExploreStage
      :state="state"
      :profile-id="sessionProfileId"
      @answered="onAnswered"
      @ignored="onIgnored"
    />
  </section>
</template>

<style scoped>
.explore {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 820px;
}

.intro {
  font-size: var(--text-sm);
}

.controls {
  display: flex;
  align-items: center;
  gap: 10px;
}

.danger {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.35);
}

.state {
  font-size: var(--text-xs);
  color: var(--text-dim);
}

.error {
  font-size: var(--text-sm);
  color: var(--accent-red);
}
</style>
