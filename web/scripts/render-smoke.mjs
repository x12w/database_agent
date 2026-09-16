#!/usr/bin/env node
/**
 * 渲染冒烟测试：把夹具事件喂进真实的 Vue 组件，在 Node 里做一次 SSR，
 * 检查**渲染出来的 HTML**。
 *
 * 这不是取代浏览器里的目视检查（那用 `/dev` 页面），而是补上"模板真的能渲染"
 * 这一层：类型检查管不到运行时的空值访问，而 HTML 字符串能管到。
 *
 *   npm run smoke
 *
 * 断言三件事：
 *   1. 运行中：步骤行、重试提示、转圈字形都在；
 *   2. 结束后：列表折叠成一行 `已完成 N 步 · …`；
 *   3. 无论哪种状态，SQL 预览都不在 HTML 里；流式回答被转义（XSS 回归）。
 */

import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createServer } from 'vite'

const failures = []

function check(label, condition, extra = '') {
  if (condition) {
    console.log(`  ✔ ${label}`)
  } else {
    failures.push(label)
    console.log(`  ✖ ${label}${extra ? ` — ${extra}` : ''}`)
  }
}

const server = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  // 本脚本只做按需 SSR 加载，不需要依赖预打包（也就不需要目录扫描）。
  optimizeDeps: { noDiscovery: true },
})

try {
  const [
    { default: StepStream },
    { default: RunView },
    { default: DisclosureBlock },
    fixtureMod,
    runMod,
  ] = await Promise.all([
    server.ssrLoadModule('/src/components/StepStream.vue'),
    server.ssrLoadModule('/src/components/RunView.vue'),
    server.ssrLoadModule('/src/components/DisclosureBlock.vue'),
    server.ssrLoadModule('/src/dev/fixture.ts'),
    server.ssrLoadModule('/src/core/run.ts'),
  ])

  const { getFixture } = fixtureMod
  const { applyEvent, createRun } = runMod

  /** 回放夹具，可以停在某个事件类型之前。 */
  function replay(id, stopBeforeType = null) {
    const run = createRun()
    let clock = 1_000_000
    for (const item of getFixture(id).steps) {
      if (stopBeforeType && item.event.type === stopBeforeType) break
      applyEvent(run, item.event, (clock += 16))
    }
    return run
  }

  const render = (component, props) => renderToString(createSSRApp({ render: () => h(component, props) }))

  console.log('\n运行中的步骤流（理解问题 → 查询 → 重试）')
  const running = replay('long', 'answer.delta')
  const runningHtml = await render(StepStream, { run: running, now: Date.now() })
  check('渲染出阶段行', runningHtml.includes('理解问题') && runningHtml.includes('查询数据'))
  check('渲染出动作行的一句话结果', runningHtml.includes('返回 12 行'))
  check('渲染出安静的阶段重试提示', runningHtml.includes('↻') && runningHtml.includes('结果可能不完整'))
  check('运行中的行有转圈字形', runningHtml.includes('spinner'))
  check('运行中表头是"进行中"而不是"已完成"', /进行中 10 步 · /.test(runningHtml))
  check('未产生图表占位', !runningHtml.includes('<img'))
  const sql = 'SELECT d.name AS dept'
  check('SQL 预览不在 HTML 里', !runningHtml.includes(sql))
  check(
    '收起的历史行连"查看 SQL"入口都不渲染',
    !runningHtml.includes('查看 SQL') && !runningHtml.includes('exam_records e'),
  )

  console.log('\n折叠块默认收起')
  const disclosureHtml = await render(DisclosureBlock, { label: '查看 SQL', text: sql, tone: 'sql' })
  check('折叠标题可见', disclosureHtml.includes('查看 SQL'))
  check('折叠内容默认不渲染', !disclosureHtml.includes(sql))

  console.log('\n结束后的折叠行')
  const finished = replay('long')
  const finishedHtml = await render(StepStream, { run: finished, now: Date.now() })
  check('折叠成一行摘要', /已完成 10 步 · \d/.test(finishedHtml))
  check('折叠后不再渲染步骤行', !finishedHtml.includes('返回 12 行'))
  check('折叠后依然没有 SQL', !finishedHtml.includes(sql))

  console.log('\n流式回答走转义（XSS 回归）')
  const xss = createRun()
  applyEvent(xss, {
    seq: 1,
    ts: 1,
    run_id: 'x',
    type: 'answer.delta',
    text: '<img src=x onerror=alert(1)> 正常文字',
  })
  const xssHtml = await render(RunView, { text: '测试', run: xss, now: Date.now(), canRetry: false })
  check('尖括号被转义', xssHtml.includes('&lt;img src=x onerror=alert(1)&gt;'))
  check('没有真的注入 img 标签', !xssHtml.includes('<img src=x'))

  console.log('\n失败态')
  const failed = replay('failed')
  const failedHtml = await render(StepStream, { run: failed, now: Date.now() })
  check('折叠摘要标明已中断', /已中断 · \d 步/.test(failedHtml))
  const failedRunHtml = await render(RunView, {
    text: '最近一天有多少条考试记录？',
    run: failed,
    now: Date.now(),
    canRetry: true,
  })
  check('展示友好错误文案', failedRunHtml.includes('查询数据库超时了，稍后可以再试一次。'))
  check('提供重试入口', failedRunHtml.includes('重试'))
  check('原始报错不在 HTML 里', !failedRunHtml.includes('Lost connection'))
  check('驱动报错折叠入口存在', failedRunHtml.includes('查看错误详情'))
} finally {
  await server.close()
}

if (failures.length) {
  console.error(`\n✖ 渲染冒烟测试失败 ${failures.length} 项`)
  process.exit(1)
}
console.log('\n✔ 渲染冒烟测试全部通过')
