/**
 * 后端 HTTP 响应形状（非 SSE 部分）。
 *
 * 后端在并行重写，这里按约定的路径与字段建模；字段全部按"可能缺失"处理，
 * 视图侧对 undefined 有回落，避免后端少一个字段就白屏。
 */

export interface DatabaseProfile {
  id: string
  name: string
  host?: string
  port?: number
  user?: string
  database: string
  db_type?: string
  description?: string
  explored?: boolean
  connected?: boolean
}

export interface DatabasesResponse {
  profiles: DatabaseProfile[]
  current: string | null
}

export interface ProfileInput {
  name: string
  host: string
  port: number
  user: string
  password: string
  database: string
  db_type: string
  description: string
}

export interface HealthResponse {
  status?: string
  database?: boolean
  db_name?: string | null
  llm_available?: boolean
  rag_ready?: boolean
  schema_tables?: number
}

export interface DatabaseStatusResponse {
  profile_id?: string
  name?: string
  database?: string
  db_type?: string
  explored?: boolean
  table_count?: number
  total_rows?: number
  tables?: string[]
  suggestions?: string[]
}

export interface TableInfo {
  name: string
  row_count: number
  column_count: number
  comment?: string
}

export interface TablesResponse {
  tables: TableInfo[]
  message?: string
}

export interface TableFilterResponse {
  success?: boolean
  message?: string
  table_filter: string[] | null
}

export interface AdminQuestion {
  question_id: string
  table_name: string
  column_name?: string
  question: string
  sample_values?: string[]
  answer?: string
  status?: string
  created_at?: string
  answered_at?: string
  profile_id?: string
}

export interface AdminQuestionsResponse {
  questions: AdminQuestion[]
  pending_count?: number
  total_count?: number
  profile_id?: string
}

// 探索流（`GET /api/admin/explore-database`）的事件**不在这里**：它是契约事件，
// 形状见 `types/events.ts`（镜像 `src/dbchat/events/explore.py`）。
