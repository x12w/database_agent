#!/usr/bin/env node
/**
 * 事件契约漂移检查。
 *
 * 读 `src/dbchat/events/chat.py` 与 `base.py`（pydantic 源码）和
 * `src/types/events.ts`（手写镜像），逐项比对：
 *   · 事件类型清单（CHAT_EVENT_TYPES ↔ ChatEventMap / EVENT_TYPES）
 *   · 每种事件的字段名
 *   · 事件基类与嵌套模型（EventBase / ProfileView / RunStats / StageView）的字段
 *   · 枚举成员（Stage / ActivityKind / ErrorCode）
 *   · 中文标签表（STAGE_LABELS / ACTIVITY_LABELS）
 *   · RETRYABLE_ERRORS
 *
 * 只用 node 内置能力做**文本解析**：不需要 Python 工具链，也不引入 codegen 依赖。
 * 用法：
 *   node scripts/check-types.mjs [path/to/events.ts]   # 默认 src/types/events.ts
 * 有漂移时退出码为 1。
 */

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(here, '..')
const repoRoot = resolve(webRoot, '..')

const CHAT_PY = resolve(repoRoot, 'src/dbchat/events/chat.py')
const BASE_PY = resolve(repoRoot, 'src/dbchat/events/base.py')
const TS = resolve(webRoot, process.argv[2] ?? 'src/types/events.ts')

const problems = []
const notes = []

function fail(message) {
  problems.push(message)
}

function read(path) {
  try {
    return readFileSync(path, 'utf8')
  } catch (error) {
    console.error(`无法读取 ${path}: ${error.message}`)
    process.exit(2)
  }
}

// ---------------------------------------------------------------------------
// Python 侧
// ---------------------------------------------------------------------------

const stripDocstrings = (src) => src.replace(/"""[\s\S]*?"""/g, '').replace(/'''[\s\S]*?'''/g, '')

/** 取某个 class 的类体（到下一个顶层 class/定义为止）。 */
function pythonClassBody(src, name) {
  const start = new RegExp(`^class ${name}\\b[^\\n]*\\n`, 'm').exec(src)
  if (!start) return null
  const rest = src.slice(start.index + start[0].length)
  const end = /^(?:class |def |[A-Za-z_]+ *[:=])/m.exec(rest)
  return end ? rest.slice(0, end.index) : rest
}

/** 类体里 4 空格缩进的 `name: Type ...` 声明。 */
function pythonFields(body) {
  const fields = []
  for (const line of body.split('\n')) {
    const match = /^ {4}([A-Za-z_]\w*)\s*:/.exec(line)
    if (match) fields.push(match[1])
  }
  return fields
}

function pythonStrEnumMembers(src, name) {
  const body = pythonClassBody(src, name)
  if (!body) return null
  const members = []
  for (const line of body.split('\n')) {
    const match = /^ {4}([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"/.exec(line)
    if (match) members.push(match[2])
  }
  return members
}

/** 解析 `NAME: dict[K, str] = { ... }`，返回 [[键标识, 值], ...]。 */
function pythonStrMap(src, name) {
  const start = new RegExp(`^${name}: *dict\\[[^\\]]*\\] *= *\\{`, 'm').exec(src)
  if (!start) return null
  const rest = src.slice(start.index + start[0].length)
  const end = rest.indexOf('\n}')
  const body = end === -1 ? rest : rest.slice(0, end)
  const entries = []
  for (const line of body.split('\n')) {
    const match = /^\s*([A-Za-z_][\w.]*)\s*:\s*"([^"]*)"/.exec(line)
    if (match) entries.push([match[1], match[2]])
  }
  return entries
}

function pythonTupleStrings(src, name) {
  const start = new RegExp(`^${name}: *tuple\\[str, *\\.\\.\\.\\] *= *\\(`, 'm').exec(src)
  if (!start) return null
  const rest = src.slice(start.index + start[0].length)
  const end = rest.indexOf('\n)')
  const body = end === -1 ? rest : rest.slice(0, end)
  return [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1])
}

function pythonFrozenSet(src, name) {
  const start = new RegExp(`^${name}: *frozenset\\[ErrorCode\\] *= *frozenset\\(`, 'm').exec(src)
  if (!start) return null
  const rest = src.slice(start.index + start[0].length)
  const end = rest.indexOf('\n)')
  const body = end === -1 ? rest : rest.slice(0, end)
  return [...body.matchAll(/ErrorCode\.([A-Z_]+)/g)].map((m) => m[1].toLowerCase())
}

const chatPy = stripDocstrings(read(CHAT_PY))
const basePy = stripDocstrings(read(BASE_PY))

// 事件类型 → Python 类名
const pyEventTypes = pythonTupleStrings(chatPy, 'CHAT_EVENT_TYPES')
if (!pyEventTypes) {
  console.error('在 chat.py 里找不到 CHAT_EVENT_TYPES —— 契约文件结构变了，请更新本脚本。')
  process.exit(2)
}

const pyClasses = [...chatPy.matchAll(/^class (\w+)\(EventBase\):/gm)].map((m) => m[1])
const pyEvents = new Map()
for (const className of pyClasses) {
  const body = pythonClassBody(chatPy, className)
  const typeLiteral = /type: *Literal\["([^"]+)"\]/.exec(body)?.[1]
  if (!typeLiteral) continue
  pyEvents.set(typeLiteral, { className, fields: pythonFields(body) })
}

const missingClasses = pyEventTypes.filter((type) => !pyEvents.has(type))
if (missingClasses.length) {
  fail(`chat.py 的 CHAT_EVENT_TYPES 里有 ${missingClasses.join(', ')}，但没有对应的 EventBase 子类`)
}

const pyStage = pythonStrEnumMembers(basePy, 'Stage')
const pyActivity = pythonStrEnumMembers(basePy, 'ActivityKind')
const pyError = pythonStrEnumMembers(basePy, 'ErrorCode')
const pyStageLabels = pythonStrMap(basePy, 'STAGE_LABELS')
const pyActivityLabels = pythonStrMap(basePy, 'ACTIVITY_LABELS')
const pyRetryable = pythonFrozenSet(basePy, 'RETRYABLE_ERRORS')
const pyModels = new Map(
  ['EventBase', 'RunStats', 'StageView', 'ProfileView'].map((name) => [
    name,
    pythonFields(pythonClassBody(basePy, name) ?? ''),
  ]),
)

// ---------------------------------------------------------------------------
// TypeScript 侧
// ---------------------------------------------------------------------------

const ts = stripComments(read(TS))

function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

function tsArray(name) {
  const match = new RegExp(`export const ${name} = \\[([\\s\\S]*?)\\] as const`).exec(ts)
  return match ? [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : null
}

function tsUnion(name) {
  const match = new RegExp(`export type ${name} =([\\s\\S]*?)\\n\\n`).exec(ts)
  return match ? [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : null
}

function tsInterface(name) {
  const match = new RegExp(`export interface ${name}\\b[^{]*\\{([\\s\\S]*?)\\n\\}`).exec(ts)
  if (!match) return null
  const fields = []
  for (const line of match[1].split('\n')) {
    const field = /^ {2}([A-Za-z_]\w*)\??\s*:/.exec(line)
    if (field) fields.push(field[1])
  }
  return fields
}

function tsMap(name) {
  const match = new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`).exec(ts)
  if (!match) return null
  const entries = []
  for (const line of match[1].split('\n')) {
    const entry = /^\s*'([^']+)'\s*:\s*(\w+)/.exec(line)
    if (entry) entries.push([entry[1], entry[2]])
  }
  return entries
}

function tsStringRecord(name) {
  const match = new RegExp(`export const ${name}: Record<[^>]*> = \\{([\\s\\S]*?)\\n\\}`).exec(ts)
  if (!match) return null
  const entries = []
  for (const line of match[1].split('\n')) {
    const entry = /^\s*(?:'([^']+)'|([A-Za-z_]\w*))\s*:\s*'([^']*)'/.exec(line)
    if (entry) entries.push([entry[1] ?? entry[2], entry[3]])
  }
  return entries
}

function tsValueArray(name) {
  const match = new RegExp(`export const ${name}: readonly ErrorCode\\[\\] = \\[([\\s\\S]*?)\\]`).exec(ts)
  return match ? [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : null
}

const tsEventTypes = tsArray('EVENT_TYPES')
const tsEventMap = tsMap('ChatEventMap')

if (!tsEventTypes || !tsEventMap) {
  console.error(`在 ${TS} 里找不到 EVENT_TYPES / ChatEventMap —— 镜像文件结构变了，请更新本脚本。`)
  process.exit(2)
}

// ---------------------------------------------------------------------------
// 比对
// ---------------------------------------------------------------------------

function compareSets(label, expected, actual) {
  const missing = expected.filter((item) => !actual.includes(item))
  const extra = actual.filter((item) => !expected.includes(item))
  if (missing.length) fail(`${label}：TS 缺少 ${missing.join(', ')}`)
  if (extra.length) fail(`${label}：TS 多出 ${extra.join(', ')}`)
}

compareSets('事件类型', pyEventTypes, tsEventTypes)
compareSets(
  '事件类型（ChatEventMap）',
  pyEventTypes,
  tsEventMap.map(([type]) => type),
)

// 每一种事件的字段
for (const [type, { className, fields }] of pyEvents) {
  const tsName = tsEventMap.find(([t]) => t === type)?.[1]
  if (!tsName) continue
  const tsFields = tsInterface(tsName)
  if (!tsFields) {
    fail(`事件 ${type}：TS 里找不到 interface ${tsName}`)
    continue
  }
  compareSets(`事件 ${type} 的字段（${className}）`, fields, tsFields)
}

// 嵌套模型
for (const [name, fields] of pyModels) {
  const tsFields = tsInterface(name)
  if (!tsFields) {
    fail(`模型 ${name}：TS 里找不到同名 interface`)
    continue
  }
  compareSets(`模型 ${name} 的字段`, fields, tsFields)
}
// EventBase 的字段会被每个事件接口继承，单独再确认一次它在 TS 里存在
if (!/export interface EventBase\b/.test(ts)) fail('TS 里缺少 EventBase')

// 枚举
compareSets('Stage 成员', pyStage ?? [], tsUnion('Stage') ?? [])
compareSets('ActivityKind 成员', pyActivity ?? [], tsUnion('ActivityKind') ?? [])
compareSets('ErrorCode 成员', pyError ?? [], tsUnion('ErrorCode') ?? [])

// 中文标签表：TS 的键必须是枚举值，值必须与 Python 一致
function compareLabelMap(label, pyEntries, tsEntries) {
  if (!pyEntries || !tsEntries) {
    fail(`${label}：解析失败`)
    return
  }
  const py = new Map(pyEntries.map(([key, value]) => [key.split('.').pop().toLowerCase(), value]))
  const tsMapEntries = new Map(tsEntries)
  for (const [key, value] of py) {
    if (!tsMapEntries.has(key)) fail(`${label}：TS 缺少键 ${key}`)
    else if (tsMapEntries.get(key) !== value) {
      fail(`${label}：${key} 的文案不一致（Python「${value}」/ TS「${tsMapEntries.get(key)}」）`)
    }
  }
  for (const key of tsMapEntries.keys()) {
    if (!py.has(key)) fail(`${label}：TS 多出键 ${key}`)
  }
}

compareLabelMap('STAGE_LABELS', pyStageLabels, tsStringRecord('STAGE_LABELS'))
compareLabelMap('ACTIVITY_LABELS', pyActivityLabels, tsStringRecord('ACTIVITY_LABELS'))
compareSets('RETRYABLE_ERRORS', pyRetryable ?? [], tsValueArray('RETRYABLE_ERRORS') ?? [])

// ---------------------------------------------------------------------------
// 报告
// ---------------------------------------------------------------------------

if (notes.length) for (const note of notes) console.log(`ℹ ${note}`)

if (problems.length === 0) {
  console.log(`✔ 事件契约无漂移（${pyEventTypes.length} 种事件，${pyModels.size} 个模型）`)
  console.log(`  python: src/dbchat/events/chat.py + base.py`)
  console.log(`  ts:     ${TS.replace(`${repoRoot}/`, '')}`)
  process.exit(0)
}

console.error(`✖ 事件契约漂移 ${problems.length} 处：`)
for (const problem of problems) console.error(`  · ${problem}`)
console.error('\n修好后重新运行：npm run check:types')
process.exit(1)
