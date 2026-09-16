/**
 * 后端路径的唯一来源。后端（并行重写中）改名时，只改这一个文件。
 */
export const API = {
  health: () => '/api/health',

  databases: () => '/api/databases',
  database: (id: string) => `/api/databases/${encodeURIComponent(id)}`,
  databaseStatus: (id: string, refresh = false) =>
    `/api/databases/${encodeURIComponent(id)}/status${refresh ? '?refresh=1' : ''}`,
  databaseRebuildCache: (id: string, sessionId?: string) =>
    `/api/databases/${encodeURIComponent(id)}/rebuild-cache${
      sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
    }`,
  databaseRefresh: (id: string) => `/api/databases/${encodeURIComponent(id)}/refresh`,

  tables: () => '/api/tables',
  sessionTableFilter: (sessionId: string) =>
    `/api/session/${encodeURIComponent(sessionId)}/table-filter`,

  /** SSE：`message` / `session_id` / `db_id` / `table_filter`（逗号分隔）。 */
  chatStream: () => '/api/chat/stream',

  adminQuestions: () => '/api/admin/questions',
  adminAnswer: (questionId: string) =>
    `/api/admin/questions/${encodeURIComponent(questionId)}/answer`,
  adminIgnore: (questionId: string) =>
    `/api/admin/questions/${encodeURIComponent(questionId)}/ignore`,
  /** SSE：`db_id` / `session_id`。 */
  adminExploreDatabase: () => '/api/admin/explore-database',
} as const
