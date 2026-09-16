/**
 * 事件 → 步骤流 的归约逻辑。
 *
 * 这个文件**不 import 任何 Vue**：它是一个纯函数式的 reducer，Node 的
 * 内置测试运行器可以直接跑（`node --test test/`）。视图层只负责渲染
 * 这里产出的 `RunState`。
 *
 * 归约的产出只有两样东西：
 *  1. `steps` —— 一条连续的步骤列表（阶段 / 动作 / 提问），就是用户看到的进度；
 *  2. 回答与其附属产物（图表、失败信息、待确认问题）。
 *
 * 纪律：只有 ChatEvent 的字段能进入这里。原始 SQL、驱动报文、工具名、
 * 分类枚举都不在契约里，因此也不可能被渲染出来。
 */

import {
  ACTIVITY_LABELS,
  STAGE_LABELS,
  assertNever,
  type ActivityKind,
  type ArtifactCreatedEvent,
  type ChatEvent,
  type ErrorCode,
  type ProfileView,
  type QuestionAskedEvent,
  type RunStats,
  type Stage,
  type StageView,
} from '../types/events.ts'

export type StepKind = 'stage' | 'activity' | 'question'
export type StepStatus = 'running' | 'ok' | 'failed'

/** 默认收起、只有用户主动展开才可见的内容（SQL / 原始报错）。 */
export interface StepDisclosure {
  id: 'sql' | 'error'
  label: string
  text: string
}

export interface Step {
  id: string
  kind: StepKind
  status: StepStatus
  /** 用户可见文案：来自事件的 `label`，缺失时回落到中文标签表。 */
  label: string
  /** 目标对象名称（表名等），绝不是 SQL。 */
  target: string | null
  /** 缩进层级：阶段 0，阶段内的动作 1。 */
  depth: number
  startedAt: number
  finishedAt: number | null
  elapsedMs: number | null
  detail: string | null
  summary: string | null
  rowCount: number | null
  /** `stage.retried` 的安静提示，只出现在阶段行上。 */
  retryNote: string | null
  thought: string
  disclosures: StepDisclosure[]
  artifactIds: string[]
  question: QuestionAskedEvent | null
}

export interface Artifact {
  id: string
  kind: ArtifactCreatedEvent['kind']
  url: string
  title: string
}

export interface RunFailure {
  code: ErrorCode
  message: string
  retryable: boolean
  /** 原始细节：默认永不显示，只在用户展开时可见。 */
  errorDetail: string | null
}

export type RunStatus = 'running' | 'ok' | 'partial' | 'failed'

export interface RunState {
  runId: string
  profile: ProfileView | null
  willLoadSchema: boolean
  plannedStages: StageView[]
  steps: Step[]
  answer: string
  /** 回答是否已经开始流出 —— 步骤列表据此自动折叠。 */
  answerStarted: boolean
  status: RunStatus
  elapsedMs: number | null
  stats: RunStats
  caveats: string[]
  artifacts: Artifact[]
  failure: RunFailure | null
  question: QuestionAskedEvent | null
  startedAt: number
  lastEventAt: number
  /** 本地收到最后一个事件的时刻，用于给运行中的定时器补上事件之间的空隙。 */
  localLastEventAt: number
  /** 尚无归属步骤的思考文本。 */
  pendingThought: string
  seq: number
  /** 内部索引：阶段 → steps 下标。序列化时丢弃。 */
  stageIndex: Record<string, number>
}

const EMPTY_STATS: RunStats = { queries: 0, rows: 0, charts: 0 }

export function createRun(runId = ''): RunState {
  return {
    runId,
    profile: null,
    willLoadSchema: false,
    plannedStages: [],
    steps: [],
    answer: '',
    answerStarted: false,
    status: 'running',
    elapsedMs: null,
    stats: { ...EMPTY_STATS },
    caveats: [],
    artifacts: [],
    failure: null,
    question: null,
    startedAt: 0,
    lastEventAt: 0,
    localLastEventAt: 0,
    pendingThought: '',
    seq: 0,
    stageIndex: {},
  }
}

function stageLabel(stage: Stage, fallback: string | null = null): string {
  return fallback ?? STAGE_LABELS[stage] ?? ''
}

function activityLabel(kind: ActivityKind, fallback: string | null = null): string {
  return fallback ?? ACTIVITY_LABELS[kind] ?? ''
}

function activeStep(run: RunState): Step | null {
  for (let i = run.steps.length - 1; i >= 0; i -= 1) {
    const step = run.steps[i]
    if (step && step.status === 'running' && step.kind !== 'question') return step
  }
  return null
}

function nextId(run: RunState, prefix: string): string {
  return `${prefix}-${run.seq}-${run.steps.length}`
}

function elapsedFrom(step: Step, at: number): number {
  return Math.max(0, Math.round((at - step.startedAt) * 1000))
}

/** 建一行阶段步骤（含重试提示）。 */
function makeStageStep(
  run: RunState,
  stage: Stage,
  at: number,
  retryNote: string | null = null,
): Step {
  return {
    id: nextId(run, `stage:${stage}`),
    kind: 'stage',
    status: 'running',
    label: stageLabel(stage),
    target: null,
    depth: 0,
    startedAt: at,
    finishedAt: null,
    elapsedMs: null,
    detail: null,
    summary: null,
    rowCount: null,
    retryNote,
    thought: run.pendingThought,
    disclosures: [],
    artifactIds: [],
    question: null,
  }
}

/** 收尾：运行结束时不能留下永远转圈的行。 */
function finalizeOpenSteps(run: RunState, status: StepStatus, at: number): void {
  for (const step of run.steps) {
    if (step.status !== 'running') continue
    step.status = status
    step.finishedAt = at
    step.elapsedMs = elapsedFrom(step, at)
  }
}

function attachThought(run: RunState, text: string): void {
  const step = activeStep(run)
  if (step) {
    step.thought += text
    return
  }
  run.pendingThought += text
}

function findStep(run: RunState, activityId: string): Step | null {
  for (let i = run.steps.length - 1; i >= 0; i -= 1) {
    const step = run.steps[i]
    if (step && step.kind === 'activity' && step.id === `act:${activityId}`) return step
  }
  return null
}

/**
 * 应用一个事件。就地修改 `run`（Vue 的 reactive 依赖就地修改做依赖追踪，
 * 测试里也就地断言）。
 *
 * `nowMs` 由调用方注入，便于测试与回放。
 */
export function applyEvent(run: RunState, ev: ChatEvent, nowMs: number = Date.now()): RunState {
  run.seq = ev.seq
  run.lastEventAt = ev.ts
  run.localLastEventAt = nowMs
  if (run.runId === '') run.runId = ev.run_id

  switch (ev.type) {
    case 'run.started': {
      run.runId = ev.run_id
      run.profile = ev.profile
      run.willLoadSchema = ev.will_load_schema
      run.plannedStages = ev.stages
      run.startedAt = ev.ts
      run.status = 'running'
      break
    }

    case 'stage.started': {
      // 同一个阶段再次开始 = 重试后的又一轮。复用同一行，避免刷出一堆卡片。
      const existing = run.stageIndex[ev.stage]
      const known = existing === undefined ? null : run.steps[existing] ?? null
      if (known) {
        known.status = 'running'
        known.label = stageLabel(ev.stage, ev.label)
        known.detail = ev.detail
        known.summary = null
        known.startedAt = ev.ts
        known.finishedAt = null
        known.elapsedMs = null
        if (run.pendingThought) {
          known.thought += run.pendingThought
          run.pendingThought = ''
        }
        break
      }
      const step = makeStageStep(run, ev.stage, ev.ts)
      step.label = stageLabel(ev.stage, ev.label)
      step.detail = ev.detail
      run.pendingThought = ''
      run.stageIndex[ev.stage] = run.steps.length
      run.steps.push(step)
      break
    }

    case 'stage.finished': {
      const index = run.stageIndex[ev.stage]
      const step = index === undefined ? null : run.steps[index] ?? null
      if (step) {
        step.status = ev.ok ? 'ok' : 'failed'
        step.summary = ev.summary
        step.finishedAt = ev.ts
        step.elapsedMs = ev.elapsed_ms || elapsedFrom(step, ev.ts)
        step.detail = null
      }
      break
    }

    case 'stage.retried': {
      const index = run.stageIndex[ev.stage]
      const step = index === undefined ? null : run.steps[index] ?? null
      // 安静的可见重试提示。`max_attempts` 是轮次上限，按契约不展示。
      const note = ev.reason ? `↻ ${ev.reason}` : '↻ 正在重试'
      const withAttempt = ev.attempt > 1 ? `${note}（第 ${ev.attempt} 次）` : note
      if (step) {
        step.retryNote = withAttempt
        step.status = 'running'
        step.finishedAt = null
      } else {
        // 极少见：重试事件先于阶段开始到达。补一行，保证提示不会丢。
        run.stageIndex[ev.stage] = run.steps.length
        run.steps.push(makeStageStep(run, ev.stage, ev.ts, withAttempt))
      }
      break
    }

    case 'activity.started': {
      const runningStage = run.steps.some((s) => s.kind === 'stage' && s.status === 'running')
      const step: Step = {
        id: `act:${ev.activity_id}`,
        kind: 'activity',
        status: 'running',
        label: activityLabel(ev.kind, ev.label),
        target: ev.target,
        depth: runningStage ? 1 : 0,
        startedAt: ev.ts,
        finishedAt: null,
        elapsedMs: null,
        detail: null,
        summary: null,
        rowCount: null,
        retryNote: null,
        thought: run.pendingThought,
        disclosures: [],
        artifactIds: [],
        question: null,
      }
      run.pendingThought = ''
      run.steps.push(step)
      break
    }

    case 'activity.finished': {
      const step = findStep(run, ev.activity_id)
      if (step) {
        step.status = ev.ok ? 'ok' : 'failed'
        step.summary = ev.summary
        step.rowCount = ev.row_count
        step.finishedAt = ev.ts
        step.elapsedMs = ev.elapsed_ms ?? elapsedFrom(step, ev.ts)
        step.artifactIds = [...ev.artifact_ids]
        if (ev.sql_preview) {
          step.disclosures.push({ id: 'sql', label: '查看 SQL', text: ev.sql_preview })
        }
        if (ev.error_detail) {
          step.disclosures.push({ id: 'error', label: '查看错误详情', text: ev.error_detail })
        }
      }
      break
    }

    case 'thought.delta': {
      attachThought(run, ev.text)
      break
    }

    case 'answer.delta': {
      run.answerStarted = true
      run.answer += ev.text
      break
    }

    case 'answer.reset': {
      run.answer = ''
      break
    }

    case 'artifact.created': {
      run.artifacts.push({
        id: ev.artifact_id,
        kind: ev.kind,
        url: ev.url,
        title: ev.title,
      })
      const step = activeStep(run)
      if (step && !step.artifactIds.includes(ev.artifact_id)) {
        step.artifactIds.push(ev.artifact_id)
      }
      break
    }

    case 'question.asked': {
      run.question = ev
      run.steps.push({
        id: `ask:${ev.question_id}`,
        kind: 'question',
        status: 'running',
        label: ev.label,
        target: `${ev.table}.${ev.column}`,
        depth: 0,
        startedAt: ev.ts,
        finishedAt: null,
        elapsedMs: null,
        detail: null,
        summary: null,
        rowCount: null,
        retryNote: null,
        thought: '',
        disclosures: [],
        artifactIds: [],
        question: ev,
      })
      break
    }

    case 'run.finished': {
      run.status = ev.status
      run.elapsedMs = ev.elapsed_ms
      run.stats = ev.stats
      run.caveats = [...ev.caveats]
      if (ev.answer_markdown) run.answer = ev.answer_markdown
      const at = run.startedAt + ev.elapsed_ms / 1000 || ev.ts
      finalizeOpenSteps(run, 'ok', at)
      run.question = null
      break
    }

    case 'run.failed': {
      run.status = 'failed'
      run.failure = {
        code: ev.code,
        message: ev.message,
        retryable: ev.retryable,
        errorDetail: ev.error_detail,
      }
      run.elapsedMs =
        run.elapsedMs ??
        (run.startedAt ? Math.max(0, Math.round((ev.ts - run.startedAt) * 1000)) : 0)
      finalizeOpenSteps(run, 'failed', ev.ts)
      run.question = null
      break
    }

    default:
      // 后端新增事件而未在前端处理时，这里是编译错误（`ev` 无法收窄为 never）。
      return assertNever(ev)
  }

  return run
}

// ---------------------------------------------------------------------------
// 视图辅助（纯函数，测试直接断言）
// ---------------------------------------------------------------------------

export function isRunActive(run: RunState): boolean {
  return run.status === 'running'
}

/** 运行中显示的耗时；结束后用 `run.finished` 报的权威值。 */
export function runElapsedMs(run: RunState, nowMs: number = Date.now()): number {
  if (run.elapsedMs !== null && run.status !== 'running') return run.elapsedMs
  if (run.startedAt === 0) return 0
  const streamed = Math.max(0, (run.lastEventAt - run.startedAt) * 1000)
  const sinceLastEvent = run.localLastEventAt ? Math.max(0, nowMs - run.localLastEventAt) : 0
  return Math.round(streamed + sinceLastEvent)
}

/** 运行中步骤的实时耗时：事件时间差 + 距最后一个事件的墙钟时间。 */
export function stepElapsedMs(step: Step, run: RunState, nowMs: number = Date.now()): number {
  if (step.status !== 'running') return step.elapsedMs ?? 0
  if (step.finishedAt !== null) return step.elapsedMs ?? 0
  const streamed = Math.max(0, (run.lastEventAt - step.startedAt) * 1000)
  const sinceLastEvent = run.localLastEventAt ? Math.max(0, nowMs - run.localLastEventAt) : 0
  return Math.round(streamed + sinceLastEvent)
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '0.0s'
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds - minutes * 60)
  return `${minutes}m${rest.toString().padStart(2, '0')}s`
}

/** 正在进行的步骤：标题栏用它显示当前在做什么。 */
export function currentStep(run: RunState): Step | null {
  return activeStep(run)
}

export function countSteps(run: RunState): number {
  return run.steps.filter((s) => s.kind !== 'question').length
}

export function countRetries(run: RunState): number {
  return run.steps.filter((s) => s.retryNote !== null).length
}

/** 折叠后的标题行：`已完成 7 步 · 12.4s`。 */
export function runSummaryLine(run: RunState, nowMs: number = Date.now()): string {
  const n = countSteps(run)
  const elapsed = formatDuration(runElapsedMs(run, nowMs))
  if (run.status === 'running') return `进行中 ${n} 步 · ${elapsed}`
  if (run.status === 'failed') return `已中断 · ${n} 步 · ${elapsed}`
  return `已完成 ${n} 步 · ${elapsed}`
}

/**
 * 一个步骤**默认可见**的全部文本。
 *
 * 这是"不该暴露的信息不会显示"的可测试边界：SQL 预览与原始报错只存在于
 * `step.disclosures` 里，不会出现在此函数的返回值中，视图也只从
 * `disclosures` 渲染它们（且必须由用户主动展开）。
 */
export function visibleStepText(step: Step): string[] {
  const out: string[] = [step.label]
  if (step.target) out.push(step.target)
  if (step.detail) out.push(step.detail)
  if (step.summary) out.push(step.summary)
  if (step.retryNote) out.push(step.retryNote)
  if (step.thought) out.push(step.thought)
  if (step.question) {
    out.push(step.question.label, step.question.table, step.question.column, ...step.question.samples)
  }
  return out
}

export function runVisibleText(run: RunState): string[] {
  const out: string[] = [run.answer, ...run.caveats]
  if (run.failure) out.push(run.failure.message)
  for (const step of run.steps) out.push(...visibleStepText(step))
  for (const artifact of run.artifacts) out.push(artifact.title)
  return out
}

/** 运行结束后把步骤列表折叠成一行标题。 */
export function shouldCollapse(run: RunState): boolean {
  return run.answerStarted || run.status !== 'running'
}

// ---------------------------------------------------------------------------
// 持久化：会话历史写 localStorage，Map 形式的内部索引不落盘。
// ---------------------------------------------------------------------------

export interface StoredRun {
  runId: string
  steps: Step[]
  answer: string
  answerStarted: boolean
  status: RunStatus
  elapsedMs: number | null
  stats: RunStats
  caveats: string[]
  artifacts: Artifact[]
  failure: RunFailure | null
  question: QuestionAskedEvent | null
  willLoadSchema: boolean
  profile: ProfileView | null
}

export function serializeRun(run: RunState): StoredRun {
  return {
    runId: run.runId,
    steps: run.steps,
    answer: run.answer,
    answerStarted: run.answerStarted,
    status: run.status,
    elapsedMs: run.elapsedMs,
    stats: run.stats,
    caveats: run.caveats,
    artifacts: run.artifacts,
    failure: run.failure,
    question: run.question,
    willLoadSchema: run.willLoadSchema,
    profile: run.profile,
  }
}

export function hydrateRun(stored: StoredRun): RunState {
  const run = createRun(stored.runId)
  run.steps = stored.steps.map((s) => ({ ...s, status: s.status === 'running' ? 'ok' : s.status }))
  run.answer = stored.answer
  run.answerStarted = stored.answerStarted
  run.status = stored.status === 'running' ? 'ok' : stored.status
  run.elapsedMs = stored.elapsedMs
  run.stats = stored.stats
  run.caveats = stored.caveats
  run.artifacts = stored.artifacts
  run.failure = stored.failure
  run.question = stored.question
  run.willLoadSchema = stored.willLoadSchema
  run.profile = stored.profile
  return run
}
