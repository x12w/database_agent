const { spawn } = require('node:child_process')
const { randomBytes } = require('node:crypto')
const { createInterface } = require('node:readline')

class Engine {
  constructor({ command, args, env = {}, onExit = () => {} }) {
    Object.assign(this, { command, args, env, onExit })
    this.child = null
    this.origin = null
    this.token = randomBytes(32).toString('hex')
  }
  async start() {
    if (this.child) throw new Error('本地引擎已在运行')
    const child = spawn(this.command, this.args, {
      env: { ...process.env, ...this.env, DBCHAT_DESKTOP_TOKEN: this.token },
      stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    })
    this.child = child
    let expected = false
    child.expectExit = () => { expected = true }
    // Keep diagnostics bounded; never return raw output (it may contain provider details).
    child.stderr.on('data', () => {})
    const lines = createInterface({ input: child.stdout })
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill(); reject(new Error('本地引擎启动超时')) }, 45000)
      const fail = (error) => { clearTimeout(timer); reject(error) }
      child.once('error', () => {
        expected = true
        if (this.child === child) { this.child = null; this.origin = null }
        lines.close()
        fail(new Error('无法启动 Python 引擎，请检查应用安装或 DBCHAT_PYTHON 路径'))
      })
      child.once('close', () => {
        clearTimeout(timer)
        lines.close()
        if (this.child === child) { this.child = null; this.origin = null }
        reject(new Error('本地引擎启动失败，请检查设置文件或重新安装应用'))
        if (!expected) this.onExit()
      })
      lines.on('line', (line) => {
        try {
          const data = JSON.parse(line)
          if (data.event === 'ready' && Number.isInteger(data.port) && data.port > 0 && data.port < 65536) {
            this.origin = `http://127.0.0.1:${data.port}`
            clearTimeout(timer)
            resolve()
          }
        } catch { /* Only the ready frame belongs to the protocol. */ }
      })
    })
  }
  async stop() {
    const child = this.child
    if (!child) return
    child.expectExit()
    await new Promise((resolve) => {
      const timer = setTimeout(() => child.kill('SIGKILL'), 7000)
      child.once('close', () => { clearTimeout(timer); resolve() })
      child.kill('SIGTERM')
    })
    this.origin = null
  }
}
module.exports = { Engine }
