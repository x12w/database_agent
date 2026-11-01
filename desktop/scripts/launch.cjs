const { spawn } = require('node:child_process')
const { existsSync, readdirSync } = require('node:fs')
const path = require('node:path')
let executable = process.env.DBCHAT_ELECTRON
if (!executable && existsSync('/nix/store')) {
  const candidates = readdirSync('/nix/store').filter(n => /-electron-\d/.test(n))
    .map(n => `/nix/store/${n}/bin/electron`).filter(existsSync)
  executable = candidates.at(-1)
}
if (!executable) executable = require('electron')
const child = spawn(executable, [path.resolve(__dirname, '..')], { stdio: 'inherit', env: process.env })
child.on('error', e => { console.error(e.message); process.exitCode = 1 })
child.on('exit', code => { process.exitCode = code ?? 1 })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
