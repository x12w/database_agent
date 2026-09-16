/**
 * 数据库列表 / 连接状态。
 *
 * 顶栏的状态标签（`就绪 · 12 表 · db`）与旧前端行为一致：点击即强制刷新。
 */

import { computed, reactive, type ComputedRef } from 'vue'

import {
  createDatabase,
  deleteDatabase,
  fetchDatabases,
  fetchDatabaseStatus,
  fetchHealth,
  rebuildCache,
  refreshDatabase,
  updateDatabase,
} from '../api/databases.ts'
import type {
  DatabaseProfile,
  DatabaseStatusResponse,
  HealthResponse,
  ProfileInput,
} from '../types/api.ts'

const DB_KEY = 'dbchat.db.v1'

type Tone = 'ok' | 'warn' | 'error' | 'busy'

export interface DatabasesStore {
  state: {
    profiles: DatabaseProfile[]
    currentId: string | null
    health: HealthResponse | null
    status: DatabaseStatusResponse | null
    loading: boolean
    busyLabel: string | null
    statusLabel: string
    statusTone: Tone
    suggestions: string[]
  }
  current: ComputedRef<DatabaseProfile | null>
  llmMode: ComputedRef<string>
  load: () => Promise<void>
  select: (id: string | null) => Promise<void>
  refreshStatus: (force?: boolean) => Promise<void>
  refreshConnection: () => Promise<void>
  rebuild: (sessionId: string | null) => Promise<string>
  save: (input: ProfileInput, id: string | null) => Promise<DatabaseProfile | undefined>
  remove: (id: string) => Promise<void>
}

const FALLBACK_SUGGESTIONS = ['数据库有哪些表？', '统计各部门人数', '有哪些人缺考？']

export function createDatabasesStore(): DatabasesStore {
  let saved: string | null = null
  try {
    saved = localStorage.getItem(DB_KEY)
  } catch {
    saved = null
  }

  const state = reactive({
    profiles: [] as DatabaseProfile[],
    currentId: saved,
    health: null as HealthResponse | null,
    status: null as DatabaseStatusResponse | null,
    loading: false,
    busyLabel: null as string | null,
    statusLabel: '加载中…',
    statusTone: 'busy' as Tone,
    suggestions: [...FALLBACK_SUGGESTIONS],
  })

  const current = computed<DatabaseProfile | null>(
    () => state.profiles.find((p) => p.id === state.currentId) ?? null,
  )
  const llmMode = computed<string>(() => {
    if (!state.health) return '连接中…'
    return state.health.llm_available === false ? '关键词模式' : 'LLM 模式'
  })

  function remember(id: string | null): void {
    try {
      if (id) localStorage.setItem(DB_KEY, id)
      else localStorage.removeItem(DB_KEY)
    } catch {
      /* 忽略 */
    }
  }

  async function load(): Promise<void> {
    state.loading = true
    try {
      const [health, databases] = await Promise.all([
        fetchHealth().catch(() => null),
        fetchDatabases(),
      ])
      state.health = health
      state.profiles = databases.profiles ?? []
      const fromServer = databases.current
      const stillThere = state.profiles.some((p) => p.id === state.currentId)
      state.currentId = stillThere ? state.currentId : fromServer ?? state.profiles[0]?.id ?? null
      remember(state.currentId)
      await refreshStatus(false)
    } catch (error) {
      state.statusTone = 'error'
      state.statusLabel = error instanceof Error ? error.message : '服务未启动'
    } finally {
      state.loading = false
    }
  }

  async function refreshStatus(force = false): Promise<void> {
    if (!state.currentId) {
      state.statusTone = 'warn'
      state.statusLabel = '未选择数据库'
      state.suggestions = [...FALLBACK_SUGGESTIONS]
      return
    }
    state.statusTone = 'busy'
    state.statusLabel = force ? '刷新中…' : '连接中…'
    try {
      const status = await fetchDatabaseStatus(state.currentId, force)
      state.status = status
      const tables = status.table_count ?? status.tables?.length ?? 0
      state.statusLabel = `就绪 · ${tables || '?'} 表${status.name ? ` · ${status.name}` : ''}`
      state.statusTone = 'ok'
      const suggestions = status.suggestions?.filter((s) => s.trim().length > 0) ?? []
      state.suggestions = suggestions.length > 0 ? suggestions.slice(0, 5) : [...FALLBACK_SUGGESTIONS]
      if (force) state.health = await fetchHealth().catch(() => state.health)
    } catch {
      state.statusTone = 'warn'
      state.statusLabel = '数据库未连接'
      state.suggestions = [...FALLBACK_SUGGESTIONS]
    }
  }

  async function refreshConnection(): Promise<void> {
    if (!state.currentId) return refreshStatus(false)
    state.busyLabel = '刷新连接…'
    state.statusTone = 'busy'
    state.statusLabel = '刷新中…'
    try {
      const status = await refreshDatabase(state.currentId)
      state.status = { ...state.status, ...status }
      state.statusTone = 'ok'
      state.statusLabel = `就绪 · ${status.table_count ?? '?'} 表${status.name ? ` · ${status.name}` : ''}`
    } catch (error) {
      state.statusTone = 'error'
      state.statusLabel = error instanceof Error ? error.message : '刷新失败'
    } finally {
      state.busyLabel = null
    }
  }

  async function rebuild(sessionId: string | null): Promise<string> {
    if (!state.currentId) throw new Error('请先选择数据库')
    state.busyLabel = '重建缓存…'
    state.statusLabel = '重建中…'
    state.statusTone = 'busy'
    try {
      const result = await rebuildCache(state.currentId, sessionId ?? undefined)
      state.statusLabel = `就绪 · ${result.table_count ?? '?'} 表`
      state.statusTone = 'ok'
      state.health = await fetchHealth().catch(() => state.health)
      return result.message ?? '缓存已重建'
    } finally {
      state.busyLabel = null
    }
  }

  async function save(input: ProfileInput, id: string | null): Promise<DatabaseProfile | undefined> {
    const result = id ? await updateDatabase(id, input) : await createDatabase(input)
    await load()
    return result.profile
  }

  async function remove(id: string): Promise<void> {
    await deleteDatabase(id)
    if (state.currentId === id) state.currentId = null
    await load()
  }

  return {
    state,
    current,
    llmMode,
    load,
    select: async (id) => {
      state.currentId = id
      remember(id)
      await refreshStatus(false)
    },
    refreshStatus,
    refreshConnection,
    rebuild,
    save,
    remove,
  }
}

export const databasesStore = createDatabasesStore()

export type { ProfileInput }
