/**
 * 事件契约的 TypeScript 镜像。
 *
 * 手写镜像自 `src/dbchat/events/chat.py` + `src/dbchat/events/base.py`。
 * 漂移由 `node scripts/check-types.mjs` 检查（读 pydantic 模型源码，不依赖 Python）。
 *
 * 约定：线上事件的字段**永远存在**（后端 `model_dump_json(exclude_none=False)`），
 * 可空字段是 `T | null` 而不是可选属性 —— 这样前端不会因为 `undefined` 与 `null`
 * 混淆而写出两套分支。
 */

// ---------------------------------------------------------------------------
// 枚举（base.py）
// ---------------------------------------------------------------------------

export const EVENT_TYPES = [
  'run.started',
  'stage.started',
  'stage.finished',
  'stage.retried',
  'activity.started',
  'activity.finished',
  'thought.delta',
  'answer.delta',
  'answer.reset',
  'artifact.created',
  'question.asked',
  'run.finished',
  'run.failed',
] as const

export type ChatEventType = (typeof EVENT_TYPES)[number]

export type Stage = 'understanding' | 'querying' | 'computing' | 'visualizing' | 'answering'

/** `Stage` 的原始值是内部标识，前端只用这张表里的中文。 */
export const STAGE_LABELS: Record<Stage, string> = {
  understanding: '理解问题',
  querying: '查询数据',
  computing: '进行计算',
  visualizing: '生成图表',
  answering: '组织回答',
}

export type ActivityKind = 'query' | 'schema' | 'search' | 'docs' | 'compute' | 'chart' | 'admin_ask'

/** `ActivityKind` 的原始值是内部标识，前端只用这张表里的中文。 */
export const ACTIVITY_LABELS: Record<ActivityKind, string> = {
  query: '正在查询数据',
  schema: '正在查看表结构',
  search: '正在检索资料',
  docs: '正在查证文档',
  compute: '正在计算',
  chart: '正在生成图表',
  admin_ask: '正在向你确认',
}

export type ErrorCode =
  | 'llm_unavailable'
  | 'llm_failed'
  | 'db_unreachable'
  | 'db_auth'
  | 'db_timeout'
  | 'db_query_failed'
  | 'validation'
  | 'timeout'
  | 'cancelled'
  | 'internal'

/** 与 base.py 的 `RETRYABLE_ERRORS` 保持一致；`run.failed.retryable` 才是权威值。 */
export const RETRYABLE_ERRORS: readonly ErrorCode[] = [
  'llm_failed',
  'db_timeout',
  'timeout',
  'internal',
]

// ---------------------------------------------------------------------------
// 公共结构（base.py）
// ---------------------------------------------------------------------------

export interface EventBase {
  seq: number
  ts: number
  run_id: string
}

export interface RunStats {
  queries: number
  rows: number
  charts: number
}

export interface StageView {
  stage: Stage
  label: string
}

export interface ProfileView {
  id: string
  name: string
  dialect_label: string
}

// ---------------------------------------------------------------------------
// 事件（chat.py）
// ---------------------------------------------------------------------------

export interface RunStartedEvent extends EventBase {
  type: 'run.started'
  profile: ProfileView
  stages: StageView[]
  will_load_schema: boolean
}

export interface StageStartedEvent extends EventBase {
  type: 'stage.started'
  stage: Stage
  label: string
  index: number
  total: number
  detail: string | null
}

export interface StageFinishedEvent extends EventBase {
  type: 'stage.finished'
  stage: Stage
  ok: boolean
  summary: string
  elapsed_ms: number
}

export interface StageRetriedEvent extends EventBase {
  type: 'stage.retried'
  stage: Stage
  attempt: number
  max_attempts: number
  reason: string
}

export interface ActivityStartedEvent extends EventBase {
  type: 'activity.started'
  activity_id: string
  kind: ActivityKind
  label: string
  target: string | null
}

export interface ActivityFinishedEvent extends EventBase {
  type: 'activity.finished'
  activity_id: string
  ok: boolean
  summary: string
  row_count: number | null
  elapsed_ms: number | null
  sql_preview: string | null
  error_detail: string | null
  artifact_ids: string[]
}

export interface ThoughtDeltaEvent extends EventBase {
  type: 'thought.delta'
  text: string
}

export interface AnswerDeltaEvent extends EventBase {
  type: 'answer.delta'
  text: string
}

export interface AnswerResetEvent extends EventBase {
  type: 'answer.reset'
}

export interface ArtifactCreatedEvent extends EventBase {
  type: 'artifact.created'
  artifact_id: string
  kind: 'chart' | 'table' | 'file'
  url: string
  title: string
}

export interface QuestionAskedEvent extends EventBase {
  type: 'question.asked'
  question_id: string
  label: string
  table: string
  column: string
  samples: string[]
}

export interface RunFinishedEvent extends EventBase {
  type: 'run.finished'
  status: 'ok' | 'partial'
  elapsed_ms: number
  stats: RunStats
  answer_markdown: string
  caveats: string[]
}

export interface RunFailedEvent extends EventBase {
  type: 'run.failed'
  code: ErrorCode
  message: string
  retryable: boolean
  error_detail: string | null
}

/** `type` → 事件的映射。新增后端事件时这里缺一条，`ChatEventMap` 的使用处就会编译失败。 */
export interface ChatEventMap {
  'run.started': RunStartedEvent
  'stage.started': StageStartedEvent
  'stage.finished': StageFinishedEvent
  'stage.retried': StageRetriedEvent
  'activity.started': ActivityStartedEvent
  'activity.finished': ActivityFinishedEvent
  'thought.delta': ThoughtDeltaEvent
  'answer.delta': AnswerDeltaEvent
  'answer.reset': AnswerResetEvent
  'artifact.created': ArtifactCreatedEvent
  'question.asked': QuestionAskedEvent
  'run.finished': RunFinishedEvent
  'run.failed': RunFailedEvent
}

export type ChatEvent = ChatEventMap[ChatEventType]

/** 按 `type` 取事件类型：`Of<'run.failed'>` = `RunFailedEvent`。 */
export type Of<T extends ChatEventType> = ChatEventMap[T]

/**
 * 穷尽性检查。事件存储的分发 switch 里用它兜底：
 * 后端新增一个事件而前端没处理时，`ev` 无法收窄成 `never`，编译失败。
 */
export function assertNever(value: never, context = 'chat event'): never {
  throw new Error(`未处理的事件类型（${context}）: ${JSON.stringify(value)}`)
}

/** 运行时判断一个未知的 SSE 载荷是不是本契约里的事件。 */
export function isChatEventType(value: unknown): value is ChatEventType {
  return typeof value === 'string' && (EVENT_TYPES as readonly string[]).includes(value)
}
