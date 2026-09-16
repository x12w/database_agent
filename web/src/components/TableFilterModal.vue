<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchTableFilter, fetchTables, saveTableFilter } from '../api/databases.ts'
import { databasesStore } from '../stores/databases.ts'
import { loadTableFilter, persistTableFilter } from '../stores/chat.ts'
import type { TableInfo } from '../types/api.ts'

/**
 * 表筛选弹层。行为与旧前端一致：
 * 全选 = 不筛选（向后端提交 null）；结果按库持久化在 `tf_{dbId}`。
 * 保存后会顺带触发一次缓存重建（否则后端缓存里还是旧表集）。
 */
const props = defineProps<{ dbId: string | null; sessionId: string }>()
const emit = defineEmits<{ close: []; saved: [message: string] }>()

const tables = ref<TableInfo[]>([])
const selected = ref<Set<string>>(new Set())
const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)

const allSelected = computed(() => tables.value.length > 0 && selected.value.size === tables.value.length)
const emptyCount = computed(() => tables.value.filter((t) => !t.row_count).length)

onMounted(async () => {
  if (!props.dbId) {
    error.value = '请先选择数据库'
    loading.value = false
    return
  }
  try {
    const [list, serverFilter] = await Promise.all([
      fetchTables(props.dbId),
      fetchTableFilter(props.sessionId).catch(() => ({ table_filter: null })),
    ])
    tables.value = list.tables ?? []
    if (list.message) error.value = list.message
    const effective =
      serverFilter.table_filter && serverFilter.table_filter.length > 0
        ? serverFilter.table_filter
        : loadTableFilter(props.dbId)
    selected.value = new Set(effective && effective.length > 0 ? effective : tables.value.map((t) => t.name))
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载表列表失败'
  } finally {
    loading.value = false
  }
})

function toggle(name: string): void {
  const next = new Set(selected.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  selected.value = next
}

function selectAll(): void {
  selected.value = new Set(tables.value.map((t) => t.name))
}

function selectNone(): void {
  selected.value = new Set()
}

function dropEmpty(): void {
  selected.value = new Set(tables.value.filter((t) => t.row_count > 0).map((t) => t.name))
}

async function save(): Promise<void> {
  saving.value = true
  error.value = null
  const picked = tables.value.map((t) => t.name).filter((name) => selected.value.has(name))
  const payload = picked.length === tables.value.length ? null : picked
  try {
    const result = await saveTableFilter(props.sessionId, payload)
    if (result.success === false) {
      error.value = result.message ?? '保存筛选失败'
      return
    }
    persistTableFilter(props.dbId, payload)
    let message = result.message ?? '筛选已保存'
    try {
      const rebuilt = await databasesStore.rebuild(props.sessionId)
      message = `${message} · ${rebuilt}`
    } catch (err) {
      message = `${message} · 缓存重建失败：${err instanceof Error ? err.message : '未知错误'}`
    }
    emit('saved', message)
    emit('close')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存筛选失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="dialog" role="dialog" aria-modal="true" aria-label="筛选表">
      <div class="dialog-header">
        <h2>筛选表</h2>
        <span class="counter mono">{{ selected.size }} / {{ tables.length }} 张</span>
      </div>

      <div class="toolbar">
        <button type="button" class="btn" :disabled="allSelected" @click="selectAll">全选</button>
        <button type="button" class="btn" :disabled="selected.size === 0" @click="selectNone">全不选</button>
        <button type="button" class="btn" :disabled="emptyCount === 0" @click="dropEmpty">
          去掉空表 ({{ emptyCount }})
        </button>
        <span v-if="error" class="error">{{ error }}</span>
      </div>

      <div class="dialog-body">
        <p v-if="loading" class="muted">加载中…</p>
        <div v-else class="grid">
          <label v-for="table in tables" :key="table.name" class="card" :class="{ on: selected.has(table.name) }">
            <input
              type="checkbox"
              :checked="selected.has(table.name)"
              @change="toggle(table.name)"
            />
            <span class="name mono">{{ table.name }}</span>
            <span class="meta">{{ table.row_count }} 行 · {{ table.column_count }} 列</span>
          </label>
        </div>
      </div>

      <div class="dialog-footer">
        <span class="hint muted">全选即等于不筛选</span>
        <span class="footer-actions">
          <button type="button" class="btn" @click="emit('close')">取消</button>
          <button type="button" class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.counter {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border);
}

.error {
  color: var(--accent-red);
  font-size: var(--text-xs);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}

.card {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 4px 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  cursor: pointer;
  transition: var(--transition);
}

.card:hover {
  border-color: var(--border-light);
}

.card.on {
  border-color: rgba(92, 201, 183, 0.45);
}

.card input {
  grid-row: span 2;
  accent-color: var(--accent-cool);
}

.name {
  font-size: var(--text-sm);
  overflow: hidden;
  text-overflow: ellipsis;
}

.meta {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.hint {
  font-size: var(--text-xs);
}

.footer-actions {
  display: flex;
  gap: 8px;
}
</style>
