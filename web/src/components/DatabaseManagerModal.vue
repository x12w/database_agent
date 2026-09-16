<script setup lang="ts">
import { reactive, ref } from 'vue'

import { databasesStore } from '../stores/databases.ts'
import type { DatabaseProfile, ProfileInput } from '../types/api.ts'

/**
 * 数据库配置管理（新增 / 编辑 / 删除）。字段与旧前端一致。
 */
const emit = defineEmits<{ close: []; changed: [] }>()

const editingId = ref<string | null>(null)
const message = ref<string | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)

const form = reactive<ProfileInput>({
  name: '',
  host: '127.0.0.1',
  port: 3306,
  user: 'root',
  password: '',
  database: '',
  db_type: 'mysql',
  description: '',
})

function reset(): void {
  editingId.value = null
  message.value = null
  error.value = null
  form.name = ''
  form.host = '127.0.0.1'
  form.port = 3306
  form.user = 'root'
  form.password = ''
  form.database = ''
  form.db_type = 'mysql'
  form.description = ''
}

function edit(profile: DatabaseProfile): void {
  editingId.value = profile.id
  message.value = null
  error.value = null
  form.name = profile.name ?? ''
  form.host = profile.host ?? '127.0.0.1'
  form.port = profile.port ?? 3306
  form.user = profile.user ?? 'root'
  form.password = ''
  form.database = profile.database ?? ''
  form.db_type = profile.db_type ?? 'mysql'
  form.description = profile.description ?? ''
}

async function submit(): Promise<void> {
  busy.value = true
  error.value = null
  message.value = null
  try {
    const result = await databasesStore.save({ ...form }, editingId.value)
    message.value = '已保存'
    if (!editingId.value && result?.id) databasesStore.select(result.id)
    emit('changed')
    reset()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = false
  }
}

async function remove(profile: DatabaseProfile): Promise<void> {
  if (!confirm(`删除数据库配置「${profile.name || profile.database}」？`)) return
  busy.value = true
  try {
    await databasesStore.remove(profile.id)
    emit('changed')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="dialog" role="dialog" aria-modal="true" aria-label="管理数据库">
      <div class="dialog-header">
        <h2>管理数据库</h2>
        <button type="button" class="btn-ghost" @click="emit('close')">关闭</button>
      </div>

      <div class="dialog-body">
        <ul class="list">
          <li v-for="profile in databasesStore.state.profiles" :key="profile.id" class="item">
            <span class="item-name">
              {{ profile.name || '(未命名)' }}
              <span class="mono muted">{{ profile.database }}</span>
            </span>
            <span class="item-meta muted mono">{{ profile.db_type }} · {{ profile.host }}:{{ profile.port }}</span>
            <span class="item-actions">
              <button type="button" class="btn" @click="edit(profile)">编辑</button>
              <button type="button" class="btn danger" :disabled="busy" @click="remove(profile)">删除</button>
            </span>
          </li>
          <li v-if="databasesStore.state.profiles.length === 0" class="muted">还没有配置任何数据库。</li>
        </ul>

        <form class="form" @submit.prevent="submit">
          <h3>{{ editingId ? '编辑数据库' : '新增数据库' }}</h3>
          <label>
            <span>名称</span>
            <input v-model="form.name" placeholder="例如：培训考试库" />
          </label>
          <label>
            <span>类型</span>
            <select v-model="form.db_type">
              <option value="mysql">MySQL</option>
              <option value="postgresql">PostgreSQL</option>
              <option value="dameng">达梦</option>
            </select>
          </label>
          <label>
            <span>主机</span>
            <input v-model="form.host" />
          </label>
          <label>
            <span>端口</span>
            <input v-model.number="form.port" type="number" min="1" max="65535" />
          </label>
          <label>
            <span>用户</span>
            <input v-model="form.user" />
          </label>
          <label>
            <span>密码</span>
            <input v-model="form.password" type="password" :placeholder="editingId ? '留空则不修改' : ''" />
          </label>
          <label>
            <span>数据库</span>
            <input v-model="form.database" required />
          </label>
          <label class="wide">
            <span>描述</span>
            <input v-model="form.description" />
          </label>

          <div class="form-actions">
            <span v-if="error" class="error">{{ error }}</span>
            <span v-else-if="message" class="ok">{{ message }}</span>
            <button v-if="editingId" type="button" class="btn" @click="reset">取消编辑</button>
            <button type="submit" class="btn btn-primary" :disabled="busy">
              {{ busy ? '测试连接中…' : editingId ? '保存修改' : '添加' }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 18px;
}

.item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}

.item-name {
  font-size: var(--text-sm);
  display: flex;
  gap: 8px;
  align-items: baseline;
  min-width: 0;
}

.item-meta {
  font-size: var(--text-xs);
}

.item-actions {
  display: flex;
  gap: 6px;
}

.danger {
  color: var(--accent-red);
  border-color: rgba(224, 85, 106, 0.35);
}

.form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  border-top: 1px solid var(--border);
  padding-top: 14px;
}

.form h3 {
  grid-column: 1 / -1;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.form .wide {
  grid-column: 1 / -1;
}

.form-actions {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.error {
  color: var(--accent-red);
  font-size: var(--text-xs);
  margin-right: auto;
}

.ok {
  color: var(--accent-green);
  font-size: var(--text-xs);
  margin-right: auto;
}
</style>
