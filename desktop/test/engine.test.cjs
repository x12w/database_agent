const test = require('node:test')
const assert = require('node:assert/strict')
const { Engine } = require('../engine.cjs')

test('engine waits for readiness and reaps its child on stop and restart', async () => {
  let exits = 0
  const script = `console.log(JSON.stringify({event:'ready',port:12345}));setInterval(()=>{},1000)`
  const engine = new Engine({ command: process.execPath, args: ['-e', script], onExit: () => exits++ })
  await engine.start()
  assert.equal(engine.origin, 'http://127.0.0.1:12345')
  assert.equal(engine.token.length, 64)
  const first = engine.child.pid
  await engine.stop()
  assert.equal(engine.child, null)
  assert.throws(() => process.kill(first, 0))
  await engine.start()
  await engine.stop()
  assert.equal(exits, 0)
})

test('unexpected child termination rejects startup without raw diagnostics', async () => {
  const engine = new Engine({ command: process.execPath, args: ['-e', 'console.error("secret");process.exit(1)'] })
  await assert.rejects(engine.start(), { message: '本地引擎启动失败，请检查设置文件或重新安装应用' })
  assert.equal(engine.child, null)
})

test('a missing interpreter leaves no live child handle', async () => {
  const engine = new Engine({ command: '/nonexistent/dbchat-python', args: [] })
  await assert.rejects(engine.start(), /无法启动 Python/)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(engine.child, null)
  await engine.stop()
})
