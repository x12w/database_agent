/**
 * 开发夹具：一条合成的长任务事件序列。
 *
 * 目的是**在没有后端**的情况下把步骤流看个够：加载结构 → 分析 → 查询 →
 * 重试 → 计算 → 图表 → 回答，以及提问与失败两种分支。
 *
 * 这些事件与后端 `src/dbchat/events/chat.py` 的字段一一对应；
 * 时间戳用真实的毫秒级间隔，回放时可以按倍速缩放。
 */

import type { ChatEvent, ChatEventMap, ChatEventType } from '../types/events.ts'

export interface FixtureStep {
  /** 距序列开始的毫秒偏移。 */
  at: number
  event: ChatEvent
}

export interface Fixture {
  id: string
  title: string
  description: string
  message: string
  totalMs: number
  steps: FixtureStep[]
}

/** 一张内联 SVG 图表，保证离线也能看到图。 */
const CHART_URL = `data:image/svg+xml;utf8,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="420" height="200" viewBox="0 0 420 200">
    <rect width="420" height="200" fill="#ffffff"/>
    <g fill="#5cc9b7">
      <rect x="40" y="90" width="46" height="80"/>
      <rect x="110" y="60" width="46" height="110"/>
      <rect x="180" y="120" width="46" height="50"/>
      <rect x="250" y="40" width="46" height="130"/>
      <rect x="320" y="140" width="46" height="30"/>
    </g>
    <g fill="#333842" font-family="sans-serif" font-size="11">
      <text x="46" y="185">财务</text><text x="116" y="185">技术</text>
      <text x="186" y="185">行政</text><text x="256" y="185">销售</text>
      <text x="326" y="185">客服</text>
      <text x="40" y="24" font-size="13">各部门缺考人数</text>
    </g>
  </svg>`,
)}`

interface Builder {
  at: number
  seq: number
  steps: FixtureStep[]
  /** 载荷类型按事件类型精确检查，写错字段编译就报错。 */
  push: <T extends ChatEventType>(
    type: T,
    waitMs: number,
    payload: Omit<ChatEventMap[T], 'seq' | 'ts' | 'run_id' | 'type'>,
  ) => void
  thought: (text: string, waitMs: number) => void
  answer: (text: string, waitMs: number) => void
}

function builder(runId: string, startAtMs = 0): Builder {
  const self: Builder = {
    at: startAtMs,
    seq: 0,
    steps: [],
    push(type, waitMs, payload) {
      self.at += waitMs
      self.seq += 1
      const base = { seq: self.seq, ts: 1_700_000_000 + self.at / 1000, run_id: runId }
      // 唯一的断言点：泛型展开后 TS 无法自行证明它与 ChatEventMap[T] 同构，
      // 但字段本身在上面已经逐个类型检查过了。
      const event = { ...base, type, ...payload } as unknown as ChatEvent
      self.steps.push({ at: self.at, event })
    },
    thought(text, waitMs) {
      self.push('thought.delta', waitMs, { text })
    },
    answer(text, waitMs) {
      self.push('answer.delta', waitMs, { text })
    },
  }
  return self
}

const PROFILE = { id: 'fixture', name: '培训考试库', dialect_label: 'MySQL' }

const STAGES = [
  { stage: 'understanding' as const, label: '理解问题' },
  { stage: 'querying' as const, label: '查询数据' },
  { stage: 'computing' as const, label: '进行计算' },
  { stage: 'visualizing' as const, label: '生成图表' },
  { stage: 'answering' as const, label: '组织回答' },
]

const ANSWER_MARKDOWN = `## 缺考情况

共有 **12 人** 缺考，分布在 5 个部门：

| 部门 | 缺考人数 |
| --- | --- |
| 销售 | 4 |
| 技术 | 3 |
| 财务 | 2 |
| 行政 | 2 |
| 客服 | 1 |

销售部门缺考最多，建议优先跟进。`

function longFixture(): Fixture {
  const b = builder('fixture-long')

  b.push('run.started', 0, {
    profile: PROFILE,
    stages: STAGES,
    will_load_schema: true,
  })

  // ---- 理解问题 ----
  b.push('stage.started', 120, {
    stage: 'understanding',
    label: '理解问题',
    index: 1,
    total: 5,
    detail: '正在加载数据库结构…',
  })
  b.thought('先看看这个库有哪些表，再判断"缺考"落在哪张表上。', 420)
  b.push('activity.started', 260, {
    activity_id: 'a-schema',
    kind: 'schema',
    label: '正在查看表结构',
    target: '12 张表',
  })
  b.thought('exam_records 里有 student_id 和 score，score 为空应该就是缺考。', 900)
  b.thought('另外 departments 表可以拿到部门名称。', 620)
  b.push('activity.finished', 380, {
    activity_id: 'a-schema',
    ok: true,
    summary: '读取 12 张表 · 86 列',
    row_count: null,
    elapsed_ms: 2160,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 140, {
    stage: 'understanding',
    ok: true,
    summary: '已确认缺考的定义',
    elapsed_ms: 3300,
  })

  // ---- 查询数据（一次重试）----
  b.push('stage.started', 200, {
    stage: 'querying',
    label: '查询数据',
    index: 2,
    total: 5,
    detail: null,
  })
  b.thought('先按 score IS NULL 统计一遍。', 380)
  b.push('activity.started', 180, {
    activity_id: 'a-q1',
    kind: 'query',
    label: '正在查询数据',
    target: 'exam_records',
  })
  b.push('activity.finished', 1180, {
    activity_id: 'a-q1',
    ok: true,
    summary: '返回 12 行',
    row_count: 12,
    elapsed_ms: 1180,
    sql_preview:
      'SELECT d.name AS dept, COUNT(*) AS absent\nFROM exam_records e\nJOIN students s ON s.id = e.student_id\nJOIN departments d ON d.id = s.dept_id\nWHERE e.score IS NULL\nGROUP BY d.name\nORDER BY absent DESC;',
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.retried', 420, {
    stage: 'querying',
    attempt: 2,
    max_attempts: 5,
    reason: '结果可能不完整，换一种口径再核对一次',
  })
  b.thought('补一个条件：只统计本学期的考试，避免把历史数据算进来。', 460)
  b.push('activity.started', 260, {
    activity_id: 'a-q2',
    kind: 'query',
    label: '正在查询数据',
    target: 'exam_records',
  })
  b.push('activity.finished', 840, {
    activity_id: 'a-q2',
    ok: true,
    summary: '返回 12 行',
    row_count: 12,
    elapsed_ms: 840,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 160, {
    stage: 'querying',
    ok: true,
    summary: '两次口径结果一致',
    elapsed_ms: 2980,
  })

  // ---- 计算 ----
  b.push('stage.started', 180, {
    stage: 'computing',
    label: '进行计算',
    index: 3,
    total: 5,
    detail: null,
  })
  b.thought('把 12 条记录按部门汇总，并算出占比。', 320)
  b.push('activity.started', 180, {
    activity_id: 'a-c1',
    kind: 'compute',
    label: '正在计算',
    target: null,
  })
  b.push('activity.finished', 420, {
    activity_id: 'a-c1',
    ok: true,
    summary: '汇总 5 个部门',
    row_count: 5,
    elapsed_ms: 420,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 120, {
    stage: 'computing',
    ok: true,
    summary: '已完成汇总',
    elapsed_ms: 740,
  })

  // ---- 图表 ----
  b.push('stage.started', 160, {
    stage: 'visualizing',
    label: '生成图表',
    index: 4,
    total: 5,
    detail: null,
  })
  b.push('activity.started', 220, {
    activity_id: 'a-v1',
    kind: 'chart',
    label: '正在生成图表',
    target: '各部门缺考人数',
  })
  b.push('artifact.created', 640, {
    artifact_id: 'chart-1',
    kind: 'chart',
    url: CHART_URL,
    title: '各部门缺考人数',
  })
  b.push('activity.finished', 200, {
    activity_id: 'a-v1',
    ok: true,
    summary: '生成 1 张图表',
    row_count: null,
    elapsed_ms: 860,
    sql_preview: null,
    error_detail: null,
    artifact_ids: ['chart-1'],
  })
  b.push('stage.finished', 120, {
    stage: 'visualizing',
    ok: true,
    summary: '已生成 1 张图表',
    elapsed_ms: 980,
  })

  // ---- 组织回答 ----
  b.push('stage.started', 180, {
    stage: 'answering',
    label: '组织回答',
    index: 5,
    total: 5,
    detail: null,
  })
  b.thought('用表格给出部门分布，并点出销售最多。', 380)
  b.answer('## 缺考情况\n\n共有 ', 260)
  b.answer('**12 人** 缺考，', 120)
  b.answer('分布在 5 个部门：\n\n', 140)
  b.answer('| 部门 | 缺考人数 |\n| --- | --- |\n', 180)
  b.answer('| 销售 | 4 |\n| 技术 | 3 |\n| 财务 | 2 |\n| 行政 | 2 |\n| 客服 | 1 |\n\n', 240)
  b.answer('销售部门缺考最多，建议优先跟进。', 200)
  b.push('stage.finished', 200, {
    stage: 'answering',
    ok: true,
    summary: '回答已生成',
    elapsed_ms: 1180,
  })
  b.push('run.finished', 120, {
    status: 'ok',
    elapsed_ms: b.at,
    stats: { queries: 2, rows: 24, charts: 1 },
    answer_markdown: ANSWER_MARKDOWN,
    caveats: [],
  })

  return {
    id: 'long',
    title: '长任务（含重试、图表）',
    description: '结构加载 → 分析 → 查询 → 重试 → 计算 → 图表 → 回答',
    message: '有哪些人缺考？按部门统计一下',
    totalMs: b.at,
    steps: b.steps,
  }
}

function questionFixture(): Fixture {
  const b = builder('fixture-question')
  b.push('run.started', 0, {
    profile: PROFILE,
    stages: STAGES,
    will_load_schema: false,
  })
  b.push('stage.started', 120, {
    stage: 'understanding',
    label: '理解问题',
    index: 1,
    total: 5,
    detail: null,
  })
  b.thought('status 这一列是编码值，先看看取值分布。', 420)
  b.push('activity.started', 220, {
    activity_id: 'a-q',
    kind: 'query',
    label: '正在查询数据',
    target: 'employees',
  })
  b.push('activity.finished', 760, {
    activity_id: 'a-q',
    ok: true,
    summary: '返回 3 行',
    row_count: 3,
    elapsed_ms: 760,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('activity.started', 260, {
    activity_id: 'a-ask',
    kind: 'admin_ask',
    label: '正在向你确认',
    target: 'employees.status',
  })
  b.push('question.asked', 320, {
    question_id: 'q-1',
    label: '需要你确认一列的取值含义',
    table: 'employees',
    column: 'status',
    samples: ['01', '02', '03'],
  })
  b.push('activity.finished', 2400, {
    activity_id: 'a-ask',
    ok: true,
    summary: '已确认',
    row_count: null,
    elapsed_ms: 2400,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 160, {
    stage: 'understanding',
    ok: true,
    summary: '已确认列含义',
    elapsed_ms: 4400,
  })
  b.push('stage.started', 180, {
    stage: 'querying',
    label: '查询数据',
    index: 2,
    total: 5,
    detail: null,
  })
  b.push('activity.started', 200, {
    activity_id: 'a-q2',
    kind: 'query',
    label: '正在查询数据',
    target: 'employees',
  })
  b.push('activity.finished', 820, {
    activity_id: 'a-q2',
    ok: true,
    summary: '返回 128 行',
    row_count: 128,
    elapsed_ms: 820,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 140, {
    stage: 'querying',
    ok: true,
    summary: '已取得数据',
    elapsed_ms: 1100,
  })
  b.push('stage.started', 160, {
    stage: 'answering',
    label: '组织回答',
    index: 5,
    total: 5,
    detail: null,
  })
  b.answer('在职 96 人，离职 24 人，试用期 8 人。', 400)
  b.push('stage.finished', 160, {
    stage: 'answering',
    ok: true,
    summary: '回答已生成',
    elapsed_ms: 560,
  })
  b.push('run.finished', 100, {
    status: 'ok',
    elapsed_ms: b.at,
    stats: { queries: 2, rows: 131, charts: 0 },
    answer_markdown: '在职 **96** 人，离职 **24** 人，试用期 **8** 人。',
    caveats: [],
  })

  return {
    id: 'question',
    title: '需要用户确认',
    description: '中途要求确认一列的取值含义',
    message: '在职和离职各有多少人？',
    totalMs: b.at,
    steps: b.steps,
  }
}

function failedFixture(): Fixture {
  const b = builder('fixture-failed')
  b.push('run.started', 0, {
    profile: PROFILE,
    stages: STAGES,
    will_load_schema: false,
  })
  b.push('stage.started', 120, {
    stage: 'understanding',
    label: '理解问题',
    index: 1,
    total: 5,
    detail: null,
  })
  b.push('activity.started', 300, {
    activity_id: 'a-s',
    kind: 'schema',
    label: '正在查看表结构',
    target: null,
  })
  b.push('activity.finished', 700, {
    activity_id: 'a-s',
    ok: true,
    summary: '读取 12 张表',
    row_count: null,
    elapsed_ms: 700,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 140, {
    stage: 'understanding',
    ok: true,
    summary: '已了解数据结构',
    elapsed_ms: 1000,
  })
  b.push('stage.started', 160, {
    stage: 'querying',
    label: '查询数据',
    index: 2,
    total: 5,
    detail: null,
  })
  b.push('activity.started', 220, {
    activity_id: 'a-q1',
    kind: 'query',
    label: '正在查询数据',
    target: 'exam_records',
  })
  b.push('activity.finished', 1500, {
    activity_id: 'a-q1',
    ok: false,
    summary: '查询超时',
    row_count: null,
    elapsed_ms: 1500,
    sql_preview: 'SELECT COUNT(*) FROM exam_records WHERE created_at > NOW() - INTERVAL 1 DAY;',
    error_detail: 'Traceback (most recent call last):\n  ...\nOperationalError: (2013, "Lost connection to MySQL server during query")',
    artifact_ids: [],
  })
  b.push('run.failed', 300, {
    code: 'db_timeout',
    message: '查询数据库超时了，稍后可以再试一次。',
    retryable: true,
    error_detail: 'query timeout after 30s',
  })

  return {
    id: 'failed',
    title: '失败（可重试）',
    description: '查询超时 → run.failed，原始报错默认收起',
    message: '最近一天有多少条考试记录？',
    totalMs: b.at,
    steps: b.steps,
  }
}

function fastFixture(): Fixture {
  const b = builder('fixture-fast')
  b.push('run.started', 0, {
    profile: PROFILE,
    stages: STAGES,
    will_load_schema: false,
  })
  b.push('stage.started', 60, {
    stage: 'querying',
    label: '查询数据',
    index: 2,
    total: 5,
    detail: null,
  })
  b.push('activity.started', 80, {
    activity_id: 'a-q',
    kind: 'query',
    label: '正在查询数据',
    target: 'departments',
  })
  b.push('activity.finished', 380, {
    activity_id: 'a-q',
    ok: true,
    summary: '返回 5 行',
    row_count: 5,
    elapsed_ms: 380,
    sql_preview: null,
    error_detail: null,
    artifact_ids: [],
  })
  b.push('stage.finished', 80, {
    stage: 'querying',
    ok: true,
    summary: '已取得数据',
    elapsed_ms: 460,
  })
  b.push('stage.started', 80, {
    stage: 'answering',
    label: '组织回答',
    index: 5,
    total: 5,
    detail: null,
  })
  b.answer('一共有 5 个部门。', 240)
  b.push('stage.finished', 80, {
    stage: 'answering',
    ok: true,
    summary: '回答已生成',
    elapsed_ms: 320,
  })
  b.push('run.finished', 60, {
    status: 'ok',
    elapsed_ms: b.at,
    stats: { queries: 1, rows: 5, charts: 0 },
    answer_markdown: '一共有 **5** 个部门。',
    caveats: [],
  })

  return {
    id: 'fast',
    title: '短任务',
    description: '两步就结束，用来看折叠后的样子',
    message: '一共有几个部门？',
    totalMs: b.at,
    steps: b.steps,
  }
}

export const FIXTURES: Fixture[] = [
  longFixture(),
  questionFixture(),
  failedFixture(),
  fastFixture(),
]

export function getFixture(id: string): Fixture {
  return FIXTURES.find((f) => f.id === id) ?? FIXTURES[0]!
}
