/**
 * 聊天会话状态：会话列表、当前运行、表筛选、SSE 生命周期。
 *
 * 归约逻辑全在 `src/core/run.ts`（纯函数），这里只负责把事件喂进去、
 * 管理连接与持久化。
 */

import { computed, reactive, type ComputedRef } from 'vue'

import { streamChat } from '../api/chat.ts'
import {
  applyEvent,
  createRun,
  hydrateRun,
  serializeRun,
  type RunState,
  type StoredRun,
} from '../core/run.ts'
import type { ChatEvent } from '../types/events.ts'

const SESSIONS_KEY = 'dbchat.sessions.v1'
const TABLE_FILTER_PREFIX = 'tf_'
const MAX_SESSIONS = 30
const MAX_TURNS = 40
/** 多久没有任何事件（含心跳）就认为连接已死。后端心跳 15s 一次。 */
const SILENCE_TIMEOUT_MS = 180_000

export interface ChatTurn {
  id: string
  text: string
  at: number
  run: RunState
}

export interface ChatSession {
  id: string
  title: string
  dbId: string | null
  createdAt: number
  updatedAt: number
  turns: ChatTurn[]
}

interface StoredTurn {
  id: string
  text: string
  at: number
  run: StoredRun
}

interface StoredSession {
  id: string
  title: string
  dbId: string | null
  createdAt: number
  updatedAt: number
  turns: StoredTurn[]
}

export function newId(): string {
  const cryptoObj = globalThis.crypto
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID().replace(/-/g, '').slice(0, 12)
  }
  return Math.random().toString(36).slice(2, 14)
}

function readJson<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// 表筛选（localStorage 键沿用旧前端：`tf_{dbId}`）
// ---------------------------------------------------------------------------

export function loadTableFilter(dbId: string | null): string[] | null {
  if (!dbId) return null
  const stored = readJson<string[]>(`${TABLE_FILTER_PREFIX}${dbId}`)
  return Array.isArray(stored) && stored.length > 0 ? stored : null
}

export function persistTableFilter(dbId: string | null, tables: string[] | null): void {
  if (!dbId) return
  try {
    if (tables && tables.length > 0) {
      localStorage.setItem(`${TABLE_FILTER_PREFIX}${dbId}`, JSON.stringify(tables))
    } else {
      localStorage.removeItem(`${TABLE_FILTER_PREFIX}${dbId}`)
    }
  } catch {
    /* 隐私模式写不进去也不算致命 */
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export interface ChatStore {
  state: {
    sessions: ChatSession[]
    activeSessionId: string
    streaming: boolean
    dbId: string | null
    tableFilter: string[] | null
    lastError: string | null
  }
  activeSession: ComputedRef<ChatSession>
  activeTurns: ComputedRef<ChatTurn[]>
  send: (text: string) => Promise<void>
  stop: () => void
  retry: (turnId: string) => Promise<void>
  newChat: () => void
  selectSession: (id: string) => void
  deleteSession: (id: string) => void
  setDb: (dbId: string | null) => void
  setTableFilter: (tables: string[] | null) => void
  /** 把外部（如开发夹具）构造好的运行直接塞进当前会话，用于无后端预览。 */
  attachRun: (text: string, run: RunState) => ChatTurn
  persist: () => void
}

function createSession(dbId: string | null, title = '新会话'): ChatSession {
  const now = Date.now()
  return { id: newId(), title, dbId, createdAt: now, updatedAt: now, turns: [] }
}

export interface ChatStoreOptions {
  /** 开发夹具用 `persist: false`，避免污染真实会话历史。 */
  persist?: boolean
  /** 初始会话列表为空时是否自动建一个（夹具不需要）。 */
  seedSession?: boolean
}

export function createChatStore(options: ChatStoreOptions = {}): ChatStore {
  const persistEnabled = options.persist !== false
  const stored = persistEnabled ? readJson<StoredSession[]>(SESSIONS_KEY) ?? [] : []

  const state = reactive({
    sessions: stored.map((s) => ({
      id: s.id,
      title: s.title,
      dbId: s.dbId,
      createdAt: s.createdAt,
      updatedAt: s.updatedAt,
      turns: s.turns.map((t) => ({
        id: t.id,
        text: t.text,
        at: t.at,
        run: hydrateRun(t.run),
      })),
    })) as ChatSession[],
    activeSessionId: '',
    streaming: false,
    dbId: null as string | null,
    tableFilter: null as string[] | null,
    lastError: null as string | null,
  })

  if (state.sessions.length === 0) state.sessions.push(createSession(null))
  state.activeSessionId = state.sessions[0]!.id

  const activeSession = computed<ChatSession>(() => {
    return state.sessions.find((s) => s.id === state.activeSessionId) ?? state.sessions[0]!
  })
  const activeTurns = computed<ChatTurn[]>(() => activeSession.value.turns)

  let controller: AbortController | null = null

  function persist(): void {
    if (!persistEnabled) return
    const payload: StoredSession[] = state.sessions.slice(0, MAX_SESSIONS).map((s) => ({
      id: s.id,
      title: s.title,
      dbId: s.dbId,
      createdAt: s.createdAt,
      updatedAt: s.updatedAt,
      turns: s.turns.slice(-MAX_TURNS).map((t) => ({
        id: t.id,
        text: t.text,
        at: t.at,
        run: serializeRun(t.run),
      })),
    }))
    try {
      localStorage.setItem(SESSIONS_KEY, JSON.stringify(payload))
    } catch {
      /* 存储配额满时静默降级为"不持久化" */
    }
  }

  /**
   * 新建一轮并把 `run` 交给流处理。
   *
   * **必须返回响应式代理，不能返回原始对象。** 数组塞进 `reactive` 之后，
   * 通过原始引用修改嵌套字段不会触发重渲染 —— 步骤流因此一直是空的、
   * 状态永远停在"运行中"。这个坑很难从类型上发现：`ChatTurn` 的类型
   * 对代理和原始对象是一样的。
   */
  function attachRun(text: string, run: RunState): ChatTurn {
    const session = activeSession.value
    const turn = reactive<ChatTurn>({ id: newId(), text, at: Date.now(), run })
    session.turns.push(turn)
    session.updatedAt = Date.now()
    if (session.title === '新会话' || session.turns.length === 1) {
      session.title = text.slice(0, 24)
    }
    return turn
  }

  async function runStream(turn: ChatTurn, text: string): Promise<void> {
    const session = activeSession.value
    state.streaming = true
    state.lastError = null
    const requestController = new AbortController()
    controller = requestController

    let silenceTimer: ReturnType<typeof setTimeout> | null = null
    const armSilence = () => {
      if (controller !== requestController || requestController.signal.aborted) return
      if (silenceTimer) clearTimeout(silenceTimer)
      silenceTimer = setTimeout(() => {
        if (controller !== requestController || requestController.signal.aborted) return
        state.lastError = '长时间没有收到进展，已停止等待。'
        requestController.abort()
      }, SILENCE_TIMEOUT_MS)
    }
    const disarmSilence = () => {
      if (silenceTimer) clearTimeout(silenceTimer)
      silenceTimer = null
    }

    armSilence()
    try {
      await streamChat(
        {
          message: text,
          sessionId: session.id,
          dbId: state.dbId,
          tableFilter: state.tableFilter,
        },
        {
          onEvent: (event: ChatEvent) => {
            if (controller !== requestController || requestController.signal.aborted) return
            armSilence()
            applyEvent(turn.run, event)
          },
          onHeartbeat: armSilence,
          onError: (error) => {
            if (controller !== requestController) return
            const detail = error instanceof Error ? error.message : String(error)
            state.lastError = detail
            if (turn.run.status === 'running') {
              turn.run.status = 'failed'
              turn.run.failure = {
                code: 'internal',
                message: '连接中断，本次回答没有完成。',
                retryable: true,
                errorDetail: detail,
              }
            }
          },
        },
        requestController.signal,
      )
    } finally {
      disarmSilence()
      const aborted = requestController.signal.aborted
      if (controller === requestController) {
        state.streaming = false
        controller = null
      }
      turn.run.localLastEventAt = 0
      if (turn.run.status === 'running') {
        // 流结束了却没有 run.finished / run.failed：补一条可重试的收尾，
        // 否则步骤行会永远转圈。
        turn.run.status = 'failed'
        turn.run.failure = turn.run.failure ?? {
          code: 'internal',
          message: aborted ? '已停止本次回答。' : '连接提前结束，本次回答没有完成。',
          retryable: true,
          errorDetail: null,
        }
        for (const step of turn.run.steps) {
          if (step.status === 'running') {
            step.status = 'failed'
            step.elapsedMs = step.elapsedMs ?? 0
          }
        }
      }
      persist()
    }
  }

  async function send(text: string): Promise<void> {
    const trimmed = text.trim()
    if (!trimmed || state.streaming) return
    const turn = attachRun(trimmed, createRun())
    await runStream(turn, trimmed)
  }

  async function retry(turnId: string): Promise<void> {
    if (state.streaming) return
    const turn = activeSession.value.turns.find((t) => t.id === turnId)
    if (!turn) return
    turn.run = createRun()
    await runStream(turn, turn.text)
  }

  function stop(): void {
    controller?.abort()
    controller = null
    state.streaming = false
  }

  function newChat(): void {
    stop()
    const session = createSession(state.dbId)
    state.sessions.unshift(session)
    state.activeSessionId = session.id
    state.lastError = null
    persist()
  }

  function selectSession(id: string): void {
    if (id === state.activeSessionId) return
    stop()
    state.activeSessionId = id
    const session = state.sessions.find((s) => s.id === id)
    if (session && session.dbId && session.dbId !== state.dbId) {
      state.dbId = session.dbId
      state.tableFilter = loadTableFilter(session.dbId)
    }
  }

  function deleteSession(id: string): void {
    const index = state.sessions.findIndex((s) => s.id === id)
    if (index === -1) return
    state.sessions.splice(index, 1)
    if (state.sessions.length === 0) state.sessions.push(createSession(state.dbId))
    if (state.activeSessionId === id) {
      stop()
      state.activeSessionId = state.sessions[0]!.id
    }
    persist()
  }

  /** 切换数据库：会话重置（表筛选与历史都随库变化）。 */
  function setDb(dbId: string | null): void {
    if (dbId === state.dbId) return
    stop()
    state.dbId = dbId
    state.tableFilter = loadTableFilter(dbId)
    const session = createSession(dbId)
    session.dbId = dbId
    state.sessions.unshift(session)
    state.activeSessionId = session.id
    persist()
  }

  function setTableFilter(tables: string[] | null): void {
    const normalized = tables && tables.length > 0 ? [...tables] : null
    state.tableFilter = normalized
    persistTableFilter(state.dbId, normalized)
    const session = activeSession.value
    session.dbId = state.dbId
    persist()
  }

  return {
    state,
    activeSession,
    activeTurns,
    send,
    stop,
    retry,
    newChat,
    selectSession,
    deleteSession,
    setDb,
    setTableFilter,
    attachRun,
    persist,
  }
}

/** 全局单例（`App.vue` 用 provide 注入，测试/夹具可以另建一个）。 */
export const chatStore = createChatStore()
