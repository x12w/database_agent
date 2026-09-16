<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import DatabaseManagerModal from '../components/DatabaseManagerModal.vue'
import RunView from '../components/RunView.vue'
import SessionSidebar from '../components/SessionSidebar.vue'
import TableFilterModal from '../components/TableFilterModal.vue'
import TopBar from '../components/TopBar.vue'
import { useTicker } from '../composables/useTicker.ts'
import { chatStore, loadTableFilter } from '../stores/chat.ts'
import { databasesStore } from '../stores/databases.ts'

const MAX_LENGTH = 4000

const state = chatStore.state
const turns = chatStore.activeTurns
const dbState = databasesStore.state

const draft = ref('')
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const showFilter = ref(false)
const showManager = ref(false)
const notice = ref<string | null>(null)

/** 只在有运行中的步骤时走动，空闲页面不做无谓重渲染。 */
const now = useTicker(() => state.streaming)

const suggestions = computed(() => dbState.suggestions.slice(0, 4))
const canSend = computed(() => draft.value.trim().length > 0 && !state.streaming)

onMounted(async () => {
  if (dbState.profiles.length === 0) await databasesStore.load()
  if (dbState.currentId && state.dbId !== dbState.currentId) {
    chatStore.state.dbId = dbState.currentId
    chatStore.state.tableFilter = loadTableFilter(dbState.currentId)
  }
  await scrollToBottom()
})

watch(
  () => turns.value.length,
  () => void scrollToBottom(),
)

watch(
  () => state.streaming,
  () => void scrollToBottom(),
)

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

async function submit(): Promise<void> {
  const text = draft.value.trim()
  if (!text || state.streaming) return
  draft.value = ''
  autoGrow()
  await chatStore.send(text)
  await scrollToBottom()
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void submit()
  }
}

function autoGrow(): void {
  const el = input.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 132)}px`
}

async function useSuggestion(text: string): Promise<void> {
  draft.value = text
  await submit()
}

/** 切换数据库：连接状态跟着换，会话重置（旧行为）。 */
async function onDbSelect(id: string): Promise<void> {
  const next = id || null
  await databasesStore.select(next)
  chatStore.setDb(next)
}

/** 管理面板里改完配置后重新读一遍列表。 */
async function onManagerChanged(): Promise<void> {
  await databasesStore.load()
  if (dbState.currentId !== state.dbId) chatStore.setDb(dbState.currentId)
}

async function rebuild(): Promise<void> {
  try {
    notice.value = await databasesStore.rebuild(chatStore.activeSession.value.id)
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '重建失败'
  }
}

function onFilterSaved(message: string): void {
  notice.value = message
}

const filterSessionId = computed(() => chatStore.activeSession.value.id)
</script>

<template>
  <div class="app-shell">
    <TopBar
      @new-chat="chatStore.newChat()"
      @open-filter="showFilter = true"
      @open-manager="showManager = true"
      @rebuild="rebuild"
      @select="onDbSelect"
    />

    <div class="body">
      <SessionSidebar />

      <main class="chat">
        <div ref="scroller" class="messages">
          <div v-if="turns.length === 0" class="welcome">
            <h2>用自然语言提问，我来查库回答</h2>
            <p class="muted">
              我会一边查一边把过程显示出来：查了哪些表、返回了多少行、花了多久。
            </p>
            <div class="examples">
              <span class="muted">示例</span>
              <button
                v-for="suggestion in suggestions"
                :key="suggestion"
                type="button"
                class="example"
                @click="useSuggestion(suggestion)"
              >
                {{ suggestion }}
              </button>
            </div>
            <p v-if="dbState.currentId === null" class="hint">先在上方选择一个数据库。</p>
          </div>

          <RunView
            v-for="turn in turns"
            :key="turn.id"
            :text="turn.text"
            :run="turn.run"
            :now="now"
            :can-retry="!state.streaming"
            @retry="chatStore.retry(turn.id)"
          />
        </div>

        <p v-if="notice" class="notice" @click="notice = null">{{ notice }}</p>

        <div class="composer">
          <div class="input-row">
            <textarea
              ref="input"
              v-model="draft"
              rows="1"
              :maxlength="MAX_LENGTH"
              placeholder="输入问题，例如：有哪些人缺考？"
              @input="autoGrow"
              @keydown="onKeydown"
            />
            <button v-if="state.streaming" type="button" class="stop" title="停止" @click="chatStore.stop()">
              ■
            </button>
            <button v-else type="button" class="send" :disabled="!canSend" title="发送" @click="submit">
              发送
            </button>
          </div>
          <div class="composer-foot">
            <span class="mono muted">{{ draft.length }} / {{ MAX_LENGTH }}</span>
            <span v-if="state.lastError" class="error">{{ state.lastError }}</span>
            <span class="muted">{{ databasesStore.llmMode }}</span>
          </div>
        </div>
      </main>
    </div>

    <TableFilterModal
      v-if="showFilter"
      :db-id="dbState.currentId"
      :session-id="filterSessionId"
      @close="showFilter = false"
      @saved="onFilterSaved"
    />

    <DatabaseManagerModal v-if="showManager" @close="showManager = false" @changed="onManagerChanged" />
  </div>
</template>

<style scoped>
.app-shell {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.chat {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 8px 28px 0;
  scroll-behavior: smooth;
}

.welcome {
  max-width: 620px;
  margin: 12vh auto 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.welcome h2 {
  font-size: var(--text-xl);
  font-weight: 600;
}

.welcome p {
  font-size: var(--text-sm);
}

.examples {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: var(--text-xs);
  margin-top: 6px;
}

.example {
  border: 1px solid var(--border-light);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: var(--text-sm);
  color: var(--text-secondary);
  transition: var(--transition);
}

.example:hover {
  border-color: var(--accent-cool-dim);
  color: var(--text-primary);
}

.hint {
  color: var(--accent-warm-dim);
}

.notice {
  margin: 0 28px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  cursor: pointer;
}

.composer {
  border-top: 1px solid var(--border);
  padding: 12px 28px 14px;
  background: var(--bg-primary);
}

.input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

textarea {
  flex: 1;
  resize: none;
  max-height: 132px;
  padding: 10px 12px;
  line-height: 1.6;
  font-size: var(--text-base);
}

.send,
.stop {
  flex: none;
  padding: 9px 18px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-light);
  font-size: var(--text-sm);
  transition: var(--transition);
}

.send {
  color: var(--accent-warm);
  border-color: var(--accent-warm-dim);
}

.send:hover:not(:disabled) {
  background: rgba(229, 181, 103, 0.1);
}

.stop {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.4);
}

.composer-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 6px;
  font-size: var(--text-xs);
  color: var(--text-dim);
}

.composer-foot .muted:last-child {
  margin-left: auto;
}

.error {
  color: var(--accent-red);
}
</style>
