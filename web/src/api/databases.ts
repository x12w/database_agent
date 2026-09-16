import { API } from './endpoints.ts'
import { apiGet, apiSend } from './client.ts'
import type {
  DatabaseProfile,
  DatabaseStatusResponse,
  DatabasesResponse,
  HealthResponse,
  ProfileInput,
  TableFilterResponse,
  TableInfo,
  TablesResponse,
} from '../types/api.ts'

export function fetchHealth(): Promise<HealthResponse> {
  return apiGet<HealthResponse>(API.health())
}

export function fetchDatabases(): Promise<DatabasesResponse> {
  return apiGet<DatabasesResponse>(API.databases())
}

export function createDatabase(input: ProfileInput): Promise<{ success?: boolean; message?: string; profile?: DatabaseProfile }> {
  return apiSend('POST', API.databases(), input)
}

export function updateDatabase(
  id: string,
  input: ProfileInput,
): Promise<{ success?: boolean; message?: string; profile?: DatabaseProfile }> {
  return apiSend('PUT', API.database(id), input)
}

export function deleteDatabase(id: string): Promise<{ success?: boolean }> {
  return apiSend('DELETE', API.database(id))
}

export function fetchDatabaseStatus(id: string, refresh = false): Promise<DatabaseStatusResponse> {
  return apiGet<DatabaseStatusResponse>(API.databaseStatus(id, refresh))
}

export function rebuildCache(
  id: string,
  sessionId?: string,
): Promise<{ success?: boolean; message?: string; table_count?: number; name?: string }> {
  return apiSend('POST', API.databaseRebuildCache(id, sessionId))
}

export function refreshDatabase(id: string): Promise<DatabaseStatusResponse> {
  return apiSend('POST', API.databaseRefresh(id))
}

export function fetchTables(dbId: string): Promise<TablesResponse> {
  return apiGet<TablesResponse>(`${API.tables()}?db_id=${encodeURIComponent(dbId)}`)
}

export function fetchTableFilter(sessionId: string): Promise<TableFilterResponse> {
  return apiGet<TableFilterResponse>(API.sessionTableFilter(sessionId))
}

export function saveTableFilter(
  sessionId: string,
  tables: string[] | null,
): Promise<TableFilterResponse> {
  return apiSend('POST', API.sessionTableFilter(sessionId), { tables })
}

export type { TableInfo }
