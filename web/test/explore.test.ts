/**
 * 探索通道（`src/dbchat/events/explore.py` ↔ `src/core/explore.ts`）的单元测试。
 *
 * 与 `eventStore.test.ts` 同一套跑法：node 内置测试运行器，归约层是纯函数。
 * 这里断言的是**管理员的界面规则**，不是实现细节：
 *   · 还有问题没人回答时不是"完成"，而是"等待管理员回答"；
 *   · 重新探索只重置进度，已经发现的卡片不能消失；
 *   · 去重命中要展示已有答案，而不是再问一遍。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  applyExploreEvent,
  answeredEntries,
  createExploreState,
  exploreProgressText,
  exploreRatio,
  exploreStatusLabel,
  exploreSummaryText,
  markEntryResolved,
  pendingEntries,
  resetExploreProgress,
  type ExploreState,
} from '../src/core/explore.ts'
import { EXPLORE_FIXTURES, getExploreFixture } from '../src/dev/exploreFixture.ts'
import {
  EXPLORE_EVENT_TYPES,
  isChatEventType,
  isExploreEventType,
  type ExploreEvent,
} from '../src/types/events.ts'

/** 用固定时钟回放，断言才是确定的。 */
function replay(fixtureId: string): ExploreState {
  const state = createExploreState()
  for (const item of getExploreFixture(fixtureId).steps) applyExploreEvent(state, item.event)
  return state
}

test('扫描夹具：进度、当前表、卡片与收尾总结都在', () => {
  const state = replay('scan')

  assert.equal(state.profile?.name, '培训考试库')
  assert.equal(state.candidates, 86)
  assert.equal(state.total, 86)
  assert.equal(state.done, 86)
  assert.equal(exploreRatio(state), 1)

  // 三张卡片：一张已在别处回答（去重命中）、两张待回答。
  assert.equal(state.questions.length, 3)
  assert.equal(pendingEntries(state).length, 2)
  assert.equal(answeredEntries(state).length, 1)
  assert.equal(state.candidatesFound, 86)
  assert.equal(state.questionsAsked, 3)
  assert.equal(state.pendingCount, 2)
})

test('还有问题没人回答时是"等待管理员回答"，不是"完成"', () => {
  const waiting = replay('scan')
  assert.equal(waiting.status, 'waiting')
  assert.equal(exploreStatusLabel(waiting), '等待管理员回答')
  assert.match(exploreSummaryText(waiting), /^等待管理员回答 — /)
  assert.match(exploreSummaryText(waiting), /2 个待管理员回答/)
  assert.ok(!exploreSummaryText(waiting).includes('完成'))

  // 全部回答完（pending_count = 0）才是"完成"。
  const done: ExploreState = { ...waiting, pendingCount: 0 }
  done.status = 'ok'
  assert.equal(exploreStatusLabel(done), '完成')
  assert.match(exploreSummaryText(done), /^完成 — /)
})

test('去重命中：带 existing_answer 的条目直接算已回答，不再提问', () => {
  const state = replay('scan')
  const deduped = answeredEntries(state)[0]!
  assert.equal(deduped.event.question_id, 'eq-2')
  assert.equal(deduped.event.existing_answer, 'M=男，F=女（上一轮已确认）')
  assert.ok(!pendingEntries(state).some((e) => e.event.question_id === 'eq-2'))
})

test('进度事件带当前表，大库扫描时看得见推进到哪了', () => {
  const state = createExploreState()
  applyExploreEvent(state, {
    seq: 1,
    ts: 1,
    run_id: 'r',
    type: 'explore.progress',
    done: 34,
    total: 86,
    current_table: 'students',
    note: '读取样本值',
  })

  assert.equal(exploreRatio(state) > 0.39 && exploreRatio(state) < 0.4, true)
  assert.equal(exploreProgressText(state), '正在检查 34/86 · students（读取样本值）')

  // 总数未知时不能显示 0/0 或 NaN。
  const unknown = createExploreState()
  unknown.status = 'running'
  assert.equal(exploreRatio(unknown), 0)
  assert.equal(exploreProgressText(unknown), '正在扫描数据库…')
})

test('重新探索只重置进度，已发现的卡片一张都不掉', () => {
  const state = replay('scan')
  const before = state.questions.map((entry) => entry.event.question_id)
  const summary = exploreSummaryText(state)
  assert.ok(summary.length > 0)

  resetExploreProgress(state)

  assert.deepEqual(
    state.questions.map((entry) => entry.event.question_id),
    before,
  )
  assert.equal(state.status, 'running')
  assert.equal(state.done, 0)
  assert.equal(state.elapsedMs, null)
  assert.equal(exploreSummaryText(state), '')
  // 上一轮的回答结果也留着（卡片上仍显示"本次已回答"）。
  assert.equal(answeredEntries(state)[0]!.deduped, true)
})

test('就地回答/忽略后卡片离开待回答队列，但留在流里', () => {
  const state = replay('scan')
  const target = pendingEntries(state)[0]!.event.question_id
  const index = state.questions.findIndex((e) => e.event.question_id === target)
  markEntryResolved(state, target, 'A=期末，B=补考', 1234)

  assert.equal(pendingEntries(state).length, 1)
  const entry = state.questions.find((e) => e.event.question_id === target)!
  assert.deepEqual(entry.resolved, { answer: 'A=期末，B=补考', at: 1234 })
  // 卡片不移动：它留在发现时的位置上，只是从"待回答"变成"已回答"。
  assert.equal(state.questions.findIndex((e) => e.event.question_id === target), index)
  assert.ok(answeredEntries(state).some((e) => e.event.question_id === target))
})

test('探索失败：保留友好文案，原始细节只在折叠区里', () => {
  const state = replay('failed')
  assert.equal(state.status, 'failed')
  assert.equal(state.failure?.code, 'db_timeout')
  assert.equal(exploreStatusLabel(state), '已中断')
  assert.equal(exploreProgressText(state), '读取 orders 的样本值超时，探索已中断。')
  assert.match(exploreSummaryText(state), /^探索中断：/)
  // 原始细节**是**契约要求带上的（读者是管理员），但要单独放，不混进状态文案里。
  assert.ok(state.failure?.errorDetail)
  assert.ok(!exploreProgressText(state).includes(state.failure!.errorDetail!))
})

test('契约里的每一种探索事件都被处理（漏一种就是运行时异常）', () => {
  const samples: Record<string, ExploreEvent> = {
    'explore.started': {
      seq: 1,
      ts: 1,
      run_id: 'r',
      type: 'explore.started',
      profile: { id: 'p', name: '库', dialect_label: 'MySQL' },
      candidates: 3,
    },
    'explore.progress': {
      seq: 2,
      ts: 2,
      run_id: 'r',
      type: 'explore.progress',
      done: 1,
      total: 3,
      current_table: 't',
      note: null,
    },
    'explore.question': {
      seq: 3,
      ts: 3,
      run_id: 'r',
      type: 'explore.question',
      question_id: 'q1',
      table: 't',
      column: 'c',
      question: '这列是什么意思？',
      agent_guess: '',
      samples: ['01'],
      context_columns: ['id', 'c'],
      context_rows: [['1', '01']],
      existing_answer: null,
    },
    'explore.finished': {
      seq: 4,
      ts: 4,
      run_id: 'r',
      type: 'explore.finished',
      candidates_found: 3,
      questions_asked: 1,
      pending_count: 1,
      elapsed_ms: 100,
    },
    'explore.failed': {
      seq: 5,
      ts: 5,
      run_id: 'r',
      type: 'explore.failed',
      code: 'internal',
      message: '出错了',
      error_detail: null,
    },
  }

  assert.deepEqual(
    Object.keys(samples).sort(),
    [...EXPLORE_EVENT_TYPES].sort(),
    '测试样本必须覆盖契约里的全部探索事件类型',
  )

  for (const type of EXPLORE_EVENT_TYPES) {
    const state = createExploreState()
    assert.doesNotThrow(() => applyExploreEvent(state, samples[type]!), `未处理的事件类型：${type}`)
  }
})

test('未知事件类型会显式抛错，而不是被安静吞掉', () => {
  const state = createExploreState()
  assert.throws(() =>
    applyExploreEvent(state, {
      seq: 1,
      ts: 1,
      run_id: 'r',
      type: 'explore.future',
    } as unknown as ExploreEvent),
  )
})

test('两条通道的运行时过滤器互不串台', () => {
  for (const type of EXPLORE_EVENT_TYPES) {
    assert.equal(isExploreEventType(type), true)
    assert.equal(isChatEventType(type), false, `探索事件不该被聊天通道收下：${type}`)
  }
  assert.equal(isExploreEventType('run.started'), false)
  assert.equal(isExploreEventType('message'), false)
  assert.equal(isExploreEventType(undefined), false)
})

test('夹具自身与契约一致：字段齐全、无空壳场景', () => {
  assert.ok(EXPLORE_FIXTURES.length >= 2)
  for (const fixture of EXPLORE_FIXTURES) {
    assert.ok(fixture.steps.length > 0, `${fixture.id} 夹具是空的`)
    let previousAt = -1
    for (const { at, event } of fixture.steps) {
      assert.ok(at >= previousAt, `${fixture.id}: 步骤时间必须单调递增`)
      previousAt = at
      assert.ok(
        (EXPLORE_EVENT_TYPES as readonly string[]).includes(event.type),
        `${fixture.id}: ${event.type} 不在契约里`,
      )
    }
  }
})
