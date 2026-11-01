<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { fetchSettings, saveSettings, testModel, type Preferences, type SettingValue } from '../api/settings.ts'
import DatabaseManagerModal from '../components/DatabaseManagerModal.vue'
import { databasesStore } from '../stores/databases.ts'
import { chatStore } from '../stores/chat.ts'
import { navigate } from '../router.ts'

const desktop = window.desktop
const settings = ref<Preferences | null>(null)
const values = reactive<Record<string, SettingValue>>({})
const secretEdits = reactive<Record<string, string>>({})
const clearSecrets = reactive<Record<string, boolean>>({})
const group = ref('model')
const loading = ref(true)
const busy = ref(false)
const testing = ref(false)
const error = ref('')
const notice = ref('')
const showDatabases = ref(false)
const needsRestart = ref(false)
const visibleFields = computed(() => settings.value?.fields.filter(f => f.group === group.value) ?? [])
const groups = computed(() => settings.value?.groups ?? { model: '模型服务', timeouts: '超时与连接', limits: '查询与计算', knowledge: '知识检索', display: '显示与存储' })
const changes = computed(() => {
  const changed: Record<string, SettingValue> = {}
  for (const [key, value] of Object.entries(values)) if (value !== settings.value?.values[key]) changed[key] = value
  for (const [key, value] of Object.entries(secretEdits)) if (value) changed[key] = value
  for (const [key, clear] of Object.entries(clearSecrets)) if (clear) changed[key] = ''
  return changed
})
const dirty = computed(() => Object.keys(changes.value).length > 0)
const canSave = computed(() => !loading.value && !busy.value && !testing.value && !!settings.value?.desktop && !chatStore.state.streaming)
const hints: Record<string, string> = {
  llm_api_base: '填写完整接口地址。OpenAI 兼容服务通常以 /v1 结尾；Anthropic 使用其服务根地址。',
  llm_model: '填写服务商提供的模型 ID，也可使用本地兼容服务中的模型名称。',
  llm_api_key: '留空保留已保存的密钥。新密钥仅在保存或测试连接时发送给本地引擎。',
  anthropic_api_key: '仅在上方 API 密钥为空时使用；通常只需配置一个密钥。',
  llm_temperature: '数值越低，回答通常越稳定。部分模型只接受特定温度。',
  query_row_limit: '限制单次查询交给模型的数据量，避免超大结果占用内存。',
  rag_doc_path: '选择自己的业务说明文件。未添加文档时，仍可使用数据库知识问答。',
  rag_structure_doc_path: '可选的数据库结构说明；文件不存在时跳过。',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    settings.value = await fetchSettings()
    Object.assign(values, settings.value.values)
    for (const key of Object.keys(settings.value.secrets)) { secretEdits[key] = ''; clearSecrets[key] = false }
  } catch (e) { error.value = e instanceof Error ? e.message : '读取设置失败' }
  finally { loading.value = false }
}
async function save() {
  if (!settings.value || !canSave.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await saveSettings(changes.value, settings.value.revision)
    settings.value.revision = response.revision
    needsRestart.value = true
    await load()
    notice.value = '设置已保存，正在重启本地引擎…'
    if (desktop) await desktop.restart()
    else notice.value = '设置已保存，请重新启动桌面应用以生效。'
  } catch (e) { error.value = e instanceof Error ? e.message : '保存失败，请重试' }
  finally { busy.value = false }
}
async function testConnection() {
  testing.value = true; error.value = ''; notice.value = ''
  try {
    const response = await testModel(changes.value)
    notice.value = `${response.message}，耗时 ${response.elapsed_ms} 毫秒。${dirty.value ? '当前修改尚未保存。' : ''}`
  } catch (e) { error.value = e instanceof Error ? e.message : '连接测试失败' }
  finally { testing.value = false }
}
async function chooseFile(key: string) {
  try { const file = await desktop?.chooseKnowledgeFile(); if (file) values[key] = file }
  catch { error.value = '无法选择文件，请直接填写完整路径。' }
}
function leave(to: '/' | '/admin') {
  if (busy.value || (dirty.value && !confirm('还有未保存的设置，确定离开？'))) return
  navigate(to)
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value && !needsRestart.value) event.preventDefault()
}
async function openData() {
  try { const message = await desktop?.openDataDirectory(); if (message) error.value = '无法打开数据目录，请手动打开下方路径。' }
  catch { error.value = '无法打开数据目录。' }
}
onMounted(() => { void load(); void databasesStore.load(); window.addEventListener('beforeunload', beforeUnload) })
onUnmounted(() => window.removeEventListener('beforeunload', beforeUnload))
</script>

<template>
  <div class="settings-app">
    <aside class="settings-nav" aria-label="设置导航">
      <div class="app-name"><span class="app-mark">▤</span><strong>dbchat</strong><span>桌面工作台</span></div>
      <button class="return-button" @click="leave('/')">‹ 返回查询</button>
      <h2>应用设置</h2>
      <nav>
        <button v-for="(label, key) in groups" :key="key" :class="{ selected: group === key }" :aria-current="group === key ? 'page' : undefined" @click="group = key">{{ label }}</button>
        <button :class="{ selected: group === 'databases' }" @click="group = 'databases'">数据库连接</button>
      </nav>
      <div class="nav-bottom">
        <button @click="leave('/admin')">数据库知识管理</button>
        <p>设置保存在这台设备上。<br>数据库内容仅在查询时读取。</p>
      </div>
    </aside>

    <main class="settings-main">
      <header class="settings-heading">
        <div><h1>{{ group === 'databases' ? '数据库连接' : groups[group] }}</h1><p>{{ group === 'model' ? '连接你使用的模型服务，让数据库可以用自然语言查询。' : group === 'databases' ? '管理可查询的数据库，保存连接时会自动验证连接。' : '按你的工作负载调整应用，保存后自动应用。' }}</p></div>
        <span class="save-state" :data-dirty="dirty">{{ dirty ? '有未保存的修改' : '本地配置' }}</span>
      </header>
      <div class="settings-scroll">
        <p v-if="loading" role="status">正在读取设置…</p>
        <div v-else-if="!settings" class="empty-panel"><p>暂时无法读取设置。</p><button class="primary" @click="load">重新加载</button></div>
        <template v-else>
          <div v-if="!settings.secrets.llm_api_key && !settings.secrets.anthropic_api_key && group === 'model'" class="setup-note">
            <strong>先连接模型，再添加数据库</strong><p>填写服务地址、模型名称和密钥，测试连接后保存。也可以直接添加数据库，先查看表结构。</p>
          </div>
          <div v-if="group === 'databases'" class="database-section">
            <div v-for="profile in databasesStore.state.profiles" :key="profile.id" class="connection-row">
              <span class="connection-icon">▤</span><div><h3>{{ profile.name || profile.database }}</h3><p>{{ profile.db_type }} · {{ profile.host }}:{{ profile.port }} / {{ profile.database }}</p></div>
            </div>
            <div v-if="!databasesStore.state.profiles.length" class="empty-panel"><h3>添加第一个数据库</h3><p>支持 MySQL、PostgreSQL 和达梦。建议使用只读账号。</p></div>
            <button class="primary" @click="showDatabases = true">管理数据库连接</button>
          </div>
          <form v-else id="preferences" @submit.prevent="save">
            <div v-for="field in visibleFields" :key="field.key" class="setting-row" :class="{ toggle: field.type === 'boolean' }">
              <label :for="field.key">{{ field.label }}<small v-if="hints[field.key]">{{ hints[field.key] }}</small></label>
              <div class="setting-control">
                <template v-if="field.type === 'secret'">
                  <input :id="field.key" v-model="secretEdits[field.key]" type="password" autocomplete="new-password" :disabled="clearSecrets[field.key] || busy" :placeholder="settings.secrets[field.key] ? '已保存 · 留空保持不变' : '尚未配置'" />
                  <label v-if="settings.secrets[field.key]" class="clear-secret"><input v-model="clearSecrets[field.key]" type="checkbox" :disabled="busy" />清除已保存的密钥</label>
                </template>
                <select v-else-if="field.type === 'select'" :id="field.key" v-model="values[field.key]" :disabled="busy"><option v-for="choice in field.choices" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select>
                <input v-else-if="field.type === 'boolean'" :id="field.key" v-model="values[field.key]" type="checkbox" :disabled="busy" />
                <input v-else-if="field.type === 'number'" :id="field.key" v-model.number="values[field.key]" type="number" :min="field.min ?? undefined" :max="field.max ?? undefined" :step="field.step" required :disabled="busy" />
                <div v-else class="path-input"><input :id="field.key" v-model="values[field.key]" type="text" :disabled="busy" spellcheck="false" /><button v-if="field.key.endsWith('_path') && desktop" type="button" class="secondary" :disabled="busy" @click="chooseFile(field.key)">选择文件</button></div>
              </div>
            </div>
            <div v-if="group === 'model'" class="test-row"><button class="secondary" type="button" :disabled="testing || busy" @click="testConnection">{{ testing ? '正在测试…' : '测试模型连接' }}</button><span>会发送一次简短请求，可能产生少量费用。</span></div>
            <div v-if="group === 'display'" class="storage-info"><h3>本地数据</h3><p>模型密钥保存在仅当前用户可读的配置文件中；此版本未使用系统钥匙串加密。</p><code>{{ settings.data_dir }}</code><button v-if="desktop" class="secondary" type="button" @click="openData">打开数据目录</button></div>
            <p v-if="group === 'limits' && !settings.sandbox_available" class="setup-note">计算与绘图需要 Linux 上可用的 bubblewrap。数据库查询不受影响。</p>
          </form>
        </template>
      </div>
      <footer class="settings-footer">
        <div class="feedback" aria-live="polite"><p v-if="error" class="error" role="alert">{{ error }}</p><p v-else-if="notice">{{ notice }}</p><p v-else-if="chatStore.state.streaming">请等待当前查询完成，或返回查询页停止后再保存。</p><p v-else>{{ settings?.desktop ? '保存会重启本地引擎，会话记录会保留。' : '请从桌面应用进入以保存运行设置。' }}</p></div>
        <button v-if="group !== 'databases'" class="secondary" :disabled="busy || !dirty" @click="load">撤销修改</button>
        <button v-if="group !== 'databases'" class="primary" :disabled="!canSave" form="preferences" type="submit">{{ busy ? '正在应用…' : '保存并应用' }}</button>
      </footer>
    </main>
    <DatabaseManagerModal v-if="showDatabases" @close="showDatabases = false" @changed="databasesStore.load()" />
  </div>
</template>

<style scoped>
.settings-app { height: 100%; display: flex; background: var(--bg-primary); }
.settings-nav { width: 230px; flex: none; background: var(--bg-surface); border-right: 1px solid var(--border); padding: 28px 18px 18px; display: flex; flex-direction: column; }
.app-name { display: grid; grid-template-columns: 28px 1fr; align-items: center; column-gap: 10px; }
.app-name strong { font-size: 20px; font-weight: 650; }
.app-name > span:last-child { grid-column: 2; font-size: 12px; color: var(--text-muted); }
.app-mark { font-size: 30px; color: var(--accent-cool); grid-row: span 2; }
.return-button { text-align: left; margin: 28px 0; padding: 8px 10px; color: var(--text-secondary); }
.settings-nav h2 { font-size: 12px; color: var(--text-muted); font-weight: 500; padding: 0 12px 10px; }
nav { display: grid; gap: 4px; }
nav button, .nav-bottom button { padding: 10px 12px; text-align: left; border-radius: 6px; font-size: 14px; }
nav button.selected { background: #33454b; color: #bce1de; box-shadow: inset 3px 0 #5cc9b7; }
nav button:hover, .return-button:hover { background: var(--bg-elevated); }
.nav-bottom { margin-top: auto; padding-top: 24px; }
.nav-bottom p { font-size: 12px; color: var(--text-muted); padding: 12px; }
.settings-main { min-width: 0; flex: 1; display: flex; flex-direction: column; }
.settings-heading { padding: 30px 40px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 20px; }
h1 { font-size: 24px; font-weight: 600; line-height: 1.4; }
.settings-heading p { font-size: 13px; color: var(--text-muted); margin-top: 8px; max-width: 65ch; }
.save-state { flex: none; font-size: 12px; color: var(--text-muted); align-self: start; padding-top: 10px; }
.save-state[data-dirty='true'] { color: var(--accent-warm); }
.settings-scroll { flex: 1; overflow-y: auto; padding: 28px 40px; }
form, .database-section { max-width: 880px; }
.setting-row { display: grid; grid-template-columns: minmax(180px, .95fr) minmax(240px, 1.2fr); gap: 28px; padding: 18px 0; border-bottom: 1px solid var(--border); }
.setting-row > label { font-weight: 500; font-size: 14px; padding-top: 8px; }
.setting-row small { display: block; font-size: 12px; font-weight: 400; color: var(--text-muted); margin-top: 5px; max-width: 42ch; line-height: 1.7; }
.setting-control input:not([type='checkbox']), .setting-control select { width: 100%; font-size: 14px; min-height: 40px; }
input[type='checkbox'] { accent-color: var(--accent-cool); width: 17px; height: 17px; }
.toggle .setting-control { display: flex; justify-content: flex-end; align-items: center; }
.clear-secret { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--text-muted); padding-top: 10px; }
.path-input { display: flex; gap: 8px; }
.path-input input { min-width: 0; }
.path-input button { flex: none; }
.primary, .secondary { padding: 9px 15px; border-radius: 6px; font-size: 13px; border: 1px solid var(--border-light); }
.primary { background: #bce1de; color: #19363a; font-weight: 600; border-color: #bce1de; }
.secondary:hover:not(:disabled) { background: var(--bg-elevated); }
.test-row { display: flex; gap: 14px; align-items: center; padding-top: 24px; }
.test-row span { font-size: 12px; color: var(--text-muted); }
.settings-footer { border-top: 1px solid var(--border); padding: 18px 40px; display: flex; gap: 12px; align-items: center; background: var(--bg-surface); }
.feedback { flex: 1; font-size: 12px; color: var(--text-secondary); }
.error { color: var(--accent-red); }
.setup-note { padding: 16px 20px; border-left: 3px solid var(--accent-cool-dim); background: #253338; margin-bottom: 18px; max-width: 880px; font-size: 13px; }
.setup-note p { color: var(--text-secondary); margin-top: 5px; }
.storage-info { margin-top: 28px; display: grid; gap: 10px; justify-items: start; font-size: 13px; }
.storage-info p, .connection-row p, .empty-panel p { color: var(--text-muted); font-size: 13px; }
.storage-info code { overflow-wrap: anywhere; font-size: 12px; }
.connection-row { display: flex; gap: 16px; padding: 20px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.connection-icon { font-size: 25px; color: var(--accent-cool); }
h3 { font-size: 15px; font-weight: 550; }
.empty-panel { padding: 30px 0; display: grid; gap: 10px; justify-items: start; }
@media(max-width: 900px) { .settings-nav { width: 190px; } .settings-heading, .settings-scroll, .settings-footer { padding-left: 24px; padding-right: 24px; } .setting-row { gap: 16px; grid-template-columns: minmax(130px, .8fr) minmax(200px, 1fr); } }
@media(max-width: 640px) { .settings-app { flex-direction: column; } .settings-nav { width: 100%; padding: 12px; } .app-name, .nav-bottom, .settings-nav h2 { display: none; } .return-button { margin: 0; } nav { display: flex; overflow-x: auto; } nav button { white-space: nowrap; } .setting-row { grid-template-columns: 1fr; gap: 8px; } .settings-heading { padding-top: 20px; } .save-state { display: none; } .settings-footer { flex-wrap: wrap; } .feedback { flex-basis: 100%; } }
</style>
