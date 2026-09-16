/**
 * 事件存储（归约层）的单元测试。
 *
 * 直接用 Node 内置测试运行器跑（Node 24 原生剥离 TS 类型）：
 *   npm test        →  node --test test/
 *
 * 这里断言的是"用户能看到什么"和"用户看不到什么"两件事 —— 后者是本次
 * 界面重做的硬要求（不暴露 SQL / 原始报错 / 内部枚举 / 轮次上限）。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { FIXTURES, getFixture, type Fixture } from '../src/dev/fixture.ts'
import {
  applyEvent,
  createRun,
  formatDuration,
  hydrateRun,
  runSummaryLine,
  runVisibleText,
  serializeRun,
  shouldCollapse,
  visibleStepText,
  type RunState,
} from '../src/core/run.ts'
import { ACTIVITY_LABELS, EVENT_TYPES, STAGE_LABELS, type ChatEvent } from '../src/types/events.ts'

/** 用固定时钟回放，耗时断言才是确定的。 */
function replay(fixture: Fixture): RunState {
  const run = createRun()
  let clock = 1_000_000
  for (const item of fixture.steps) {
    applyEvent(run, item.event, (clock += 16))
  }
  return run
}

function allVisibleText(run: RunState): string {
  return runVisibleText(run).join('\n')
}

test('长任务夹具：步骤流完整、没有永远转圈的行', () => {
  const run = replay(getFixture('long'))

  assert.equal(run.status, 'ok')
  assert.equal(run.profile?.name, '培训考试库')
  assert.equal(run.willLoadSchema, true)

  const stages = run.steps.filter((s) => s.kind === 'stage')
  const activities = run.steps.filter((s) => s.kind === 'activity')
  assert.equal(stages.length, 5, '五个阶段各占一行')
  assert.equal(activities.length, 5)
  assert.equal(run.steps.filter((s) => s.status === 'running').length, 0)

  // 阶段行：标签来自中文标签表，而不是 Stage 的原始值
  assert.deepEqual(
    stages.map((s) => s.label),
    ['理解问题', '查询数据', '进行计算', '生成图表', '组织回答'],
  )

  // 动作行：一句话结果 + 行数
  const query = activities.find((s) => s.target === 'exam_records')
  assert.equal(query?.summary, '返回 12 行')
  assert.equal(query?.rowCount, 12)
  assert.ok((query?.elapsedMs ?? 0) > 0)

  // 重试提示只出现在阶段行上，且是一个安静的提示
  const retried = stages.filter((s) => s.retryNote !== null)
  assert.equal(retried.length, 1)
  assert.match(retried[0]!.retryNote!, /^↻ /)
  assert.equal(retried[0]!.label, '查询数据')

  // 回答与产物
  assert.match(run.answer, /销售部门缺考最多/)
  assert.equal(run.artifacts.length, 1)
  assert.equal(run.artifacts[0]!.kind, 'chart')

  // 折叠标题
  assert.equal(shouldCollapse(run), true)
  assert.match(runSummaryLine(run), /^已完成 10 步 · /)
})

test('步骤列表随回答开始自动折叠', () => {
  const run = createRun()
  let clock = 0
  const fixture = getFixture('long')
  for (const item of fixture.steps) {
    applyEvent(run, item.event, (clock += 10))
    if (item.event.type === 'answer.delta') break
  }
  assert.equal(run.answerStarted, true)
  assert.equal(shouldCollapse(run), true)
})

test('不泄漏：SQL 预览与原始报错只在折叠区里', () => {
  const long = replay(getFixture('long'))
  const text = allVisibleText(long)

  const sqlStep = long.steps.find((s) => s.disclosures.some((d) => d.id === 'sql'))
  assert.ok(sqlStep, 'SQL 存在 disclosure 里')
  const sql = sqlStep!.disclosures.find((d) => d.id === 'sql')!.text
  assert.ok(!text.includes(sql), '默认可见文本里不能出现 SQL')
  assert.ok(!visibleStepText(sqlStep!).join('\n').includes(sql))

  const failed = replay(getFixture('failed'))
  const failedText = allVisibleText(failed)
  const detail = failed.failure!.errorDetail!
  assert.ok(!failedText.includes(detail), '默认可见文本里不能出现原始报错')
  assert.ok(!failedText.includes('Lost connection'), '驱动报错不能出现在默认视图里')
  const failedStep = failed.steps.find((s) => s.disclosures.some((d) => d.id === 'error'))
  assert.ok(failedStep, '原始报错存在 disclosure 里')
})

test('不泄漏：内部枚举与轮次上限不出现在可见文本里', () => {
  for (const fixture of FIXTURES) {
    const run = replay(fixture)
    const text = allVisibleText(run)

    for (const stage of Object.keys(STAGE_LABELS)) {
      assert.ok(
        !new RegExp(`(^|[^a-z_])${stage}([^a-z_]|$)`).test(text),
        `${fixture.id}: 不该出现 Stage 原始值 ${stage}`,
      )
    }
    for (const kind of Object.keys(ACTIVITY_LABELS)) {
      assert.ok(
        !new RegExp(`(^|[^a-z_])${kind}([^a-z_]|$)`).test(text),
        `${fixture.id}: 不该出现 ActivityKind 原始值 ${kind}`,
      )
    }

    // 动作行的文案一律来自中文标签表
    for (const step of run.steps.filter((s) => s.kind === 'activity')) {
      assert.ok(
        Object.values(ACTIVITY_LABELS).includes(step.label),
        `${fixture.id}: 动作文案必须是中文标签`,
      )
    }

    // 轮次上限（max_attempts=5）不得出现在任何可见文本里
    for (const step of run.steps) {
      if (!step.retryNote) continue
      assert.ok(!step.retryNote.includes('5'), '重试提示不能带出轮次上限')
      assert.ok(!/max_attempts|最多/.test(step.retryNote))
    }
  }
})

test('提问事件渲染成一张可读的卡片', () => {
  const run = replay(getFixture('question'))
  const ask = run.steps.find((s) => s.kind === 'question')
  assert.ok(ask, '提问占一行')
  assert.equal(ask!.label, '需要你确认一列的取值含义')
  assert.equal(ask!.question?.table, 'employees')
  assert.equal(ask!.question?.column, 'status')
  assert.deepEqual(ask!.question?.samples, ['01', '02', '03'])
  assert.ok(visibleStepText(ask!).includes('需要你确认一列的取值含义'))
  // 运行结束后不该留下等待中的提问行
  assert.equal(run.steps.filter((s) => s.status === 'running').length, 0)
})

test('run.failed 保留可重试标记与折叠的原始报错', () => {
  const run = replay(getFixture('failed'))
  assert.equal(run.status, 'failed')
  assert.equal(run.failure?.message, '查询数据库超时了，稍后可以再试一次。')
  assert.equal(run.failure?.retryable, true)
  assert.equal(run.steps.filter((s) => s.status === 'running').length, 0)
  assert.ok(run.steps.some((s) => s.status === 'failed'))
  assert.match(runSummaryLine(run), /^已中断 · /)
})

test('answer.reset 丢弃已经流出的片段', () => {
  const run = createRun()
  const base = { seq: 1, ts: 100, run_id: 'r' }
  applyEvent(run, { ...base, type: 'answer.delta', text: '半截答' } as ChatEvent)
  assert.equal(run.answer, '半截答')
  applyEvent(run, { ...base, seq: 2, type: 'answer.reset' } as ChatEvent)
  assert.equal(run.answer, '')
  applyEvent(run, { ...base, seq: 3, type: 'answer.delta', text: '完整回答' } as ChatEvent)
  assert.equal(run.answer, '完整回答')
})

test('契约里的每一种事件都被处理（漏一种就是运行时异常）', () => {
  const samples: Record<string, ChatEvent> = {
    'run.started': {
      seq: 1,
      ts: 1,
      run_id: 'r',
      type: 'run.started',
      profile: { id: 'p', name: 'n', dialect_label: 'MySQL' },
      stages: [],
      will_load_schema: false,
    },
    'stage.started': {
      seq: 2,
      ts: 2,
      run_id: 'r',
      type: 'stage.started',
      stage: 'querying',
      label: '查询数据',
      index: 1,
      total: 3,
      detail: null,
    },
    'stage.finished': {
      seq: 3,
      ts: 3,
      run_id: 'r',
      type: 'stage.finished',
      stage: 'querying',
      ok: true,
      summary: '好了',
      elapsed_ms: 10,
    },
    'stage.retried': {
      seq: 4,
      ts: 4,
      run_id: 'r',
      type: 'stage.retried',
      stage: 'querying',
      attempt: 2,
      max_attempts: 5,
      reason: '再核一次',
    },
    'activity.started': {
      seq: 5,
      ts: 5,
      run_id: 'r',
      type: 'activity.started',
      activity_id: 'a1',
      kind: 'query',
      label: '正在查询数据',
      target: null,
    },
    'activity.finished': {
      seq: 6,
      ts: 6,
      run_id: 'r',
      type: 'activity.finished',
      activity_id: 'a1',
      ok: true,
      summary: '返回 1 行',
      row_count: 1,
      elapsed_ms: 5,
      sql_preview: null,
      error_detail: null,
      artifact_ids: [],
    },
    'thought.delta': { seq: 7, ts: 7, run_id: 'r', type: 'thought.delta', text: '想' },
    'answer.delta': { seq: 8, ts: 8, run_id: 'r', type: 'answer.delta', text: '答' },
    'answer.reset': { seq: 9, ts: 9, run_id: 'r', type: 'answer.reset' },
    'artifact.created': {
      seq: 10,
      ts: 10,
      run_id: 'r',
      type: 'artifact.created',
      artifact_id: 'c1',
      kind: 'chart',
      url: '/x.png',
      title: '图',
    },
    'question.asked': {
      seq: 11,
      ts: 11,
      run_id: 'r',
      type: 'question.asked',
      question_id: 'q1',
      label: '需要你确认一列的取值含义',
      table: 't',
      column: 'c',
      samples: [],
    },
    'run.finished': {
      seq: 12,
      ts: 12,
      run_id: 'r',
      type: 'run.finished',
      status: 'ok',
      elapsed_ms: 100,
      stats: { queries: 1, rows: 1, charts: 0 },
      answer_markdown: '答',
      caveats: [],
    },
    'run.failed': {
      seq: 13,
      ts: 13,
      run_id: 'r',
      type: 'run.failed',
      code: 'internal',
      message: '出错了',
      retryable: false,
      error_detail: null,
    },
  }

  assert.deepEqual(
    Object.keys(samples).sort(),
    [...EVENT_TYPES].sort(),
    '测试样本必须覆盖契约里的全部事件类型',
  )

  for (const type of EVENT_TYPES) {
    const run = createRun()
    assert.doesNotThrow(() => applyEvent(run, samples[type]!), `未处理的事件类型：${type}`)
  }
})

test('未知事件类型会显式抛错，而不是被安静吞掉', () => {
  const run = createRun()
  assert.throws(() =>
    applyEvent(run, { seq: 1, ts: 1, run_id: 'r', type: 'future.event' } as unknown as ChatEvent),
  )
})

test('会话持久化往返：运行中的行被收尾', () => {
  const run = createRun()
  const base = { run_id: 'r', ts: 5 }
  applyEvent(run, {
    ...base,
    seq: 1,
    type: 'stage.started',
    stage: 'querying',
    label: '查询数据',
    index: 1,
    total: 1,
    detail: null,
  } as ChatEvent)

  const restored = hydrateRun(JSON.parse(JSON.stringify(serializeRun(run))))
  assert.equal(restored.steps[0]!.status, 'ok')
  assert.equal(restored.status, 'ok')
  assert.equal(restored.steps[0]!.label, '查询数据')
})

test('耗时格式：短任务到分钟都能读', () => {
  assert.equal(formatDuration(0), '0.0s')
  assert.equal(formatDuration(12400), '12.4s')
  assert.equal(formatDuration(45_000), '45.0s')
  assert.equal(formatDuration(83_000), '1m23s')
})
