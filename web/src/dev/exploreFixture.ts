/**
 * 探索通道的开发夹具：一条合成的探索事件序列。
 *
 * 与 `dev/fixture.ts` 同构，但走的是**另一条通道**（`explore.*`）：
 * 字段与 `src/dbchat/events/explore.py` 一一对应，喂进 `core/explore.ts`
 * 的同一个归约函数与同一个 `ExploreStage` 组件，因此 `/dev` 里看到的就是
 * 后端接上以后的真实渲染 —— 不需要后端进程。
 *
 * 覆盖到的分支：进度推进（含当前表）、就地冒出的问题卡片（带推测、样本值、
 * 上下文表）、去重命中（携带已有答案）、以及 `pending_count > 0` 的收尾
 * （显示"等待管理员回答"而不是"完成"）。
 */

import type { ExploreEvent, ExploreEventMap, ExploreEventType } from '../types/events.ts'

export interface ExploreFixtureStep {
  /** 距序列开始的毫秒偏移。 */
  at: number
  event: ExploreEvent
}

export interface ExploreFixture {
  id: string
  title: string
  description: string
  totalMs: number
  steps: ExploreFixtureStep[]
}

interface Builder {
  at: number
  seq: number
  steps: ExploreFixtureStep[]
  /** 载荷按事件类型精确检查，写错字段编译就报错。 */
  push: <T extends ExploreEventType>(
    type: T,
    waitMs: number,
    payload: Omit<ExploreEventMap[T], 'seq' | 'ts' | 'run_id' | 'type'>,
  ) => void
}

function builder(runId: string): Builder {
  const self: Builder = {
    at: 0,
    seq: 0,
    steps: [],
    push(type, waitMs, payload) {
      self.at += waitMs
      self.seq += 1
      const base = { seq: self.seq, ts: 1_700_000_000 + self.at / 1000, run_id: runId }
      // 唯一的断言点：泛型展开后 TS 无法自行证明它与 ExploreEventMap[T] 同构，
      // 但字段本身在上面已经逐个类型检查过了。
      const event = { ...base, type, ...payload } as unknown as ExploreEvent
      self.steps.push({ at: self.at, event })
    },
  }
  return self
}

const PROFILE = { id: 'fixture', name: '培训考试库', dialect_label: 'MySQL' }

/** 上下文行的形状：列名 + 若干行的相关取值。 */
const EXAM_COLUMNS = ['id', 'student_name', 'score', 'status']
const EXAM_ROWS = [
  ['10231', '陈晓明', '88', '01'],
  ['10232', '李慧', '76', '02'],
  ['10233', '王建国', '91', '01'],
]

function scanFixture(): ExploreFixture {
  const b = builder('explore-fixture-scan')

  b.push('explore.started', 0, { profile: PROFILE, candidates: 86 })

  b.push('explore.progress', 900, {
    done: 12,
    total: 86,
    current_table: 'exam_records',
    note: '读取样本值',
  })
  b.push('explore.progress', 1100, {
    done: 34,
    total: 86,
    current_table: 'students',
    note: '读取样本值',
  })

  // 第一个问题：agent 有推测，管理员据此判断"同意"还是"修正"。
  b.push('explore.question', 700, {
    question_id: 'eq-1',
    table: 'exam_records',
    column: 'status',
    question: 'exam_records.status 只用 01/02/03 三个取值，它的业务含义是什么？',
    agent_guess: '推测与考试状态有关：01=正常、02=缺考、03=缓考',
    samples: ['01', '02', '03'],
    context_columns: EXAM_COLUMNS,
    context_rows: EXAM_ROWS,
    existing_answer: null,
  })

  b.push('explore.progress', 1400, {
    done: 58,
    total: 86,
    current_table: 'departments',
    note: '读取样本值',
  })

  // 第二个问题：去重命中，直接展示已有答案，不再重复提问。
  b.push('explore.question', 800, {
    question_id: 'eq-2',
    table: 'employees',
    column: 'gender',
    question: 'employees.gender 的取值是什么含义？',
    agent_guess: 'M=男、F=女',
    samples: ['M', 'F'],
    context_columns: ['id', 'name', 'gender'],
    context_rows: [
      ['7', '赵磊', 'M'],
      ['8', '孙倩', 'F'],
    ],
    existing_answer: 'M=男，F=女（上一轮已确认）',
  })

  b.push('explore.progress', 1300, {
    done: 86,
    total: 86,
    current_table: 'exam_records',
    note: '汇总候选列',
  })

  b.push('explore.question', 600, {
    question_id: 'eq-3',
    table: 'exam_records',
    column: 'exam_type',
    question: 'exam_records.exam_type 是编码列，它的取值含义是什么？',
    agent_guess: '',
    samples: ['A', 'B', 'C'],
    context_columns: ['id', 'exam_type', 'score'],
    context_rows: [
      ['9001', 'A', '88'],
      ['9002', 'B', '61'],
    ],
    existing_answer: null,
  })

  // pending_count > 0：收尾显示"等待管理员回答"，而不是"完成"。
  b.push('explore.finished', 700, {
    candidates_found: 86,
    questions_asked: 3,
    pending_count: 2,
    elapsed_ms: b.at,
  })

  return {
    id: 'scan',
    title: '全库扫描（有待回答）',
    description: '进度推进 → 就地提问（含去重命中）→ 收尾时仍有 2 个待管理员回答',
    totalMs: b.at,
    steps: b.steps,
  }
}

function failedFixture(): ExploreFixture {
  const b = builder('explore-fixture-failed')

  b.push('explore.started', 0, { profile: PROFILE, candidates: 40 })
  b.push('explore.progress', 800, {
    done: 9,
    total: 40,
    current_table: 'orders',
    note: '读取样本值',
  })
  b.push('explore.failed', 1200, {
    code: 'db_timeout',
    message: '读取 orders 的样本值超时，探索已中断。',
    error_detail: 'read timeout after 30s on table orders',
  })

  return {
    id: 'failed',
    title: '探索中断',
    description: '读取样本值超时 → explore.failed（原始细节可展开）',
    totalMs: b.at,
    steps: b.steps,
  }
}

export const EXPLORE_FIXTURES: ExploreFixture[] = [scanFixture(), failedFixture()]

export function getExploreFixture(id: string): ExploreFixture {
  return EXPLORE_FIXTURES.find((f) => f.id === id) ?? EXPLORE_FIXTURES[0]!
}
