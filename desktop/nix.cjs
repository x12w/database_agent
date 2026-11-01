// Resolve matching-architecture runtime libraries for portable Python on NixOS.
const fs = require('node:fs')
function pythonEnvironment() {
  if (!fs.existsSync('/nix/store')) return {}
  const directories = fs.readdirSync('/nix/store').sort((a, b) => b.slice(33).localeCompare(a.slice(33), undefined, { numeric: true }))
  const needed = [
    [/gcc-.*-lib$/, 'libstdc++.so.6'], [/gcc-.*-lib$/, 'libgcc_s.so.1'],
    [/zlib-[\d.]+$/, 'libz.so.1'], [/glib-[\d.]+$/, 'libglib-2.0.so.0'],
  ]
  const found = []
  for (const [pattern, library] of needed) {
    for (const directory of directories.filter(d => pattern.test(d))) {
      const file = `/nix/store/${directory}/lib/${library}`
      let fd
      try {
        fd = fs.openSync(file, 'r')
        const header = Buffer.alloc(20)
        fs.readSync(fd, header, 0, 20, 0)
        const machine = header.readUInt16LE(18)
        if (header[4] === 2 && machine === (process.arch === 'arm64' ? 183 : 62)) {
          found.push(`/nix/store/${directory}/lib`)
          break
        }
      } catch { /* This output does not contain the required library. */ }
      finally { if (fd !== undefined) fs.closeSync(fd) }
    }
  }
  return { LD_LIBRARY_PATH: [...new Set(found), process.env.LD_LIBRARY_PATH].filter(Boolean).join(':') }
}
module.exports = { pythonEnvironment }
