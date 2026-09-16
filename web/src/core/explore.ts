/**
 * 探索事件 → 管理面板状态 的归约逻辑。
 *
 * 与 `core/run.ts` 同构：这个文件**不 import 任何 Vue**，是一个纯函数式
 * reducer，Node 的内置测试运行器可以直接跑（`node --test test/`）。
 *
 * 与聊天通道的关键差别：这里的读者是管理员，所以表名、列名、样本值、
 * 上下文与 agent 的推测**都要留下来**给卡片渲染 —— 那不是泄漏，是契约要求。
 *
 * 三条产品规则集中在这里，视图不再各判各的：
 *  1. `explore.finished.pending_count > 0` 不是"完成"，是"等待管理员回答"；
 *  2. 重新探索只重置**进度**，已经发现的问答卡片一张都不清掉；
 *  3. `explore.question.existing_answer` 有值 = 去重命中，直接展示已有答案。
 */

import {
  assertNever,
  type ErrorCode,
  type ExploreEvent,
  type ExploreQuestionEvent,
  type ProfileView,
} from '../types/events.ts'
import { formatDuration } from './run.ts'

export type ExploreStatus =
  /** 还没开始过。 */
  | 'idle'
  /** 正在扫描。 */
  | 'running'
  /** 扫完了，但还有问题没人回答。 */
  | 'waiting'
  /** 扫完了且没有遗留问题。 */
  | 'ok'
  /** 出错中断。 */
  | 'failed'
  /** 管理员主动停止。 */
  | 'stopped'

/** 探索中发现的一个列。 */
export interface ExploreQuestionEntry {
  /** 契约事件原样保留 —— 卡片要用的字段全在里面。 */
  event: ExploreQuestionEvent
  /** 去重命中（`existing_answer` 有值）：展示已有答案，不再提问。 */
  deduped: boolean
  /**
   * 管理员**本次就地**处理掉的结果（本地状态，不是契约字段）。
   * `answer` 为 null 表示忽略。卡片留在原位，只是从"待回答"变成"已处理"。
   */
  resolved: { answer: string | null; at: number } | null
}

export interface ExploreFailure {
  code: ErrorCode
  message: string
  /** 探索通道**允许**展示原始细节（读者是管理员，需要它来判断）。 */
  errorDetail: string | null
}

export interface ExploreState {
  runId: string
  profile: ProfileView | null
  /** 本次计划的候选列数（`explore.started`）。 */
  candidates: number
  done: number
  total: number
  currentTable: string | null
  note: string | null
  /** 发现的问答卡片，按发现顺序。视图倒序展示（新的在上）。 */
  questions: ExploreQuestionEntry[]
  candidatesFound: number
  questionsAsked: number
  pendingCount: number
  elapsedMs: number | null
  failure: ExploreFailure | null
  status: ExploreStatus
  startedAt: number
  lastEventAt: number
  seq: number
}

export function createExploreState(): ExploreState {
  return {
    runId: '',
    profile: null,
    candidates: 0,
    done: 0,
    total: 0,
    currentTable: null,
    note: null,
    questions: [],
    candidatesFound: 0,
    questionsAsked: 0,
    pendingCount: 0,
    elapsedMs: null,
    failure: null,
    status: 'idle',
    startedAt: 0,
    lastEventAt: 0,
    seq: 0,
  }
}

/**
 * 只重置**进度**，保留已经发现的问答卡片。
 *
 * 管理员重新探索时，上一轮的问题与答案还在眼前 —— 把列表清空等于让他
 * 丢掉刚看过的内容。这条规则在 `explore.started` 与手动开始处共用。
 */
export function resetExploreProgress(state: ExploreState): void {
  state.candidates = 0
  state.done = 0
  state.total = 0
  state.currentTable = null
  state.note = null
  state.candidatesFound = 0
  state.questionsAsked = 0
  state.pendingCount = 0
  state.elapsedMs = null
  state.failure = null
  state.status = 'running'
}

/**
 * 应用一个探索事件。就地修改 `state`（Vue 的 reactive 依赖就地修改做依赖追踪，
 * 测试里也就地断言），与 `core/run.ts` 的 `applyEvent` 一致。
 */
export function applyExploreEvent(state: ExploreState, ev: ExploreEvent): void {
  state.seq = ev.seq
  state.lastEventAt = ev.ts
  if (state.runId === '') state.runId = ev.run_id

  switch (ev.type) {
    case 'explore.started': {
      state.runId = ev.run_id
      state.profile = ev.profile
      state.startedAt = ev.ts
      resetExploreProgress(state)
      state.candidates = ev.candidates
      state.total = ev.candidates
      break
    }

    case 'explore.progress': {
      state.done = ev.done
      state.total = ev.total
      state.currentTable = ev.current_table
      state.note = ev.note
      state.status = 'running'
      break
    }

    case 'explore.question': {
      state.questions.push({
        event: ev,
        deduped: ev.existing_answer !== null && ev.existing_answer !== '',
        resolved: null,
      })
      break
    }

    case 'explore.finished': {
      state.candidatesFound = ev.candidates_found
      state.questionsAsked = ev.questions_asked
      state.pendingCount = ev.pending_count
      state.elapsedMs = ev.elapsed_ms
      state.currentTable = null
      state.note = null
      if (state.total > 0) state.done = state.total
      // 还有问题没人回答时**不是**"完成"。
      state.status = ev.pending_count > 0 ? 'waiting' : 'ok'
      break
    }

    case 'explore.failed': {
      state.failure = { code: ev.code, message: ev.message, errorDetail: ev.error_detail }
      state.status = 'failed'
      break
    }

    default:
      // 后端新增事件而未在前端处理时，这里是编译错误（`ev` 无法收窄为 never）。
      return assertNever(ev, 'explore event')
  }
}

// ---------------------------------------------------------------------------
// 视图辅助（纯函数，测试直接断言）
// ---------------------------------------------------------------------------

/** 已完成的比例（0–1）。总数未知时按 0 处理。 */
export function exploreRatio(state: ExploreState): number {
  if (state.total <= 0) return 0
  return Math.max(0, Math.min(1, state.done / state.total))
}

/**
 * 进度条下面那一行字：`正在检查 12/86 · exam_records（读取样本值）`。
 *
 * 大库扫描可能持续几分钟，所以进度、当前表、当前动作三样都要看得见 ——
 * 只有一个转圈的图标是熬不过三分钟的。
 */
export function exploreProgressText(state: ExploreState): string {
  switch (state.status) {
    case 'running': {
      if (state.total <= 0) return state.note ?? '正在扫描数据库…'
      const where = state.currentTable ? ` · ${state.currentTable}` : ''
      const note = state.note ? `（${state.note}）` : ''
      return `正在检查 ${state.done}/${state.total}${where}${note}`
    }
    case 'failed':
      return state.failure?.message ?? '探索出错'
    case 'stopped':
      return '已停止探索'
    default:
      return exploreStatusLabel(state)
  }
}

/** 状态文案。`waiting` 与 `ok` 是两个不同的词，不能都写成"完成"。 */
export function exploreStatusLabel(state: ExploreState): string {
  switch (state.status) {
    case 'idle':
      return '尚未探索'
    case 'running':
      return '探索中'
    case 'waiting':
      return '等待管理员回答'
    case 'ok':
      return '完成'
    case 'failed':
      return '已中断'
    case 'stopped':
      return '已停止'
    default:
      return assertNever(state.status, 'explore status')
  }
}

/** 结束后的总结行。空串表示还没有可总结的东西。 */
export function exploreSummaryText(state: ExploreState): string {
  switch (state.status) {
    case 'idle':
      return ''
    case 'failed':
      return `探索中断：${state.failure?.message ?? '探索出错'}`
    case 'stopped':
      return '已停止探索'
    case 'running':
      return ''
    case 'waiting':
    case 'ok': {
      const parts = [`发现 ${state.candidatesFound} 个候选列`, `提出 ${state.questionsAsked} 个问题`]
      if (state.pendingCount > 0) parts.push(`${state.pendingCount} 个待管理员回答`)
      if (state.elapsedMs) parts.push(`用时 ${formatDuration(state.elapsedMs)}`)
      return `${exploreStatusLabel(state)} — ${parts.join('，')}`
    }
    default:
      return assertNever(state.status, 'explore status')
  }
}

/** 还没处理的卡片（去重命中的不算 —— 它已经有答案了）。 */
export function pendingEntries(state: ExploreState): ExploreQuestionEntry[] {
  return state.questions.filter((entry) => !entry.deduped && entry.resolved === null)
}

/** 已经有答案的卡片：去重命中的，或管理员刚刚回答/忽略的。 */
export function answeredEntries(state: ExploreState): ExploreQuestionEntry[] {
  return state.questions.filter((entry) => entry.deduped || entry.resolved !== null)
}

/** 记下管理员就地处理掉的一张卡片（本地状态，列表由后端在下次拉取时接管）。 */
export function markEntryResolved(
  state: ExploreState,
  questionId: string,
  answer: string | null,
  at = Date.now(),
): void {
  const entry = state.questions.find((item) => item.event.question_id === questionId)
  if (entry) entry.resolved = { answer, at }
}
