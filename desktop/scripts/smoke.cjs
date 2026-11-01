// Run with a virtual display on CI: xvfb-run -a npm run test:app.
const { _electron: electron } = require('playwright')
const { mkdtemp, readFile, rm, mkdir } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const assert = require('node:assert/strict')
const { createServer } = require('node:http')

async function main() {
  const userData = await mkdtemp(path.join(tmpdir(), 'dbchat-desktop-test-'))
  let application
  const requests = []
  const modelServer = createServer((request, response) => {
    requests.push(request.url)
    request.resume()
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ id: 'synthetic', object: 'chat.completion', model: 'desktop-smoke-model', choices: [{ index: 0, message: { role: 'assistant', content: 'OK' }, finish_reason: 'stop' }] }))
  })
  await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve))
  try {
    application = await electron.launch({ executablePath: process.env.DBCHAT_ELECTRON || require('electron'),
      args: [process.env.DBCHAT_APP_PATH || path.resolve(__dirname, '..')], timeout: 60000,
      env: { ...process.env, DBCHAT_USER_DATA: userData },
    })
    const page = await application.firstWindow({ timeout: 60000 })
    await page.getByRole('heading', { name: '模型服务', exact: true }).waitFor()
    await page.locator('#llm_model').waitFor()
    assert.match(page.url(), /^dbchat:\/\/app\/settings/)
    await page.evaluate(() => localStorage.setItem('restart-marker', 'preserved'))
    await page.locator('#llm_model').fill('desktop-smoke-model')
    await page.locator('#llm_api_base').fill(`http://127.0.0.1:${modelServer.address().port}/v1`)
    await page.locator('#llm_api_key').fill('synthetic-desktop-key')
    await page.getByRole('button', { name: '测试模型连接', exact: true }).click()
    await page.getByText(/模型连接成功，耗时/).waitFor({ timeout: 30000 })
    assert.deepEqual(requests, ['/v1/chat/completions'])
    await page.getByRole('button', { name: '查询与计算', exact: true }).click()
    await page.locator('#query_row_limit').fill('321')
    await page.getByRole('button', { name: '保存并应用', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('#llm_model')?.value === 'desktop-smoke-model', { timeout: 30000 })
    assert.equal(await page.evaluate(() => localStorage.getItem('restart-marker')), 'preserved')
    const saved = JSON.parse(await readFile(path.join(userData, 'data', 'preferences.json'), 'utf8'))
    assert.equal(saved.query_row_limit, 321)
    assert.equal(saved.llm_model, 'desktop-smoke-model')
    assert.equal(await page.evaluate(() => typeof window.require), 'undefined')
    const config = await page.evaluate(() => fetch('/api/admin/settings').then(r => r.json()))
    assert.equal(config.desktop, true)
    assert.equal(config.values.query_row_limit, 321)
    assert.equal(config.secrets.llm_api_key, true)
    assert.ok(!JSON.stringify(config).includes('synthetic-desktop-key'))
    const out = path.resolve(__dirname, '..', 'test-results')
    await mkdir(out, { recursive: true })
    await page.screenshot({ path: path.join(out, 'settings.png'), fullPage: true })
    await page.getByRole('button', { name: '数据库连接', exact: true }).click()
    await page.getByRole('heading', { name: '添加第一个数据库' }).waitFor()
    await page.screenshot({ path: path.join(out, 'databases.png'), fullPage: true })
    await page.getByRole('button', { name: '返回查询', exact: false }).click()
    await page.getByRole('heading', { name: '用自然语言提问，我来查库回答' }).waitFor()
    await page.screenshot({ path: path.join(out, 'chat.png'), fullPage: true })
    console.log('Desktop smoke passed: native window, GUI settings, engine restart, persistent origin, database onboarding and renderer isolation')
  } finally {
    await application?.close()
    await new Promise(resolve => modelServer.close(resolve))
    await rm(userData, { recursive: true, force: true })
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
