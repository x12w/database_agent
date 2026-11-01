const { app, BrowserWindow, protocol, ipcMain, session, dialog, shell, Menu } = require('electron')
const path = require('node:path')
const fs = require('node:fs')
const { Engine } = require('./engine.cjs')
const { pythonEnvironment } = require('./nix.cjs')

protocol.registerSchemesAsPrivileged([{ scheme: 'dbchat', privileges: {
  standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true,
} }])
app.setName('dbchat')
if (process.env.DBCHAT_USER_DATA) app.setPath('userData', path.resolve(process.env.DBCHAT_USER_DATA))
const ORIGIN = 'dbchat://app'
const allowedURL = (url) => { try { const u = new URL(url); return u.protocol === 'dbchat:' && u.host === 'app' } catch { return false } }
let window, engine, restarting = false, quitting = false
const dataDirectory = path.join(app.getPath('userData'), 'data')

function createEngine() {
  const root = path.resolve(__dirname, '..')
  const resources = process.env.DBCHAT_RESOURCES_DIR || (app.isPackaged ? process.resourcesPath : null)
  const backend = resources ? path.join(resources, 'backend') : null
  const python = backend
    ? path.join(backend, 'python', 'bin', 'python3.13')
    : process.env.DBCHAT_PYTHON || path.join(root, 'venv', 'bin', 'python')
  const source = backend ? path.join(backend, 'src') : path.join(root, 'src')
  const ui = resources ? path.join(resources, 'ui') : path.join(root, 'web', 'dist')
  if (!fs.existsSync(path.join(ui, 'index.html'))) throw new Error('界面尚未构建，请先运行 npm --prefix web run build')
  return new Engine({ command: python,
    args: ['-m', 'dbchat.desktop', '--data-dir', dataDirectory, '--web-dir', ui],
    env: { ...pythonEnvironment(), PYTHONPATH: source, PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1' },
    onExit: () => {
      if (!quitting && !restarting && window) {
        dialog.showMessageBox(window, { type: 'error', message: '本地引擎意外退出',
          detail: '可以重新启动引擎，已保存的配置和会话会保留。', buttons: ['重新启动', '退出应用'] })
          .then(({ response }) => response === 0 ? restart().catch(showFailure) : app.quit())
      }
    },
  })
}

async function proxy(request) {
  if (!allowedURL(request.url) || (request.initiatorOrigin && request.initiatorOrigin !== ORIGIN)) return new Response('Forbidden', { status: 403 })
  if (!engine?.origin) return new Response('本地引擎未启动', { status: 503 })
  const url = new URL(request.url)
  // Only forward to our own loopback process. Never forward browser credentials or redirects.
  const headers = { Authorization: `Bearer ${engine.token}` }
  for (const name of ['content-type', 'accept']) if (request.headers.has(name)) headers[name] = request.headers.get(name)
  const response = await fetch(`${engine.origin}${url.pathname}${url.search}`, {
    method: request.method, headers, redirect: 'manual', signal: request.signal,
    body: ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer(),
  })
  const outgoing = new Headers(response.headers)
  outgoing.delete('content-length')
  outgoing.delete('content-encoding')
  outgoing.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'")
  outgoing.set('X-Content-Type-Options', 'nosniff')
  return new Response(response.body, { status: response.status, headers: outgoing })
}

function trusted(event) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || !allowedURL(event.senderFrame.url)) throw new Error('不允许的窗口请求')
}
async function restart() {
  if (restarting || quitting) return
  restarting = true
  try {
    await engine.stop()
    await engine.start()
    await window.loadURL(`${ORIGIN}/settings`)
  } finally { restarting = false }
}
function showFailure(error) {
  dialog.showErrorBox('dbchat 无法启动', error.message)
  app.quit()
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (window) { window.restore(); window.focus() } })
  app.whenReady().then(async () => {
    fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 })
    if (process.platform !== 'win32') fs.chmodSync(dataDirectory, 0o700)
    Menu.setApplicationMenu(null)
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
    session.defaultSession.setPermissionCheckHandler(() => false)
    protocol.handle('dbchat', proxy)
    engine = createEngine()
    await engine.start()
    window = new BrowserWindow({ width: 1280, height: 860, minWidth: 840, minHeight: 620,
      icon: path.join(__dirname, 'assets', 'icon.png'),
      title: 'dbchat', backgroundColor: '#1a1d23', show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
        nodeIntegration: false, sandbox: true, webSecurity: true, spellcheck: false },
    })
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    window.webContents.on('will-navigate', (event, url) => { if (!allowedURL(url)) event.preventDefault() })
    window.webContents.on('will-attach-webview', (event) => event.preventDefault())
    ipcMain.handle('desktop:restart', async (event) => { trusted(event); await restart() })
    ipcMain.handle('desktop:open-data', async (event) => { trusted(event); return shell.openPath(dataDirectory) })
    ipcMain.handle('desktop:choose-knowledge', async (event) => {
      trusted(event)
      const selection = await dialog.showOpenDialog(window, { properties: ['openFile'], filters: [{ name: 'Markdown 文档', extensions: ['md', 'markdown', 'txt'] }] })
      return selection.canceled ? null : selection.filePaths[0]
    })
    window.once('ready-to-show', () => window.show())
    const firstRun = !fs.existsSync(path.join(dataDirectory, 'preferences.json'))
    await window.loadURL(`${ORIGIN}${firstRun ? '/settings' : '/'}`)
  }).catch(showFailure)
  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', (event) => {
    if (quitting) return
    event.preventDefault()
    quitting = true
    Promise.resolve(engine?.stop()).finally(() => app.quit())
  })
}
